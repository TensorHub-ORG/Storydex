import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  fetchPromptRepository: vi.fn(),
  createCustomPrompt: vi.fn(),
  updateCustomPrompt: vi.fn()
}));

vi.mock("@/api/help", () => ({
  fetchPromptRepository: api.fetchPromptRepository,
  createCustomPrompt: api.createCustomPrompt,
  updateCustomPrompt: api.updateCustomPrompt
}));
vi.mock("@/api/client", async (load) => ({
  ...(await load<any>()),
  describeTransportError: (_error: unknown, fallback: string) => fallback
}));
vi.mock("@/api/system", () => ({ updateUiPreferences: vi.fn().mockResolvedValue({ data: {} }) }));

import PromptRepositorySidebar from "@/components/PromptRepositorySidebar.vue";
import { useAgentStore } from "@/stores/agent";
import { useUiStore } from "@/stores/ui";
import { useWorkspaceStore } from "@/stores/workspace";

describe("PromptRepositorySidebar", () => {
  beforeEach(() => {
    const pinia = createPinia();
    setActivePinia(pinia);
    api.fetchPromptRepository.mockReset().mockResolvedValue({
      data: {
        root: "docs/prompts",
        query: "",
        category: "",
        categories: [{ id: "项目包装", label: "项目包装", count: 1 }],
        items: [
          {
            id: "项目包装/01-简介",
            title: "根据当前小说项目生成简介",
            summary: "生成忠于项目的简介。",
            category: "项目包装",
            relativePath: "项目包装/01-简介.md",
            content: "# 简介",
            promptText: "请生成[目标字数]简介。",
            placeholders: ["[目标字数]"],
            updatedAt: "",
            isCustom: false
          }
        ]
      }
    });
  });

  it("loads, opens and sends a repository prompt to the Agent composer", async () => {
    const workspaceStore = useWorkspaceStore();
    workspaceStore.launchScreenVisible = false;
    const wrapper = mount(PromptRepositorySidebar);
    await flushPromises();

    expect(wrapper.text()).toContain("根据当前小说项目生成简介");
    await wrapper.find(".prompt-list-item").trigger("click");
    expect(wrapper.text()).toContain("[目标字数]");

    const sendButton = wrapper.findAll("button").find((button) => button.text().includes("填入 Agent"));
    expect(sendButton).toBeTruthy();
    await sendButton!.trigger("click");
    expect(useAgentStore().promptInput).toBe("请生成[目标字数]简介。");
    expect(useUiStore().agentCollapsed).toBe(false);
  });

  it("creates a custom prompt and only edits its body afterward", async () => {
    const customItem = {
      id: "custom/abc123",
      title: "检查章节节奏",
      summary: "用户自定义的可复用指令。",
      category: "自定义",
      relativePath: "",
      content: "检查当前章节节奏。",
      promptText: "检查当前章节节奏。",
      placeholders: [],
      updatedAt: "",
      isCustom: true
    };
    api.fetchPromptRepository.mockResolvedValue({
      data: {
        root: "docs/prompts",
        query: "",
        category: "",
        categories: [{ id: "自定义", label: "自定义", count: 0 }],
        items: []
      }
    });
    api.createCustomPrompt.mockResolvedValue({ data: { item: customItem } });
    api.updateCustomPrompt.mockResolvedValue({
      data: { item: { ...customItem, promptText: "检查节奏、冲突和章尾钩子。", content: "检查节奏、冲突和章尾钩子。" } }
    });

    const wrapper = mount(PromptRepositorySidebar);
    await flushPromises();
    await wrapper.findAll(".prompt-category-tabs button").find((button) => button.text().includes("自定义"))!.trigger("click");
    await wrapper.get(".prompt-custom-toolbar button").trigger("click");
    await wrapper.get<HTMLInputElement>(".prompt-custom-create input").setValue("检查章节节奏");
    await wrapper.get<HTMLTextAreaElement>(".prompt-custom-create textarea").setValue("检查当前章节节奏。");
    await wrapper.get(".prompt-custom-create").trigger("submit");
    await flushPromises();

    expect(api.createCustomPrompt).toHaveBeenCalledWith({ title: "检查章节节奏", promptText: "检查当前章节节奏。" });
    expect(wrapper.find(".prompt-content-editor").exists()).toBe(true);
    expect(wrapper.find('input[aria-label="编辑自定义指令名称"]').exists()).toBe(false);

    await wrapper.get<HTMLTextAreaElement>(".prompt-content-editor").setValue("检查节奏、冲突和章尾钩子。");
    await wrapper.findAll("button").find((button) => button.text().includes("保存正文"))!.trigger("click");
    await flushPromises();
    expect(api.updateCustomPrompt).toHaveBeenCalledWith("custom/abc123", "检查节奏、冲突和章尾钩子。");
    wrapper.unmount();
  });
});
