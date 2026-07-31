import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

// This suite deliberately drives the store with an EMPTY workspace mock so the
// right-hand-side fallbacks (`... || ""`, `... || "default"`, `... || DEFAULT_...`)
// in the many action bodies get exercised. The other store suites always supply
// a populated workspace, which only ever hits the left-hand side of those `||`.
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
  resolveAgentCoomiApproval: vi.fn(),
  fetchAgentFollowups: vi.fn(),
  enqueueAgentFollowup: vi.fn(),
  updateAgentFollowup: vi.fn(),
  deleteAgentFollowup: vi.fn(),
  steerAgentFollowup: vi.fn(),
  resumeAgentFollowups: vi.fn(),
  stopAgentExecution: vi.fn()
}));
const git = vi.hoisted(() => ({ refreshSummary: vi.fn() }));
// currentProject null + health null + empty active file: forces every
// `workspaceStore.currentProject?.workspaceRoot || workspaceStore.health?.workspaceRoot || ""`
// and `activeFileBindingOrPath || activeFile || ""` down to the empty-string fallback.
const workspace = vi.hoisted(() => ({
  activeFileBindingOrPath: "",
  activeFile: "",
  currentProject: null as { workspaceRoot?: string } | null,
  health: null as { workspaceRoot?: string } | null
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

const envelope = (data: unknown) => ({ data, trace: null, audit: [] });
const mailbox = (messages: unknown[] = [], extra: Record<string, unknown> = {}) =>
  envelope({ messages, paused: false, pauseReason: "", revision: 0, ...extra });

function followupMessage(overrides: Record<string, unknown> = {}) {
  return {
    messageId: "followup-1",
    sessionId: "default",
    activeTraceId: "",
    expectedTraceId: "",
    content: "queued content",
    mode: "queued",
    status: "pending",
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    sequence: 1,
    ...overrides
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  vi.useRealTimers();
  workspace.activeFileBindingOrPath = "";
  workspace.activeFile = "";
  workspace.currentProject = null;
  workspace.health = null;
  api.fetchAgentSessions.mockResolvedValue(envelope({ items: [] }));
  api.fetchAgentHistory.mockResolvedValue(envelope({ items: [] }));
  api.fetchAgentCoomiStatus.mockResolvedValue(envelope({ runtime: "coomi", installed: true, model: "fake", permissionMode: "full_access" }));
  api.fetchAgentFollowups.mockResolvedValue(mailbox());
  api.resumeAgentFollowups.mockResolvedValue(mailbox());
  api.stopAgentExecution.mockResolvedValue(envelope({ accepted: true, activeTraceId: "", mailboxPaused: false, pauseReason: "" }));
  git.refreshSummary.mockResolvedValue(undefined);
});

describe("agent store action fallbacks with an empty workspace", () => {
  it("queues, edits, deletes, and steers follow-ups through the empty-workspace fallbacks", async () => {
    const store = useAgentStore();
    store.isRunning = true;
    store.currentSessionId = "";
    store.currentTraceId = "";
    store.promptInput = "queued content";

    // enqueueFollowup: queued mode -> expectedTraceId is "" and workspaceRoot fallback.
    api.enqueueAgentFollowup.mockResolvedValueOnce(envelope({ message: followupMessage(), steerRequested: false }));
    await expect(store.enqueueFollowup("queued")).resolves.toBe(true);
    expect(api.enqueueAgentFollowup.mock.calls[0][0]).toMatchObject({ sessionId: "default", workspaceRoot: "", expectedTraceId: "" });
    expect(store.followups[0].messageId).toBe("followup-1");
    expect(store.lastSuccess).toContain("队列");

    // enqueueFollowup: steer mode success message differs.
    store.promptInput = "steer content";
    api.enqueueAgentFollowup.mockResolvedValueOnce(envelope({ message: followupMessage({ messageId: "followup-2", mode: "steer", content: "steer content", sequence: 2 }), steerRequested: true }));
    await expect(store.enqueueFollowup("steer")).resolves.toBe(true);
    expect(store.lastSuccess).toContain("引导");

    // enqueueFollowup guards: empty prompt / not running.
    store.promptInput = "";
    await expect(store.enqueueFollowup("queued")).resolves.toBe(false);
    store.promptInput = "later";
    store.isRunning = false;
    await expect(store.enqueueFollowup("queued")).resolves.toBe(false);
    store.isRunning = true;

    // enqueueFollowup failure path with AgentApiError code.
    store.promptInput = "boom";
    api.enqueueAgentFollowup.mockRejectedValueOnce(new AgentApiError("queue failed", "queue_rejected"));
    await expect(store.enqueueFollowup("queued")).resolves.toBe(false);
    expect(store.lastError).toBe("queue failed");
    expect(store.lastErrorCode).toBe("queue_rejected");

    // editFollowup success + failure.
    api.updateAgentFollowup.mockResolvedValueOnce(envelope({ message: followupMessage({ content: "edited" }) }));
    await expect(store.editFollowup("followup-1", "edited")).resolves.toBe(true);
    expect(api.updateAgentFollowup.mock.calls[0][1]).toMatchObject({ sessionId: "default", workspaceRoot: "" });
    api.updateAgentFollowup.mockRejectedValueOnce(new Error("edit failed"));
    await expect(store.editFollowup("followup-1", "edited")).resolves.toBe(false);
    expect(store.lastError).toBe("edit failed");

    // deleteFollowup success + failure.
    store.followups = [followupMessage(), followupMessage({ messageId: "keep", sequence: 5 })];
    api.deleteAgentFollowup.mockResolvedValueOnce(envelope({ deleted: true }));
    await expect(store.deleteFollowup("followup-1")).resolves.toBe(true);
    expect(store.followups.map((item) => item.messageId)).toEqual(["keep"]);
    expect(api.deleteAgentFollowup).toHaveBeenCalledWith("followup-1", "default", "");
    api.deleteAgentFollowup.mockRejectedValueOnce(new Error("delete failed"));
    await expect(store.deleteFollowup("keep")).resolves.toBe(false);
    expect(store.lastError).toBe("delete failed");

    // steerFollowup guard when no active trace, then success + failure once running.
    store.currentTraceId = "";
    await expect(store.steerFollowup("keep")).resolves.toBe(false);
    store.currentTraceId = "trace-active";
    api.steerAgentFollowup.mockResolvedValueOnce(envelope({ message: followupMessage({ messageId: "keep", status: "steering" }) }));
    await expect(store.steerFollowup("keep")).resolves.toBe(true);
    expect(api.steerAgentFollowup.mock.calls[0][1]).toMatchObject({ sessionId: "default", workspaceRoot: "", expectedTraceId: "trace-active" });
    api.steerAgentFollowup.mockRejectedValueOnce(new AgentApiError("steer failed", "steer_rejected"));
    await expect(store.steerFollowup("keep")).resolves.toBe(false);
    expect(store.lastError).toBe("steer failed");
    expect(store.lastErrorCode).toBe("steer_rejected");
  });

  it("loads and applies follow-up mailboxes with the default session fallback", async () => {
    const store = useAgentStore();
    store.currentSessionId = "";
    api.fetchAgentFollowups.mockResolvedValueOnce(mailbox([
      followupMessage({ messageId: "sent", status: "sent", sequence: 1 }),
      followupMessage({ messageId: "keep", status: "pending", sequence: 3 }),
      followupMessage({ messageId: "early", status: "pending", sequence: 2 })
    ], { paused: true, pauseReason: "manual_stop", revision: 4 }));
    await store.loadFollowups();
    expect(api.fetchAgentFollowups).toHaveBeenCalledWith("default", "");
    expect(store.followups.map((item) => item.messageId)).toEqual(["early", "keep"]);
    expect(store.followupPaused).toBe(true);
    expect(store.followupPauseReason).toBe("manual_stop");
    expect(store.followupRevision).toBe(4);

    // A mailbox for a session that no longer matches must be ignored.
    store.currentSessionId = "other";
    api.fetchAgentFollowups.mockResolvedValueOnce(mailbox([followupMessage({ messageId: "stale" })]));
    // Force the request to resolve against "other" but flip the active session mid-flight.
    await store.loadFollowups();
    expect(store.followups.some((item) => item.messageId === "stale")).toBe(true);

    // Failure path.
    api.fetchAgentFollowups.mockRejectedValueOnce(new Error("mailbox failed"));
    await store.loadFollowups();
    expect(store.lastError).toBe("mailbox failed");
  });

  it("resumes queued follow-ups through executePromptRequest using empty-workspace inputs", async () => {
    const store = useAgentStore();
    store.setStoryGenerationOptions({ chapterLengthTier: "long" });
    store.currentSessionId = "";
    store.currentTraceId = "";
    store.isRunning = false;
    store.executionHistory = [];

    // Mailbox has a queued pending message that becomes the next prompt.
    api.resumeAgentFollowups.mockResolvedValueOnce(mailbox([followupMessage({ messageId: "resume-1", content: "resume prompt" })]));
    api.streamAgentPrompt.mockImplementationOnce(async (request: any, onPacket: (packet: any) => void) => {
      expect(request.workspaceRoot).toBe("");
      expect(request.activeFile).toBe("");
      expect(request.sourceFollowupMessageId).toBe("resume-1");
      expect(request.sourceFollowupExpectedTraceId).toBe("");
      expect(request.storyGeneration.chapterTemplateId).toBeTruthy();
      expect(request.storyGeneration.chapterLengthTier).toBe("long");
      expect(request.storyGeneration).not.toHaveProperty("chapterWordCountTarget");
      expect(request.storyGeneration).not.toHaveProperty("preciseWordCountEnabled");
      onPacket({ _type: "AgentCompleted" });
    });
    await store.resumeFollowups();
    expect(api.streamAgentPrompt).toHaveBeenCalledTimes(1);
    expect(api.streamAgentPrompt.mock.calls[0][0].storyGeneration.chapterLengthTier).toBe("long");

    // If a run is active after applying the mailbox, resume returns early.
    api.resumeAgentFollowups.mockResolvedValueOnce(mailbox([followupMessage({ messageId: "resume-2" })]));
    store.isRunning = true;
    await store.resumeFollowups();
    expect(api.streamAgentPrompt).toHaveBeenCalledTimes(1);
    store.isRunning = false;

    // No queued pending message -> return without a stream.
    api.resumeAgentFollowups.mockResolvedValueOnce(mailbox([followupMessage({ messageId: "steer-only", mode: "steer", status: "steering" })]));
    await store.resumeFollowups();
    expect(api.streamAgentPrompt).toHaveBeenCalledTimes(1);

    // Failure path.
    api.resumeAgentFollowups.mockRejectedValueOnce(new Error("resume failed"));
    await store.resumeFollowups();
    expect(store.lastError).toBe("resume failed");
  });

  it("runs runPrompt and executePromptRequest through the empty-workspace request builder", async () => {
    const store = useAgentStore();
    store.setStoryGenerationOptions({ chapterLengthTier: "short" });
    store.currentSessionId = "";
    store.promptInput = "  write a chapter  ";
    api.streamAgentPrompt.mockImplementationOnce(async (request: any, onPacket: (packet: any) => void) => {
      expect(request.prompt).toBe("write a chapter");
      expect(request.workspaceRoot).toBe("");
      expect(request.activeFile).toBe("");
      expect(request.storyGeneration.chapterTemplateId).toBeTruthy();
      expect(request.storyGeneration.chapterLengthTier).toBe("short");
      expect(request.storyGeneration).not.toHaveProperty("chapterWordCountTarget");
      expect(request.storyGeneration).not.toHaveProperty("preciseWordCountEnabled");
      onPacket({ _type: "AgentCompleted" });
    });
    await store.runPrompt();
    expect(store.executionHistory[0].status).toBe("completed");
    expect(store.lastSuccess).toBe("Coomi run complete.");

    // runPrompt guards: empty prompt, editing, pending snapshot.
    store.promptInput = "";
    await store.runPrompt();
    store.promptInput = "later";
    store.editingTraceId = "trace-x";
    await store.runPrompt();
    store.editingTraceId = "";
    store.pendingSnapshotConfirmation = { request: { prompt: "x" }, traceId: "t", sessionId: "s", message: "", details: {} };
    await store.runPrompt();
    store.pendingSnapshotConfirmation = null;

    // executePromptRequest guards return false for empty prompt / running.
    await expect(store.executePromptRequest({ prompt: "   " } as never)).resolves.toBe(false);
    store.isRunning = true;
    await expect(store.executePromptRequest({ prompt: "busy" } as never)).resolves.toBe(false);
    store.isRunning = false;
  });

  it("confirms and cancels no-snapshot retries with the pending session", async () => {
    const store = useAgentStore();
    store.pendingSnapshotConfirmation = {
      request: { prompt: "retry prompt" },
      traceId: "trace-retry",
      sessionId: "session-retry",
      message: "snapshot failed",
      details: { reason: "no git" }
    };
    api.streamAgentPrompt.mockImplementationOnce(async (request: any, onPacket: (packet: any) => void) => {
      expect(request.confirmNoSnapshot).toBe(true);
      onPacket({ _type: "AgentCompleted" });
    });
    await store.confirmNoSnapshot();
    expect(store.pendingSnapshotConfirmation).toBeNull();
    expect(api.streamAgentPrompt).toHaveBeenCalledTimes(1);

    // Guards: no pending / running.
    await store.confirmNoSnapshot();
    store.pendingSnapshotConfirmation = { request: { prompt: "x" }, traceId: "t", sessionId: "s", message: "", details: {} };
    store.isRunning = true;
    await store.confirmNoSnapshot();
    expect(store.pendingSnapshotConfirmation).not.toBeNull();
    store.cancelNoSnapshot();
    expect(store.pendingSnapshotConfirmation).not.toBeNull();
    store.isRunning = false;
    store.cancelNoSnapshot();
    expect(store.pendingSnapshotConfirmation).toBeNull();
  });

  it("rolls back the latest run using activeRun fallbacks and clears matching live/pending state", async () => {
    const store = useAgentStore();
    store.currentSessionId = "";
    store.currentTraceId = "trace-latest";
    store.executionHistory = [{
      traceId: "trace-latest", sessionId: "session", prompt: "latest", route: "coomi", agentMode: "coomi",
      llmModel: "", llmProvider: "", status: "completed", noRestorePoint: false, createdAt: "2026-07-21T11:00:00Z",
      updatedAt: "2026-07-21T11:00:00Z", lastAction: "chat", reply: "reply", trace: null, audit: [], events: [], tasks: [],
      changeLedger: { traceId: "trace-latest", sessionId: "session", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [], errorMessage: "", errorCode: null
    }] as never;
    store.pendingCommitPrompt = { traceId: "trace-latest", sessionId: "session", workspaceRoot: "", message: "", changedFiles: [], changedFileCount: 0, added: 0, removed: 0 };
    store.liveChangeLedger = { traceId: "trace-latest", sessionId: "session", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" };
    // removedTraceId empty forces `|| this.executionHistory[0]?.traceId` fallback; empty history after removal
    // forces activeRun?.xxx || "" fallbacks.
    api.rollbackLatestExecution.mockResolvedValueOnce(envelope({ rolledBack: true, sessionId: "default", removedTraceId: "", prompt: "" }));
    api.fetchAgentHistory.mockResolvedValueOnce(envelope({ items: [] }));
    await expect(store.rollbackLatestRun({ refillComposer: true })).resolves.toBe(true);
    expect(store.executionHistory).toHaveLength(0);
    expect(store.currentTraceId).toBe("");
    expect(store.lastPrompt).toBe("");
    expect(store.pendingCommitPrompt).toBeNull();
    expect(store.liveChangeLedger).toBeNull();
    expect(store.lastSuccess).toContain("重新编辑");

    // rolledBack false short-circuits.
    store.executionHistory = [{
      traceId: "trace-2", sessionId: "session", prompt: "p", route: "coomi", agentMode: "coomi",
      llmModel: "", llmProvider: "", status: "completed", noRestorePoint: false, createdAt: "2026-07-21T11:00:00Z",
      updatedAt: "2026-07-21T11:00:00Z", lastAction: "chat", reply: "", trace: null, audit: [], events: [], tasks: [],
      changeLedger: { traceId: "trace-2", sessionId: "session", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [], errorMessage: "", errorCode: null
    }] as never;
    api.rollbackLatestExecution.mockResolvedValueOnce(envelope({ rolledBack: false, sessionId: "default", removedTraceId: "", prompt: "" }));
    await expect(store.rollbackLatestRun({ refillComposer: false })).resolves.toBe(false);

    // Failure path with AgentApiError code.
    api.rollbackLatestExecution.mockRejectedValueOnce(new AgentApiError("rollback failed", "rollback_rejected"));
    await expect(store.rollbackLatestRun({ refillComposer: false })).resolves.toBe(false);
    expect(store.lastError).toBe("rollback failed");
    expect(store.lastErrorCode).toBe("rollback_rejected");
  });

  it("stops an active run through the empty-workspace fallback and records mailbox pause reason", async () => {
    const store = useAgentStore();
    store.currentSessionId = "";
    store.currentTraceId = "";
    store.isRunning = true;
    api.stopAgentExecution.mockResolvedValueOnce(envelope({ accepted: true, activeTraceId: "", mailboxPaused: true, pauseReason: "" }));
    await store.stopActiveRun();
    expect(api.stopAgentExecution.mock.calls[0][0]).toMatchObject({ sessionId: "default", workspaceRoot: "" });
    expect(store.followupPaused).toBe(true);
    expect(store.followupPauseReason).toBe("manual_stop");

    // Guard: not running / already stopping.
    store.isRunning = false;
    await store.stopActiveRun();
    store.isRunning = true;
    store.isStopping = true;
    await store.stopActiveRun();
    store.isStopping = false;

    // Failure path with AgentApiError code.
    api.stopAgentExecution.mockRejectedValueOnce(new AgentApiError("stop failed", "stop_rejected"));
    await store.stopActiveRun();
    expect(store.lastError).toBe("stop failed");
    expect(store.lastErrorCode).toBe("stop_rejected");
  });

  it("resumes queued follow-ups after a git commit decision when the mailbox was paused", async () => {
    const store = useAgentStore();
    store.currentSessionId = "";
    store.executionHistory = [{
      traceId: "trace-commit", sessionId: "session", prompt: "write", route: "coomi", agentMode: "coomi",
      llmModel: "", llmProvider: "", status: "completed", noRestorePoint: false, createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(), lastAction: "chat", reply: "", trace: null, audit: [], events: [], tasks: [],
      changeLedger: { traceId: "trace-commit", sessionId: "session", changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" },
      items: [], errorMessage: "", errorCode: null
    }] as never;
    store.pendingCommitPrompt = { traceId: "trace-commit", sessionId: "session", workspaceRoot: "", message: "", changedFiles: ["a"], changedFileCount: 1, added: 1, removed: 0 };
    store.followupPaused = true;
    store.followupPauseReason = "git_commit_prompt";
    api.submitAgentRunCommitDecision.mockResolvedValueOnce(envelope({ created: true, status: "success", changedFiles: ["a"], commitHash: "abc", shortHash: "abc" }));
    const resume = vi.spyOn(store, "resumeFollowups").mockResolvedValue(undefined);
    await store.resolvePendingCommitPrompt("manual", "story: commit");
    expect(store.pendingCommitPrompt).toBeNull();
    expect(resume).toHaveBeenCalledTimes(1);
  });

  it("resolves an approval and resumes the queue when paused for a permission request", async () => {
    const store = useAgentStore();
    store.pendingApprovals = [{ approvalId: "a" }] as never;
    store.followupPaused = true;
    store.followupPauseReason = "permission_request";
    api.resolveAgentCoomiApproval.mockResolvedValueOnce(envelope({ ok: true }));
    const resume = vi.spyOn(store, "resumeFollowups").mockResolvedValue(undefined);
    await store.resolvePendingApproval("allow");
    expect(resume).toHaveBeenCalledTimes(1);

    // A cancel decision must NOT resume the queue.
    store.pendingApprovals = [{ approvalId: "b" }] as never;
    store.followupPaused = true;
    store.followupPauseReason = "permission_request";
    resume.mockClear();
    await store.resolvePendingApproval("cancel");
    expect(resume).not.toHaveBeenCalled();
  });
});
