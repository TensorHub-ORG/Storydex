import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, shallowMount } from "@vue/test-utils";

const api = vi.hoisted(() => ({
  fetchAgentCoomiConfig: vi.fn(),
  fetchAgentCoomiModels: vi.fn(),
  updateAgentCoomiConfig: vi.fn()
}));

const agentStore = vi.hoisted(() => ({
  refreshCoomiStatus: vi.fn()
}));

vi.mock("@/api/agent", () => api);
vi.mock("@/stores/agent", () => ({ useAgentStore: () => agentStore }));

import CoomiConfigPanel from "@/components/CoomiConfigPanel.vue";

const initialConfig = {
  version: 1,
  active: "primary",
  providers: {
    primary: {
      type: "openai",
      display: "Primary",
      api_key: "primary-key",
      base_url: "https://primary.example/v1",
      model: "primary-model",
      tool_protocol: "native"
    },
    secondary: {
      type: "generic",
      display: "Secondary",
      api_key: "secondary-key",
      base_url: "https://secondary.example/v1",
      model: "secondary-model",
      tool_protocol: "auto"
    }
  }
};

beforeEach(() => {
  vi.clearAllMocks();
  api.fetchAgentCoomiConfig.mockResolvedValue({
    data: {
      configPath: "C:/isolated/providers.json",
      content: `${JSON.stringify(initialConfig)}\n`,
      updatedAt: "2026-07-21T00:00:00Z"
    }
  });
  api.fetchAgentCoomiModels.mockResolvedValue({ data: { endpoint: "https://example.test/v1/models", models: [] } });
  api.updateAgentCoomiConfig.mockImplementation(async ({ content }: { content: string }) => ({
    data: {
      configPath: "C:/isolated/providers.json",
      content,
      updatedAt: "2026-07-21T00:00:01Z"
    }
  }));
  agentStore.refreshCoomiStatus.mockResolvedValue(undefined);
});

describe("CoomiConfigPanel", () => {
  it("renders free-form model comboboxes and only the Save and Apply footer actions", async () => {
    const wrapper = shallowMount(CoomiConfigPanel, { props: { visible: true } });
    await flushPromises();

    expect(wrapper.find("input.coomi-model-input").exists()).toBe(true);
    expect(wrapper.find("input.coomi-fast-model-input").exists()).toBe(true);
    expect(wrapper.findAll("datalist")).toHaveLength(0);
    expect(wrapper.findAll(".llm-model-combobox")).toHaveLength(2);
    expect(wrapper.text()).not.toContain("设为当前");
    expect(wrapper.text()).not.toContain("测试响应");
    expect(wrapper.findAll(".coomi-config-footer .coomi-config-action").map((button) => button.text())).toEqual([
      "save保存",
      "task_alt应用"
    ]);
  });

  it("keeps custom model names and renders fetched suggestions in a scrollable custom list", async () => {
    api.fetchAgentCoomiModels.mockResolvedValue({
      data: { endpoint: "https://primary.example/v1/models", models: ["listed-model", "listed-fast-model"] }
    });
    const wrapper = shallowMount(CoomiConfigPanel, { props: { visible: true } });
    await flushPromises();

    const standardInput = wrapper.find("input.coomi-model-input");
    const fastInput = wrapper.find("input.coomi-fast-model-input");
    await standardInput.setValue("private/custom-model");
    await fastInput.setValue("private/custom-fast-model");
    await wrapper.find(".coomi-model-fetch-row button").trigger("click");
    await flushPromises();

    expect((standardInput.element as HTMLInputElement).value).toBe("private/custom-model");
    expect((fastInput.element as HTMLInputElement).value).toBe("private/custom-fast-model");
    const list = wrapper.find(".coomi-standard-model-options");
    expect(list.exists()).toBe(true);
    expect(list.classes()).toContain("llm-options-menu--models");
    expect(list.findAll(".llm-model-option").map((option) => option.text())).toEqual([
      "listed-model",
      "listed-fast-model"
    ]);
  });

  it("normalizes legacy provider aliases to Coomi 1.2.1 canonical modes", async () => {
    const wrapper = shallowMount(CoomiConfigPanel, { props: { visible: true } });
    await flushPromises();

    const typeTrigger = wrapper.find(".coomi-provider-type-trigger");
    expect(typeTrigger.text()).toContain("OpenAI Responses");
    await wrapper.findAll(".coomi-config-footer .coomi-config-action")[0].trigger("click");
    await flushPromises();

    const savedPayload = JSON.parse(api.updateAgentCoomiConfig.mock.calls[0][0].content);
    expect(savedPayload.providers.primary.type).toBe("openai_responses");
    expect(savedPayload.providers.secondary.type).toBe("openai_compatible");
  });

  it("omits the current provider type from the open menu instead of repeating its label", async () => {
    const wrapper = shallowMount(CoomiConfigPanel, { props: { visible: true } });
    await flushPromises();

    const trigger = wrapper.find(".coomi-provider-type-trigger");
    expect(trigger.text()).toContain("OpenAI Responses");
    await trigger.trigger("click");

    const options = wrapper.findAll(".llm-provider-type-option");
    expect(options).toHaveLength(2);
    expect(options.map((option) => option.text())).toEqual(["OpenAI Compatible", "Anthropic Messages"]);
    expect(wrapper.find(".llm-options-menu--provider").text()).not.toContain("OpenAI Responses");

    await options[0].trigger("click");
    expect(wrapper.find(".coomi-provider-type-trigger").text()).toContain("OpenAI Compatible");
    expect(wrapper.find(".llm-options-menu--provider").exists()).toBe(false);
  });

  it("supports keyboard selection in the fetched model list", async () => {
    api.fetchAgentCoomiModels.mockResolvedValue({
      data: { endpoint: "https://primary.example/v1/models", models: ["model-a", "model-b", "model-c"] }
    });
    const wrapper = shallowMount(CoomiConfigPanel, { props: { visible: true } });
    await flushPromises();

    await wrapper.find(".coomi-model-fetch-row button").trigger("click");
    await flushPromises();
    const standardInput = wrapper.find("input.coomi-model-input");
    expect(standardInput.attributes("aria-expanded")).toBe("true");

    await standardInput.trigger("keydown", { key: "ArrowDown" });
    await standardInput.trigger("keydown", { key: "Enter" });
    expect((standardInput.element as HTMLInputElement).value).toBe("model-b");
    expect(standardInput.attributes("aria-expanded")).toBe("false");
  });

  it("shows the provider type explanation from a compact hoverable and clickable info icon", async () => {
    const wrapper = shallowMount(CoomiConfigPanel, { props: { visible: true } });
    await flushPromises();

    const help = wrapper.find(".llm-field-help");
    const trigger = wrapper.find(".llm-field-help__trigger");
    expect(trigger.text()).toBe("!");
    expect(trigger.attributes("aria-expanded")).toBe("false");
    expect(wrapper.find(".llm-field-help__tooltip").exists()).toBe(false);

    await help.trigger("mouseenter");
    expect(wrapper.find(".llm-field-help__tooltip").exists()).toBe(true);
    await help.trigger("mouseleave");
    expect(wrapper.find(".llm-field-help__tooltip").exists()).toBe(false);

    await trigger.trigger("click");
    expect(trigger.attributes("aria-expanded")).toBe("true");
    expect(wrapper.find(".llm-field-help__tooltip").text()).toContain("Coomi 1.2.1");
    await trigger.trigger("keydown", { key: "Escape" });
    expect(wrapper.find(".llm-field-help__tooltip").exists()).toBe(false);
  });

  it("saves without changing active and applies the edited provider after an id rename", async () => {
    const wrapper = shallowMount(CoomiConfigPanel, { props: { visible: true } });
    await flushPromises();

    const providerSelect = wrapper.find(".coomi-provider-picker select");
    await providerSelect.setValue("secondary");
    await wrapper.find('input[placeholder="deepseek"]').setValue("renamed-secondary");

    const footerActions = wrapper.findAll(".coomi-config-footer .coomi-config-action");
    await footerActions[0].trigger("click");
    await flushPromises();
    const savedPayload = JSON.parse(api.updateAgentCoomiConfig.mock.calls[0][0].content);
    expect(savedPayload.active).toBe("primary");
    expect(savedPayload.providers["renamed-secondary"].model).toBe("secondary-model");
    expect(savedPayload.providers.secondary).toBeUndefined();
    expect(agentStore.refreshCoomiStatus).not.toHaveBeenCalled();

    await footerActions[1].trigger("click");
    await flushPromises();
    const appliedPayload = JSON.parse(api.updateAgentCoomiConfig.mock.calls[1][0].content);
    expect(appliedPayload.active).toBe("renamed-secondary");
    expect(agentStore.refreshCoomiStatus).toHaveBeenCalledTimes(1);
    expect(wrapper.emitted("saved")).toHaveLength(1);
  });
});
