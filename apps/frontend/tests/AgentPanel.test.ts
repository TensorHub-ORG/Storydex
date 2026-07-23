import { beforeEach, describe, expect, it, vi } from "vitest";
import { shallowMount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick, unref } from "vue";

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
vi.mock("@/api/workspace", () => ({ fetchStoryChapterTemplates: vi.fn().mockResolvedValue({ data: { items: [] } }) }));
vi.mock("@/api/client", () => ({ describeTransportError: (_error: unknown, fallback: string) => fallback }));

import AgentPanel from "@/components/AgentPanel.vue";
import { useAgentStore } from "@/stores/agent";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  window.localStorage.clear();
  api.fetchAgentCoomiStatus.mockResolvedValue({ data: { runtime: "coomi", installed: true, model: "fake", permissionMode: "full_access" } });
  api.fetchAgentSessions.mockResolvedValue({ data: { items: [] } });
  api.fetchAgentHistory.mockResolvedValue({ data: { items: [] } });
  api.cycleAgentCoomiPermission.mockResolvedValue({ data: { permissionMode: "ask_approval", permissionLabel: "Ask" } });
  api.setAgentCoomiPermission.mockResolvedValue({ data: { permissionMode: "ask_approval", permissionLabel: "Ask" } });
  api.resolveAgentCoomiApproval.mockResolvedValue({ data: { resolved: true } });
  api.clearConversation.mockResolvedValue({ data: { cleared: true } });
  api.deleteAgentSession.mockResolvedValue({ data: { deleted: true } });
  api.rollbackLatestExecution.mockResolvedValue({ data: { rolledBack: false, sessionId: "default", removedTraceId: "", prompt: "" } });
  api.fetchAgentFollowups.mockResolvedValue({ data: { messages: [], paused: false, pauseReason: "", revision: 0 } });
  api.resumeAgentFollowups.mockResolvedValue({ data: { messages: [], paused: false, pauseReason: "", revision: 1 } });
  api.stopAgentExecution.mockResolvedValue({ data: { accepted: true, activeTraceId: "trace", mailboxPaused: true, pauseReason: "manual_stop" } });
});

describe("AgentPanel", () => {
  it("enters cancellable edit mode without rolling back and keeps delete semantics separate", async () => {
    const store = useAgentStore();
    const previous = {
      traceId: "trace-previous", sessionId: "session-a", prompt: "previous", route: "coomi", agentMode: "coomi",
      llmModel: "", llmProvider: "", status: "completed", noRestorePoint: false, createdAt: "2026-07-21T10:00:00Z",
      updatedAt: "2026-07-21T10:00:00Z", lastAction: "chat", reply: "previous reply", trace: null, audit: [], events: [],
      tasks: [], changeLedger: { traceId: "trace-previous", sessionId: "session-a", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [], errorMessage: "", errorCode: null
    } as const;
    const latest = {
      ...previous,
      traceId: "trace-latest",
      prompt: "latest",
      createdAt: "2026-07-21T11:00:00Z",
      updatedAt: "2026-07-21T11:00:00Z"
    };
    store.executionHistory = [latest as any, previous as any];
    store.promptInput = "unsent draft";
    const rollback = vi.spyOn(store, "rollbackLatestRun").mockResolvedValue(true);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const wrapper = shallowMount(AgentPanel, { attachTo: document.body });
    await nextTick();

    expect(wrapper.findAll(".coomi-run-actions")).toHaveLength(1);
    const buttons = wrapper.findAll(".coomi-run-action");
    expect(buttons).toHaveLength(2);
    expect(buttons[0].attributes("aria-label")).toBe("编辑最新消息");
    expect(buttons[1].attributes("aria-label")).toBe("删除本轮");

    await buttons[0].trigger("click");
    expect(rollback).not.toHaveBeenCalled();
    expect(api.rollbackLatestExecution).not.toHaveBeenCalled();
    expect(store.editingTraceId).toBe("trace-latest");
    expect(store.promptInput).toBe("latest");
    expect(wrapper.find(".coomi-edit-session").text()).toContain("取消编辑");
    expect(document.activeElement).toBe(wrapper.find("textarea.coomi-input").element);

    await wrapper.find(".coomi-secondary-action").trigger("click");
    expect(store.promptInput).toBe("unsent draft");
    expect(store.executionHistory.map((run) => run.traceId)).toEqual(["trace-latest", "trace-previous"]);

    const refreshedButtons = wrapper.findAll(".coomi-run-action");
    await refreshedButtons[1].trigger("click");
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("不会回滚已产生的文件变更"));
    expect(rollback).toHaveBeenCalledWith({ refillComposer: false });

    store.isRunning = true;
    await nextTick();
    expect(wrapper.find(".coomi-run-actions").exists()).toBe(false);
    wrapper.unmount();
  });

  it("keeps queue and stop actions in the composer while steering an existing queued message from the mailbox", async () => {
    const store = useAgentStore();
    store.currentSessionId = "session-a";
    store.currentTraceId = "trace-active";
    store.isRunning = true;
    store.promptInput = "追加说明";
    store.followupPaused = true;
    store.followupPauseReason = "manual_stop";
    store.followups = [{
      messageId: "followup-1", sessionId: "session-a", activeTraceId: "trace-active", content: "稍后处理",
      mode: "queued", status: "pending", statusDetail: "等待当前轮完成", createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(), sequence: 1
    }];
    api.fetchAgentFollowups.mockResolvedValueOnce({
      data: { messages: store.followups, paused: true, pauseReason: "manual_stop", revision: 1 }
    });
    const enqueue = vi.spyOn(store, "enqueueFollowup").mockResolvedValue(true);
    const steer = vi.spyOn(store, "steerFollowup").mockResolvedValue(true);
    const stop = vi.spyOn(store, "stopActiveRun").mockResolvedValue(undefined);
    const remove = vi.spyOn(store, "deleteFollowup").mockResolvedValue(true);
    const resume = vi.spyOn(store, "resumeFollowups").mockResolvedValue(undefined);
    vi.spyOn(window, "confirm").mockReturnValue(true);

    const wrapper = shallowMount(AgentPanel);
    await nextTick();
    expect(wrapper.find("textarea.coomi-input").attributes("disabled")).toBeUndefined();
    expect(wrapper.find(".coomi-steer-send").exists()).toBe(false);
    expect(wrapper.find(".coomi-stop-run").exists()).toBe(true);
    expect(wrapper.find(".coomi-stop-run .coomi-stop-glyph").exists()).toBe(true);
    expect(wrapper.find(".coomi-stop-run .material-symbols-rounded").exists()).toBe(false);

    await wrapper.find(".coomi-send").trigger("click");
    expect(enqueue).toHaveBeenCalledWith("queued");
    await wrapper.find(".coomi-stop-run").trigger("click");
    expect(stop).toHaveBeenCalledTimes(1);

    expect(wrapper.find(".coomi-followup-mailbox").text()).toContain("队列已暂停");
    const steerAction = wrapper.find(".coomi-followup-actions .steer");
    expect(steerAction.text()).toBe("立即引导执行");
    await steerAction.trigger("click");
    expect(steer).toHaveBeenCalledWith("followup-1");
    expect(enqueue).toHaveBeenCalledTimes(1);
    await wrapper.find(".coomi-followup-actions .danger").trigger("click");
    expect(remove).toHaveBeenCalledWith("followup-1");
    await wrapper.find(".coomi-followup-resume").trigger("click");
    expect(resume).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });

  it("grows the composer automatically without exposing a manual resize control", async () => {
    const wrapper = shallowMount(AgentPanel, { attachTo: document.body });
    const utils = (wrapper.vm as any).__testUtils;
    const input = wrapper.find("textarea.coomi-input").element as HTMLTextAreaElement;

    expect(wrapper.find(".coomi-composer-resizer").exists()).toBe(false);
    expect(input.style.maxHeight).toBe("180px");
    Object.defineProperty(input, "scrollHeight", { configurable: true, value: 96 });
    utils.resizeComposer();
    expect(input.style.height).toBe("96px");

    Object.defineProperty(input, "scrollHeight", { configurable: true, value: 600 });
    utils.resizeComposer();
    expect(input.style.height).toBe("180px");
    expect(window.localStorage.getItem("storydex.composerMaxHeight")).toBeNull();
    wrapper.unmount();
  });

  it("shows snapshot confirmation controls and a persistent no-restore-point marker", async () => {
    const store = useAgentStore();
    store.pendingSnapshotConfirmation = {
      request: { prompt: "continue" },
      traceId: "trace-rejected",
      sessionId: "session-1",
      message: "snapshot failed",
      details: {}
    };
    store.executionHistory = [{
      traceId: "trace-risk", sessionId: "session-1", prompt: "continue", route: "coomi", agentMode: "coomi", llmModel: "", llmProvider: "",
      status: "completed", noRestorePoint: true, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), lastAction: "chat", reply: "", trace: null,
      audit: [], events: [], tasks: [], changeLedger: { traceId: "trace-risk", sessionId: "session-1", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [], errorMessage: "", errorCode: null
    }];
    const confirm = vi.spyOn(store, "confirmNoSnapshot").mockResolvedValue(undefined);
    const cancel = vi.spyOn(store, "cancelNoSnapshot");
    const wrapper = shallowMount(AgentPanel);
    await nextTick();

    expect(wrapper.find('[role="dialog"]').text()).toContain("无法创建恢复点");
    expect(wrapper.find(".coomi-no-restore-point").text()).toContain("无恢复点");
    const buttons = wrapper.findAll(".coomi-snapshot-modal-button");
    expect(buttons).toHaveLength(2);
    await buttons[1].trigger("click");
    expect(confirm).toHaveBeenCalledTimes(1);
    await buttons[0].trigger("click");
    expect(cancel).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });

  it("renders phase timing without exposing hidden reasoning text", async () => {
    const store = useAgentStore();
    store.executionHistory = [{
      traceId: "trace-1", sessionId: "session-1", prompt: "hello", route: "coomi", agentMode: "coomi", llmModel: "fake", llmProvider: "fake",
      status: "running", createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), lastAction: "chat", reply: "", trace: null,
      audit: [], events: [], tasks: [], changeLedger: { traceId: "trace-1", sessionId: "session-1", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [{ id: "phase-1", type: "phase", status: "running", title: "意图识别", content: "意图识别 · 0.5s", timestamp: new Date().toISOString(), raw: {} }],
      errorMessage: "", errorCode: null
    }];
    const wrapper = shallowMount(AgentPanel);
    await nextTick();
    expect(wrapper.find(".coomi-phase-text").text()).toContain("0.5s");
    expect(wrapper.text()).not.toContain("chain-of-thought-secret");
    wrapper.unmount();
  });

  it("pauses stream following after user scroll and formats run duration", async () => {
    const store = useAgentStore();
    const wrapper = shallowMount(AgentPanel);
    const utils = (wrapper.vm as any).__testUtils;
    const stream = wrapper.find(".coomi-stream").element as HTMLElement;
    let scrollTop = 120;
    Object.defineProperty(stream, "scrollTop", { configurable: true, get: () => scrollTop, set: (value) => { scrollTop = Number(value); } });
    Object.defineProperty(stream, "scrollHeight", { configurable: true, value: 1000 });
    Object.defineProperty(stream, "clientHeight", { configurable: true, value: 300 });

    utils.handleStreamScroll();
    await nextTick();
    expect(wrapper.find(".coomi-scroll-latest").exists()).toBe(true);

    store.executionHistory = [{
      traceId: "scroll-run", sessionId: "session", prompt: "p", route: "coomi", agentMode: "coomi", llmModel: "", llmProvider: "",
      status: "running", createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), lastAction: "chat", reply: "new output", trace: null,
      audit: [], events: [], tasks: [], changeLedger: { traceId: "scroll-run", sessionId: "session", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" }, items: [], errorMessage: "", errorCode: null
    }];
    await nextTick();
    expect(scrollTop).toBe(120);

    utils.scrollToBottom(true);
    await nextTick();
    expect(scrollTop).toBe(1000);
    expect(wrapper.find(".coomi-scroll-latest").exists()).toBe(false);
    expect(utils.formatRunDuration(56_000)).toBe("56s");
    expect(utils.formatRunDuration(76_000)).toBe("1m16s");
    expect(utils.formatRunDuration(3_676_000)).toBe("1h1m16s");
    wrapper.unmount();
  });

  it("does not duplicate an AgentError in the composer footer", async () => {
    const store = useAgentStore();
    store.currentTraceId = "failed-run";
    store.lastError = "provider failed";
    store.executionHistory = [{
      traceId: "failed-run", sessionId: "session", prompt: "p", route: "coomi", agentMode: "coomi", llmModel: "", llmProvider: "",
      status: "failed", createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), lastAction: "chat", reply: "", trace: null,
      audit: [], events: [], tasks: [], changeLedger: { traceId: "failed-run", sessionId: "session", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [{ id: "error", type: "error", status: "error", title: "Error", content: "provider failed", timestamp: new Date().toISOString(), raw: {} }],
      errorMessage: "provider failed", errorCode: "provider"
    }];
    const wrapper = shallowMount(AgentPanel);
    await nextTick();
    expect(wrapper.findAll(".coomi-error-text")).toHaveLength(1);
    expect(wrapper.find("footer .coomi-error").exists()).toBe(false);
    wrapper.unmount();
  });

  it("collapses the Git prompt on the same click while the decision is pending", async () => {
    const pending = deferred<unknown>();
    api.submitAgentRunCommitDecision.mockReturnValue(pending.promise);
    const store = useAgentStore();
    store.pendingCommitPrompt = {
      traceId: "trace-1", sessionId: "session-1", workspaceRoot: "C:/isolated/story", message: "commit?",
      changedFiles: ["chapters/001.md"], changedFileCount: 1, added: 3, removed: 1
    };
    const wrapper = shallowMount(AgentPanel);
    await nextTick();
    expect(wrapper.find(".coomi-commit-menu").exists()).toBe(true);
    const options = wrapper.findAll(".coomi-command-option");
    await options[0].trigger("click");
    await nextTick();
    expect(wrapper.find(".coomi-commit-menu").exists()).toBe(false);
    expect(store.isCommittingGit).toBe(true);
    pending.resolve({ data: { created: false, reason: "done", changedFiles: [] } });
    await nextTick();
    wrapper.unmount();
  });

  it("supports manual input and skip through click-compatible buttons", async () => {
    api.submitAgentRunCommitDecision.mockResolvedValue({ data: { created: false, reason: "skipped", changedFiles: [] } });
    const store = useAgentStore();
    store.pendingCommitPrompt = {
      traceId: "trace-1", sessionId: "session-1", workspaceRoot: "C:/isolated/story", message: "commit?",
      changedFiles: [], changedFileCount: 0, added: 0, removed: 0
    };
    const wrapper = shallowMount(AgentPanel);
    await nextTick();
    const options = wrapper.findAll(".coomi-command-option");
    await options[1].trigger("click");
    expect(wrapper.find("textarea.coomi-approval-input").exists()).toBe(true);
    await wrapper.find("textarea.coomi-approval-input").setValue("test: local commit");
    expect(wrapper.find(".coomi-approval-action.primary").attributes("disabled")).toBeUndefined();
    await options[2].trigger("click");
    expect(api.submitAgentRunCommitDecision).toHaveBeenCalled();
    wrapper.unmount();
  });

  it("covers deterministic display, folding, formatting and operation helpers", async () => {
    const wrapper = shallowMount(AgentPanel);
    const utils = (wrapper.vm as any).__testUtils;
    expect(utils).toBeTruthy();
    expect(utils.buildPendingTargetPathOperationItems(null)).toEqual([]);
    const targetItems = utils.buildPendingTargetPathOperationItems({ targetPaths: ["chapters/a.md", ""] });
    expect(targetItems).toHaveLength(1);
    const previewItems = [{ operationId: "op", targetPath: "a", usesWholePendingWrite: false }];
    expect(utils.buildLiveOperationItemsForPending({ writePreview: { items: previewItems } })).toBe(previewItems);
    expect(utils.buildLiveOperationItemsForPending({ targetPaths: ["a.md"] })).toHaveLength(1);
    expect(utils.attachPendingWriteContext(previewItems, {})).toBe(previewItems);
    expect(utils.shouldApplyWholePendingWrite(targetItems[0])).toBe(true);
    await utils.handleApproveOperation(targetItems[0]);
    await utils.handleApproveOperation(previewItems[0]);
    await utils.handleRejectOperation(targetItems[0]);
    await utils.handleRejectOperation(previewItems[0]);

    const tool = (id: string, status = "success", extra: Record<string, unknown> = {}) => ({
      id, type: "tool", status, title: "Tool", content: "", timestamp: new Date().toISOString(), raw: {}, ...extra
    });
    const reasoning = { id: "reason", type: "reasoning", status: "running", title: "Reasoning", content: "line1\nline2", timestamp: new Date().toISOString(), raw: {} };
    const displayRun = {
      traceId: "display", status: "running", items: [tool("one", "running", { toolName: "read_file", arguments: { path: "chapters/a.md" } }), tool("two", "success", { resultPreview: "done" }), reasoning]
    } as any;
    const entries = utils.displayEntries(displayRun);
    expect(entries).toHaveLength(2);
    const group = entries[0];
    expect(utils.toolGroupStatus(group.tools, false)).toBe("running");
    expect(utils.toolGroupStatus([tool("e", "error")], true)).toBe("error");
    expect(utils.toolGroupStatus([tool("s")], true)).toBe("success");
    expect(utils.toolGroupDefaultOpen(group)).toBe(false);
    expect(utils.isToolGroupOpen(group)).toBe(false);
    const runningGroup = { ...group, status: "running" };
    expect(utils.toolGroupDefaultOpen(runningGroup)).toBe(true);
    expect(utils.isToolGroupOpen(runningGroup)).toBe(true);
    utils.toggleFold(runningGroup.id, true); expect(utils.isToolGroupOpen(runningGroup)).toBe(false);
    const chunkGroup = { ...runningGroup, terminal: false, tools: Array.from({ length: 7 }, (_, index) => tool(`t${index}`, index === 0 ? "running" : "success")) };
    const chunks = utils.toolChunks(chunkGroup);
    expect(chunks).toHaveLength(2);
    expect(utils.toolChunkDefaultOpen(runningGroup, chunks[0])).toBe(true);
    expect(utils.isToolChunkOpen(runningGroup, chunks[0])).toBe(true);
    utils.toggleToolChunk(chunks[0].id, true); expect(utils.isToolChunkOpen(group, chunks[0])).toBe(false);
    expect(utils.reasoningTitle(displayRun, reasoning)).toContain("2 lines");
    expect(utils.isActiveReasoning(displayRun, reasoning)).toBe(true);
    expect(utils.isActiveReasoning({ ...displayRun, status: "completed" }, reasoning)).toBe(false);
    expect(utils.toolGroupTitle({ ...group, tools: [tool("one")] })).toBeTruthy();
    expect(utils.toolGroupTitle(group)).toContain("2");
    expect(utils.toolChunkTitle(chunks[0])).toBeTruthy();
    expect(utils.toolSummary(tool("p", "success", { toolName: "write_file", arguments: { path: "a.md" } }))).toContain("a.md");
    expect(utils.toolSummary(tool("p", "success", { toolName: "write_file", resultPreview: "preview" }))).toContain("preview");
    for (const status of ["success", "running", "error", "pending", "warning"]) expect(utils.toolStatusLabel(status)).toBeTruthy();
    expect(utils.compactToolDetail(tool("x"))).toBe("");
    const rowId = utils.toolRowId(group, group.tools[0]);
    expect(utils.isToolRowOpen(group, group.tools[0])).toBe(false);
    utils.toggleToolRow(rowId); expect(utils.isToolRowOpen(group, group.tools[0])).toBe(true);
    for (const type of ["user", "assistant", "reasoning", "tool", "usage", "compression", "phase", "system", "error"]) expect(utils.formatItemType(type)).toBeTruthy();
    for (const status of ["running", "completed", "cancelled", "stopped", "failed", "custom", ""]) expect(utils.formatStatus(status, "")).toBeTruthy();
    expect(utils.formatStatus("running", "error")).toBeTruthy();
    expect(utils.formatDate("bad")).toBe("bad");
    expect(utils.formatDate(new Date().toISOString(), true)).toBeTruthy();
    expect(utils.compactJson({ a: 1 })).toContain("a");
    const circular: any = {}; circular.self = circular;
    expect(utils.compactJson(circular)).toBeTruthy();
    expect(utils.renderMarkdown("[link](chapters/a.md)")).toContain("href");
    expect(utils.compactText("x".repeat(20), 5)).toContain("x");
    expect(utils.firstStringFromRecord(null, ["a"])).toBe("");
    expect(utils.firstStringFromRecord({ a: " ", b: " value " }, ["a", "b"])).toBe("value");
    for (const value of [Number.NaN, 0, 1000, 1_000_000]) expect(utils.formatTokenCount(value)).toBeTruthy();
    wrapper.unmount();
  });

  it("covers composer, session, permission, options and approval handlers", async () => {
    const store = useAgentStore();
    const wrapper = shallowMount(AgentPanel);
    const utils = (wrapper.vm as any).__testUtils;
    store.promptInput = "/";
    utils.handleComposerInput();
    utils.selectCommand(99);
    utils.selectCommand(0);
    await nextTick();
    expect(store.promptInput.startsWith("/")).toBe(true);
    utils.insertCommand("/compact "); expect(store.promptInput).toBe("/compact ");
    utils.togglePermissionMenu(); utils.togglePermissionMenu();
    utils.toggleReasoningMenu(); utils.toggleReasoningMenu();
    utils.toggleStoryOptions(); utils.toggleStoryOptions();
    await utils.handleCyclePermission(); expect(api.cycleAgentCoomiPermission).toHaveBeenCalled();
    store.coomiStatus = { runtime: "coomi", installed: true, permissionMode: "full_access", planMode: false } as any;
    expect(utils.isPermissionOptionActive("full_access")).toBe(true);
    expect(utils.permissionToneClass("full_access")).toBeTruthy();
    await utils.selectPermissionOption("ask_approval"); expect(api.setAgentCoomiPermission).toHaveBeenCalled();
    utils.selectReasoningOption("high");
    store.promptInput = "";
    api.streamAgentPrompt.mockImplementationOnce(async (_request: unknown, onPacket: (packet: any) => void) => onPacket({ _type: "AgentCompleted" }));
    await utils.runCoomiCommand("/compact "); expect(store.promptInput).toBe("");
    store.isRunning = true; utils.handleNewSession();
    store.isRunning = false; utils.handleNewSession(); expect(store.currentSessionId).toBeTruthy();
    await utils.handleSessionSelect("");
    await utils.handleSessionSelect("session-other");
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await utils.handleSessionDelete("session-other"); expect(api.deleteAgentSession).toHaveBeenCalled();
    await utils.handleConfigSaved(); expect(api.fetchAgentCoomiStatus).toHaveBeenCalled();

    utils.updateStoryFragmentCount({ target: { value: "3" } } as any);
    utils.updateStoryFragmentWordCount({ target: { value: "1500" } } as any);
    utils.updateStoryChapterTemplate({ target: { value: "custom" } } as any);
    expect(store.storyFragmentCount).toBe(3); expect(store.storyFragmentWordCount).toBe(1500);
    workspace.storySettings = { storyFragmentCount: 4, storyFragmentWordCount: 1600 } as any;
    utils.syncStoryGenerationOptionsFromProjectSettings(); expect(store.storyFragmentCount).toBe(4);
    await utils.persistStoryGenerationOptions({ fragmentCount: 5 }); expect(workspace.updateStorySettings).toHaveBeenCalled();

    store.pendingApprovals = [{ approvalId: "a", kind: "question", allowText: true, options: [{ label: "Answer", value: "answer" }] }] as any;
    await nextTick();
    expect(utils.isApprovalDraftComplete(store.pendingApprovals[0], undefined)).toBe(false);
    utils.updateApprovalDraftText("yes");
    utils.selectApprovalOption("answer");
    utils.goToApproval(-1); utils.goToApproval(0);
    await utils.handleApprovalConfirm(); expect(api.resolveAgentCoomiApproval).toHaveBeenCalled();
    store.pendingApprovals = [{ approvalId: "b", kind: "permission", allowText: false, options: [{ label: "Allow", value: "allow", isRecommended: true }] }] as any;
    await nextTick();
    expect(utils.approvalOptionLabel("Allow", "allow")).toBeTruthy();
    expect(utils.approvalOptionLabel("Deny", "deny")).toBeTruthy();
    expect(utils.approvalOptionLabel("Other", "other")).toBe("Other");
    expect(utils.approvalOptionDescription("allow")).toBeTruthy(); expect(utils.approvalOptionDescription("deny")).toBeTruthy();
    await utils.handleApprovalCancel();
    utils.collapseExecutionFloat(); utils.expandExecutionFloat(); utils.collapsePromptDock(); utils.expandPromptDock();
    wrapper.unmount();
  });

  it("covers submit/stop, keyboard, commit failures and Markdown link handling", async () => {
    const store = useAgentStore();
    const wrapper = shallowMount(AgentPanel, { attachTo: document.body });
    const utils = (wrapper.vm as any).__testUtils;
    store.promptInput = "hello";
    api.streamAgentPrompt.mockImplementation(async (_request: unknown, onPacket: (packet: any) => void) => onPacket({ _type: "AgentCompleted" }));
    await utils.handleSubmitOrStop(); expect(api.streamAgentPrompt).toHaveBeenCalled();
    store.isRunning = true; utils.handleSubmitOrStop(); store.isRunning = false;
    store.pendingCommitPrompt = { traceId: "t", sessionId: "s", workspaceRoot: "root", message: "commit", changedFiles: [], changedFileCount: 0, added: 0, removed: 0 };
    await utils.handleSubmitOrStop();
    const keyboard = (key: string, extra: Record<string, unknown> = {}) => ({ key, preventDefault: vi.fn(), ...extra } as any);
    utils.handleComposerKeydown(keyboard("ArrowDown")); utils.handleComposerKeydown(keyboard("ArrowUp"));
    utils.handleComposerKeydown(keyboard("Escape")); utils.handleComposerKeydown(keyboard("Enter"));
    store.pendingCommitPrompt = null;
    store.pendingApprovals = [{ approvalId: "a", kind: "permission", options: [{ label: "Allow", value: "allow" }] }] as any;
    await nextTick(); utils.handleComposerKeydown(keyboard("Escape"));
    store.pendingApprovals = [];
    utils.handleComposerKeydown(keyboard("Enter", { shiftKey: true, isComposing: false }));
    utils.handleComposerKeydown(keyboard("Enter", { shiftKey: false, isComposing: true }));

    store.pendingCommitPrompt = { traceId: "t", sessionId: "s", workspaceRoot: "root", message: "commit", changedFiles: [], changedFileCount: 0, added: 0, removed: 0 };
    api.submitAgentRunCommitDecision.mockRejectedValueOnce(new Error("auto failed"));
    await utils.handleCommitPromptAuto(); expect(store.lastError).toBeTruthy();
    utils.selectCommitPromptManual();
    await utils.handleCommitPromptManual();
    api.submitAgentRunCommitDecision.mockResolvedValue({ data: { created: false, reason: "skip", changedFiles: [] } });
    await utils.handleCommitPromptSkip();

    const internal = wrapper.find(".coomi-stream").element;
    Object.defineProperty(internal, "scrollHeight", { value: 123, configurable: true });
    utils.scrollToBottom(); expect((internal as HTMLElement).scrollTop).toBe(123);
    const relativeAnchor = document.createElement("a"); relativeAnchor.href = "chapters/002.md"; relativeAnchor.setAttribute("href", "chapters/002.md");
    const relativeEvent = { target: relativeAnchor, preventDefault: vi.fn(), stopPropagation: vi.fn() } as any;
    utils.handleMarkdownLinkClick(relativeEvent); expect(workspace.openFile).toHaveBeenCalled();
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const external = document.createElement("a"); external.href = "https://example.com"; external.setAttribute("href", "https://example.com");
    utils.handleMarkdownLinkClick({ target: external, preventDefault: vi.fn(), stopPropagation: vi.fn() } as any); expect(open).toHaveBeenCalled();
    utils.handleMarkdownLinkClick({ target: document.createElement("span"), preventDefault: vi.fn(), stopPropagation: vi.fn() } as any);
    wrapper.unmount();
  });

  it("renders computed-state and keyboard/pointer branch matrix", async () => {
    const store = useAgentStore();
    const wrapper = shallowMount(AgentPanel, { attachTo: document.body });
    const utils = (wrapper.vm as any).__testUtils;
    const key = (value: string, extra: Record<string, unknown> = {}) => ({ key: value, preventDefault: vi.fn(), ...extra } as any);

    workspace.launchScreenVisible = true;
    utils.syncStoryGenerationOptionsFromProjectSettings();
    await utils.persistStoryGenerationOptions({ fragmentCount: 2 });
    utils.toggleStoryOptions();
    await nextTick();
    workspace.launchScreenVisible = false;
    workspace.currentProject = null as any;
    await utils.persistStoryGenerationOptions({ fragmentWordCount: 500 });
    workspace.currentProject = { workspaceRoot: "C:/isolated/story" };
    workspace.updateStorySettings.mockRejectedValueOnce(new Error("disk"));
    await utils.persistStoryGenerationOptions({ fragmentCount: 3 });

    store.coomiStatus = { runtime: "coomi", installed: true, planMode: true, permissionMode: "full_access", permissionLabel: "Full" } as any;
    expect(utils.isPermissionOptionActive("plan_mode")).toBe(true);
    expect(utils.isPermissionOptionActive("full_access")).toBe(false);
    store.isRunning = true; await utils.selectPermissionOption("plan_mode");
    store.isRunning = false; await utils.selectPermissionOption("plan_mode");
    api.streamAgentPrompt.mockImplementationOnce(async (_request: unknown, onPacket: (packet: any) => void) => onPacket({ _type: "AgentCompleted" }));
    await utils.selectPermissionOption("read_only");
    store.coomiStatus = { runtime: "coomi", installed: true, planMode: false, permissionMode: "full_access" } as any;
    api.streamAgentPrompt.mockImplementationOnce(async (_request: unknown, onPacket: (packet: any) => void) => onPacket({ _type: "AgentCompleted" }));
    await utils.selectPermissionOption("plan_mode");

    store.promptInput = "/"; utils.handleComposerInput(); await nextTick();
    utils.handleComposerKeydown(key("ArrowDown"));
    utils.handleComposerKeydown(key("ArrowUp"));
    utils.handleComposerKeydown(key("Tab"));
    store.promptInput = "/"; utils.handleComposerInput(); await nextTick(); utils.handleComposerKeydown(key("Escape"));
    utils.handleComposerKeydown(key("Tab", { shiftKey: true }));
    utils.handleComposerKeydown(key("Enter", { shiftKey: false, isComposing: false }));

    utils.togglePermissionMenu();
    const composer = wrapper.find(".coomi-composer").element;
    utils.handleDocumentPointerDown({ target: composer } as any);
    utils.handleDocumentPointerDown({ target: document.body } as any);
    utils.handleDocumentPointerDown({ target: "not-node" } as any);
    utils.updateStoryFragmentCount({ target: null } as any);
    utils.updateStoryFragmentWordCount({ target: null } as any);
    utils.updateStoryChapterTemplate({ target: null } as any);

    store.storyChapterTemplates = [
      { id: "one", name: "One", description: "", segmentNaming: "001.md" },
      { id: "two", name: "Two", description: "Description", segmentNaming: "" }
    ] as any;
    store.storyChapterTemplateId = "one";
    store.storyChapterTemplatesError = "404 not found";
    store.usedTokens = 0; store.usageRatio = null; store.contextWindow = null;
    await nextTick();
    store.storyChapterTemplateId = "two";
    store.storyChapterTemplatesError = "real error";
    store.usedTokens = 50; store.contextWindow = 100; store.usageRatio = 0.5; store.warningThreshold = 60; store.compactThreshold = 85; store.cumulativeTokens = 1000;
    await nextTick();
    store.usageRatio = 0.7; await nextTick();
    store.usageRatio = 0.9; await nextTick();

    store.pendingApprovals = [
      { approvalId: "one", kind: "permission", allowText: false, options: [{ label: "Allow", value: "allow" }] },
      { approvalId: "two", kind: "question", allowText: true, options: [{ label: "Answer", value: "answer" }] }
    ] as any;
    await nextTick();
    utils.goToApproval(1); utils.selectApprovalOption("answer"); utils.updateApprovalDraftText("");
    await utils.handleApprovalConfirm();
    utils.updateApprovalDraftText("detail"); await utils.handleApprovalConfirm();
    store.pendingApprovals = [];
    await nextTick();

    vi.spyOn(window, "confirm").mockReturnValue(false);
    await utils.handleSessionDelete("missing");
    wrapper.unmount();
  });

  it("evaluates every computed state across empty, active, warning and prompt modes", async () => {
    const store = useAgentStore();
    const wrapper = shallowMount(AgentPanel);
    const utils = (wrapper.vm as any).__testUtils;
    const read = (name: string) => unref(utils[name]);

    workspace.launchScreenVisible = true;
    store.executionHistory = [];
    store.availableSessions = [];
    store.coomiStatus = null;
    store.storyChapterTemplates = [];
    store.storyChapterTemplateId = "missing";
    store.storyChapterTemplatesError = "";
    store.usedTokens = null; store.contextWindow = null; store.usageRatio = null; store.cumulativeTokens = null;
    store.pendingApprovals = []; store.pendingCommitPrompt = null; store.promptInput = "plain";
    await nextTick();
    for (const name of ["conversationRuns", "sessionSummaries", "modelLabel", "permissionControlLabel", "activePermissionTone", "selectedReasoningOption", "reasoningLabel", "storyOptionsLabel", "selectedChapterTemplate", "selectedChapterTemplateDescription", "storyChapterTemplateErrorMessage", "contextRatio", "contextLevel", "contextRingStyle", "contextTooltip", "filteredCommands", "commandMenuVisible", "approvalQueue", "activeApproval", "activeApprovalDraft", "allApprovalsComplete", "canConfirmApproval", "approvalConfirmLabel", "approvalConfirmTitle", "commitPromptSummary", "commitPromptFiles", "promptDockActive", "promptDockHandleTitle", "collapsedHandlesVisible"]) read(name);

    workspace.launchScreenVisible = false;
    const firstRun = { traceId: "old", createdAt: "2021-01-01T00:00:00Z", updatedAt: "2021-01-01T00:00:01Z", status: "completed", items: [] } as any;
    const secondRun = { traceId: "new", createdAt: "2022-01-01T00:00:00Z", updatedAt: "2022-01-01T00:00:01Z", status: "running", items: [] } as any;
    store.executionHistory = [secondRun, firstRun];
    store.availableSessions = [{ sessionId: "s", firstPrompt: "p" }] as any;
    store.coomiStatus = { runtime: "coomi", installed: true, display: "Display", model: "Model", permissionMode: "ask_approval", permissionLabel: "Ask", planMode: false } as any;
    store.storyChapterTemplates = [{ id: "selected", name: "Selected", description: "Description", segmentNaming: "001.md" }] as any;
    store.storyChapterTemplateId = "selected";
    store.usedTokens = 50; store.contextWindow = 100; store.usageRatio = Number.NaN; store.cumulativeTokens = 100; store.compressionStatus = "idle";
    store.promptInput = "/co";
    utils.commandMenuOpen.value = true;
    store.pendingApprovals = [{ approvalId: "a", kind: "permission", allowText: false, options: [{ label: "Allow", value: "allow" }] }] as any;
    store.pendingCommitPrompt = { traceId: "t", sessionId: "s", workspaceRoot: "root", message: "commit", changedFiles: ["a"], changedFileCount: 1, added: 2, removed: 1 };
    utils.executionFloatCollapsed.value = true; utils.promptDockCollapsed.value = true;
    await nextTick();
    for (const name of ["conversationRuns", "sessionSummaries", "modelLabel", "permissionControlLabel", "activePermissionTone", "selectedChapterTemplate", "selectedChapterTemplateDescription", "contextRatio", "contextLevel", "contextRingStyle", "contextTooltip", "filteredCommands", "commandMenuVisible", "activeApproval", "activeApprovalDraft", "allApprovalsComplete", "canConfirmApproval", "approvalConfirmLabel", "approvalConfirmTitle", "commitPromptSummary", "commitPromptFiles", "promptDockActive", "promptDockHandleTitle", "collapsedHandlesVisible"]) read(name);

    store.coomiStatus = { ...store.coomiStatus, display: "", model: "Model", planMode: true } as any;
    store.storyChapterTemplates[0].description = ""; store.storyChapterTemplates[0].segmentNaming = "";
    store.storyChapterTemplatesError = "request failed with status code 404";
    store.usageRatio = -1; store.usedTokens = 10; store.warningThreshold = 20; store.compactThreshold = 80;
    store.pendingApprovals = [
      { approvalId: "a", kind: "permission", allowText: false, options: [{ label: "Allow", value: "allow" }] },
      { approvalId: "b", kind: "question", allowText: true, options: [{ label: "Answer", value: "answer" }] }
    ] as any;
    await nextTick();
    for (const name of ["modelLabel", "permissionControlLabel", "activePermissionTone", "selectedChapterTemplateDescription", "storyChapterTemplateErrorMessage", "contextRatio", "contextLevel", "contextRingStyle", "approvalConfirmLabel", "approvalConfirmTitle"]) read(name);

    store.storyChapterTemplatesError = "visible failure";
    store.usageRatio = 2; store.usedTokens = 100;
    store.pendingCommitPrompt = null;
    await nextTick();
    for (const name of ["storyChapterTemplateErrorMessage", "contextRatio", "contextLevel", "contextRingStyle", "promptDockHandleTitle"]) read(name);
    wrapper.unmount();
  });

  it("covers remaining fallback and approval decision branches", async () => {
    const store = useAgentStore();
    const wrapper = shallowMount(AgentPanel);
    const utils = (wrapper.vm as any).__testUtils;
    const read = (name: string) => unref(utils[name]);

    utils.selectedReasoningMode.value = "invalid";
    read("selectedReasoningOption");
    store.usedTokens = 1; store.contextWindow = null; store.usageRatio = null; read("contextRatio"); read("contextLevel");
    store.contextWindow = 100; store.usageRatio = 0.6; store.warningThreshold = null; store.compactThreshold = null; store.compressionStatus = "";
    read("contextLevel"); read("contextRingStyle"); read("contextTooltip");
    store.storyChapterTemplateId = ""; utils.syncStoryGenerationOptionsFromProjectSettings();

    store.promptInput = "draft";
    api.streamAgentPrompt.mockImplementationOnce(async (_request: unknown, onPacket: (packet: any) => void) => onPacket({ _type: "AgentCompleted" }));
    await utils.runCoomiCommand("/plan"); expect(store.promptInput).toBe("draft");

    store.pendingApprovals = [];
    utils.selectApprovalOption("allow"); utils.updateApprovalDraftText("none");
    await utils.handleApprovalConfirm();
    const permission = { approvalId: "p", kind: "permission", allowText: false, options: [{ label: "Allow", value: "allow" }, { label: "Deny", value: "deny" }] } as any;
    expect(utils.isApprovalDraftComplete(permission, { value: "", text: "text" })).toBe(true);
    store.pendingApprovals = [permission]; await nextTick();
    utils.approvalDrafts.value = {};
    utils.selectApprovalOption("allow");
    utils.approvalDrafts.value = { p: { value: "allow", text: "" } };
    await utils.handleApprovalConfirm();
    store.pendingApprovals = [permission]; await nextTick();
    utils.approvalDrafts.value = { p: { value: "deny", text: "" } };
    await utils.handleApprovalConfirm();
    store.pendingApprovals = [{ approvalId: "q", kind: "question", allowText: true, options: [] }] as any; await nextTick();
    utils.approvalDrafts.value = { q: { value: "", text: "free" } };
    await utils.handleApprovalConfirm();
    expect(utils.approvalOptionLabel("Custom Allow", "allow")).toBe("Custom Allow");
    expect(utils.approvalOptionLabel("Custom Deny", "deny")).toBe("Custom Deny");

    const base = { traceId: "r", status: "completed", createdAt: "", updatedAt: "2020-01-01T00:00:00Z", items: [] } as any;
    store.executionHistory = [{ ...base, traceId: "a" }, { ...base, traceId: "b" }]; read("conversationRuns");
    const skipped = [
      { id: "u", type: "usage", status: "success", title: "U", content: "", timestamp: "", raw: {} },
      { id: "c", type: "compression", status: "success", title: "C", content: "", timestamp: "", raw: {} },
      { id: "s", type: "system", status: "success", title: "S", content: "", timestamp: "", raw: {} }
    ];
    expect(utils.displayEntries({ ...base, items: skipped })).toEqual([]);
    const emptyReason = { id: "reason", type: "reasoning", status: "success", title: "", content: "", timestamp: "", raw: {} };
    expect(utils.reasoningTitle({ ...base, items: [emptyReason] }, emptyReason)).toContain("1 lines");
    expect(utils.toolSummary({ id: "x", type: "tool", status: "success", title: "", content: "", timestamp: "", raw: {} })).toBeTruthy();
    expect(utils.formatItemType("unknown")).toBe("unknown");
    expect(utils.formatDate("")).toBe("");
    expect(utils.renderMarkdown("")).toBe("");
    expect(utils.compactText("short", 10)).toBe("short");
    expect(utils.firstStringFromRecord({ a: 1 }, ["a"])).toBe("");
    wrapper.unmount();
  });
});
