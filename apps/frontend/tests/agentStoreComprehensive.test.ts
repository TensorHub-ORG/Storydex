import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const api = vi.hoisted(() => ({
  streamAgentPrompt: vi.fn(), fetchAgentSessions: vi.fn(), fetchAgentHistory: vi.fn(),
  fetchAgentCoomiStatus: vi.fn(), submitAgentRunCommitDecision: vi.fn(), clearConversation: vi.fn(),
  deleteAgentSession: vi.fn(), cycleAgentCoomiPermission: vi.fn(), setAgentCoomiPermission: vi.fn(),
  resolveAgentCoomiApproval: vi.fn()
}));
const chapterApi = vi.hoisted(() => ({ fetchStoryChapterTemplates: vi.fn() }));
const git = vi.hoisted(() => ({ refreshSummary: vi.fn() }));
const workspace = vi.hoisted(() => ({
  activeFileBindingOrPath: "chapters/001.md", activeFile: "chapters/001.md",
  currentProject: { workspaceRoot: "C:/isolated/story" }, health: null
}));

vi.mock("@/api/agent", () => ({
  AgentApiError: class AgentApiError extends Error {
    code: string | null;
    constructor(message: string, code: string | null = null) { super(message); this.code = code; }
  },
  ...api
}));
vi.mock("@/api/workspace", () => chapterApi);
vi.mock("@/stores/git", () => ({ useGitStore: () => git }));
vi.mock("@/stores/workspace", () => ({ useWorkspaceStore: () => workspace }));
vi.mock("@/api/client", () => ({ describeTransportError: (error: unknown, fallback: string) => error instanceof Error ? error.message : fallback }));

import { AgentApiError } from "@/api/agent";
import { useAgentStore } from "@/stores/agent";

const envelope = (data: unknown) => ({ data, trace: null, audit: [] });
const now = () => new Date().toISOString();
function emptyLedger(traceId = "trace", sessionId = "session") {
  return { traceId, sessionId, changedFiles: [], changedFileCount: 0, added: 0, removed: 0, commitHash: "", shortHash: "", diffSource: "", updatedAt: "" };
}
function run(traceId = "trace", sessionId = "session") {
  return {
    traceId, sessionId, prompt: "prompt", route: "coomi", agentMode: "coomi", llmModel: "fake", llmProvider: "fake",
    status: "running", createdAt: now(), updatedAt: now(), lastAction: "chat", reply: "old", trace: null,
    audit: [], events: [], tasks: [], changeLedger: emptyLedger(traceId, sessionId), items: [], errorMessage: "", errorCode: null
  } as never;
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  vi.useFakeTimers();
  api.fetchAgentCoomiStatus.mockResolvedValue(envelope({ runtime: "coomi", installed: true, model: "fake", permissionMode: "full_access", contextWindow: 100, usedTokens: 20 }));
  api.fetchAgentSessions.mockResolvedValue(envelope({ items: [] }));
  api.fetchAgentHistory.mockResolvedValue(envelope({ items: [] }));
  api.cycleAgentCoomiPermission.mockResolvedValue(envelope({ permissionMode: "read_only", permissionLabel: "Read" }));
  api.setAgentCoomiPermission.mockResolvedValue(envelope({ permissionMode: "full_access", permissionLabel: "Full" }));
  api.resolveAgentCoomiApproval.mockResolvedValue(envelope({ ok: true }));
  api.clearConversation.mockResolvedValue(envelope({ cleared: true }));
  api.deleteAgentSession.mockResolvedValue(envelope({ deleted: true }));
  api.submitAgentRunCommitDecision.mockResolvedValue(envelope({ created: false, reason: "skipped", changedFiles: [] }));
  chapterApi.fetchStoryChapterTemplates.mockResolvedValue(envelope({ items: [] }));
  git.refreshSummary.mockResolvedValue(undefined);
});

describe("agent store lifecycle and normalization", () => {
  it("covers getters, reset variants, session creation, trace selection, and option clamping", () => {
    const store = useAgentStore();
    expect(store.statusLabel).toBeTruthy();
    store.lastError = "bad"; expect(store.statusLabel).toContain("Coomi");
    store.lastError = ""; store.executionHistory = [run()]; store.currentTraceId = "trace";
    for (const status of ["committed", "discarded", "completed", "cancelled", "stopped", "failed"] as const) {
      store.executionHistory[0].status = status; expect(store.statusLabel).toContain("Coomi");
    }
    store.selectTraceRun("trace"); expect(store.activeTraceRun?.traceId).toBe("trace");
    store.coomiStatus = { permissionMode: "read_only" } as never; expect(store.permissionModeLabel).toBe("read_only");
    store.pendingApprovals = [{ approvalId: "a" } as never]; expect(store.pendingApproval?.approvalId).toBe("a");
    store.isRunning = true; store.createNewSession(); expect(store.currentSessionId).toBe("");
    store.isRunning = false; store.createNewSession(); expect(store.currentSessionId).toMatch(/^session-/);
    store.availableSessions = [{ sessionId: "old" } as never];
    store.resetSession({ clearSessionId: true, clearAvailableSessions: true });
    expect(store.currentSessionId).toBe(""); expect(store.availableSessions).toEqual([]);
    store.setStoryGenerationOptions({ fragmentCount: -1, fragmentWordCount: 99999, chapterTemplateId: "" });
    expect(store.storyFragmentCount).toBe(1); expect(store.storyFragmentWordCount).toBe(20000); expect(store.storyChapterTemplateId).toBeTruthy();
  });

  it("refreshes status and permission modes across direct and fallback branches", async () => {
    const store = useAgentStore();
    await store.refreshCoomiStatus();
    expect(store.contextWindow).toBe(100); expect(store.usageRatio).toBe(0.2);
    await store.cycleCoomiPermission(); expect(store.coomiStatus?.permissionMode).toBe("read_only");
    await store.setCoomiPermission("full_access"); expect(store.coomiStatus?.permissionMode).toBe("full_access");
    store.coomiStatus = null;
    api.cycleAgentCoomiPermission.mockResolvedValueOnce(envelope({ permissionMode: "" }));
    await store.cycleCoomiPermission(); expect(api.fetchAgentCoomiStatus).toHaveBeenCalled();
    store.coomiStatus = null;
    api.setAgentCoomiPermission.mockResolvedValueOnce(envelope({ permissionMode: "" }));
    await store.setCoomiPermission("read_only"); expect(store.coomiStatus).not.toBeNull();
    api.fetchAgentCoomiStatus.mockRejectedValueOnce(new Error("offline"));
    await store.refreshCoomiStatus(); expect(store.coomiStatus).toBeNull();
  });

  it("handles approval selection, missing approvals, and transport failure", async () => {
    const store = useAgentStore();
    await store.resolvePendingApproval("allow"); expect(api.resolveAgentCoomiApproval).not.toHaveBeenCalled();
    store.pendingApprovals = [{ approvalId: "a" }, { approvalId: "b" }] as never;
    await store.resolvePendingApproval("answer", { text: "yes" }, "b");
    expect(store.pendingApprovals.map((item) => item.approvalId)).toEqual(["a"]);
    api.resolveAgentCoomiApproval.mockRejectedValueOnce(new Error("failed"));
    await store.resolvePendingApproval("deny"); expect(store.lastError).toBe("failed");
  });

  it("loads templates with caching, invalid selection, missing endpoint, and errors", async () => {
    const store = useAgentStore();
    store.storyChapterTemplatesLoading = true; await store.loadStoryChapterTemplates(); expect(chapterApi.fetchStoryChapterTemplates).not.toHaveBeenCalled();
    store.storyChapterTemplatesLoading = false;
    store.storyChapterTemplates = [{ id: "cached" }] as never; await store.loadStoryChapterTemplates(); expect(chapterApi.fetchStoryChapterTemplates).not.toHaveBeenCalled();
    chapterApi.fetchStoryChapterTemplates.mockResolvedValueOnce(envelope({ items: [null, { id: "default_chapter_directory", name: "Default" }, { id: "custom", name: "Custom" }] }));
    store.storyChapterTemplateId = "missing"; await store.loadStoryChapterTemplates({ force: true });
    expect(store.storyChapterTemplateId).toBe("default_chapter_directory");
    chapterApi.fetchStoryChapterTemplates.mockRejectedValueOnce(Object.assign(new Error("missing"), { response: { status: 404 } }));
    await store.loadStoryChapterTemplates({ force: true }); expect(store.storyChapterTemplatesError).toBe("");
    chapterApi.fetchStoryChapterTemplates.mockRejectedValueOnce(new Error("broken"));
    await store.loadStoryChapterTemplates({ force: true }); expect(store.storyChapterTemplatesError).toContain("broken");
  });

  it("normalizes context metrics and ignores invalid values", () => {
    const store = useAgentStore();
    store.applyContextMetrics(null);
    store.applyContextMetrics({ context_window: 200, used_tokens: 50, cumulative_tokens: 70, compact_threshold: 0.8, warning_threshold: 0.7, usage_ratio: 0.25, compression_status: "warning" });
    expect(store.contextWindow).toBe(200); expect(store.usedTokens).toBe(50); expect(store.compressionStatus).toBe("warning");
    store.applyContextMetrics({ contextWindow: -1, usedTokens: -1, usageRatio: -1, compressionStatus: "" });
    expect(store.contextWindow).toBe(200);
    store.applyCoomiStatusContext(null);
  });

  it("covers sessions, history, selection, clear and delete success/failure branches", async () => {
    const store = useAgentStore();
    api.fetchAgentSessions.mockResolvedValueOnce(envelope({ items: [null, { sessionId: " s1 ", firstPrompt: "p", traceCount: 2 }] }));
    await store.loadSessions(); expect(store.currentSessionId).toBe(" s1 ");
    api.fetchAgentHistory.mockResolvedValueOnce(envelope({ items: [null, { traceId: "t", sessionId: "s1", prompt: "p", reply: "r", status: "unknown", events: "bad", audit: "bad", tasks: "bad" }] }));
    await store.loadHistory(); expect(store.lastReply).toBe("r");
    await store.selectSession(""); await store.selectSession("s1");
    api.fetchAgentHistory.mockResolvedValueOnce(envelope({ items: [] })); await store.selectSession("s2"); expect(store.currentSessionId).toBe("s2");
    await store.clearConversation(); expect(api.clearConversation).toHaveBeenCalledWith("s2");
    store.isRunning = true; await store.deleteSession("x"); expect(api.deleteAgentSession).not.toHaveBeenCalled();
    store.isRunning = false; store.currentSessionId = "active"; store.availableSessions = [{ sessionId: "active" }, { sessionId: "other" }] as never;
    await store.deleteSession("active"); expect(api.deleteAgentSession).toHaveBeenCalledWith("active");
    api.fetchAgentSessions.mockRejectedValueOnce(new Error("sessions failed")); await store.loadSessions(); expect(store.lastError).toContain("sessions failed");
    api.fetchAgentHistory.mockRejectedValueOnce(new Error("history failed")); await store.loadHistory(); expect(store.lastError).toContain("history failed");
    api.clearConversation.mockRejectedValueOnce(new Error("clear failed")); await store.clearConversation(); expect(store.lastError).toContain("clear failed");
    api.deleteAgentSession.mockRejectedValueOnce(new Error("delete failed")); await store.deleteSession("other"); expect(store.lastError).toContain("delete failed");
  });
});

describe("agent packet state machine", () => {
  it("processes every stream packet family and task terminal state", () => {
    const store = useAgentStore();
    store.executionHistory = [run()];
    const packets = [
      { _type: "TextChunk", content: "<tool_call>hidden</tool_call>visible" },
      { _type: "TextReset", preserve_visible: true }, { _type: "TextReset", preserve_visible: false },
      { _type: "UsageUpdate", usage: { prompt_tokens: 10, completion_tokens: 5 } },
      { _type: "CompressionEvent", summary: "compact", usedTokens: 30 },
      { _type: "TurnContract", status: "ready", intentFrame: { primary: "chat" }, turnPlan: { requiresChapterTemplateSelection: true, fragmentCount: 2, fragmentWordCount: 1000 }, skillRegistry: { skillCount: 1 }, toolRegistry: { toolCount: 2 }, contextAssembly: { budget: { blockCount: 3, totalChars: 4 } } },
      { _type: "PermissionRequest", approval_id: "approval", request_type: "question", questions: [{ question: "Continue?", options: ["yes"] }] },
      { _type: "TaskPlanCreated", tasks: [{ id: "one", title: "One", status: "pending" }, null] },
      { _type: "TaskStarted", task_id: "one", title: "One" },
      { _type: "TaskCompleted", task_id: "one" },
      { _type: "TaskFailed", task_id: "two", title: "Two" },
      { _type: "TaskSkipped", task_id: "three", title: "Three" },
      { _type: "ToolDone", tool_name: "write_file", tool_call_id: "call", is_error: false, result_preview: "wrote chapters/002.md", input: { path: "chapters/002.md" } },
      { _type: "ToolDone", tool_name: "read_file", tool_call_id: "read", is_error: true, result_preview: "bad" },
      { _type: "GitAutoCommit", status: "ok", created: false, changedFiles: ["a.md", "chapters/002.md"], changedFileCount: 2 },
      { _type: "GitCommitPrompt", message: "commit?", changedFiles: ["a.md"], changedFileCount: 1 },
      { _type: "GitCommitResult", created: true, commitHash: "abc" },
    ];
    for (const packet of packets) store.applyStreamPacket("trace", packet as never);
    expect(store.executionHistory[0].changeLedger.changedFiles).toContain("a.md");
    expect(store.executionHistory[0].status).toBe("committed");
    store.applyStreamPacket("missing", { _type: "TextChunk", content: "x" } as never);
    store.applyStreamPacket("trace", { _type: "TextChunk", content: "<tool_call>only hidden</tool_call>" } as never);
    store.applyStreamPacket("trace", { _type: "GitCommitResult", status: "error", message: "git failed" } as never);
    expect(store.executionHistory[0].status).toBe("failed");
    store.applyStreamPacket("trace", { _type: "AgentCompleted" } as never); expect(store.executionHistory[0].status).toBe("completed");
    store.applyStreamPacket("trace", { _type: "AgentCancelled" } as never); expect(store.executionHistory[0].status).toBe("cancelled");
    store.applyStreamPacket("trace", { _type: "AgentError", message: "boom", error_type: "provider" } as never); expect(store.executionHistory[0].errorCode).toBe("provider");
  });

  it("covers finish/upsert ordering and missing run branches", () => {
    const store = useAgentStore();
    store.finishRun("missing", "completed");
    const old = run("old"); old.updatedAt = "2020-01-01T00:00:00Z";
    const newer = run("new"); newer.updatedAt = "2021-01-01T00:00:00Z";
    store.upsertExecutionRun(old); store.upsertExecutionRun(newer); store.upsertExecutionRun({ ...old, reply: "updated", updatedAt: "2022-01-01T00:00:00Z" } as never);
    expect(store.executionHistory[0].traceId).toBe("old");
    store.finishRun("old", "failed", "bad", "code"); expect(store.executionHistory[0].errorCode).toBe("code");
  });
});

describe("commit and cancellation behavior", () => {
  it("uses auto fallback, reports retry failure, and clears progress timers", async () => {
    const store = useAgentStore();
    store.executionHistory = [run()];
    store.pendingCommitPrompt = { traceId: "trace", sessionId: "session", workspaceRoot: "C:/isolated/story", message: "commit", changedFiles: ["a"], changedFileCount: 1, added: 1, removed: 0 };
    let rejectInitial!: (reason: unknown) => void;
    api.submitAgentRunCommitDecision.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectInitial = reject; }))
      .mockResolvedValueOnce(envelope({ created: true, commitHash: "abc", changedFiles: ["a"] }));
    const pending = store.resolvePendingCommitPrompt("auto");
    await vi.advanceTimersByTimeAsync(2200); expect(store.commitActionLabel).toBeTruthy();
    rejectInitial({ response: { status: 502, data: { error: { message: "commit message generation failed" } } } });
    await pending; expect(api.submitAgentRunCommitDecision).toHaveBeenCalledTimes(2);
    store.pendingCommitPrompt = { traceId: "trace", sessionId: "session", workspaceRoot: "root", message: "commit", changedFiles: [], changedFileCount: 0, added: 0, removed: 0 };
    api.submitAgentRunCommitDecision.mockRejectedValueOnce({ response: { status: 502, data: { error: { message: "commit message generation failed" } } } }).mockRejectedValueOnce(new Error("retry failed"));
    await store.resolvePendingCommitPrompt("auto"); expect(store.lastError).toBe("retry failed");
    store.pendingCommitPrompt = null; await store.resolvePendingCommitPrompt("skip");
    vi.runAllTimers(); expect(store.isCommittingGit).toBe(false);
  });

  it("aborts an active stream and maps abort versus provider errors", async () => {
    const store = useAgentStore();
    let rejectStream!: (reason: unknown) => void;
    api.streamAgentPrompt.mockImplementation((_request: unknown, _on: unknown, _trace: unknown, _session: unknown, signal: AbortSignal) => new Promise((_resolve, reject) => {
      rejectStream = reject; signal.addEventListener("abort", () => reject(new AgentApiError("stopped", "request_aborted")));
    }));
    store.promptInput = "run";
    const running = store.runPrompt();
    store.stopActiveRun();
    await running; expect(store.executionHistory[0].status).toBe("stopped");
    store.stopActiveRun();
    api.streamAgentPrompt.mockRejectedValueOnce(new Error("provider failed")); store.promptInput = "again";
    await store.runPrompt(); expect(store.executionHistory[0].status).toBe("failed");
    void rejectStream;
  });
});
