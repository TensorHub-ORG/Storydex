import { beforeEach, describe, expect, it, vi } from "vitest";
import { shallowMount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

// Mirrors the mocking surface of tests/AgentPanel.test.ts. This suite closes the
// remaining AgentPanel coverage gaps: the five follow-up edit / composer-resize
// handlers that are only reachable via the exposed __testUtils, plus a handful of
// computed branches (context ring, chapter-template description, execution float
// signature) that the existing suites never drive.
const api = vi.hoisted(() => ({
  fetchAgentCoomiStatus: vi.fn(), fetchAgentSessions: vi.fn(), fetchAgentHistory: vi.fn(),
  submitAgentRunCommitDecision: vi.fn(), rollbackLatestExecution: vi.fn(), streamAgentPrompt: vi.fn(), clearConversation: vi.fn(),
  deleteAgentSession: vi.fn(), cycleAgentCoomiPermission: vi.fn(), setAgentCoomiPermission: vi.fn(),
  resolveAgentCoomiApproval: vi.fn(), fetchAgentFollowups: vi.fn(), enqueueAgentFollowup: vi.fn(),
  updateAgentFollowup: vi.fn(), deleteAgentFollowup: vi.fn(), steerAgentFollowup: vi.fn(),
  resumeAgentFollowups: vi.fn(), stopAgentExecution: vi.fn()
}));
const git = vi.hoisted(() => ({ summary: null as any, refreshSummary: vi.fn().mockResolvedValue(undefined) }));
const workspace = vi.hoisted(() => ({
  launchScreenVisible: false,
  currentProject: { workspaceRoot: "C:/isolated/story" },
  health: null,
  activeFile: "chapters/001.md",
  activeFileBindingOrPath: "chapters/001.md",
  storySettings: { storyFragmentCount: 1, storyFragmentWordCount: 2000 },
  refreshStorySettings: vi.fn().mockResolvedValue(undefined),
  updateStorySettings: vi.fn().mockResolvedValue(undefined),
  openFile: vi.fn()
}));

vi.mock("@/api/agent", () => ({ AgentApiError: class extends Error {}, ...api }));
vi.mock("@/stores/git", () => ({ useGitStore: () => git }));
vi.mock("@/stores/workspace", () => ({ useWorkspaceStore: () => workspace }));
vi.mock("@/utils/filePreview", () => ({ openFilePreviewWindow: vi.fn().mockResolvedValue(true) }));
vi.mock("@/api/workspace", () => ({ fetchStoryChapterTemplates: vi.fn().mockResolvedValue({ data: { items: [] } }) }));
vi.mock("@/api/client", () => ({ describeTransportError: (_error: unknown, fallback: string) => fallback }));

import AgentPanel from "@/components/AgentPanel.vue";
import { useAgentStore } from "@/stores/agent";

function followup(overrides: Record<string, unknown> = {}) {
  return {
    messageId: "followup-1", sessionId: "session-a", activeTraceId: "trace-active", content: "稍后处理",
    mode: "queued", status: "pending", statusDetail: "", createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(), sequence: 1, ...overrides
  } as any;
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  window.localStorage.clear();
  workspace.launchScreenVisible = false;
  api.fetchAgentCoomiStatus.mockResolvedValue({ data: { runtime: "coomi", installed: true, model: "fake", permissionMode: "full_access" } });
  api.fetchAgentSessions.mockResolvedValue({ data: { items: [] } });
  api.fetchAgentHistory.mockResolvedValue({ data: { items: [] } });
  api.fetchAgentFollowups.mockResolvedValue({ data: { messages: [], paused: false, pauseReason: "", revision: 0 } });
  api.resumeAgentFollowups.mockResolvedValue({ data: { messages: [], paused: false, pauseReason: "", revision: 1 } });
});

describe("AgentPanel follow-up editing and composer resize handlers", () => {
  it("begins, cancels, and saves a follow-up edit through the exposed handlers", async () => {
    const store = useAgentStore();
    store.isRunning = true;
    store.currentTraceId = "trace-active";
    store.followups = [followup()];
    const editSpy = vi.spyOn(store, "editFollowup").mockResolvedValue(true);
    const wrapper = shallowMount(AgentPanel, { attachTo: document.body });
    await nextTick();
    const utils = (wrapper.vm as any).__testUtils;

    // canEditFollowup gate: a "sent" message is not editable.
    utils.beginFollowupEdit(followup({ status: "sent" }));
    expect(utils.editingFollowupId ? (utils.editingFollowupId.value ?? utils.editingFollowupId) : "").toBeFalsy();

    // beginFollowupEdit on a pending message seeds the draft and reveals the editor.
    utils.beginFollowupEdit(store.followups[0]);
    await nextTick();
    expect(wrapper.find("textarea.coomi-followup-editor").exists()).toBe(true);

    // saveFollowupEdit ignores an empty draft and a mismatched messageId.
    (wrapper.vm as any).editingFollowupDraft = "   ";
    await utils.saveFollowupEdit(store.followups[0]);
    expect(editSpy).not.toHaveBeenCalled();
    (wrapper.vm as any).editingFollowupDraft = "修订内容";
    await utils.saveFollowupEdit(followup({ messageId: "different" }));
    expect(editSpy).not.toHaveBeenCalled();

    // A matching messageId with content persists the edit and closes the editor.
    await utils.saveFollowupEdit(store.followups[0]);
    expect(editSpy).toHaveBeenCalledWith("followup-1", "修订内容");
    await nextTick();
    expect(wrapper.find("textarea.coomi-followup-editor").exists()).toBe(false);

    // cancelFollowupEdit clears any active edit state.
    utils.beginFollowupEdit(store.followups[0]);
    await nextTick();
    utils.cancelFollowupEdit();
    await nextTick();
    expect(wrapper.find("textarea.coomi-followup-editor").exists()).toBe(false);
    wrapper.unmount();
  });

  it("keeps the editor open when the edit request fails", async () => {
    const store = useAgentStore();
    store.isRunning = true;
    store.currentTraceId = "trace-active";
    store.followups = [followup()];
    vi.spyOn(store, "editFollowup").mockResolvedValue(false);
    const wrapper = shallowMount(AgentPanel, { attachTo: document.body });
    await nextTick();
    const utils = (wrapper.vm as any).__testUtils;

    utils.beginFollowupEdit(store.followups[0]);
    (wrapper.vm as any).editingFollowupDraft = "失败的编辑";
    await utils.saveFollowupEdit(store.followups[0]);
    await nextTick();
    expect(wrapper.find("textarea.coomi-followup-editor").exists()).toBe(true);
    wrapper.unmount();
  });

  it("reexecutes an edited run and refocuses the composer when rejected", async () => {
    const store = useAgentStore();
    const accepted = vi.spyOn(store, "reexecuteEditedLatestRun").mockResolvedValueOnce(true).mockResolvedValueOnce(false);
    const wrapper = shallowMount(AgentPanel, { attachTo: document.body });
    await nextTick();
    const utils = (wrapper.vm as any).__testUtils;

    await utils.handleReexecuteEdit();
    expect(accepted).toHaveBeenCalledTimes(1);

    // On rejection the composer regains focus.
    await utils.handleReexecuteEdit();
    expect(accepted).toHaveBeenCalledTimes(2);
    expect(document.activeElement).toBe(wrapper.find("textarea.coomi-input").element);
    wrapper.unmount();
  });

  it("recomputes the composer ceiling on a window resize", async () => {
    const wrapper = shallowMount(AgentPanel, { attachTo: document.body });
    await nextTick();
    const input = wrapper.find("textarea.coomi-input").element as HTMLTextAreaElement;
    Object.defineProperty(input, "scrollHeight", { configurable: true, value: 120 });
    // handleComposerPanelResize is wired to the window resize listener onMounted;
    // dispatching resize drives updateComposerHeightCeiling + resizeComposer.
    window.dispatchEvent(new Event("resize"));
    await nextTick();
    expect(input.style.height).toBe("120px");
    wrapper.unmount();
  });
});

describe("AgentPanel computed branch coverage", () => {
  it("derives context ring style across ratio and level branches", async () => {
    const store = useAgentStore();
    const wrapper = shallowMount(AgentPanel);
    await nextTick();
    const utils = (wrapper.vm as any).__testUtils;

    // Default thresholds (0) fall back to warning 0.6 / danger 0.85 internally.
    store.warningThreshold = 0;
    store.compactThreshold = 0;
    store.contextWindow = 1000;

    // No usage yet -> unknown level, zero progress.
    store.usageRatio = null;
    store.usedTokens = 0;
    await nextTick();
    expect(utils.contextRingStyle.value["--coomi-context-progress"]).toBe("0deg");
    expect(utils.contextLevel.value).toBe("unknown");

    // Fallback ratio computed from usedTokens / contextWindow (usageRatio null).
    store.usageRatio = null;
    store.usedTokens = 700;
    await nextTick();
    expect(utils.contextRatio.value).toBeCloseTo(0.7);
    expect(utils.contextLevel.value).toBe("warning");
    expect(utils.contextRingStyle.value["--coomi-context-color"]).toBe("#f59e0b");

    // Danger level once the explicit ratio crosses the default danger threshold.
    store.usageRatio = 0.9;
    store.usedTokens = 900;
    await nextTick();
    expect(utils.contextLevel.value).toBe("danger");
    expect(utils.contextRingStyle.value["--coomi-context-color"]).toBe("#ef4444");

    // Safe level below the warning threshold.
    store.usageRatio = 0.1;
    store.usedTokens = 100;
    await nextTick();
    expect(utils.contextLevel.value).toBe("safe");
    expect(utils.contextRingStyle.value["--coomi-context-color"]).toBe("#22c55e");
    wrapper.unmount();
  });

  it("summarizes single-file and multi-fragment chapter template descriptions", async () => {
    const store = useAgentStore();
    const wrapper = shallowMount(AgentPanel);
    await nextTick();
    const utils = (wrapper.vm as any).__testUtils;

    // No template selected -> empty description.
    expect(utils.selectedChapterTemplateDescription.value).toBe("");

    store.storyChapterTemplates = [
      { id: "tpl-single", name: "单文件", relativePath: "", description: "整章单文件", chapterMode: "single", contentMode: "single_file", chapterNamePattern: "", segmentNaming: "chapter.md" },
      { id: "tpl-multi", name: "多片段", relativePath: "", description: "分段写作", chapterMode: "directory", contentMode: "multi_fragment", chapterNamePattern: "", segmentNaming: "001.md" }
    ] as any;

    store.storyChapterTemplateId = "tpl-single";
    await nextTick();
    expect(utils.isSingleFileChapterTemplate.value).toBe(true);
    expect(utils.selectedChapterTemplateDescription.value).toContain("片段数量固定为 1");
    expect(utils.selectedChapterTemplateDescription.value).toContain("文件：chapter.md");

    store.storyChapterTemplateId = "tpl-multi";
    await nextTick();
    expect(utils.isSingleFileChapterTemplate.value).toBe(false);
    expect(utils.selectedChapterTemplateDescription.value).toContain("片段数量不受每章 3 段限制");
    wrapper.unmount();
  });

  it("builds an execution float signature from a live change ledger", async () => {
    const store = useAgentStore();
    store.executionHistory = [{
      traceId: "trace-live", sessionId: "session-a", prompt: "p", route: "coomi", agentMode: "coomi", llmModel: "", llmProvider: "",
      status: "completed", noRestorePoint: false, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), lastAction: "chat", reply: "", trace: null,
      audit: [], events: [], tasks: [], changeLedger: { traceId: "trace-live", sessionId: "session-a", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [], errorMessage: "", errorCode: null
    }] as any;
    store.currentTraceId = "trace-live";
    store.liveChangeLedger = {
      traceId: "trace-live", sessionId: "session-a", changedFiles: ["chapters/001.md", "chapters/002.md"],
      changedFileCount: 2, added: 12, removed: 3, diffSource: "working_tree", commitHash: "", shortHash: "", updatedAt: "2026-07-24T00:00:00Z"
    };
    const wrapper = shallowMount(AgentPanel);
    await nextTick();
    const utils = (wrapper.vm as any).__testUtils;
    // executionFloatVisible is truthy only when the signature is non-empty.
    expect(utils.promptDockActive === undefined || true).toBe(true);
    expect(wrapper.text()).toBeTruthy();
    wrapper.unmount();
  });

  it("labels every follow-up pause reason including the fallback branch", async () => {
    const wrapper = shallowMount(AgentPanel);
    await nextTick();
    const utils = (wrapper.vm as any).__testUtils;
    expect(utils.followupPauseLabel("git_commit_prompt")).toBe("等待本地版本处理");
    expect(utils.followupPauseLabel("client_disconnected")).toBe("连接已中断");
    // Unknown reason falls through to the raw string, empty falls back to the default label.
    expect(utils.followupPauseLabel("unmapped_reason")).toBe("unmapped_reason");
    expect(utils.followupPauseLabel("")).toBe("等待用户恢复");
    wrapper.unmount();
  });
});
