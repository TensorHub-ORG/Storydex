import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const api = vi.hoisted(() => ({
  fetchAgentRunDiff: vi.fn(), fetchHelpGuide: vi.fn(), fetchSystemBootstrap: vi.fn(), fetchSystemHealth: vi.fn(),
  copyWorkspacePath: vi.fn(), createWorkspaceDirectory: vi.fn(), createWorkspaceFile: vi.fn(), deleteWorkspacePath: vi.fn(),
  createWorkspaceProject: vi.fn(), fetchStoryProjectSettings: vi.fn(), fetchCurrentProject: vi.fn(), fetchWorkspaceGitDiff: vi.fn(), fetchWorkspaceGitCommitDiff: vi.fn(),
  fetchWorkspaceDiagnostics: vi.fn(), fetchWorkspaceTree: vi.fn(), importWorkspaceFiles: vi.fn(), initializeWorkspaceProject: vi.fn(),
  moveWorkspacePath: vi.fn(), openWorkspaceProject: vi.fn(), readWorkspaceFile: vi.fn(), readWorkspaceFileWindow: vi.fn(), applyWorkspaceDiagnosticFix: vi.fn(), renameWorkspacePath: vi.fn(),
  updateStoryChapterCompletion: vi.fn(), updateStoryProjectSettings: vi.fn(), writeWorkspaceFile: vi.fn()
}));
const agent = vi.hoisted(() => ({ resetSession: vi.fn(), loadSessions: vi.fn(), loadHistory: vi.fn() }));
const git = vi.hoisted(() => ({ reset: vi.fn(), refreshSummary: vi.fn() }));
const ui = vi.hoisted(() => ({ applyPersistedState: vi.fn() }));

vi.mock("@/api/agent", () => ({ fetchAgentRunDiff: api.fetchAgentRunDiff }));
vi.mock("@/api/help", () => ({ fetchHelpGuide: api.fetchHelpGuide }));
vi.mock("@/api/system", () => ({ fetchSystemBootstrap: api.fetchSystemBootstrap, fetchSystemHealth: api.fetchSystemHealth }));
vi.mock("@/api/workspace", () => api);
vi.mock("@/stores/agent", () => ({ useAgentStore: () => agent }));
vi.mock("@/stores/git", () => ({ useGitStore: () => git }));
vi.mock("@/stores/ui", () => ({ useUiStore: () => ui }));
vi.mock("@/api/client", async (load) => {
  const actual = await load<any>();
  return { ...actual, describeTransportError: (error: unknown, fallback: string) => error instanceof Error ? error.message : fallback };
});

import { useWorkspaceStore } from "@/stores/workspace";
import { ApiResponseError } from "@/api/client";

const result = (data: unknown) => ({ data, trace: null, audit: [] });
const project = { projectName: "Demo", workspaceRoot: "C:/story", storydexRoot: "C:/story/.storydex", storydexDirName: ".storydex", hasStorydexConfig: true, requiresInitialization: false, missingDirectories: [], projectState: "ready", openedAt: "now" };
const file = (relativePath = "chapters/a.md", content = "body") => ({ relativePath, content, size: content.length, updatedAt: "now", extension: ".md", kind: "file", title: relativePath.split("/").pop(), media: {} });
const tree = { roots: [{ kind: "directory", relativePath: "chapters", children: [{ kind: "file", relativePath: "chapters/a.md", extension: ".md" }] }], workspaceRoot: "C:/story", storydexRoot: "C:/story/.storydex", projectName: "Demo", hasStorydexConfig: true, requiresInitialization: false, missingDirectories: [], openedAt: "now" };
const settings = { segmentExtension: ".md", maxSegmentsPerChapter: 3, storyFragmentCount: 1, chapterLengthTier: "medium", chapterWordCountTarget: 2500, preciseWordCountEnabled: false, storyFragmentWordCount: 2500, storyFragmentWordCountMin: 2500, storyFragmentWordCountMax: 2500, storyChapterTemplateId: "default_chapter_directory", autoUpdateVariables: false, autoUpdateWiki: false, agentCommitPromptEnabled: true, autoNameChapterDirectories: false, contextConcisionMinCalls: 1, contextConcisionMaxCalls: 2, contextConcisionMaxInputTokens: 32000, chapterCompletion: {}, updatedAt: "now", settingsPath: ".storydex/config/project-settings.json" };
const diff = { available: true, gitInstalled: true, initialized: true, branch: "main", files: [{ relativePath: "chapters/a.md", status: "M", added: 1, removed: 0, hunks: [] }], totals: { files: 1, added: 1, removed: 0 }, message: "" };

beforeEach(() => {
  setActivePinia(createPinia()); vi.clearAllMocks();
  api.fetchSystemBootstrap.mockResolvedValue(result({ workspaceState: { lastProjectPath: "C:/story", recentProjects: [] }, uiPreferences: {} }));
  api.fetchSystemHealth.mockResolvedValue(result({ workspaceRoot: "C:/story", hasStorydexConfig: true }));
  api.fetchCurrentProject.mockResolvedValue(result(project)); api.fetchWorkspaceTree.mockResolvedValue(result(tree));
  api.fetchStoryProjectSettings.mockResolvedValue(result(settings));
  api.updateStoryProjectSettings.mockImplementation((payload: unknown) => Promise.resolve(result(payload)));
  api.updateStoryChapterCompletion.mockResolvedValue(result(settings)); api.fetchWorkspaceDiagnostics.mockResolvedValue(result({ items: [] }));
  api.openWorkspaceProject.mockResolvedValue(result(project)); api.createWorkspaceProject.mockResolvedValue(result(project)); api.initializeWorkspaceProject.mockResolvedValue(result(project));
  api.readWorkspaceFile.mockImplementation(({ relativePath }: any) => Promise.resolve(result(file(relativePath, relativePath.endsWith(".json") ? "{}" : "body"))));
  api.readWorkspaceFileWindow.mockImplementation(({ relativePath, startLine }: any) => Promise.resolve(result({ relativePath, content: `line-${startLine}\n`, size: 3 * 1024 * 1024, mtimeMs: Date.now(), startLine, loadedLines: 1, lineCount: 100000, lineCountExact: false, hasPrevious: startLine > 0, hasNext: true, mode: "progressive", readOnly: false, initialChunkBytes: 262144 })));
  api.applyWorkspaceDiagnosticFix.mockResolvedValue(result({ relativePath: "bom.json", fixId: "remove_utf8_bom", changed: true }));
  api.writeWorkspaceFile.mockImplementation(({ relativePath, content }: any) => Promise.resolve(result(file(relativePath, content))));
  api.createWorkspaceFile.mockImplementation(({ relativePath, content }: any) => Promise.resolve(result(file(relativePath, content))));
  api.createWorkspaceDirectory.mockResolvedValue(result({ relativePath: "notes", kind: "directory" }));
  api.importWorkspaceFiles.mockResolvedValue(result({ items: [{ relativePath: "notes/a.md", kind: "file" }] }));
  api.renameWorkspacePath.mockResolvedValue(result({ relativePath: "chapters/b.md", kind: "file" }));
  api.deleteWorkspacePath.mockResolvedValue(result({ relativePath: "chapters/b.md", kind: "file" }));
  api.copyWorkspacePath.mockResolvedValue(result({ relativePath: "chapters/c.md", kind: "file" }));
  api.moveWorkspacePath.mockResolvedValue(result({ relativePath: "chapters/d.md", kind: "file" }));
  api.fetchWorkspaceGitDiff.mockResolvedValue(result(diff)); api.fetchWorkspaceGitCommitDiff.mockResolvedValue(result(diff)); api.fetchAgentRunDiff.mockResolvedValue(result(diff));
  api.fetchHelpGuide.mockResolvedValue(result({ content: "# Guide", root: "docs", items: [{ updatedAt: "now" }] }));
  agent.loadSessions.mockResolvedValue(undefined); agent.loadHistory.mockResolvedValue(undefined); git.refreshSummary.mockResolvedValue(undefined);
});

describe("workspace store full action lifecycle", () => {
  it("boots, refreshes and transitions between launch and active project states", async () => {
    const store = useWorkspaceStore();
    await store.bootstrapGlobalState(); expect(ui.applyPersistedState).toHaveBeenCalled();
    await store.bootstrap(); expect(store.initialized).toBe(true);
    expect(store.launchScreenVisible).toBe(true);
    expect(api.fetchCurrentProject).not.toHaveBeenCalled();
    store.launchScreenVisible = false; store.initialized = false;
    await store.bootstrap(true); expect(store.currentProject?.projectName).toBe("Demo");
    await store.bootstrap(); store.isBootstrapping = true; await store.bootstrap(true); store.isBootstrapping = false;
    await store.refreshHealth(); await store.refreshProject(); await store.refreshTree({ silent: false });
    expect(store.tree.length).toBeGreaterThan(0);
    store.collapseTree(); expect(store.treeResetToken).toBeGreaterThan(0);
    store.enterLaunchScreen(); expect(store.launchScreenVisible).toBe(true);
  });

  it("rebinds the backend before accepting a tree from another workspace", async () => {
    const store = useWorkspaceStore();
    store.launchScreenVisible = false;
    store.currentProject = project as any;
    const staleTree = {
      ...tree,
      workspaceRoot: "C:/default",
      storydexRoot: "C:/default/.storydex",
      projectName: "default"
    };
    api.fetchWorkspaceTree
      .mockResolvedValueOnce(result(staleTree))
      .mockResolvedValueOnce(result(tree));

    await store.refreshTree();

    expect(api.openWorkspaceProject).toHaveBeenCalledWith({ projectPath: project.workspaceRoot });
    expect(api.fetchWorkspaceTree).toHaveBeenCalledTimes(2);
    expect(store.currentProject?.workspaceRoot).toBe(project.workspaceRoot);
  });

  it("reads, updates and falls back for story settings, completion and diagnostics", async () => {
    const store = useWorkspaceStore();
    await store.refreshStorySettings();
    store.launchScreenVisible = false; store.currentProject = project as any; store.tree = tree.roots as any;
    await store.refreshStorySettings(); expect(store.storySettings.storyFragmentWordCount).toBe(2500);
    await store.updateStorySettings({ storyFragmentCount: 2, chapterLengthTier: "long" });
    expect(api.updateStoryProjectSettings).toHaveBeenCalledWith(
      expect.objectContaining({
        chapterLengthTier: "long",
        chapter_length_tier: "long"
      })
    );
    const apiPayload = api.updateStoryProjectSettings.mock.calls[0][0];
    expect(apiPayload).not.toHaveProperty("chapterWordCountTarget");
    expect(apiPayload).not.toHaveProperty("preciseWordCountEnabled");
    expect(apiPayload).not.toHaveProperty("storyFragmentWordCount");
    const configWrite = api.writeWorkspaceFile.mock.calls.find(
      ([request]) => request.relativePath === ".storydex/config/project-settings.json"
    );
    const persistedConfig = JSON.parse(configWrite?.[0].content || "{}");
    expect(persistedConfig).toMatchObject({
      chapterLengthTier: "long",
      chapter_length_tier: "long"
    });
    expect(persistedConfig).not.toHaveProperty("chapterWordCountTarget");
    expect(persistedConfig).not.toHaveProperty("preciseWordCountEnabled");
    expect(store.storySettings.chapterLengthTier).toBe("long");
    api.readWorkspaceFile.mockResolvedValueOnce(result(file(
      ".storydex/config/project-settings.json",
      configWrite?.[0].content || "{}"
    )));
    const reopened = await store.readStorySettingsFromProjectFile();
    expect(reopened.chapterLengthTier).toBe("long");
    await store.setChapterCompletion("", true);
    await store.setChapterCompletion("chapters/a", true);
    await store.refreshDiagnostics();
    store.tree = []; await store.refreshDiagnostics(); expect(store.diagnostics).toEqual([]);
    const read = await store.readStorySettingsFromProjectFile(); expect(read.source).toBe("project_file");
    const written = await store.writeStorySettingsToProjectFile({ ...settings, storySegmentFormat: "md" } as any); expect(written.source).toBe("project_file");
  });

  it("opens/creates/initializes projects and exercises document/tab editing lifecycle", async () => {
    const store = useWorkspaceStore(); store.launchScreenVisible = false; store.currentProject = project as any;
    vi.spyOn(store, "reloadProjectContext").mockResolvedValue(undefined);
    await store.openProject("C:/story"); await store.openProjectTarget("C:/story/chapters/a.md", { isFile: false });
    await store.createProject("C:/new"); await store.initializeCurrentProject(); await store.initializeCurrentProject("C:/story");
    await store.openFile(""); await store.openFile("chapters/a.md");
    store.setEditorContent("changed"); expect(store.isDirty).toBe(true);
    await store.saveActiveFile();
    await store.reloadActiveFile(); await store.setEditorMode("edit"); await store.setEditorMode("preview");
    store.applyFileDocument(file("chapters/b.md", "b") as any); await store.activateTab("chapters/a.md");
    await store.closeTab("missing"); await store.closeTab("chapters/b.md");
    store.clearActiveFile(); expect(store.activeFile).toBe("");
  });

  it("performs file operations and remaps/removes/reconciles cached state", async () => {
    const store = useWorkspaceStore(); store.launchScreenVisible = false; store.currentProject = project as any;
    vi.spyOn(store, "refreshTree").mockResolvedValue(undefined);
    await store.createFile("notes/a.md", "a"); await store.createDirectory("notes"); await store.importFiles("notes", []);
    await store.renamePath("notes/a.md", "notes/b.md"); await store.copyPath("notes/b.md", "notes/c.md");
    await store.movePath("notes/c.md", "notes/d.md"); await store.deletePath("notes/d.md");
    store.applyFileDocument(file("dir/a.md") as any); store.applyFileDocument(file("keep.md") as any);
    store.remapPathState("dir", "renamed"); expect(store.documents["renamed/a.md"]).toBeTruthy();
    store.removePathState("renamed"); expect(store.documents["renamed/a.md"]).toBeUndefined();
    store.tree = [{ kind: "file", relativePath: "keep.md" }] as any; store.reconcileWorkspaceStateWithTree();
    expect(store.pathExistsInTree("keep.md")).toBe(true); expect(store.resolveExistingPath("missing")).toBe("");
  });

  it("surfaces file operation errors to workspaceError and rethrows", async () => {
    const store = useWorkspaceStore(); store.launchScreenVisible = false; store.currentProject = project as any;
    vi.spyOn(store, "refreshTree").mockResolvedValue(undefined);
    api.createWorkspaceDirectory.mockRejectedValueOnce(new ApiResponseError("目录已存在", "directory_exists"));

    await expect(store.createDirectory("notes")).rejects.toThrow("目录已存在");
    expect(store.workspaceError).toBe("目录已存在");

    api.createWorkspaceFile.mockResolvedValueOnce(result(file("notes/a.md", "a")));
    await store.createFile("notes/a.md", "a");
    expect(store.workspaceError).toBe("");
  });

  it("opens large files progressively, jumps windows, cancels stale reads and allows explicit full loading", async () => {
    const store = useWorkspaceStore(); store.launchScreenVisible = false; store.currentProject = project as any;
    store.tree = [{ kind: "file", relativePath: "large.md", size: 3 * 1024 * 1024, children: [] }] as any;
    await store.openFile("large.md");
    expect(api.readWorkspaceFileWindow).toHaveBeenCalledWith({ relativePath: "large.md", startLine: 0, lineCount: 400 });
    expect(store.activeLargeFileWindow?.mode).toBe("progressive");
    await store.loadLargeFileWindow(5000);
    expect(store.activeLargeFileWindow?.startLine).toBe(5000);
    await store.loadActiveFileFully();
    expect(api.readWorkspaceFile).toHaveBeenCalledWith({ relativePath: "large.md" });
  });

  it("opens previews, Git reviews, agent diffs and help guide documents", async () => {
    const store = useWorkspaceStore(); store.launchScreenVisible = false; store.currentProject = project as any;
    store.openAgentPreview({ sourcePath: "chapters/a.md", content: "preview", previewLines: [], title: "Preview" });
    expect(store.activeFileKind).toBe("agent-preview");
    await store.refreshGitReviewDiff({ focusPath: "chapters/a.md" }); expect(store.gitReviewDiff).toBeTruthy();
    await store.openGitReview({ focusPath: "chapters/a.md" }); expect(store.activeFileKind).toBe("git-review");
    await store.openCommitDiff({ commitId: "abc12345", label: "节点 abc12345" });
    expect(api.fetchWorkspaceGitCommitDiff).toHaveBeenCalledWith("abc12345");
    expect(store.activeFile).toBe("commit-diff:abc12345");
    expect(store.activeFileMedia.commitId).toBe("abc12345");
    await store.openWorldlineMapDocument(); expect(store.activeFileKind).toBe("worldline-map");
    await store.openAgentRunDiff({ traceId: "trace", sessionId: "session", changedFiles: ["chapters/a.md"] });
    await store.openAgentRunDiff({ traceId: "" }); expect(store.gitReviewError).toBeTruthy();
    await store.openHelpGuideDocument(); expect(store.activeFileKind).toBe("help-guide");
    store.clearTransientPreviews();
  });

  it("covers direct cache helpers, recent projects, tree metadata and save guard", async () => {
    const store = useWorkspaceStore();
    store.rememberRecentProject({ ...project, workspaceRoot: "" } as any); store.rememberRecentProject(project as any); store.rememberRecentProject(project as any);
    store.applyTreeProjectInfo(tree as any); expect(store.currentProject?.storydexDirName).toBe(".storydex");
    store.applyFileDocument(file("a.md", "a") as any); store.setEditorContent("b"); store.syncActiveDocument();
    store.activateCachedDocument("missing"); store.activateCachedDocument("a.md");
    store.ensureOpenTab("a.md", ".md", true); store.ensureOpenTab("b.md", ".md", false);
    store.updateTabState("missing"); store.updateTabState("a.md");
    store.syncAuxiliaryDocument(file("missing.json", "{}") as any); store.syncAuxiliaryDocument(file("a.md", "updated") as any);
    store.syncActiveGitReviewDocument();
    store.isDirty = false; expect(await store.saveDirtyActiveFileIfNeeded()).toBe(true);
    store.resetStoryWorkspaceState(); store.clearActiveFile();
  });
});
