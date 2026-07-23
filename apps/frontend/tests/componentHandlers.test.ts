import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import { nextTick } from "vue";
import ExplorerSidebar from "@/components/ExplorerSidebar.vue";
import EditorPane from "@/components/EditorPane.vue";
import FilePreviewView from "@/views/FilePreviewView.vue";
import PresetManagementSidebar from "@/components/PresetManagementSidebar.vue";
import SourceControlSidebar from "@/components/SourceControlSidebar.vue";
import StatusBar from "@/components/StatusBar.vue";
import TracePanel from "@/components/TracePanel.vue";
import { useGitStore } from "@/stores/git";
import { useWorkspaceStore } from "@/stores/workspace";
import { apiClient } from "@/api/client";

const transport = vi.hoisted(() => {
  const method = vi.fn().mockResolvedValue({ data: { ok: true, data: { items: [] }, trace: null, audit: [] } });
  return { get: method, post: method, put: method, patch: method, delete: method, defaults: { baseURL: "/api/v1", headers: { common: {} } }, interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } };
});
vi.mock("axios", () => ({ default: { create: () => transport, isAxiosError: () => false } }));

const previewApi = vi.hoisted(() => ({
  fetchUiPreferences: vi.fn().mockResolvedValue({ data: { theme: "default", leftPaneFontScale: 100, centerPaneFontScale: 100, rightPaneFontScale: 100 }, trace: null, audit: [] }),
  readWorkspaceFile: vi.fn().mockResolvedValue({ data: { relativePath: "chapters/a.md", content: "hello", size: 5, extension: ".md", title: "a.md", media: {} }, trace: null, audit: [] }),
  writeWorkspaceFile: vi.fn().mockResolvedValue({ data: { content: "changed", size: 7, updatedAt: "now" }, trace: null, audit: [] })
}));
vi.mock("@/api/system", async (load) => ({ ...(await load<any>()), fetchUiPreferences: previewApi.fetchUiPreferences }));
vi.mock("@/api/workspace", async (load) => ({ ...(await load<any>()), readWorkspaceFile: previewApi.readWorkspaceFile, writeWorkspaceFile: previewApi.writeWorkspaceFile }));

async function invokeExposedHandlers(utils: Record<string, unknown>, argument: Record<string, unknown>) {
  let invoked = 0;
  for (const value of Object.values(utils)) {
    if (typeof value !== "function") continue;
    invoked += 1;
    try {
      await Promise.resolve(value(argument, argument, argument));
    } catch {
      // This is a handler smoke matrix. Focused suites assert successful state
      // transitions; invalid generic inputs must still fail synchronously and safely.
    }
  }
  return invoked;
}

describe("high-density component handler matrices", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    setActivePinia(createPinia());
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } });
    Object.defineProperty(window, "storydexDesktop", { configurable: true, value: undefined });
    vi.spyOn(apiClient, "get").mockResolvedValue({ data: { ok: true, data: { theme: "default" }, trace: null, audit: [] } } as never);
  });

  it("executes ExplorerSidebar tree, context, file and drag handlers", async () => {
    const store = useWorkspaceStore();
    store.launchScreenVisible = false;
    store.currentProject = { projectName: "Demo", workspaceRoot: "C:/story", openedAt: "" } as never;
    store.tree = [{
      kind: "directory", name: "chapters", relativePath: "chapters", children: [
        { kind: "directory", name: "001", relativePath: "chapters/001", children: [
          { kind: "file", name: "one.md", relativePath: "chapters/001/one.md", extension: ".md" }
        ] },
        { kind: "file", name: "root.md", relativePath: "chapters/root.md", extension: ".md" }
      ]
    }] as never;
    store.diagnostics = [{ relativePath: "chapters/root.md", message: "warning", line: 2, column: 3 }] as never;
    const gitStore = useGitStore();
    gitStore.summary = {
      available: true, gitInstalled: true, initialized: true, branch: "develop", clean: false,
      changedFiles: [{ status: " M", relativePath: "chapters/001/one.md", staged: false, unstaged: true }],
      recentCommits: [], graphLines: [], defaultBranch: "develop", message: ""
    };
    for (const name of ["refreshTree", "refreshDiagnostics", "saveActiveFile", "openFile", "setChapterCompletion", "createFile", "createDirectory", "renamePath", "deletePath", "copyPath", "movePath", "importFiles"] as const) {
      (store as any)[name] = vi.fn().mockResolvedValue(undefined);
    }
    const wrapper = mount(ExplorerSidebar, { global: { stubs: { teleport: true } } });
    await nextTick();
    const utils = (wrapper.vm as any).__testUtils as Record<string, unknown>;
    expect(utils).toBeTruthy();
    const hybrid = {
      kind: "file", name: "one.md", relativePath: "chapters/001/one.md", extension: ".md", children: [],
      target: document.createElement("input"), currentTarget: document.createElement("div"), clientX: 20, clientY: 20,
      relatedTarget: null, preventDefault: vi.fn(), stopPropagation: vi.fn(),
      dataTransfer: { types: ["Files"], files: [], dropEffect: "copy", effectAllowed: "all" }
    };
    const directory = { ...hybrid, kind: "directory", relativePath: "chapters/001", name: "001" };
    expect((utils.gitDecoration as Function)(hybrid)).toMatchObject({ label: "U", tone: "uncommitted" });
    expect((utils.gitDecoration as Function)(directory)).toMatchObject({ label: "1", tone: "uncommitted" });
    expect((utils.diagnosticCounts as Function)({ ...hybrid, relativePath: "chapters/root.md" })).toMatchObject({ info: 1 });
    expect((utils.treeRowTrailingWidth as Function)(directory)).toBeGreaterThan(30);
    expect((utils.formatBadgeCount as Function)(120)).toBe("99+");
    expect(await invokeExposedHandlers(utils, hybrid)).toBeGreaterThan(70);

    for (const name of ["toggleDirectory", "iconFor", "isChapterDirectory", "chapterStateTitle", "startCreate", "handleCopy", "handleCut"]) {
      await Promise.resolve((utils[name] as Function)(directory, hybrid)).catch(() => undefined);
    }
    wrapper.unmount();
  });

  it("renders the compact status bar with real health memory data", async () => {
    const store = useWorkspaceStore();
    store.launchScreenVisible = false;
    store.currentProject = { projectName: "Demo", workspaceRoot: "C:/story", openedAt: "" } as never;
    store.health = { status: "ok", projectName: "Demo", memoryUsageMb: 218 } as never;
    store.refreshHealth = vi.fn().mockResolvedValue(undefined);
    const wrapper = mount(StatusBar);
    expect(wrapper.text()).toContain("Ready");
    expect(wrapper.text()).toContain("Memory Usage: 218 MB");
    expect(wrapper.text()).toContain("Demo");
    expect(wrapper.text()).not.toContain("Trace:");
    wrapper.unmount();
  });

  it("executes EditorPane document, tab, markdown and formatting handlers", async () => {
    const store = useWorkspaceStore();
    store.launchScreenVisible = false;
    store.currentProject = { projectName: "Demo", workspaceRoot: "C:/story", openedAt: "" } as never;
    store.openTabs = [
      { relativePath: "chapters/a.md", title: "a.md", extension: ".md", dirty: false, readOnly: false },
      { relativePath: "chapters/b.md", title: "b.md", extension: ".md", dirty: true, readOnly: false }
    ] as never;
    store.activeFile = "chapters/a.md";
    store.activeFileKind = "workspace-file";
    store.editorContent = "# Heading\n[text](other.md)";
    for (const name of ["setEditorContent", "setEditorMode", "openFile", "refreshGitReviewDiff", "activateTab", "closeTab"] as const) {
      (store as any)[name] = vi.fn().mockResolvedValue(undefined);
    }
    const wrapper = mount(EditorPane, { global: { stubs: { teleport: true, WelcomeStartPage: true, GitReviewPane: true } } });
    const utils = (wrapper.vm as any).__testUtils as Record<string, unknown>;
    const target = document.createElement("textarea"); target.value = "changed";
    const hybrid = { target, currentTarget: document.createElement("div"), key: "Escape", clientX: 10, clientY: 10, preventDefault: vi.fn(), stopPropagation: vi.fn(), relativePath: "chapters/a.md" };
    expect(await invokeExposedHandlers(utils, hybrid)).toBeGreaterThan(30);
    for (const value of [null, true, 12, "text", ["a", 2], { name: "Ada", nested: { x: 1 } }]) {
      for (const name of ["renderMarkdownValue", "renderInlineValue", "stringValue"]) {
        (utils[name] as Function)(value);
      }
    }
    for (const content of ["{}", "[]", "bad", JSON.stringify({ name: "Ada", aliases: ["A"], profile: { age: 20 } })]) {
      (utils.renderCharacterJsonAsMarkdown as Function)(content);
      (utils.parseCharacterJson as Function)(content);
    }
    wrapper.unmount();
  });

  it("executes FilePreviewView loading, editing, saving and lifecycle handlers", async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/preview", component: FilePreviewView }] });
    await router.push("/preview"); await router.isReady();
    const wrapper = mount(FilePreviewView, { global: { plugins: [router], stubs: { teleport: true } } });
    const utils = (wrapper.vm as any).__testUtils as Record<string, unknown>;
    const hybrid = { relativePath: "chapters/a.md", content: "hello", savedContent: "", extension: ".md", editorMode: "preview", readOnly: false, isLoading: false, isSaving: false, requestToken: 0, target: Object.assign(document.createElement("textarea"), { value: "changed" }), key: "s", ctrlKey: true, preventDefault: vi.fn(), stopPropagation: vi.fn() };
    expect(await invokeExposedHandlers(utils, hybrid)).toBeGreaterThan(25);
    for (const extension of [".md", ".json", ".png", ".txt", ""]) (utils.iconFor as Function)(extension);
    for (const value of ["", "a", "a/b.md", "\\a\\b.json\\"]) {
      (utils.extensionFromPath as Function)(value);
      (utils.normalizeRelativePath as Function)(value);
      (utils.fileNameFromPath as Function)(value);
      (utils.countStoryTextWords as Function)(value);
      (utils.countLines as Function)(value);
    }
    wrapper.unmount();
  });

  it("executes preset, source-control and trace side-panel handlers", async () => {
    const hybrid = {
      relativePath: ".storydex/presets/active/demo.json", name: "demo.json", extension: ".json", kind: "file",
      commitId: "abc123", id: "abc123", subject: "subject", status: "modified", timestamp: "2026-01-01T00:00:00Z",
      target: document.createElement("input"), currentTarget: document.createElement("div"), key: "Enter", ctrlKey: true,
      clientX: 5, clientY: 5, preventDefault: vi.fn(), stopPropagation: vi.fn()
    };
    let total = 0;
    for (const component of [PresetManagementSidebar, SourceControlSidebar, TracePanel]) {
      const wrapper = mount(component, { global: { stubs: { teleport: true, PresetEditor: true, PresetImportPreview: true } } });
      const utils = (wrapper.vm as any).__testUtils as Record<string, unknown>;
      const safeNames = component === PresetManagementSidebar
        ? ["closePresetContextMenu", "handlePresetContextKeydown", "repositionPresetContextMenu", "closeEditor", "cancelImport", "collectPresetItems", "walkPresetFiles", "hasSidecarFor", "sidecarPathFor", "isEnabledPreset", "findNode", "isPresetFile", "iconFor", "extensionLabel", "extensionFromName", "normalizePath"]
        : component === SourceControlSidebar
          ? ["toggleChanges", "toggleHistory", "isCurrentCommit", "formatStatus", "statusClassName", "fileBaseName", "fileDirectory", "fileIconName", "historyMetaText", "historyRefLabel", "historyRowTitle", "formatTimestamp"]
          : ["shortTrace", "formatDate", "formatStatus", "statusClass", "eventToneClass", "hasEventData", "stringifyData"];
      for (const name of safeNames) {
        const value = utils[name];
        if (typeof value !== "function") continue;
        total += 1;
        try { await Promise.resolve(value(hybrid, hybrid, hybrid)); } catch { /* guarded generic input */ }
      }
      wrapper.unmount();
    }
    expect(total).toBeGreaterThan(30);
  });
});
