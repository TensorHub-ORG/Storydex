import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { createApp } from "vue";
import { ApiResponseError } from "@/api/client";

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
import { readProjectDrafts } from "@/utils/draftStorage";
import * as draftStorage from "@/utils/draftStorage";
import { flushPromises } from "@vue/test-utils";

const result = (data: unknown) => ({ data, trace: null, audit: [] });
const project = { projectName: "Demo", workspaceRoot: "C:/story", storydexRoot: "C:/story/.storydex", storydexDirName: ".storydex", hasStorydexConfig: true, requiresInitialization: false, missingDirectories: [], projectState: "ready", openedAt: "now" };
const file = (relativePath = "chapters/a.md", content = "body") => ({ relativePath, content, size: content.length, updatedAt: "now", extension: ".md", kind: "file", title: relativePath.split("/").pop(), media: {} });
const tree = { roots: [{ kind: "directory", relativePath: "chapters", children: [{ kind: "file", relativePath: "chapters/a.md", extension: ".md" }] }], workspaceRoot: "C:/story", storydexRoot: "C:/story/.storydex", projectName: "Demo", hasStorydexConfig: true, requiresInitialization: false, missingDirectories: [], openedAt: "now" };
const settings = { segmentExtension: ".md", maxSegmentsPerChapter: 3, storyFragmentCount: 1, chapterLengthTier: "medium", chapterWordCountTarget: 2500, preciseWordCountEnabled: false, storyFragmentWordCount: 2500, storyFragmentWordCountMin: 2500, storyFragmentWordCountMax: 2500, storyChapterTemplateId: "default_chapter_directory", autoUpdateVariables: false, autoUpdateWiki: false, agentCommitPromptEnabled: true, autoNameChapterDirectories: false, contextConcisionMinCalls: 1, contextConcisionMaxCalls: 2, contextConcisionMaxInputTokens: 32000, chapterCompletion: {}, updatedAt: "now", settingsPath: ".storydex/config/project-settings.json" };
const diff = { available: true, gitInstalled: true, initialized: true, branch: "main", files: [{ relativePath: "chapters/a.md", status: "M", added: 1, removed: 0, hunks: [] }], totals: { files: 1, added: 1, removed: 0 }, message: "" };

// Input and saving enqueue background transactions. Finish them before the next
// test replaces IndexedDB, just as a real page keeps its database for its lifetime.
afterEach(async () => {
  await vi.waitFor(() => expect(useWorkspaceStore().draftWritesPending).toBe(0));
  vi.unstubAllEnvs();
});

beforeEach(() => {
  // Install the real development plugin, including patchActionForGrouping.
  // Plain Vitest Pinia omits it and cannot reproduce per-action Proxy identities.
  vi.stubEnv("NODE_ENV", "development");
  const pinia = createPinia();
  createApp({}).use(pinia);
  setActivePinia(pinia); vi.clearAllMocks(); localStorage.clear();
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
  it("runs with the real Pinia development action wrapper", () => {
    expect((useWorkspaceStore() as any)._isOptionsAPI).toBe(true);
  });

  it("keeps a settings save error through successful polling until a successful write", async () => {
    const store = useWorkspaceStore(); store.currentProject = project; store.launchScreenVisible = false;
    api.updateStoryProjectSettings.mockRejectedValueOnce(new Error("设置保存失败"));
    await expect(store.updateStorySettings({ chapterLengthTier: "long" })).rejects.toThrow();
    await store.refreshStorySettings();
    expect(store.storySettingsError).toContain("设置保存失败");
    await store.updateStorySettings({ chapterLengthTier: "short" });
    expect(store.storySettingsError).toBe("");
    api.updateStoryProjectSettings.mockRejectedValueOnce(new Error("另一次失败"));
    await expect(store.updateStorySettings({ chapterLengthTier: "long" })).rejects.toThrow();
    store.dismissStorySettingsError();
    await store.refreshStorySettings();
    expect(store.storySettingsError).toBe("");
  });

  it("does not rebind pending project drafts when polling sees the next backend project", async () => {
    const store = useWorkspaceStore(); store.currentProject = project; store.launchScreenVisible = false;
    store.applyFileDocument(file()); store.setEditorContent("belongs to A");
    api.fetchCurrentProject.mockResolvedValueOnce(result({ ...project, workspaceRoot: "C:/B" }));
    await store.refreshProject();
    expect(store.currentProject.workspaceRoot).toBe(project.workspaceRoot);
    await store.persistDrafts();
    expect((await readProjectDrafts<any>(project.workspaceRoot))["chapters/a.md"].content).toBe("belongs to A");
    expect(await readProjectDrafts("C:/B")).toEqual({});
  });

  it("ignores polling errors belonging to a previous project", async () => {
    const store = useWorkspaceStore(); store.currentProject = project; store.launchScreenVisible = false;
    const rejects: ((error: Error) => void)[] = [];
    const delay = () => new Promise((_resolve, reject) => rejects.push(reject));
    api.fetchSystemHealth.mockImplementationOnce(delay);
    api.fetchCurrentProject.mockImplementationOnce(delay);
    api.fetchWorkspaceTree.mockImplementationOnce(delay);
    const polls = [store.refreshHealth(), store.refreshProject(), store.refreshTree()];
    await store.enterLaunchScreen();
    store.workspaceError = "current project message";
    rejects.forEach(reject => reject(new Error("old project failed")));
    await Promise.all(polls);
    expect(store.workspaceError).toBe("current project message");
  });

  it("ignores older settings reads, including failures and another project's response", async () => {
    const store = useWorkspaceStore(); store.currentProject = project; store.launchScreenVisible = false;
    let old!: (value: unknown) => void;
    api.fetchStoryProjectSettings.mockImplementationOnce(() => new Promise(resolve => { old = resolve; }));
    const oldRead = store.refreshStorySettings();
    api.fetchStoryProjectSettings.mockResolvedValueOnce(result({ ...settings, chapterLengthTier: "long" }));
    await store.refreshStorySettings();
    old(result(settings)); await oldRead;
    expect(store.storySettings.chapterLengthTier).toBe("long");

    let fail!: (error: Error) => void;
    api.fetchStoryProjectSettings.mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
    const failing = store.refreshStorySettings();
    await store.updateStorySettings({ chapterLengthTier: "short" });
    fail(new Error("old GET failed")); await failing;
    expect(store.storySettings.chapterLengthTier).toBe("short");
    expect(store.storySettingsError).toBe("");

    api.fetchStoryProjectSettings.mockImplementationOnce(() => new Promise(resolve => { old = resolve; }));
    const projectARead = store.refreshStorySettings();
    await store.enterLaunchScreen();
    store.currentProject = { ...project, workspaceRoot: "C:/B" }; store.launchScreenVisible = false;
    await store.refreshStorySettings();
    old(result({ ...settings, chapterLengthTier: "long" })); await projectARead;
    expect(store.storySettings.chapterLengthTier).toBe("medium");
    expect(api.fetchStoryProjectSettings).toHaveBeenLastCalledWith("C:/B");
  });

  it("queues chapter completion with settings and project switches", async () => {
    const store = useWorkspaceStore(); store.currentProject = project; store.launchScreenVisible = false;
    vi.spyOn(store, "reloadProjectContext").mockResolvedValue(undefined);
    let finishSettings!: (value: unknown) => void; let finishCompletion!: (value: unknown) => void;
    api.updateStoryProjectSettings.mockImplementationOnce(() => new Promise(resolve => { finishSettings = resolve; }));
    api.updateStoryChapterCompletion.mockImplementationOnce(() => new Promise(resolve => { finishCompletion = resolve; }));
    const setting = store.updateStorySettings({ chapterLengthTier: "long" });
    const completion = store.setChapterCompletion("chapters/a", true);
    const switching = store.openProject("C:/B");
    expect(store.isProjectSwitching).toBe(true);
    await expect(store.openProject("C:/C")).rejects.toThrow("项目操作正在进行");
    await flushPromises();
    expect(api.updateStoryChapterCompletion).not.toHaveBeenCalled();
    expect(api.openWorkspaceProject).not.toHaveBeenCalled();
    finishSettings(result({ ...settings, chapterLengthTier: "long" })); await setting;
    await flushPromises();
    expect(api.updateStoryChapterCompletion).toHaveBeenCalledWith({ chapterPath: "chapters/a", completed: true, workspaceRoot: project.workspaceRoot });
    expect(api.openWorkspaceProject).not.toHaveBeenCalled();
    finishCompletion(result({ ...settings, chapterLengthTier: "long", chapterCompletion: { "chapters/a": true } }));
    await Promise.all([completion, switching]);
    expect(store.isProjectSwitching).toBe(false);
    expect(api.updateStoryProjectSettings.mock.calls[0][0].workspaceRoot).toBe(project.workspaceRoot);
  });

  it("keeps queues separate for two Pinia instances sharing the same store id", async () => {
    const first = useWorkspaceStore(); first.currentProject = project; first.launchScreenVisible = false;
    const secondPinia = createPinia(); createApp({}).use(secondPinia);
    const second = useWorkspaceStore(secondPinia); second.currentProject = { ...project, workspaceRoot: "C:/B" }; second.launchScreenVisible = false;
    let finish!: (value: unknown) => void;
    api.updateStoryProjectSettings.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const writing = first.updateStorySettings({ chapterLengthTier: "short" });
    await flushPromises();
    await second.updateStorySettings({ chapterLengthTier: "long" });
    expect(second.storySettings.chapterLengthTier).toBe("long");
    finish(result({ ...settings, chapterLengthTier: "short" })); await writing;
    expect(first.storySettings.chapterLengthTier).toBe("short");
  });
  it("waits for the newest draft while a project switch is pending", async () => {
    const store = useWorkspaceStore(); store.currentProject = project;
    store.applyFileDocument(file()); store.setEditorContent("first");
    await store.persistDrafts();
    let release!: () => void;
    const updating = draftStorage.updateProjectDrafts;
    const delay = vi.spyOn(draftStorage, "updateProjectDrafts").mockImplementationOnce(async (root, changes) => {
      await new Promise<void>(resolve => { release = resolve; });
      await updating(root, changes);
    });
    const preserving = store.preserveProjectDrafts();
    await flushPromises();
    store.setEditorContent("typed during backup");
    release();
    await preserving;
    expect(store.draftWritesPending).toBe(0);
    expect((await readProjectDrafts<any>(project.workspaceRoot))["chapters/a.md"].content).toBe("typed during backup");
    delay.mockRestore();
  });

  it("does not restore a saved and closed draft after later edits are coalesced", async () => {
    const store = useWorkspaceStore(); store.currentProject = project;
    vi.spyOn(store, "refreshTree").mockResolvedValue(undefined);
    store.applyFileDocument(file()); store.setEditorContent("saved then closed");
    await store.persistDrafts();
    await store.closeTab("chapters/a.md");
    store.applyFileDocument(file("other.md")); store.setEditorContent("keep this draft");
    await store.persistDrafts();
    const drafts = await readProjectDrafts<any>(project.workspaceRoot);
    expect(drafts["chapters/a.md"]).toBeUndefined();
    expect(drafts["other.md"].content).toBe("keep this draft");
  });

  it("updates draft paths on rename and removes backups after explicit deletion", async () => {
    const store = useWorkspaceStore(); store.currentProject = project;
    store.applyFileDocument(file()); store.setEditorContent("renamed draft");
    await store.persistDrafts();
    store.remapPathState("chapters/a.md", "chapters/new.md");
    await store.persistDrafts();
    const drafts = await readProjectDrafts<any>(project.workspaceRoot);
    expect(drafts["chapters/a.md"]).toBeUndefined();
    expect(drafts["chapters/new.md"].content).toBe("renamed draft");
    store.removePathState("chapters/new.md");
    await store.persistDrafts();
    expect(await readProjectDrafts(project.workspaceRoot)).toEqual({});
  });

  it("finishes settings writes before switching the backend project", async () => {
    const store = useWorkspaceStore(); store.currentProject = project; store.launchScreenVisible = false;
    vi.spyOn(store, "reloadProjectContext").mockResolvedValue(undefined);
    let finish!: (value: unknown) => void;
    api.updateStoryProjectSettings.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const writing = store.updateStorySettings({ chapterLengthTier: "long" });
    const switching = store.openProject("C:/next");
    await flushPromises();
    expect(api.openWorkspaceProject).not.toHaveBeenCalled();
    finish(result({ ...settings, chapterLengthTier: "long" }));
    await Promise.all([writing, switching]);
    expect(api.openWorkspaceProject).toHaveBeenCalledWith({ projectPath: "C:/next" });
  });

  it("persists precise settings in order and rejects stale polling responses", async () => {
    const store = useWorkspaceStore();
    store.currentProject = project; store.launchScreenVisible = false;
    store.storySettings = { ...settings } as any;
    let finishPoll!: (value: unknown) => void;
    let finishWrite!: (value: unknown) => void;
    api.fetchStoryProjectSettings.mockImplementationOnce(() => new Promise(resolve => { finishPoll = resolve; }));
    const polling = store.refreshStorySettings();
    api.updateStoryProjectSettings.mockImplementationOnce(() => new Promise(resolve => { finishWrite = resolve; }));
    const first = store.updateStorySettings({ preciseWordCountEnabled: true });
    const second = store.updateStorySettings({ chapterWordCountTarget: 6000, chapterLengthTier: "short" });
    await flushPromises();
    expect(api.updateStoryProjectSettings).toHaveBeenCalledTimes(1);
    expect(api.updateStoryProjectSettings.mock.calls[0][0]).toMatchObject({ preciseWordCountEnabled: true, chapterWordCountTarget: 2500 });
    finishWrite(result({ ...settings, preciseWordCountEnabled: true }));
    await Promise.all([first, second]);
    expect(api.updateStoryProjectSettings.mock.calls[1][0]).toMatchObject({ preciseWordCountEnabled: true, chapterWordCountTarget: 6000, chapterLengthTier: "short" });
    finishPoll(result(settings)); await polling;
    expect(store.storySettings).toMatchObject({ preciseWordCountEnabled: true, chapterWordCountTarget: 6000, chapterLengthTier: "short" });
    expect(store.storySettingsWritesPending).toBe(0);
  });

  it("restores a large draft after reload without using localStorage or changing disk", async () => {
    const store = useWorkspaceStore(); store.currentProject = project;
    const saved = "正文".repeat(1_390_000);
    const draft = saved + "TAIL_AFTER_EDIT";
    store.applyFileDocument({ ...file("large.md", saved), fullContentSha256: "disk-v1" });
    const quota = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("quota", "QuotaExceededError"); });
    store.setEditorContent(draft);
    expect(await store.persistDrafts()).toBe(true);
    await store.openFile("other.md");
    expect(store.activeFile).toBe("other.md");
    setActivePinia(createPinia());
    const reloaded = useWorkspaceStore();
    await reloaded.openProject(project.workspaceRoot);
    expect(reloaded.documents["large.md"].content).toBe(draft);
    expect(reloaded.documents["large.md"].savedContent).toBe(saved);
    expect(reloaded.documents["large.md"].dirty).toBe(true);
    expect(api.writeWorkspaceFile).not.toHaveBeenCalled();
    quota.mockRestore();
  });

  it("keeps memory drafts usable and errors visible if the backup transaction fails", async () => {
    const store = useWorkspaceStore(); store.currentProject = project;
    store.applyFileDocument(file());
    const fail = vi.spyOn(draftStorage, "updateProjectDrafts").mockRejectedValue(new Error("disk full"));
    expect(() => store.setEditorContent("unsaved")).not.toThrow();
    expect(await store.persistDrafts()).toBe(false);
    await store.openFile("other.md");
    expect(store.activeFile).toBe("other.md");
    await store.refreshTree();
    expect(store.draftPersistenceError).toContain("备份失败");
    await expect(store.openProject("C:/next")).rejects.toThrow("备份失败");
    expect(store.isProjectSwitching).toBe(false);
    expect(store.workspaceError).toContain("备份失败");
    expect(api.openWorkspaceProject).not.toHaveBeenCalled();
    expect(store.documents["chapters/a.md"].content).toBe("unsaved");
    fail.mockRestore();
    expect(await store.persistDrafts()).toBe(true);
    expect(store.draftPersistenceError).toBe("");
  });

  it("preserves a save error through polling until the document is saved successfully", async () => {
    const store = useWorkspaceStore(); store.currentProject = project;
    store.applyFileDocument(file()); store.setEditorContent("too big");
    api.writeWorkspaceFile.mockRejectedValueOnce(new Error("内容超过 8 MiB 保存上限"));
    await store.saveActiveFile();
    await store.refreshTree(); await store.refreshHealth();
    expect(store.documents["chapters/a.md"].saveError).toContain("8 MiB");
    expect(store.isDirty).toBe(true);
    await store.saveActiveFile();
    expect(store.documents["chapters/a.md"]?.saveError).toBeUndefined();
  });

  it("coalesces rapid draft updates and restores only the latest content", async () => {
    const store = useWorkspaceStore(); store.currentProject = project;
    store.applyFileDocument(file());
    for (let index = 0; index < 30; index++) store.setEditorContent(`draft-${index}`);
    expect(await store.persistDrafts()).toBe(true);
    const drafts = await readProjectDrafts<any>(project.workspaceRoot);
    expect(drafts["chapters/a.md"].content).toBe("draft-29");
    expect(store.draftWritesPending).toBe(0);
  });
  it("clears project loading without letting an old response end the next read", async () => {
    const store = useWorkspaceStore();
    store.currentProject = project;
    let finishOld!: (value: unknown) => void;
    let finishNew!: (value: unknown) => void;
    api.readWorkspaceFile.mockImplementationOnce(() => new Promise(resolve => { finishOld = resolve; }));
    const oldRead = store.openFile("old.md");
    expect(store.isFileLoading).toBe(true);
    await store.reloadProjectContext();
    expect(store.isFileLoading).toBe(false);
    api.readWorkspaceFile.mockImplementationOnce(() => new Promise(resolve => { finishNew = resolve; }));
    const newRead = store.openFile("new.md");
    finishOld(result(file("old.md", "obsolete")));
    await oldRead;
    expect(store.isFileLoading).toBe(true);
    expect(store.documents["old.md"]).toBeUndefined();
    finishNew(result(file("new.md", "current")));
    await newRead;
    expect(store.isFileLoading).toBe(false);
    expect(store.editorContent).toBe("current");
  });

  it("saves the full large document after progressive loading and editing", async () => {
    const store = useWorkspaceStore();
    store.currentProject = project;
    store.tree = [{ kind: "file", relativePath: "large.md", size: 3 * 1024 * 1024 }] as any;
    const content = "正文\n".repeat(320000);
    api.readWorkspaceFile.mockResolvedValueOnce(result({ ...file("large.md", content), fullContentSha256: "large-v1" }));
    await store.openFile("large.md");
    expect(store.activeDocumentReadOnly).toBe(true);
    await store.loadActiveFileFully();
    expect(store.activeDocumentReadOnly).toBe(false);
    expect(store.editorContent).toBe(content);
    vi.spyOn(store, "refreshTree").mockResolvedValue(undefined);
    store.setEditorContent(content + "TAIL_MARKER");
    await store.saveActiveFile();
    expect(api.writeWorkspaceFile).toHaveBeenCalledWith(expect.objectContaining({ content: content + "TAIL_MARKER", expectedContentSha256: "large-v1" }));
    expect(store.isDirty).toBe(false);
  });

  it("keeps typing after a save request and advances only the saved baseline", async () => {
    const store = useWorkspaceStore();
    store.currentProject = project;
    store.applyFileDocument({ ...file(), fullContentSha256: "v1" });
    vi.spyOn(store, "refreshTree").mockResolvedValue(undefined);
    let finish!: (value: unknown) => void;
    api.writeWorkspaceFile.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    store.setEditorContent("submitted");
    const saving = store.saveActiveFile();
    store.setEditorContent("submitted plus later typing");
    finish(result({ ...file("chapters/a.md", "submitted"), fullContentSha256: "v2" }));
    await saving;
    expect(api.writeWorkspaceFile).toHaveBeenCalledWith(expect.objectContaining({ content: "submitted", expectedContentSha256: "v1", workspaceRoot: project.workspaceRoot }));
    expect(store.editorContent).toBe("submitted plus later typing");
    expect(store.documents[store.activeFile].savedContent).toBe("submitted");
    expect(store.documents[store.activeFile].fullContentSha256).toBe("v2");
    expect(store.isDirty).toBe(true);
    await store.saveActiveFile();
    expect(api.writeWorkspaceFile.mock.calls[1][0].expectedContentSha256).toBe("v2");
    expect(store.isDirty).toBe(false);
  });

  it("restores project drafts independently and blocks switching during a save", async () => {
    const store = useWorkspaceStore();
    store.currentProject = project;
    store.launchScreenVisible = false;
    store.applyFileDocument(file());
    store.setEditorContent("project A draft");
    await store.openProject(project.workspaceRoot);
    expect(store.editorContent).toBe("project A draft");
    expect(store.isDirty).toBe(true);
    store.isSaving = true;
    await expect(store.openProject("C:/another")).rejects.toThrow("正在保存");
    store.isSaving = false;
    await store.enterLaunchScreen();
    store.currentProject = { ...project, workspaceRoot: "C:/another" };
    await store.restoreProjectDrafts();
    expect(Object.keys(store.documents)).toHaveLength(0);
    store.currentProject = project;
    await store.restoreProjectDrafts();
    expect(store.documents["chapters/a.md"].content).toBe("project A draft");
  });

  it("refreshes clean editors and preserves both versions of a dirty editor", async () => {
    const store = useWorkspaceStore();
    store.currentProject = project;
    store.applyFileDocument({ ...file(), fullContentSha256: "v1" });
    api.readWorkspaceFile.mockResolvedValue(result({ ...file("chapters/a.md", "agent edit"), fullContentSha256: "v2" }));
    await store.refreshOpenDocuments();
    expect(store.editorContent).toBe("agent edit");
    store.setEditorContent("my draft");
    api.readWorkspaceFile.mockResolvedValue(result({ ...file("chapters/a.md", "another agent edit"), fullContentSha256: "v3" }));
    await store.refreshOpenDocuments();
    expect(store.editorContent).toBe("my draft");
    expect(store.activeConflict?.content).toBe("another agent edit");
    await store.saveActiveFile();
    expect(api.writeWorkspaceFile).not.toHaveBeenCalled();
    store.openConflictCopy();
    expect(store.editorContent).toBe("another agent edit");
    expect(store.activeDocumentReadOnly).toBe(true);
    await store.activateTab("chapters/a.md");
    vi.spyOn(store, "refreshTree").mockResolvedValue(undefined);
    await store.resolveActiveConflict();
    expect(api.writeWorkspaceFile).toHaveBeenCalledWith(expect.objectContaining({ content: "my draft", expectedContentSha256: "v3" }));
    expect(store.activeConflict).toBeUndefined();
    expect(store.isDirty).toBe(false);
  });

  it("keeps a draft when the source is removed and ignores an obsolete file read", async () => {
    const store = useWorkspaceStore();
    store.currentProject = project;
    store.applyFileDocument(file());
    store.setEditorContent("recover me");
    store.tree = [];
    store.reconcileWorkspaceStateWithTree();
    expect(store.editorContent).toBe("recover me");
    let finish!: (value: unknown) => void;
    api.readWorkspaceFile.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const opening = store.openFile("slow.md");
    await store.activateTab("chapters/a.md");
    finish(result(file("slow.md", "late")));
    await opening;
    expect(store.activeFile).toBe("chapters/a.md");
    expect(store.editorContent).toBe("recover me");
  });
  it("locks the workspace after a desktop runtime identity mismatch", async () => {
    const store = useWorkspaceStore();
    store.launchScreenVisible = false;
    store.currentProject = project as any;
    await store.markRuntimeMismatch(new ApiResponseError(
      "桌面端未连接到 Storydex Rust 后端。",
      "runtime_mismatch"
    ));

    expect(store.runtimeMismatch).toBe(true);
    expect(store.launchScreenVisible).toBe(true);
    expect(store.currentProject).toBeNull();
    expect(() => store.ensureRuntimeAvailable()).toThrowError(/Rust 后端/);
  });

  it("acknowledges current diagnostics without hiding changed diagnostics", () => {
    const store = useWorkspaceStore();
    const diagnostic = {
      source: "workspace",
      severity: "error",
      relativePath: "chapters/a.md",
      line: 2,
      column: 1,
      message: "旧问题"
    } as never;
    store.diagnostics = [diagnostic];

    expect(store.visibleDiagnostics).toHaveLength(1);
    store.acknowledgeDiagnostic(diagnostic);
    expect(store.visibleDiagnostics).toHaveLength(0);

    store.diagnostics = [{ ...diagnostic, message: "文件变化后的新问题" }];
    expect(store.visibleDiagnostics).toHaveLength(1);
    store.acknowledgeAllDiagnostics();
    expect(store.visibleDiagnostics).toHaveLength(0);
  });

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
    await store.enterLaunchScreen(); expect(store.launchScreenVisible).toBe(true);
  });

  it("rejects a tree from another workspace without switching the backend", async () => {
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
      .mockResolvedValueOnce(result(staleTree));

    await store.refreshTree();

    expect(api.openWorkspaceProject).not.toHaveBeenCalled();
    expect(api.fetchWorkspaceTree).toHaveBeenCalledTimes(1);
    expect(store.workspaceError).toContain("工作区根目录不一致");
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
    expect(apiPayload).toHaveProperty("chapterWordCountTarget", 2500);
    expect(apiPayload).toHaveProperty("preciseWordCountEnabled", false);
    expect(apiPayload).not.toHaveProperty("storyFragmentWordCount");
    expect(api.writeWorkspaceFile).not.toHaveBeenCalled();
    expect(store.storySettings.chapterLengthTier).toBe("long");
    api.updateStoryProjectSettings.mockRejectedValueOnce(new Error("settings write failed"));
    await expect(store.updateStorySettings({ chapterLengthTier: "short" })).rejects.toThrow("settings write failed");
    expect(api.writeWorkspaceFile).not.toHaveBeenCalled();
    expect(store.storySettings.chapterLengthTier).toBe("long");
    await store.setChapterCompletion("", true);
    await store.setChapterCompletion("chapters/a", true);
    await store.refreshDiagnostics();
    store.tree = []; await store.refreshDiagnostics(); expect(store.diagnostics).toEqual([]);
    const read = await store.readStorySettingsFromProjectFile(); expect(read.source).toBe("project_file");
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
    expect(api.readWorkspaceFile).toHaveBeenCalledWith({ relativePath: "large.md", workspaceRoot: "C:/story" });
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
