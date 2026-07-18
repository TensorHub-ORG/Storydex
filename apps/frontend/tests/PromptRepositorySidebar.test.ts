import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ fetchPromptRepository: vi.fn() }));

vi.mock("@/api/help", () => ({ fetchPromptRepository: api.fetchPromptRepository }));
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
            updatedAt: ""
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
});
