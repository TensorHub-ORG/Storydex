import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const authApi = vi.hoisted(() => ({ changeAccountPassword: vi.fn(), fetchAccountSummary: vi.fn(), fetchCurrentAccount: vi.fn(), fetchPersistedSession: vi.fn(), loginAccount: vi.fn(), logoutAccount: vi.fn(), registerAccount: vi.fn(), updateAccountProfile: vi.fn() }));
const workspaceApi = vi.hoisted(() => ({ commitWorkspaceGitChanges: vi.fn(), fetchWorkspaceGitSummary: vi.fn(), initializeWorkspaceGitRepository: vi.fn(), restoreWorkspaceGitCommit: vi.fn(), fetchStoryChapters: vi.fn(), fetchStoryCurrentState: vi.fn(), fetchStoryLatestSnapshot: vi.fn() }));
const presetApi = vi.hoisted(() => ({ activatePreset: vi.fn(), compilePreset: vi.fn(), deactivatePreset: vi.fn(), fetchActivePreset: vi.fn(), fetchPresetDocument: vi.fn(), listPresets: vi.fn(), patchPresetParams: vi.fn(), riskCheckPreset: vi.fn(), savePresetDocument: vi.fn() }));
const systemApi = vi.hoisted(() => ({ updateUiPreferences: vi.fn() }));
const workspace = vi.hoisted(() => ({ launchScreenVisible: false, currentProject: { workspaceRoot: "C:/story" }, activeFileBindingOrPath: "preset.md", isDirty: false, openFile: vi.fn(), refreshTree: vi.fn() }));
const client = vi.hoisted(() => ({ setApiAuthToken: vi.fn() }));

vi.mock("@/api/auth", () => ({
  AuthApiError: class AuthApiError extends Error { status?: number; }, ...authApi
}));
vi.mock("@/api/workspace", () => workspaceApi);
vi.mock("@/api/presets", () => ({ PresetApiError: class PresetApiError extends Error {}, ...presetApi }));
vi.mock("@/api/system", () => systemApi);
vi.mock("@/stores/workspace", () => ({ useWorkspaceStore: () => workspace }));
vi.mock("@/api/client", async (load) => {
  const actual = await load<any>();
  return { ...actual, setApiAuthToken: client.setApiAuthToken, describeTransportError: (error: unknown, fallback: string) => error instanceof Error ? error.message : fallback };
});

import { AuthApiError } from "@/api/auth";
import { useAuthStore } from "@/stores/auth";
import { useGitStore } from "@/stores/git";
import { usePresetStore } from "@/stores/preset";
import { useStoryStore } from "@/stores/story";
import { useUiStore } from "@/stores/ui";

const result = (data: unknown) => ({ data, trace: null, audit: [] });
const user = { id: 1, username: "demo", nickname: "Demo" };
const gitSummary = { initialized: true, changedFiles: ["a.md"], recentCommits: [{ id: "abc", subject: "test" }] };

beforeEach(() => {
  setActivePinia(createPinia()); vi.clearAllMocks(); vi.useFakeTimers();
  authApi.fetchPersistedSession.mockResolvedValue(result({ authenticated: true, accessToken: "token", user }));
  authApi.loginAccount.mockResolvedValue(result({ accessToken: "token", user })); authApi.registerAccount.mockResolvedValue(result({ user }));
  authApi.fetchCurrentAccount.mockResolvedValue(result(user)); authApi.fetchAccountSummary.mockResolvedValue(result({ user, projects: [] }));
  authApi.updateAccountProfile.mockResolvedValue(result({ ...user, nickname: "Updated" })); authApi.changeAccountPassword.mockResolvedValue(result({ message: "ok" })); authApi.logoutAccount.mockResolvedValue(result({ message: "ok" }));
  workspaceApi.fetchWorkspaceGitSummary.mockResolvedValue(result(gitSummary)); workspaceApi.initializeWorkspaceGitRepository.mockResolvedValue(result(gitSummary));
  workspaceApi.commitWorkspaceGitChanges.mockResolvedValue(result({ created: true, summary: gitSummary })); workspaceApi.restoreWorkspaceGitCommit.mockResolvedValue(result({ summary: gitSummary, restoredCommit: { subject: "restore" }, backupRef: "backup" }));
  presetApi.listPresets.mockResolvedValue(result({ active: [], library: [], activeMainPreset: "main" }));
  presetApi.fetchActivePreset.mockResolvedValue(result({ activeMainPreset: "preset.md", document: { meta: {} }, warnings: [] }));
  presetApi.fetchPresetDocument.mockResolvedValue(result({ document: { meta: {} }, warnings: ["warning"] }));
  presetApi.compilePreset.mockResolvedValue(result({ ok: true })); presetApi.riskCheckPreset.mockResolvedValue(result({ ok: true }));
  presetApi.savePresetDocument.mockResolvedValue(result({ ok: true })); presetApi.patchPresetParams.mockResolvedValue(result({ ok: true })); presetApi.activatePreset.mockResolvedValue(result({})); presetApi.deactivatePreset.mockResolvedValue(result({}));
  workspaceApi.fetchStoryChapters.mockResolvedValue(result({ items: [{ relativePath: "chapters/one", name: "One", displayName: "Chapter One", chapterNumber: 1, completed: false }] }));
  workspaceApi.fetchStoryCurrentState.mockResolvedValue(result({ currentStatePath: ".storydex/current.json", latestSnapshotIndexPath: ".storydex/index.json", data: { updated_at: "now", latest_snapshot_path: ".storydex/memory/chapters/one/001.variables.json", full_state: { hp: 1 } } }));
  workspaceApi.fetchStoryLatestSnapshot.mockResolvedValue(result({ relativePath: ".storydex/memory/chapters/one/001.variables.json", snapshot: { chapterId: "one", segmentId: "001", snapshotOrder: 1, operations: [{ op: "set", path: "hp" }], memoryUpdates: [{ memory: "m" }], characterUpdates: [{ character: "A", changes: ["x"] }], eventUpdates: [{ event: "e", impact: "i" }], fullState: { hp: 1 } } }));
  systemApi.updateUiPreferences.mockResolvedValue(result({})); workspace.openFile.mockResolvedValue(undefined); workspace.refreshTree.mockResolvedValue(undefined);
});

describe("auth store", () => {
  it("covers bootstrap/login/register/profile/password/logout and getters", async () => {
    const store = useAuthStore(); expect(store.displayName).toBeTruthy(); expect(store.initials).toBe("S");
    await store.bootstrap(); expect(store.isAuthenticated).toBe(true); expect(store.displayName).toBe("Demo"); expect(store.initials).toBe("D");
    await store.bootstrap(); store.clearAuthError();
    expect(await store.login({ username: "demo", password: "pass" } as any)).toBe(true);
    expect(await store.register({ username: "demo", password: "pass" } as any)).toBe(true);
    expect(await store.refreshUser()).toEqual(user); expect(await store.refreshSummary()).toBeTruthy();
    expect(await store.updateProfile({ nickname: "Updated" } as any)).toBe(true); expect(store.user?.nickname).toBe("Updated");
    expect(await store.changePassword({ oldPassword: "old", newPassword: "new" } as any)).toBe(true);
    await store.logout(); expect(store.authToken).toBe(""); store.clearSession(); store.setSession(" token ", user as any); expect(store.authToken).toBe("token");
  });

  it("covers unauthenticated, API failures and expired-session branches", async () => {
    const store = useAuthStore();
    authApi.fetchPersistedSession.mockResolvedValueOnce(result({ authenticated: false })); await store.bootstrap(); expect(store.authToken).toBe("");
    store.bootstrapped = false; authApi.fetchPersistedSession.mockRejectedValueOnce(new Error("bootstrap failed")); await store.bootstrap(); expect(store.authError).toContain("bootstrap failed");
    authApi.loginAccount.mockRejectedValueOnce(new Error("login failed")); expect(await store.login({} as any)).toBe(false);
    authApi.registerAccount.mockRejectedValueOnce(new Error("register failed")); expect(await store.register({} as any)).toBe(false);
    expect(await store.refreshUser()).toBeNull(); expect(await store.refreshSummary()).toBeNull(); expect(await store.updateProfile({} as any)).toBe(false); expect(await store.changePassword({} as any)).toBe(false);
    store.setSession("token", user as any);
    const expired = new AuthApiError("expired") as AuthApiError & { status?: number }; expired.status = 401;
    authApi.fetchCurrentAccount.mockRejectedValueOnce(expired); expect(await store.refreshUser()).toBeNull();
    store.setSession("token", user as any); authApi.fetchAccountSummary.mockRejectedValueOnce(expired); expect(await store.refreshSummary({ silentAuthFailure: true })).toBeNull();
    store.setSession("token", user as any); authApi.updateAccountProfile.mockRejectedValueOnce(expired); expect(await store.updateProfile({} as any)).toBe(false);
    store.setSession("token", user as any); authApi.changeAccountPassword.mockRejectedValueOnce(expired); expect(await store.changePassword({} as any)).toBe(false);
    store.setSession("token", user as any); authApi.logoutAccount.mockRejectedValueOnce(new Error("ignored")); await store.logout();
  });
});

describe("git store", () => {
  it("covers all success, guard, message and failure paths", async () => {
    const store = useGitStore(); expect(store.changedCount).toBe(0); expect(store.recentCommits).toEqual([]);
    await store.refreshSummary(); expect(store.changedCount).toBe(1); expect(store.recentCommits).toHaveLength(1);
    store.isLoading = true; await store.refreshSummary(); store.isLoading = false;
    await store.initializeRepository(); await store.commitAll("message"); await store.restoreToCommit("abc");
    workspaceApi.commitWorkspaceGitChanges.mockResolvedValueOnce(result({ created: false, summary: gitSummary })); await store.commitAll("none");
    workspaceApi.restoreWorkspaceGitCommit.mockResolvedValueOnce(result({ summary: gitSummary, restoredCommit: null, backupRef: "" })); await store.restoreToCommit("abc", false);
    for (const [method, mock] of [["refreshSummary", workspaceApi.fetchWorkspaceGitSummary], ["initializeRepository", workspaceApi.initializeWorkspaceGitRepository], ["commitAll", workspaceApi.commitWorkspaceGitChanges], ["restoreToCommit", workspaceApi.restoreWorkspaceGitCommit]] as const) {
      mock.mockRejectedValueOnce(new Error("git failed")); await (store as any)[method]("x"); expect(store.error).toContain("git failed");
    }
    store.reset(); expect(store.summary).toBeNull();
  });
});

describe("preset store", () => {
  it("covers list/load/edit/compile/save/patch/activation and errors", async () => {
    const store = usePresetStore(); await store.refreshList(); await store.loadActiveDocument(); await store.loadDocument("preset.md");
    store.markDirty({ ...store.document }); store.setRuntimeOverrides({ enabledModuleIds: ["a"], disabledModuleIds: [], temporaryRules: ["r"] });
    await store.compileCurrentPreset(); await store.riskCheckCurrentPreset(); await store.save(); await store.patchParams({ a: 1 }); await store.activate("preset.md"); await store.deactivate("preset.md");
    store.currentName = ""; await store.compileCurrentPreset(); await store.riskCheckCurrentPreset(); await store.save(); await store.patchParams({});
    const mocks = [presetApi.listPresets, presetApi.fetchActivePreset, presetApi.fetchPresetDocument, presetApi.compilePreset, presetApi.riskCheckPreset, presetApi.savePresetDocument, presetApi.patchPresetParams, presetApi.activatePreset, presetApi.deactivatePreset];
    const calls = [() => store.refreshList(), () => store.loadActiveDocument(), () => store.loadDocument("x"), () => { store.currentName = "x"; return store.compileCurrentPreset(); }, () => store.riskCheckCurrentPreset(), () => store.save(), () => store.patchParams({}), () => store.activate("x"), () => store.deactivate("x")];
    for (let i = 0; i < mocks.length; i++) { mocks[i].mockRejectedValueOnce(new Error("preset failed")); await calls[i](); }
    expect(store.errorMessage || store.compileError).toBeTruthy();
  });
});

describe("story and UI stores", () => {
  it("normalizes full story state and exercises ready/unready/error flows", async () => {
    const store = useStoryStore(); expect(store.hasData).toBe(false); await store.refreshAll(); expect(store.hasData).toBe(true); expect(store.focusChapter?.relativePath).toBe("chapters/one"); expect(store.fullState).toEqual({ hp: 1 });
    await store.refreshChapters(); await store.refreshCurrentState(); await store.refreshLatestSnapshot(); store.clearErrors();
    workspace.launchScreenVisible = true; await store.refreshAll(); expect(store.hasData).toBe(false);
    workspace.launchScreenVisible = false; workspace.currentProject = { workspaceRoot: "C:/story" };
    workspaceApi.fetchStoryChapters.mockRejectedValueOnce(new Error("chapters failed")); await store.refreshChapters();
    workspaceApi.fetchStoryCurrentState.mockRejectedValueOnce(new Error("state failed")); await store.refreshCurrentState();
    workspaceApi.fetchStoryLatestSnapshot.mockRejectedValueOnce(new Error("snapshot failed")); await store.refreshLatestSnapshot(); expect(store.storyError).toBeTruthy(); store.clear();
  });

  it("applies/clamps UI state, toggles controls and persists debounced settings", async () => {
    const store = useUiStore();
    store.applyPersistedState({
      theme: "invalid",
      activeActivity: "bad",
      sidebarWidth: 1,
      agentWidth: 9999,
      leftPaneFontScale: 1,
      rightPaneFontScale: 999,
      fileFontSize: 24
    } as any);
    expect(store.sidebarWidth).toBe(220); expect(store.agentWidth).toBe(760);
    expect(store.leftPaneFontScale).toBe(75); expect(store.centerPaneFontScale).toBe(150); expect(store.rightPaneFontScale).toBe(150);
    store.setTheme("white"); store.setActivity("search"); store.setActivity("bad"); store.setWorkbenchMode("storydex");
    store.setSidebarWidth(400); store.setSidebarCollapsed(true); store.toggleSidebarCollapsed(); store.setAgentCollapsed(true); store.toggleAgentCollapsed();
    store.setAgentWidth(500); store.setLeftPaneFontScale(90); store.setCenterPaneFontScale(115); store.setRightPaneFontScale(130); store.setSystemSettingsOpen(true);
    vi.advanceTimersByTime(200); await vi.runAllTimersAsync(); expect(systemApi.updateUiPreferences).toHaveBeenCalled(); await store.flushPersistedState();
    expect(systemApi.updateUiPreferences).toHaveBeenLastCalledWith(expect.objectContaining({
      leftPaneFontScale: 90,
      centerPaneFontScale: 115,
      rightPaneFontScale: 130
    }));
  });
});
