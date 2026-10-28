import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import WritingGuidanceEditor from "@/components/WritingGuidanceEditor.vue";
import { usePresetStore } from "@/stores/preset";

const api = vi.hoisted(() => ({
  listPresets: vi.fn(), savePresetDocument: vi.fn(), createWorkspaceFile: vi.fn(), refreshTree: vi.fn(),
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
});

describe("optional writing guidance", () => {
  it("starts disabled in existing presets and keeps author modules, style and custom advice when edited", async () => {
    const store = usePresetStore();
    store.document.modules = [{ id: "author", content: "保留我的文风" }];
    store.document.style.pov = "第三人称限知";
    store.document.guidanceModules = [{ id: "custom", content: "自己的建议" }];
    const wrapper = mount(WritingGuidanceEditor, { props: { document: store.document } });
    expect(wrapper.findAll<HTMLInputElement>("input").every((input) => !input.element.checked)).toBe(true);
    await wrapper.findAll("input")[0].setValue(true);
    let changed = wrapper.emitted("change")!.at(-1)![0] as typeof store.document;
    expect(changed.modules).toEqual(store.document.modules);
    expect(changed.style.pov).toBe("第三人称限知");
    expect(changed.guidanceModules!.find((m) => m.id === "custom")!.content).toBe("自己的建议");
    await wrapper.setProps({ document: changed });
    await wrapper.findAll("textarea")[0].setValue("保留连贯的长句。具体情绪通过本书文风表达。");
    changed = wrapper.emitted("change")!.at(-1)![0] as typeof store.document;
    await wrapper.setProps({ document: changed });
    await wrapper.findAll("input")[0].setValue(false);
    changed = wrapper.emitted("change")!.at(-1)![0] as typeof store.document;
    expect(changed.guidanceModules![0].enabledByDefault).toBe(false);
    expect(changed.guidanceModules![0].content).toContain("连贯的长句");
    expect(changed.guidanceModules![1].enabledByDefault).toBe(false);
    wrapper.unmount();
  });

  it("creates an editable library preset without replacing the active preset", async () => {
    const store = usePresetStore();
    const active = ".storydex/presets/active/本书文风.md";
    api.listPresets.mockResolvedValue({ data: { active: [{ name: "本书文风", path: active }], library: [], activeMainPreset: active } });
    const path = await store.createNaturalWritingPreset();
    expect(path).toBe(".storydex/presets/library/自然写作.md");
    expect(api.createWorkspaceFile).toHaveBeenCalledOnce();
    const saved = api.savePresetDocument.mock.calls[0][1];
    expect(saved.guidanceModules).toHaveLength(3);
    expect(saved.guidanceModules.every((m: any) => m.enabledByDefault && m.content)).toBe(true);
    expect(store.activeMainPreset).toBe(active);
    expect(api.refreshTree).toHaveBeenCalledOnce();
  });

  it("reopens the user's existing natural-writing preset without resetting edits or toggles", async () => {
    const path = ".storydex/presets/active/自然写作.md";
    api.listPresets.mockResolvedValue({ data: { active: [{ name: "自然写作", path }], library: [], activeMainPreset: path } });
    expect(await usePresetStore().createNaturalWritingPreset()).toBe(path);
    expect(api.createWorkspaceFile).not.toHaveBeenCalled();
    expect(api.savePresetDocument).not.toHaveBeenCalled();
  });
});
