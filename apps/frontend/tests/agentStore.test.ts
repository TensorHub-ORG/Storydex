import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const api = vi.hoisted(() => ({
  streamAgentPrompt: vi.fn(),
  fetchAgentSessions: vi.fn(),
  fetchAgentHistory: vi.fn(),
  fetchAgentCoomiStatus: vi.fn(),
  setAgentCoomiPlanMode: vi.fn(),
  submitAgentRunCommitDecision: vi.fn(),
  rollbackLatestExecution: vi.fn(),
  clearConversation: vi.fn(),
  deleteAgentSession: vi.fn(),
  cycleAgentCoomiPermission: vi.fn(),
  setAgentCoomiPermission: vi.fn(),
  resolveAgentCoomiApproval: vi.fn(),
  fetchAgentFollowups: vi.fn(), enqueueAgentFollowup: vi.fn(), updateAgentFollowup: vi.fn(),
  deleteAgentFollowup: vi.fn(), steerAgentFollowup: vi.fn(), resumeAgentFollowups: vi.fn(), stopAgentExecution: vi.fn()
}));
const git = vi.hoisted(() => ({ refreshSummary: vi.fn() }));
const workspace = vi.hoisted(() => ({
  activeFileBindingOrPath: "chapters/001.md",
  activeFile: "chapters/001.md",
  currentProject: { workspaceRoot: "C:/isolated/story" },
  health: null
}));

vi.mock("@/api/agent", () => ({
  AgentApiError: class AgentApiError extends Error {
    code: string | null;
    details?: Record<string, unknown>;

    constructor(message: string, code: string | null = null, details?: Record<string, unknown>) {
      super(message);
      this.code = code;
      this.details = details;
    }
  },
  ...api
}));
vi.mock("@/stores/git", () => ({ useGitStore: () => git }));
vi.mock("@/stores/workspace", () => ({ useWorkspaceStore: () => workspace }));
vi.mock("@/api/workspace", () => ({ fetchStoryChapterTemplates: vi.fn().mockResolvedValue({ data: { items: [] } }) }));
vi.mock("@/api/client", () => ({ describeTransportError: (error: unknown, fallback: string) => error instanceof Error ? error.message : fallback }));

import { __agentStoreTestUtils, useAgentStore } from "@/stores/agent";
import { AgentApiError } from "@/api/agent";

function sessions(items: unknown[] = []) {
  return { data: { items }, trace: null, audit: [] };
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  workspace.activeFileBindingOrPath = "chapters/001.md";
  workspace.activeFile = "chapters/001.md";
  workspace.currentProject = { workspaceRoot: "C:/isolated/story" };
  workspace.health = null;
  api.fetchAgentSessions.mockResolvedValue(sessions([]));
  api.fetchAgentCoomiStatus.mockResolvedValue({
    data: { runtime: "coomi", installed: true, model: "fake", permissionMode: "full_access" }
  });
  api.setAgentCoomiPlanMode.mockImplementation((sessionId: string, active: boolean) => Promise.resolve({
    data: { sessionId, planMode: active, permissionMode: active ? "plan_mode" : "full_access", permissionLabel: active ? "Plan mode" : "Full access" }
  }));
  git.refreshSummary.mockResolvedValue(undefined);
  api.rollbackLatestExecution.mockResolvedValue({
    data: { rolledBack: false, sessionId: "default", removedTraceId: "", prompt: "" }
  });
  api.fetchAgentFollowups.mockResolvedValue(envelopeMailbox());
  api.resumeAgentFollowups.mockResolvedValue(envelopeMailbox());
  api.stopAgentExecution.mockResolvedValue({ data: { accepted: true, activeTraceId: "trace", mailboxPaused: true, pauseReason: "manual_stop" } });
});

function envelopeMailbox(messages: unknown[] = []) {
  return { data: { messages, paused: false, pauseReason: "", revision: 0 } };
}

describe("agent store streaming", () => {
  it("handles /plan and /exit_plan locally without entering the story generation stream", async () => {
    const store = useAgentStore();
    store.currentSessionId = "session-plan";
    store.promptInput = "/plan";

    await store.runPrompt();

    expect(api.setAgentCoomiPlanMode).toHaveBeenCalledWith("session-plan", true);
    expect(api.streamAgentPrompt).not.toHaveBeenCalled();
    expect(store.coomiStatus?.planMode).toBe(true);
    expect(store.executionHistory[0].items.some((item) => item.type === "info")).toBe(true);
    expect(store.promptInput).toBe("");

    store.promptInput = "/exit_plan";
    await store.runPrompt();

    expect(api.setAgentCoomiPlanMode).toHaveBeenLastCalledWith("session-plan", false);
    expect(store.coomiStatus?.planMode).toBe(false);
    expect(store.executionHistory[0].reply).toContain("退出计划模式");
  });

  it("publishes immediate phases, updates heartbeats in place, and appends text chunks", async () => {
    api.streamAgentPrompt.mockImplementation(async (_request: unknown, onPacket: (packet: unknown) => void) => {
      onPacket({ _type: "RunAccepted", phase: "request", status: "running", elapsedMs: 1, label: "请求已接收" });
      onPacket({ _type: "TurnPhase", phase: "intent_classification", status: "running", elapsedMs: 500, heartbeat: true, label: "意图识别" });
      onPacket({ _type: "TurnPhase", phase: "intent_classification", status: "running", elapsedMs: 1000, heartbeat: true, label: "意图识别" });
      onPacket({ _type: "TextChunk", content: "第一段" });
      onPacket({ _type: "TextChunk", content: "输出" });
      onPacket({ _type: "AgentCompleted", status: "success" });
    });
    const store = useAgentStore();
    store.currentSessionId = "session-a";
    store.promptInput = "继续";
    await store.runPrompt();

    expect(api.streamAgentPrompt).toHaveBeenCalledTimes(1);
    const request = api.streamAgentPrompt.mock.calls[0][0];
    expect(request.reasoningEffort).toBe("auto");
    expect(request.storyGeneration).toEqual({
      fragmentCount: 1,
      chapterLengthTier: "medium",
      chapterTemplateId: "default_chapter_directory"
    });
    expect(request.storyGeneration).not.toHaveProperty("chapterWordCountTarget");
    expect(request.storyGeneration).not.toHaveProperty("preciseWordCountEnabled");
    expect(request.storyGeneration).not.toHaveProperty("fragmentWordCount");
    expect(store.isRunning).toBe(false);
    expect(store.lastReply).toBe("第一段输出");
    expect(store.executionHistory[0].status).toBe("completed");
    const phaseItems = store.executionHistory[0].items.filter((item) => item.title.includes("意图") || item.content.includes("意图"));
    expect(phaseItems).toHaveLength(1);
    expect(phaseItems[0].content).toContain("1.0s");
    const assistant = store.executionHistory[0].items.find((item) => item.type === "assistant");
    expect(assistant?.content).toBe("第一段输出");
  });

  it("rolls back an incomplete provider attempt before rendering retry output", async () => {
    api.streamAgentPrompt.mockImplementation(async (_request: unknown, onPacket: (packet: unknown) => void) => {
      onPacket({ _type: "TurnPhase", phase: "model", status: "running", label: "模型" });
      onPacket({ _type: "TextChunk", content: "未完成输出" });
      onPacket({
        _type: "ConnectionRetry",
        attempt: 1,
        maxAttempts: 3,
        resetTextCharacters: 5,
        message: "上游流中断，正在重试"
      });
      onPacket({ _type: "TextChunk", content: "完整替代输出" });
      onPacket({ _type: "AgentCompleted", status: "success" });
    });
    const store = useAgentStore();
    store.currentSessionId = "session-retry-reset";
    store.promptInput = "生成正文";

    await store.runPrompt();

    expect(store.lastReply).toBe("完整替代输出");
    expect(store.executionHistory[0].reply).toBe("完整替代输出");
    expect(store.executionHistory[0].events.some((event) => event.event === "ConnectionRetry")).toBe(true);
    expect(store.executionHistory[0].events.some(
      (event) => event.event === "TextChunk" && event.detail.includes("未完成")
    )).toBe(false);
    const assistantText = store.executionHistory[0].items
      .filter((item) => item.type === "assistant")
      .map((item) => item.content)
      .join("");
    expect(assistantText).toBe("完整替代输出");
  });

  it("records cancellation and failures without leaving the run locked", async () => {
    api.streamAgentPrompt.mockRejectedValueOnce(Object.assign(new Error("cancelled"), { code: "request_aborted" }));
    const store = useAgentStore();
    store.promptInput = "stop";
    await store.runPrompt();
    expect(store.isRunning).toBe(false);
    expect(store.executionHistory[0].status).toBe("failed");
    expect(store.lastError).toBe("cancelled");
  });

  it("ignores empty prompts and concurrent submissions", async () => {
    const store = useAgentStore();
    await store.runPrompt();
    store.promptInput = "hello";
    store.isRunning = true;
    await store.runPrompt();
    expect(api.streamAgentPrompt).not.toHaveBeenCalled();
  });

  it("keeps reasoning diagnostics in trace events without rendering conversation items", async () => {
    api.streamAgentPrompt.mockImplementation(async (_request: unknown, onPacket: (packet: unknown) => void) => {
      onPacket({
        _type: "ReasoningPlan",
        plan: {
          requested: "max",
          control: "native",
          sent: true,
          promptApplied: false,
          wireFields: [{ path: "reasoning.effort", value: "max" }],
          support: "supported",
          source: "model_rule",
          routeSensitive: false
        }
      });
      onPacket({
        _type: "ModelCompleted",
        round: 1,
        upstreamResponded: true,
        responseModel: "routed-model",
        nativeReasoning: true,
        reasoningTokens: 321,
        finishReason: "stop"
      });
      onPacket({ _type: "AgentCompleted", status: "success" });
    });
    const store = useAgentStore();
    store.reasoningEffort = "max";
    store.promptInput = "inspect reasoning diagnostics";

    await store.runPrompt();

    const run = store.executionHistory[0];
    expect(run.events.map((event) => event.event)).toEqual(expect.arrayContaining([
      "ReasoningPlan",
      "ModelCompleted"
    ]));
    expect(run.items.some((item) => item.raw?._type === "ReasoningPlan")).toBe(false);
    expect(run.items.some((item) => item.raw?._type === "ModelCompleted")).toBe(false);
    expect(__agentStoreTestUtils?.streamPacketToWaterfallItem(
      "trace-evidence",
      { _type: "ReasoningPlan" },
      []
    )).toBeNull();
    expect(__agentStoreTestUtils?.streamPacketToWaterfallItem(
      "trace-evidence",
      { _type: "ModelCompleted" },
      []
    )).toBeNull();
  });

  it("does not infer native reasoning when capability fields are missing", () => {
    const status = __agentStoreTestUtils?.normalizeCoomiStatus({
      runtime: "coomi",
      installed: true,
      reasoningCapability: {
        support: "supported",
        source: "provider_config",
        fallbackReason: "high: invalid mapping",
        levels: [
          { effort: "low", wireFields: [{ path: "reasoning_effort", value: "low" }] },
          { effort: "high", control: "native", wireFields: [] }
        ]
      },
      reasoningRequestPlan: {
        requested: "high",
        control: "auto",
        sent: false,
        fallbackReason: "invalid mapping"
      }
    });

    expect(status?.reasoningCapability?.levels).toEqual([
      {
        effort: "low",
        control: "native",
        wireFields: [{ path: "reasoning_effort", value: "low" }],
        routeSensitive: false
      }
    ]);
    expect(status?.reasoningCapability?.fallbackReason).toBe("high: invalid mapping");
    expect(status?.reasoningRequestPlan?.fallbackReason).toBe("invalid mapping");
  });

  it("asks before retrying without a restore point and persists the run risk flag", async () => {
    let firstTraceId = "";
    api.streamAgentPrompt
      .mockImplementationOnce(async (_request: unknown, onPacket: (packet: unknown) => void, traceId: string) => {
        firstTraceId = traceId;
        onPacket({
          _type: "AgentError",
          error_type: "SNAPSHOT_FAILED",
          message: "snapshot failed",
          details: { reason: "git unavailable", confirmNoSnapshotRequired: true }
        });
        throw new AgentApiError(
          "snapshot failed",
          "SNAPSHOT_FAILED",
          { reason: "git unavailable", confirmNoSnapshotRequired: true }
        );
      })
      .mockImplementationOnce(async (request: any, onPacket: (packet: unknown) => void, traceId: string) => {
        expect(traceId).not.toBe(firstTraceId);
        expect(request.confirmNoSnapshot).toBe(true);
        onPacket({ _type: "RunAccepted", noRestorePoint: true });
        onPacket({ _type: "TurnPhase", phase: "workspace_snapshot", noRestorePoint: true, status: "warning" });
        onPacket({ _type: "AgentCompleted" });
      });

    const store = useAgentStore();
    store.currentSessionId = "session-a";
    store.promptInput = "继续写作";
    await store.runPrompt();

    expect(store.pendingSnapshotConfirmation?.request).toMatchObject({
      prompt: "继续写作",
      workspaceRoot: "C:/isolated/story"
    });
    expect(store.pendingSnapshotConfirmation?.details.reason).toBe("git unavailable");
    expect(store.executionHistory).toHaveLength(0);

    await store.confirmNoSnapshot();

    expect(api.streamAgentPrompt).toHaveBeenCalledTimes(2);
    expect(store.pendingSnapshotConfirmation).toBeNull();
    expect(store.executionHistory[0].status).toBe("completed");
    expect(store.executionHistory[0].noRestorePoint).toBe(true);
  });

  it("cancels a pending no-restore-point retry without starting another request", () => {
    const store = useAgentStore();
    store.pendingSnapshotConfirmation = {
      request: { prompt: "do not retry" },
      traceId: "trace-rejected",
      sessionId: "session-a",
      message: "snapshot failed",
      details: {}
    };
    store.cancelNoSnapshot();
    expect(store.pendingSnapshotConfirmation).toBeNull();
    expect(api.streamAgentPrompt).not.toHaveBeenCalled();
  });
});

describe("agent store sessions and Git decision UX", () => {
  it("rolls back the latest run, reloads history, and optionally refills the composer", async () => {
    api.rollbackLatestExecution.mockResolvedValue({
      data: {
        rolledBack: true,
        sessionId: "session-a",
        removedTraceId: "trace-latest",
        prompt: "rewrite this prompt"
      }
    });
    api.fetchAgentHistory.mockResolvedValue({
      data: {
        items: [{
          traceId: "trace-previous",
          sessionId: "session-a",
          prompt: "previous",
          reply: "previous reply",
          status: "completed",
          events: []
        }]
      }
    });
    const store = useAgentStore();
    store.currentSessionId = "session-a";
    store.currentTraceId = "trace-latest";
    store.executionHistory = [
      {
        traceId: "trace-latest", sessionId: "session-a", prompt: "rewrite this prompt", route: "coomi", agentMode: "coomi",
        llmModel: "", llmProvider: "", status: "completed", noRestorePoint: false, createdAt: "2026-07-21T11:00:00Z",
        updatedAt: "2026-07-21T11:00:00Z", lastAction: "chat", reply: "latest reply", trace: null, audit: [], events: [],
        tasks: [], changeLedger: { traceId: "trace-latest", sessionId: "session-a", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
        items: [], errorMessage: "", errorCode: null
      },
      {
        traceId: "trace-previous", sessionId: "session-a", prompt: "previous", route: "coomi", agentMode: "coomi",
        llmModel: "", llmProvider: "", status: "completed", noRestorePoint: false, createdAt: "2026-07-21T10:00:00Z",
        updatedAt: "2026-07-21T10:00:00Z", lastAction: "chat", reply: "previous reply", trace: null, audit: [], events: [],
        tasks: [], changeLedger: { traceId: "trace-previous", sessionId: "session-a", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
        items: [], errorMessage: "", errorCode: null
      }
    ];

    await expect(store.rollbackLatestRun({ refillComposer: true })).resolves.toBe(true);

    expect(api.rollbackLatestExecution).toHaveBeenCalledWith("session-a", "trace-latest");
    expect(store.executionHistory.map((run) => run.traceId)).toEqual(["trace-previous"]);
    expect(store.currentTraceId).toBe("trace-previous");
    expect(store.lastPrompt).toBe("previous");
    expect(store.lastReply).toBe("previous reply");
    expect(store.promptInput).toBe("rewrite this prompt");
    expect(store.isRollingBack).toBe(false);
  });

  it("does not call rollback while an execution is running", async () => {
    const store = useAgentStore();
    store.isRunning = true;
    await expect(store.rollbackLatestRun({ refillComposer: false })).resolves.toBe(false);
    expect(api.rollbackLatestExecution).not.toHaveBeenCalled();
  });

  it("loads and selects persisted session history", async () => {
    api.fetchAgentSessions.mockResolvedValue(sessions([{ sessionId: "session-a", firstPrompt: "old", traceCount: 1 }]));
    api.fetchAgentHistory.mockResolvedValue({
      data: { items: [{ traceId: "trace-old", sessionId: "session-a", prompt: "old", reply: "需要执行变量整理吗？", status: "completed", events: [] }] }
    });
    const store = useAgentStore();
    await store.loadSessions();
    await store.loadHistory();
    expect(store.currentSessionId).toBe("session-a");
    expect(store.lastReply).toBe("需要执行变量整理吗？");
    expect(store.executionHistory[0].traceId).toBe("trace-old");
  });

  it("does not let a stale mount-time history response overwrite a live run", async () => {
    let resolveHistory!: (value: unknown) => void;
    api.fetchAgentHistory.mockReturnValue(new Promise((resolve) => { resolveHistory = resolve; }));
    const store = useAgentStore();
    store.currentSessionId = "session-a";
    const loading = store.loadHistory();
    store.isRunning = true;
    store.executionHistory = [{
      traceId: "trace-live", sessionId: "session-a", prompt: "live", route: "coomi", agentMode: "coomi", llmModel: "", llmProvider: "",
      status: "running", createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), lastAction: "chat", reply: "", trace: null,
      audit: [], events: [], tasks: [], changeLedger: { traceId: "trace-live", sessionId: "session-a", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" }, items: [], errorMessage: "", errorCode: null
    }];
    resolveHistory({ data: { items: [{ traceId: "trace-old", sessionId: "session-a", prompt: "old", reply: "old", events: [] }] } });
    await loading;
    expect(store.executionHistory[0].traceId).toBe("trace-live");
  });

  it("shows operation state synchronously and does not await background Git refresh", async () => {
    let resolveDecision!: (value: unknown) => void;
    api.submitAgentRunCommitDecision.mockReturnValue(new Promise((resolve) => { resolveDecision = resolve; }));
    git.refreshSummary.mockReturnValue(new Promise(() => undefined));
    const store = useAgentStore();
    store.pendingCommitPrompt = {
      traceId: "trace-1", sessionId: "session-1", workspaceRoot: "C:/isolated/story", message: "commit?",
      changedFiles: ["chapters/001.md"], changedFileCount: 1, added: 2, removed: 0
    };
    store.executionHistory = [{
      traceId: "trace-1", sessionId: "session-1", prompt: "write", route: "coomi", agentMode: "coomi", llmModel: "", llmProvider: "",
      status: "completed", createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), lastAction: "chat", reply: "", trace: null,
      audit: [], events: [], tasks: [], changeLedger: { traceId: "trace-1", sessionId: "session-1", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" }, items: [], errorMessage: "", errorCode: null
    }];

    const pending = store.resolvePendingCommitPrompt("skip");
    expect(store.isCommittingGit).toBe(true);
    expect(store.commitActionLabel).not.toBe("");
    resolveDecision({ data: { created: false, reason: "skipped", changedFiles: ["chapters/001.md"] } });
    await pending;
    expect(store.pendingCommitPrompt).toBeNull();
    expect(store.isCommittingGit).toBe(false);
    expect(git.refreshSummary).toHaveBeenCalledWith({ silent: true });
  });

  it("clears only live changes after a successful commit and preserves history", async () => {
    api.submitAgentRunCommitDecision.mockResolvedValue({
      data: { created: true, status: "success", changedFiles: ["chapters/001.md"], changedFileCount: 1, commitHash: "abc", shortHash: "abc" }
    });
    const store = useAgentStore();
    store.pendingCommitPrompt = {
      traceId: "trace-commit", sessionId: "session", workspaceRoot: "C:/isolated/story", message: "commit?",
      changedFiles: ["chapters/001.md"], changedFileCount: 1, added: 2, removed: 1
    };
    store.liveChangeLedger = {
      traceId: "trace-commit", sessionId: "session", changedFiles: ["chapters/001.md"], changedFileCount: 1,
      added: 2, removed: 1, diffSource: "working_tree", commitHash: "", shortHash: "", updatedAt: new Date().toISOString()
    };
    store.executionHistory = [{
      traceId: "trace-commit", sessionId: "session", prompt: "write", route: "coomi", agentMode: "coomi", llmModel: "", llmProvider: "",
      status: "completed", createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), lastAction: "chat", reply: "", trace: null,
      audit: [], events: [], tasks: [], changeLedger: { ...store.liveChangeLedger }, items: [], errorMessage: "", errorCode: null
    }];

    await store.resolvePendingCommitPrompt("manual", "story: update");
    expect(store.liveChangeLedger).toBeNull();
    expect(store.executionHistory[0].changeLedger.commitHash).toBe("abc");
    expect(store.executionHistory[0].changeLedger.changedFiles).toEqual(["chapters/001.md"]);
  });

  it("requires a manual commit message before sending", async () => {
    const store = useAgentStore();
    store.pendingCommitPrompt = {
      traceId: "trace-1", sessionId: "session-1", workspaceRoot: "C:/isolated/story", message: "commit?",
      changedFiles: [], changedFileCount: 0, added: 0, removed: 0
    };
    await store.resolvePendingCommitPrompt("manual", "   ");
    expect(api.submitAgentRunCommitDecision).not.toHaveBeenCalled();
    expect(store.lastError).not.toBe("");
  });

  it("edits the latest message in two phases and restores the unsent draft on cancel", () => {
    const store = useAgentStore();
    const latest = {
      traceId: "trace-latest", sessionId: "session-a", prompt: "original prompt", route: "coomi", agentMode: "coomi",
      llmModel: "", llmProvider: "", status: "completed", createdAt: "2026-07-21T11:00:00Z",
      updatedAt: "2026-07-21T11:00:00Z", lastAction: "chat", reply: "original reply", trace: { traceId: "trace-latest" },
      audit: [{ action: "kept" }], events: [{ event: "AgentCompleted" }], tasks: [],
      changeLedger: { traceId: "trace-latest", sessionId: "session-a", changedFiles: ["chapters/001.md"], changedFileCount: 1, added: 1, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [], errorMessage: "", errorCode: null
    } as any;
    store.currentSessionId = "session-a";
    store.executionHistory = [latest];
    store.promptInput = "unsent draft";

    expect(store.beginEditLatestRun(latest)).toBe(true);
    expect(api.rollbackLatestExecution).not.toHaveBeenCalled();
    expect(store.promptInput).toBe("original prompt");
    expect(store.editingHasFileChanges).toBe(true);
    store.cancelEditLatestRun();

    expect(store.promptInput).toBe("unsent draft");
    expect(store.executionHistory[0].reply).toBe("original reply");
    expect(store.executionHistory[0].trace?.traceId).toBe("trace-latest");
    expect(api.rollbackLatestExecution).not.toHaveBeenCalled();
  });

  it("reexecutes only after confirmation and restores the original run when startup fails", async () => {
    const store = useAgentStore();
    const latest = {
      traceId: "trace-latest", sessionId: "session-a", prompt: "original", route: "coomi", agentMode: "coomi",
      llmModel: "", llmProvider: "", status: "completed", createdAt: "2026-07-21T11:00:00Z",
      updatedAt: "2026-07-21T11:00:00Z", lastAction: "chat", reply: "answer", trace: { traceId: "trace-latest" },
      audit: [], events: [], tasks: [], changeLedger: { traceId: "trace-latest", sessionId: "session-a", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [], errorMessage: "", errorCode: null
    } as any;
    store.currentSessionId = "session-a";
    store.currentTraceId = "trace-latest";
    store.executionHistory = [latest];
    store.beginEditLatestRun(latest);
    store.setStoryGenerationOptions({ chapterLengthTier: "long" });
    store.promptInput = "replacement";
    api.streamAgentPrompt.mockRejectedValueOnce(new AgentApiError("preflight failed", "replacement_preflight"));

    await expect(store.reexecuteEditedLatestRun()).resolves.toBe(false);
    const request = api.streamAgentPrompt.mock.calls[0][0];
    expect(request.replaceLatestTraceId).toBe("trace-latest");
    expect(request.storyGeneration).toEqual({
      fragmentCount: 1,
      chapterLengthTier: "long",
      chapterTemplateId: "default_chapter_directory"
    });
    expect(store.executionHistory).toHaveLength(1);
    expect(store.executionHistory[0].traceId).toBe("trace-latest");
    expect(store.executionHistory[0].reply).toBe("answer");
    expect(store.editingTraceId).toBe("trace-latest");
  });

  it("successfully replaces the latest dialogue while retaining the superseded run", async () => {
    const store = useAgentStore();
    const latest = {
      traceId: "trace-latest", sessionId: "session-a", prompt: "original", route: "coomi", agentMode: "coomi",
      llmModel: "", llmProvider: "", status: "completed", createdAt: "2026-07-21T11:00:00Z",
      updatedAt: "2026-07-21T11:00:00Z", lastAction: "chat", reply: "original answer", trace: { traceId: "trace-latest" },
      audit: [{ action: "kept" }], events: [{ event: "AgentCompleted" }], tasks: [],
      changeLedger: { traceId: "trace-latest", sessionId: "session-a", changedFiles: ["chapters/001.md"], changedFileCount: 1, added: 1, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [], errorMessage: "", errorCode: null
    } as any;
    store.currentSessionId = "session-a";
    store.currentTraceId = "trace-latest";
    store.executionHistory = [latest];
    store.promptInput = "draft before edit";
    store.beginEditLatestRun(latest);
    store.promptInput = "replacement prompt";

    let replacementTraceId = "";
    api.streamAgentPrompt.mockImplementationOnce(async (request: any, onPacket: (packet: any) => void, traceId: string) => {
      replacementTraceId = traceId;
      expect(request.replaceLatestTraceId).toBe("trace-latest");
      onPacket({ _type: "RunAccepted", traceId });
      onPacket({ _type: "TurnContract", traceId, status: "ready" });
      onPacket({ _type: "TextChunk", traceId, content: "replacement answer" });
      onPacket({ _type: "AgentCompleted", traceId });
    });

    await expect(store.reexecuteEditedLatestRun()).resolves.toBe(true);

    expect(replacementTraceId).not.toBe("");
    expect(store.executionHistory).toHaveLength(2);
    expect(store.executionHistory[0].traceId).toBe(replacementTraceId);
    expect(store.executionHistory[0].prompt).toBe("replacement prompt");
    expect(store.executionHistory[0].reply).toBe("replacement answer");
    expect(store.executionHistory[0].status).toBe("completed");
    const superseded = store.executionHistory.find((run) => run.traceId === "trace-latest");
    expect(superseded?.status).toBe("superseded");
    expect(superseded?.reply).toBe("original answer");
    expect(superseded?.trace?.traceId).toBe("trace-latest");
    expect(store.editingTraceId).toBe("");
    expect(store.promptInput).toBe("");
    expect(api.rollbackLatestExecution).not.toHaveBeenCalled();
  });

  it("retries an HTTP failure without requiring a server-side replacement record", async () => {
    const store = useAgentStore();
    const failed = {
      traceId: "trace-http-503", sessionId: "session-a", prompt: "继续生成", route: "coomi", agentMode: "coomi",
      llmModel: "", llmProvider: "", status: "failed", noRestorePoint: false,
      createdAt: "2026-07-21T11:00:00Z", updatedAt: "2026-07-21T11:00:00Z", lastAction: "chat", reply: "", trace: null,
      audit: [], events: [], tasks: [], changeLedger: { traceId: "trace-http-503", sessionId: "session-a", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [], errorMessage: "Request failed with status code 503", errorCode: "service_unavailable", turnTokens: null, turnDurationMs: null
    } as any;
    store.currentSessionId = "session-a";
    store.executionHistory = [failed];
    store.promptInput = "尚未发送的草稿";
    api.streamAgentPrompt.mockImplementationOnce(async (request: any, onPacket: (packet: any) => void) => {
      expect(request.prompt).toBe("继续生成");
      expect(request.replaceLatestTraceId).toBeUndefined();
      onPacket({ _type: "AgentCompleted" });
    });

    await expect(store.retryFailedRun(failed)).resolves.toBe(true);

    expect(store.executionHistory).toHaveLength(1);
    expect(store.executionHistory[0].traceId).not.toBe("trace-http-503");
    expect(store.executionHistory[0].status).toBe("completed");
    expect(store.promptInput).toBe("尚未发送的草稿");
  });

  it("replaces a persisted failed turn when regenerating it", async () => {
    const store = useAgentStore();
    const failed = {
      traceId: "trace-provider-error", sessionId: "session-a", prompt: "重写这一段", route: "coomi", agentMode: "coomi",
      llmModel: "", llmProvider: "", status: "failed", noRestorePoint: false,
      createdAt: "2026-07-21T11:00:00Z", updatedAt: "2026-07-21T11:00:00Z", lastAction: "chat", reply: "", trace: null,
      audit: [],
      events: [{ index: 1, event: "AgentError", phase: "agent", status: "error", detail: "503", timestamp: "2026-07-21T11:00:00Z", data: { message: "503" } }],
      tasks: [], changeLedger: { traceId: "trace-provider-error", sessionId: "session-a", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [], errorMessage: "Provider returned 503", errorCode: "provider_error", turnTokens: null, turnDurationMs: null
    } as any;
    store.currentSessionId = "session-a";
    store.executionHistory = [failed];
    store.promptInput = "保留这个草稿";
    api.streamAgentPrompt.mockImplementationOnce(async (request: any, onPacket: (packet: any) => void) => {
      expect(request.replaceLatestTraceId).toBe("trace-provider-error");
      onPacket({ _type: "TurnContract", status: "ready" });
      onPacket({ _type: "TextChunk", content: "新的结果" });
      onPacket({ _type: "AgentCompleted" });
    });

    await expect(store.retryFailedRun(failed)).resolves.toBe(true);

    expect(store.executionHistory[0].reply).toBe("新的结果");
    expect(store.executionHistory.find((run) => run.traceId === "trace-provider-error")?.status).toBe("superseded");
    expect(store.promptInput).toBe("保留这个草稿");
  });

  it("rejects retry when the failed run is not eligible", async () => {
    const store = useAgentStore();
    const failed = {
      traceId: "trace-guarded", sessionId: "session-a", prompt: "retry me", route: "coomi", agentMode: "coomi",
      llmModel: "", llmProvider: "", status: "failed", noRestorePoint: false,
      createdAt: "2026-07-21T11:00:00Z", updatedAt: "2026-07-21T11:00:00Z", lastAction: "chat", reply: "", trace: null,
      audit: [], events: [], tasks: [], changeLedger: { traceId: "trace-guarded", sessionId: "session-a", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [], errorMessage: "failed", errorCode: "provider_error", turnTokens: null, turnDurationMs: null
    } as any;
    store.executionHistory = [failed];

    await expect(store.retryFailedRun({ ...failed, prompt: undefined })).resolves.toBe(false);
    await expect(store.retryFailedRun({ ...failed, traceId: "" })).resolves.toBe(false);
    await expect(store.retryFailedRun({ ...failed, traceId: "trace-older" })).resolves.toBe(false);
    await expect(store.retryFailedRun({ ...failed, status: "completed" })).resolves.toBe(false);

    store.isRunning = true;
    await expect(store.retryFailedRun(failed)).resolves.toBe(false);
    store.isRunning = false;
    store.isRollingBack = true;
    await expect(store.retryFailedRun(failed)).resolves.toBe(false);
    store.isRollingBack = false;
    store.isReexecuting = true;
    await expect(store.retryFailedRun(failed)).resolves.toBe(false);
    store.isReexecuting = false;
    store.editingTraceId = "trace-editing";
    await expect(store.retryFailedRun(failed)).resolves.toBe(false);

    expect(api.streamAgentPrompt).not.toHaveBeenCalled();
  });

  it("keeps the original failed turn when retry creates no replacement run", async () => {
    const store = useAgentStore();
    const failed = {
      traceId: "trace-no-replacement", sessionId: "", prompt: "retry me", route: "coomi", agentMode: "coomi",
      llmModel: "", llmProvider: "", status: "failed", noRestorePoint: false,
      createdAt: "2026-07-21T11:00:00Z", updatedAt: "2026-07-21T11:00:00Z", lastAction: "chat", reply: "", trace: null,
      audit: [], events: [], tasks: [], changeLedger: { traceId: "trace-no-replacement", sessionId: "", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [], errorMessage: "failed", errorCode: "provider_error", turnTokens: null, turnDurationMs: null
    } as any;
    store.executionHistory = [failed];
    store.currentSessionId = "";
    store.storyChapterTemplateId = "";
    workspace.activeFileBindingOrPath = "";
    workspace.activeFile = "chapters/fallback.md";
    workspace.currentProject = null as any;
    workspace.health = { workspaceRoot: "C:/health/story" } as any;
    const execute = vi.spyOn(store, "executePromptRequest").mockResolvedValue(false);

    await expect(store.retryFailedRun(failed)).resolves.toBe(false);

    expect(execute).toHaveBeenCalledWith(
      expect.objectContaining({
        activeFile: "chapters/fallback.md",
        workspaceRoot: "C:/health/story",
        storyGeneration: expect.objectContaining({ chapterTemplateId: "default_chapter_directory" })
      }),
      { sessionId: "default", preserveComposer: true }
    );
    expect(store.executionHistory).toEqual([failed]);
    expect(store.isReexecuting).toBe(false);
  });

  it("restores a persisted failed turn when regeneration fails again", async () => {
    const store = useAgentStore();
    const failed = {
      traceId: "trace-retry-failed", sessionId: "session-a", prompt: "retry me", route: "coomi", agentMode: "coomi",
      llmModel: "", llmProvider: "", status: "failed", noRestorePoint: false,
      createdAt: "2026-07-21T11:00:00Z", updatedAt: "2026-07-21T11:00:00Z", lastAction: "chat", reply: "", trace: null,
      audit: [],
      events: [{ index: 1, event: "AgentError", phase: "agent", status: "error", detail: "503", timestamp: "2026-07-21T11:00:00Z", data: { message: "503" } }],
      tasks: [], changeLedger: { traceId: "trace-retry-failed", sessionId: "session-a", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [], errorMessage: "first failure", errorCode: "provider_error", turnTokens: null, turnDurationMs: null
    } as any;
    store.currentSessionId = "session-a";
    store.executionHistory = [failed];
    api.streamAgentPrompt.mockRejectedValueOnce(new AgentApiError("retry also failed", "service_unavailable"));

    await expect(store.retryFailedRun(failed)).resolves.toBe(false);

    expect(store.executionHistory).toHaveLength(1);
    expect(store.executionHistory[0]).toMatchObject({
      traceId: "trace-retry-failed",
      status: "failed",
      errorMessage: "retry also failed",
      errorCode: "service_unavailable"
    });
    expect(store.currentTraceId).toBe("trace-retry-failed");
    expect(store.isReexecuting).toBe(false);
  });

  it("accepts a persisted retry after planning starts even if history refresh removed the original", async () => {
    const store = useAgentStore();
    const failed = {
      traceId: "trace-original-removed", sessionId: "session-a", prompt: "retry me", route: "coomi", agentMode: "coomi",
      llmModel: "", llmProvider: "", status: "failed", noRestorePoint: false,
      createdAt: "2026-07-21T11:00:00Z", updatedAt: "2026-07-21T11:00:00Z", lastAction: "chat", reply: "", trace: null,
      audit: [],
      events: [{ index: 1, event: "AgentError", phase: "agent", status: "error", detail: "503", timestamp: "2026-07-21T11:00:00Z", data: { message: "503" } }],
      tasks: [], changeLedger: { traceId: "trace-original-removed", sessionId: "session-a", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [], errorMessage: "first failure", errorCode: "provider_error", turnTokens: null, turnDurationMs: null
    } as any;
    const replacement = {
      ...failed,
      traceId: "trace-replacement-accepted",
      status: "failed",
      events: [{ index: 1, event: "RunAccepted", phase: "task_planning", status: "running", detail: "", timestamp: "2026-07-21T11:01:00Z", data: {} }]
    } as any;
    store.executionHistory = [failed];
    vi.spyOn(store, "executePromptRequest").mockImplementation(async () => {
      store.executionHistory = [replacement];
      return false;
    });

    await expect(store.retryFailedRun(failed)).resolves.toBe(true);

    expect(store.executionHistory).toEqual([replacement]);
    expect(store.isReexecuting).toBe(false);
  });

  it("persists queued follow-ups and resumes the first pending message with an idempotent source id", async () => {
    const store = useAgentStore();
    store.currentSessionId = "session-a";
    store.currentTraceId = "trace-active";
    store.isRunning = true;
    store.promptInput = "queued content";
    const message = {
      messageId: "followup-1", sessionId: "session-a", activeTraceId: "trace-active", expectedTraceId: "trace-active",
      content: "queued content", mode: "queued", status: "pending", createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(), sequence: 1
    };
    api.enqueueAgentFollowup.mockResolvedValue({ data: { message, steerRequested: false } });
    await expect(store.enqueueFollowup("queued")).resolves.toBe(true);
    expect(api.enqueueAgentFollowup.mock.calls[0][0].messageId).toMatch(/^followup-/);
    expect(store.followups[0].messageId).toBe("followup-1");

    store.isRunning = false;
    store.executionHistory = [{ ...latestRunForFollowup(), traceId: "trace-active" } as any];
    api.resumeAgentFollowups.mockResolvedValue(envelopeMailbox([message]));
    api.streamAgentPrompt.mockImplementationOnce(async (request: any, onPacket: (packet: any) => void) => {
      expect(request.sourceFollowupMessageId).toBe("followup-1");
      expect(request.sourceFollowupExpectedTraceId).toBe("trace-active");
      onPacket({ _type: "AgentCompleted" });
    });
    await store.resumeFollowups();
    expect(api.streamAgentPrompt).toHaveBeenCalledTimes(1);
  });

  it("removes steer messages when continuation starts and renders the guidance immediately", () => {
    const store = useAgentStore();
    store.currentSessionId = "session-a";
    store.currentTraceId = "trace-active";
    store.isRunning = true;
    store.executionHistory = [{
      ...latestRunForFollowup(),
      status: "running",
      items: [{
        id: "trace-active-user",
        type: "user",
        status: "success",
        title: "User",
        content: "original prompt",
        timestamp: new Date().toISOString()
      }]
    } as any];
    const steering = {
      messageId: "steer-1",
      sessionId: "session-a",
      activeTraceId: "trace-active",
      expectedTraceId: "trace-active",
      content: "new guidance",
      mode: "steer",
      status: "steering",
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      sequence: 1
    } as const;
    store.followups = [steering];

    const appliedPacket = {
      _type: "SteerApplied",
      messageId: "steer-1",
      sessionId: "session-a",
      activeTraceId: "trace-active",
      content: "new guidance",
      mode: "steer",
      status: "sent",
      segmentId: "trace-active-segment-2"
    } as any;
    store.applyStreamPacket("trace-active", appliedPacket);
    expect(store.followups).toEqual([]);

    const continuationPacket = {
      ...appliedPacket,
      _type: "ContinuationStarted",
      continuationMode: "steer"
    } as any;
    store.applyStreamPacket("trace-active", continuationPacket);
    store.applyStreamPacket("trace-active", continuationPacket);
    expect(store.executionHistory[0].items.filter((item) => item.type === "user").map((item) => item.content)).toEqual([
      "original prompt",
      "new guidance"
    ]);

    store.applyFollowupMailbox({
      _type: "FollowupMailbox",
      _version: 1,
      revision: 2,
      workspaceRoot: "C:/isolated/story",
      sessionId: "session-a",
      activeTraceId: "trace-active",
      paused: false,
      pauseReason: "",
      messages: [
        { ...steering, status: "sent" },
        { ...steering, messageId: "queued-1", content: "later", mode: "queued", status: "pending", sequence: 2 }
      ],
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString()
    });
    expect(store.followups.map((message) => message.messageId)).toEqual(["queued-1"]);
  });

  it("reconstructs missing continuation runs across current and legacy packet shapes", () => {
    const store = useAgentStore();
    store.currentSessionId = "";
    store.coomiStatus = {
      runtime: "coomi", installed: true, model: "model-a", providerId: "provider-a"
    } as any;

    store.applyStreamPacket("trace-default-session", {
      type: "ContinuationStarted",
      content: "resume queued prompt",
      messageId: "followup-default",
      mode: "queued",
      status: "dispatching",
      noRestorePoint: true,
      coomiStatus: { runtime: "coomi", installed: true, model: "model-b", providerId: "provider-b" }
    } as any);

    const defaultSessionRun = store.executionHistory.find((run) => run.traceId === "trace-default-session");
    expect(defaultSessionRun).toMatchObject({
      sessionId: "default",
      prompt: "resume queued prompt",
      status: "running",
      noRestorePoint: true,
      llmModel: "model-a",
      llmProvider: "provider-a"
    });
    expect(store.coomiStatus).toMatchObject({ model: "model-b", providerId: "provider-b" });
    expect(store.followups[0]).toMatchObject({ messageId: "followup-default", sessionId: "default" });

    store.currentSessionId = "session-current";
    store.coomiStatus = null;
    store.applyStreamPacket("trace-current-session", {
      _type: "ContinuationStarted",
      content: "resume current session",
      messageId: "followup-current",
      mode: "steer",
      status: "steering"
    } as any);
    expect(store.executionHistory.find((run) => run.traceId === "trace-current-session")).toMatchObject({
      sessionId: "session-current",
      llmModel: "",
      llmProvider: ""
    });

    store.applyStreamPacket("trace-explicit-session", {
      _type: "ContinuationStarted",
      content: "resume explicit session",
      sessionId: "session-explicit",
      createdAt: "2026-07-21T12:00:00Z",
      messageId: "followup-explicit",
      mode: "queued",
      status: "pending"
    } as any);
    expect(store.executionHistory.find((run) => run.traceId === "trace-explicit-session")).toMatchObject({
      sessionId: "session-explicit",
      createdAt: "2026-07-21T12:00:00Z"
    });

    const runCount = store.executionHistory.length;
    store.applyStreamPacket("trace-ignored-reasoning", { _type: "ReasoningChunk", content: "hidden" } as any);
    store.applyStreamPacket("trace-ignored-text", { _type: "TextChunk", content: "" } as any);
    store.applyStreamPacket("trace-ignored-event", { _type: "UnknownEvent" } as any);
    expect(store.executionHistory).toHaveLength(runCount);
  });
});

function latestRunForFollowup() {
  return {
    traceId: "trace-active", sessionId: "session-a", prompt: "previous", route: "coomi", agentMode: "coomi",
    llmModel: "", llmProvider: "", status: "completed", createdAt: "2026-07-21T11:00:00Z",
    updatedAt: "2026-07-21T11:00:00Z", lastAction: "chat", reply: "answer", trace: null, audit: [], events: [], tasks: [],
    changeLedger: { traceId: "trace-active", sessionId: "session-a", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
    items: [], errorMessage: "", errorCode: null
  };
}
