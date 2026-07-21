import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const api = vi.hoisted(() => ({
  streamAgentPrompt: vi.fn(),
  fetchAgentSessions: vi.fn(),
  fetchAgentHistory: vi.fn(),
  fetchAgentCoomiStatus: vi.fn(),
  submitAgentRunCommitDecision: vi.fn(),
  rollbackLatestExecution: vi.fn(),
  clearConversation: vi.fn(),
  deleteAgentSession: vi.fn(),
  cycleAgentCoomiPermission: vi.fn(),
  setAgentCoomiPermission: vi.fn(),
  resolveAgentCoomiApproval: vi.fn()
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

import { useAgentStore } from "@/stores/agent";
import { AgentApiError } from "@/api/agent";

function sessions(items: unknown[] = []) {
  return { data: { items }, trace: null, audit: [] };
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  api.fetchAgentSessions.mockResolvedValue(sessions([]));
  api.fetchAgentCoomiStatus.mockResolvedValue({
    data: { runtime: "coomi", installed: true, model: "fake", permissionMode: "full_access" }
  });
  git.refreshSummary.mockResolvedValue(undefined);
  api.rollbackLatestExecution.mockResolvedValue({
    data: { rolledBack: false, sessionId: "default", removedTraceId: "", prompt: "" }
  });
});

describe("agent store streaming", () => {
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
    expect(store.isRunning).toBe(false);
    expect(store.lastReply).toBe("第一段输出");
    expect(store.executionHistory[0].status).toBe("completed");
    const phaseItems = store.executionHistory[0].items.filter((item) => item.title.includes("意图") || item.content.includes("意图"));
    expect(phaseItems).toHaveLength(1);
    expect(phaseItems[0].content).toContain("1.0s");
    const assistant = store.executionHistory[0].items.find((item) => item.type === "assistant");
    expect(assistant?.content).toBe("第一段输出");
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

    expect(api.rollbackLatestExecution).toHaveBeenCalledWith("session-a");
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
});
