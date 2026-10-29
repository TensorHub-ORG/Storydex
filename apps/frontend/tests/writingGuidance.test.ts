import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import WritingGuidanceEditor from "@/components/WritingGuidanceEditor.vue";
import { usePresetStore } from "@/stores/preset";

const api = vi.hoisted(() => ({
  listPresets: vi.fn(), savePresetDocument: vi.fn(), createWorkspaceFile: vi.fn(), refreshTree: vi.fn(), activatePreset: vi.fn(),
}));
vi.mock("@/api/presets", async (load) => ({ ...(await load<any>()), ...api }));
vi.mock("@/api/workspace", () => ({ createWorkspaceFile: api.createWorkspaceFile }));
vi.mock("@/stores/workspace", () => ({ useWorkspaceStore: () => ({ refreshTree: api.refreshTree }) }));

beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  api.listPresets.mockResolvedValue({ data: { active: [], library: [], activeMainPreset: "" } });
  api.savePresetDocument.mockResolvedValue({ data: {} });
  api.createWorkspaceFile.mockResolvedValue({ data: {} });
  api.activatePreset.mockResolvedValue({ data: { activeMainPreset: ".storydex/presets/active/小说写作指导.md" } });
});

describe("default writing guidance", () => {
  it("starts enabled in existing presets and keeps author modules, style and custom advice when edited", async () => {
    const store = usePresetStore();
    store.document.modules = [{ id: "author", content: "保留我的文风" }];
    store.document.style.pov = "第三人称限知";
    store.document.guidanceModules = [{ id: "custom", content: "自己的建议" }];
    const wrapper = mount(WritingGuidanceEditor, { props: { document: store.document } });
    expect(wrapper.findAll<HTMLInputElement>("input").every((input) => input.element.checked)).toBe(true);
    await wrapper.findAll("input")[0].setValue(false);
    let changed = wrapper.emitted("change")!.at(-1)![0] as typeof store.document;
    expect(changed.modules).toEqual(store.document.modules);
    expect(changed.style.pov).toBe("第三人称限知");
    expect(changed.guidanceModules!.find((m) => m.id === "custom")!.content).toBe("自己的建议");
    await wrapper.setProps({ document: changed });
    await wrapper.findAll("textarea")[0].setValue("保留连贯的长句。具体情绪通过本书文风表达。");
    changed = wrapper.emitted("change")!.at(-1)![0] as typeof store.document;
    await wrapper.setProps({ document: changed });
    await wrapper.findAll("input")[0].setValue(true);
    changed = wrapper.emitted("change")!.at(-1)![0] as typeof store.document;
    expect(changed.guidanceModules![0].enabledByDefault).toBe(true);
    expect(changed.guidanceModules![0].content).toContain("连贯的长句");
    expect(changed.guidanceModules![1].enabledByDefault).toBe(true);
    wrapper.unmount();
  });

  it("opens the active preset without replacing author settings", async () => {
    const store = usePresetStore();
    const active = ".storydex/presets/active/本书文风.md";
    api.listPresets.mockResolvedValue({ data: { active: [{ name: "本书文风", path: active }], library: [], activeMainPreset: active } });
    expect(await store.configureWritingGuidance()).toBe(active);
    expect(api.createWorkspaceFile).not.toHaveBeenCalled();
    expect(api.activatePreset).not.toHaveBeenCalled();
  });

  it("makes a configurable active preset for a project with no preset without adding a length contract", async () => {
    const path = await usePresetStore().configureWritingGuidance();
    expect(path).toBe(".storydex/presets/active/小说写作指导.md");
    expect(api.createWorkspaceFile).toHaveBeenCalledOnce();
    const saved = api.savePresetDocument.mock.calls[0][1];
    expect(saved.guidanceModules).toHaveLength(3);
    expect(saved.guidanceModules.every((m: any) => m.enabledByDefault && m.content)).toBe(true);
    expect(saved.lengthContract.bodyMinChars).toBe(0);
    expect(saved.lengthContract.bodyMaxChars).toBe(0);
    expect(api.activatePreset).toHaveBeenCalledWith(".storydex/presets/library/小说写作指导.md");
    expect(api.refreshTree).toHaveBeenCalledOnce();
  });

  it("reopens the user's existing natural-writing preset without resetting edits or toggles", async () => {
    const path = ".storydex/presets/active/自然写作.md";
    api.listPresets.mockResolvedValue({ data: { active: [{ name: "自然写作", path }], library: [], activeMainPreset: path } });
    expect(await usePresetStore().configureWritingGuidance()).toBe(path);
    expect(api.createWorkspaceFile).not.toHaveBeenCalled();
    expect(api.savePresetDocument).not.toHaveBeenCalled();
  });

  it("honors explicitly disabled advice in an older preset", () => {
    const store = usePresetStore();
    store.document.guidanceModules = [{ id: "writing_expression", enabledByDefault: false, content: "我的规则" }];
    const wrapper = mount(WritingGuidanceEditor, { props: { document: store.document } });
    expect(wrapper.findAll<HTMLInputElement>("input")[0].element.checked).toBe(false);
    expect(wrapper.findAll<HTMLInputElement>("input")[1].element.checked).toBe(true);
    expect(wrapper.findAll<HTMLTextAreaElement>("textarea")[0].element.value).toBe("我的规则");
    wrapper.unmount();
  });
});
