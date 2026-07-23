import { describe, expect, it } from "vitest";
import { AgentApiError } from "@/api/agent";
import { __agentStoreTestUtils } from "@/stores/agent";

const u = __agentStoreTestUtils!;
const packet = (value: Record<string, unknown>) => value as never;

describe("agent store deterministic helpers", () => {
  it("maps packet phase, status, detail, and waterfall variants", () => {
    const events = [
      ["ToolStart", { tool_name: "read" }], ["TextChunk", { content: "text" }],
      ["ReasoningChunk", { content: "reason" }], ["GitCommitPrompt", { status: "pending" }],
      ["ConnectionRetry", { attempt: 1, maxAttempts: 3, message: "retry" }],
      ["TaskStarted", { title: "task" }], ["TaskCompleted", {}], ["TaskFailed", {}], ["TaskSkipped", {}],
      ["TurnContract", { status: "needs_user_input" }],
      ["StoryGenerationValidation", { passed: false, message: "needs correction" }],
      ["RunAccepted", { label: "accepted" }],
      ["TurnPhase", { detail: "phase" }], ["UsageUpdate", {}], ["CompressionEvent", {}],
      ["AgentCompleted", { total_tokens: 12 }], ["AgentCancelled", {}], ["AgentError", { message: "bad" }],
      ["Unknown", {}]
    ] as const;
    for (const [name, data] of events) {
      const p = packet({ _type: name, ...data });
      expect(u.streamPacketToTraceEvent(p, 1).event).toBe(name);
      expect(u.phaseForEvent(name)).toBeTruthy();
      expect(u.statusForPacket(name, p)).toBeTruthy();
      expect(u.detailForPacket(name, p)).toBeTruthy();
    }
    expect(u.statusForPacket("ToolDone", packet({ is_error: true }))).toBe("error");
    for (const status of ["error", "warning", "success", "running", "pending", "other"]) {
      expect(u.statusForPacket("GitCommitResult", packet({ status }))).toBeTruthy();
    }
    const existing = [u.createWaterfallItem({ id: "t-assistant-1", type: "assistant", status: "running", title: "A", content: "one" })];
    const waterfallPackets = [
      { _type: "RunAccepted", elapsedMs: 1000, label: "Accepted", detail: "Detail" },
      { _type: "TurnPhase", status: "running", label: "Phase" },
      { _type: "TextChunk", content: "two" }, { _type: "ReasoningChunk", content: "why" },
      { _type: "ConnectionRetry", attempt: 1, maxAttempts: 3, message: "retry" },
      { _type: "ToolStart", tool_name: "write_file", tool_call_id: "1", arguments: { path: "a.md" } },
      { _type: "ToolRunning", tool_name: "write_file", progress: "half" },
      { _type: "ToolDone", tool_name: "write_file", is_error: false, result_preview: "done" },
      { _type: "ToolCacheHit", tool_name: "read_file" },
      { _type: "TurnContract", status: "ready" },
      { _type: "StoryGenerationValidation", passed: false, targetWordCount: 100, structurePassed: true, fragments: [{ path: "chapters/1/001.md", generatedWordCount: 90, targetWordCount: 100, difference: -10 }] },
      { _type: "ContinuationStarted", continuationMode: "story_generation_correction", correctionAttempt: 1, maximumCorrectionAttempts: 2 },
      { _type: "GitCommitPrompt", changedFileCount: 2 }, { _type: "GitCommitResult", created: true },
      { _type: "AgentError", error_type: "provider", message: "bad" }, { _type: "Unknown" }
    ];
    for (const value of waterfallPackets) u.streamPacketToWaterfallItem("t", packet(value), existing);
    expect(u.streamPacketToWaterfallItem("t", packet({ _type: "TextChunk", content: "<read>hidden</read>" }), existing)).toBeNull();
    for (const ignored of ["UsageUpdate", "CompressionEvent", "PermissionRequest", "TaskPlanCreated", "TaskStarted", "TaskCompleted", "TaskFailed", "TaskSkipped", "TaskPlanUpdated", "StageOutput", "AgentStarted", "AgentCompleted", "AgentCancelled"]) {
      expect(u.streamPacketToWaterfallItem("t", packet({ _type: ignored }), existing)).toBeNull();
    }
    const merged = u.mergeWaterfallItem(existing, u.createWaterfallItem({ id: "t-assistant-1", type: "assistant", status: "success", title: "A", content: "two" }));
    expect(merged[0].content).toBe("onetwo");
    expect(u.mergeWaterfallItem(existing, u.createWaterfallItem({ id: "new", type: "system", status: "info", title: "N", content: "n" }))).toHaveLength(2);
    expect(u.segmentItemId([...existing, u.createWaterfallItem({ id: "sys", type: "system", status: "info", title: "S", content: "" })], "t", "assistant")).toBe("t-assistant-1");
    expect(u.segmentItemId([], "t", "reasoning")).toBe("t-reasoning-1");
  });

  it("summarizes Git, contracts, presets, context, usage, and compression", () => {
    expect(u.summarizeGitAutoCommitPacket(packet({ _type: "GitCommitPrompt", changedFileCount: 2, workspaceRoot: "C:/story" }))).toContain("2");
    expect(u.summarizeGitAutoCommitPacket(packet({ _type: "GitCommitResult", created: true, shortHash: "abc", changedFileCount: 1 }))).toContain("abc");
    expect(u.summarizeGitAutoCommitPacket(packet({ _type: "GitAutoCommit", message: "none" }))).toContain("none");
    const contract = packet({
      status: "ready", intentFrame: { primary: "story" },
      turnPlan: { fragmentCount: 2, fragmentWordCount: 1200, requiresChapterTemplateSelection: true, invalidChapterTemplate: "bad", nextSegmentPath: "chapters/2.md" },
      executionPolicy: { directFileWrites: true, localGitAutoCommit: true }, updatePolicy: { autoUpdateVariables: true, autoUpdateWiki: false },
      skillRegistry: { skillCount: 2 }, toolRegistry: { toolCount: 3 },
      contextAssembly: { budget: { blockCount: 4, totalChars: 500 }, sources: [{ kind: "chapter", count: 2 }, {}, null], notes: ["preset_compile_failed: demo", "other"] }
    });
    expect(u.summarizeTurnContractPacket(contract)).toContain("chapters/2.md");
    expect(u.summarizeTurnContractPacket(packet({ turnPlan: { selectedChapterTemplate: "id", selectedChapterTemplateDetail: { name: "Template" } } }))).toContain("Template");
    expect(u.summarizeTurnContractPacket(packet({
      intentFrame: { primary: "story_generation" },
      updatePolicy: { autoUpdateVariables: false }
    }))).toContain("正文生成后直接整理");
    expect(u.summarizeStoryGenerationValidationPacket(packet({
      passed: false,
      targetWordCount: 100,
      structurePassed: true,
      fragments: [{ path: "chapters/1/001.md", generatedWordCount: 90, targetWordCount: 100, difference: -10 }]
    }))).toContain("90/100");
    expect(u.summarizePresetCompileFailures({ notes: [] })).toBe("");
    expect(u.summarizePresetCompileFailures({ notes: ["preset_compile_failed:", "preset_compile_failed: one", "preset_compile_failed: two", "preset_compile_failed: three"] })).toBeTruthy();
    expect(u.summarizeContextAssembly({})).toBe("");
    expect(u.summarizeContextAssembly({ budget: { blockCount: 1, totalChars: 10 } })).toBeTruthy();
    expect(u.summarizeUsagePacket(packet({ usage: { prompt_tokens: 1000, completion_tokens: 200, total_tokens: 1200 } }))).toBeTruthy();
    expect(u.summarizeUsagePacket(packet({ promptTokens: 10, completionTokens: 2 }))).toBeTruthy();
    expect(u.summarizeCompressionPacket(packet({ status: "completed", before_tokens: 100, after_tokens: 50, summary: "short" }))).toBeTruthy();
    expect(u.summarizeCompressionPacket(packet({ action: "start" }))).toBeTruthy();
    expect(u.extractCompressionMeta(packet({ compression: { before: 1 }, before_tokens: 2 }))).toBeTruthy();
  });

  it("removes tool markup and DSML without hiding ordinary model text", () => {
    expect(u.stripTextualToolBlocks("")).toBe("");
    expect(u.stripTextualToolBlocks("ordinary text")).toBe("ordinary text");
    expect(u.stripTextualToolBlocks("<read>secret</read>visible")).toBe("visible");
    expect(u.stripTextualToolBlocks("<read>\n<path>a</path>\n")).toBe("");
    expect(u.stripDsmlToolText("ordinary dsml discussion")).toContain("ordinary");
    expect(u.stripDsmlToolText("<||DSML tool_calls invoke parameter")).toBe("");
    expect(u.stripDsmlToolText("keep\nDSML tool_call invoke\nmore")).toBe("keep\nmore");
    expect(u.looksLikeToolXmlFragment("<read>", /^<read>$/, /^<path>/)).toBe(true);
    expect(u.looksLikeToolXmlFragment("plain", /^<read>$/, /^<path>/)).toBe(false);
  });

  it("normalizes history, traces, audits, statuses, tasks, and ledgers", () => {
    const events = [
      { event: "TaskPlanCreated", data: { tasks: [{ id: "1", title: "Task", status: "pending" }] } },
      { event: "TaskStarted", task_id: "1", title: "Task" }, { event: "ToolDone", tool_name: "write_file", result_preview: "chapters/1.md" },
      null, "bad"
    ];
    const history = u.normalizeHistoryRuns([
      null, { traceId: "t", prompt: "p", reply: "r", events, status: "completed", createdAt: "2020-01-01T00:00:00Z", changeLedger: {} },
      { prompt: "fallback", errorMessage: "bad", status: "running" }
    ], "session");
    expect(history).toHaveLength(2);
    expect(u.normalizeHistoryRuns("bad", "session")).toEqual([]);
    expect(u.normalizeHistoryRun(null, "session")).toBeNull();
    expect(u.buildHistoryWaterfallItems("t", "p", "r", u.normalizeTraceEvents(events), "completed").length).toBeGreaterThan(0);
    expect(u.normalizeTraceEvents("bad")).toEqual([]);
    expect(u.normalizeTraceEvents([{ event: "x", detail: "d", data: { a: 1 } }, null])).toHaveLength(1);
    expect(u.normalizeTrace(null)).toBeNull();
    expect(u.normalizeTrace({ traceId: "t", durationMs: 1 })).toMatchObject({ traceId: "t" });
    expect(u.normalizeAudit(null)).toEqual([]);
    expect(u.normalizeAudit([{ action: "a" }, null])).toHaveLength(1);
    expect(u.normalizeAudit({ action: "a" })).toHaveLength(1);
    for (const status of ["completed", "committed", "discarded", "preview", "failed", "cancelled", "stopped", "running", "other"]) {
      expect(u.normalizeRunStatus(status, "")).toBeTruthy();
    }
    expect(u.normalizeRunStatus("running", "error")).toBe("failed");

    const tasks = u.normalizeTaskPlan([null, {}, { id: "a", title: "Analyze", status: "running", dependsOn: ["x", ""] }], "trace");
    expect(tasks).toHaveLength(1);
    expect(u.normalizeTaskPlan("bad", "trace", tasks)).toEqual(tasks);
    expect(u.upsertTaskEvent(tasks, packet({ task_id: "a", title: "Analyze" }), "trace", "session", "TaskCompleted")[0].status).toBe("completed");
    expect(u.upsertTaskEvent([], packet({ title: "New" }), "trace", "session", "TaskStarted")).toHaveLength(1);
    expect(u.deriveTasksFromEvents(u.normalizeTraceEvents([{ event: "TaskStarted", task_id: "a", title: "A" }, { event: "Other" }]), "trace")).toHaveLength(1);
    expect(u.sanitizeTaskList([{ id: "a", title: "analysis", status: "pending", order: 1 }, { id: "b", title: "Specific work", status: "pending", order: 2 }] as never)).toHaveLength(1);
    expect(u.isGenericTaskTitle("analysis")).toBe(true);
    expect(u.isGenericTaskTitle("specific work")).toBe(false);
    for (const value of ["pending", "running", "completed", "failed", "skipped", "other"]) expect(u.normalizeTaskStatus(value)).toBeTruthy();
    for (const name of ["TaskStarted", "TaskCompleted", "TaskFailed", "TaskSkipped", "Other"]) expect(u.statusForTaskEvent(name, "pending")).toBeTruthy();
    expect(u.finalizeTaskStatuses([{ taskId: "a", title: "Write chapter", status: "running", order: 1 }, { taskId: "b", title: "Verify output", status: "pending", order: 2 }] as never, "completed").every((t: any) => t.status === "completed")).toBe(true);
    expect(u.finalizeTaskStatuses([{ taskId: "a", title: "Write chapter", status: "running", order: 1 }, { taskId: "b", title: "Verify output", status: "completed", order: 2 }] as never, "failed")).toHaveLength(2);

    expect(u.createEmptyChangeLedger("t", "s").changedFileCount).toBe(0);
    const ledger = u.normalizeChangeLedger(packet({ changedFiles: ["a", "a", ""], added: -1, removed: 2, commitHash: " abc " }), "t", "s", undefined);
    expect(ledger.changedFiles).toEqual(["a", "a"]);
    expect(u.mergeChangeLedgerPaths(ledger, ["b", "a"], "t", "s").changedFiles).toEqual(["a", "b"]);
    expect(u.normalizeHistoryChangeLedger(null, u.normalizeTraceEvents([{ event: "GitCommitResult", data: { changedFiles: ["a"] } }]), "t", "s")).toBeTruthy();
  });

  it("normalizes sessions, templates, Coomi status, approvals and commits", () => {
    expect(u.normalizeCoomiStatus(null)).toBeNull();
    expect(u.normalizeCoomiStatus({ runtime: "coomi", installed: true, toolCount: 2, planMode: false })).toMatchObject({ runtime: "coomi", installed: true });
    expect(u.normalizeSessionSummaries("bad")).toEqual([]);
    expect(u.normalizeSessionSummaries([null, {}, { sessionId: "s", createdAt: "2020-01-01", traceCount: 1 }, { sessionId: "new", updatedAt: "2021-01-01" }])[0].sessionId).toBe("new");
    expect(u.normalizeStoryChapterTemplates("bad")).toEqual([]);
    expect(u.normalizeStoryChapterTemplates([null, {}, { id: " x ", name: "", chapterMode: "", contentMode: "single_file", segmentNaming: "" }])[0]).toMatchObject({ id: "x", name: "x", contentMode: "single_file" });
    expect(u.normalizeStoryChapterTemplateError(new Error("bad"))).toContain("bad");
    expect(u.normalizeStoryChapterTemplateError("bad")).toBeTruthy();
    expect(u.isStoryChapterTemplateNotFoundError({ response: { status: 404 } })).toBe(true);
    expect(u.isStoryChapterTemplateNotFoundError({ code: "story_chapter_templates_not_found" })).toBe(true);
    expect(u.isStoryChapterTemplateNotFoundError({})).toBe(false);
    expect(u.normalizePendingApproval(packet({}))).toBeNull();
    const approval = u.normalizePendingApproval(packet({ approval_id: "a", kind: "question", options: [{ label: "Yes" }, { value: "no" }, null], questions: [{ question: "Q" }] }));
    expect(approval?.approvalId).toBe("a");
    const prompt = u.normalizeCommitPrompt(packet({ message: "commit", changedFiles: ["a"], changedFileCount: 1 }), "t", "s");
    expect(u.buildCommitDecisionPacket(prompt, packet({ created: true })).traceId).toBe("t");
    expect(u.fallbackCommitMessage(prompt)).toContain("1 files");
    expect(u.fallbackCommitMessage({ ...prompt, changedFiles: [], changedFileCount: 0 })).toBe("agent: update project files");
    expect(u.shouldRetryCommitWithFallbackMessage(new AgentApiError("bad", "commit_message_generation_failed"))).toBe(true);
    expect(u.shouldRetryCommitWithFallbackMessage({ response: { status: 502, data: { error: { message: "commit message failed" } } } })).toBe(true);
    expect(u.shouldRetryCommitWithFallbackMessage(new Error("other"))).toBe(false);
  });

  it("extracts and secures changed paths from nested tools and previews", () => {
    const output: string[] = [];
    u.collectPathCandidates({ path: "chapters/a.md", nested: [{ file_path: "notes/b.txt" }], ignored: "plain", deep: { a: { b: { c: { d: { e: { f: { path: "too-deep" } } } } } } } }, output);
    expect(output).toContain("chapters/a.md");
    expect(u.isPathLikeKey("file_path")).toBe(true);
    expect(u.isPathLikeKey("changedFiles")).toBe(false);
    expect(u.isPathLikeKey("other")).toBe(false);
    for (const value of ["chapters/a.md", "C:\\story\\a.md", "./notes/a.txt", "plain text", "https://example.com/a"]) expect(typeof u.looksLikePathText(value)).toBe("boolean");
    expect(u.normalizeChangedPath("C:/story/chapters/a.md", "C:/story")).toBe("chapters/a.md");
    expect(u.normalizeChangedPath("../escape", "C:/story")).toBe("");
    expect(u.normalizeChangedPath("/absolute/outside", "C:/story")).toBe("");
    expect(u.normalizeChangedPath(".storydex/wiki/a.json", "C:/story")).toBe(".storydex/wiki/a.json");
    expect(u.extractPathsFromPreview("Wrote file: chapters/a.md\nCreated file: notes/b.txt", "C:/story")).toEqual(expect.arrayContaining(["chapters/a.md", "notes/b.txt"]));
    expect(u.uniqueStrings([" a ", "a", "", "B"])).toEqual(["a", "B"]);
    expect(u.escapeRegExp("a+b")).toBe("a\\+b");
    const run = { items: [{ arguments: { path: "chapters/a.md" }, toolCallId: "1", toolName: "write_file" }], changeLedger: u.createEmptyChangeLedger("t", "s"), sessionId: "s" } as never;
    expect(u.findToolArgumentsForPacket(run, packet({ tool_call_id: "1" }))).toEqual({ path: "chapters/a.md" });
    expect(u.findToolArgumentsForPacket(run, packet({ tool_name: "write_file" }))).toEqual({ path: "chapters/a.md" });
    expect(u.findToolArgumentsForPacket(run, packet({ tool_name: "missing" }))).toBeNull();
    expect(u.extractChangedPathsFromToolPacket(packet({ tool_name: "write_file", changedFiles: ["a.md"], arguments: { path: "chapters/b.md" }, result_preview: "Wrote file: notes/c.md" }), run, "C:/story")).toEqual(expect.arrayContaining(["a.md", "chapters/b.md", "notes/c.md"]));
    expect(u.extractChangedPathsFromToolPacket(packet({ tool_name: "write_file", tool_call_id: "1" }), run, "C:/story")).toContain("chapters/a.md");
    for (const name of ["write_file", "edit", "patch", "save", "create", "delete", "move", "rename", "mkdir", "apply_story_increment", "sync_wiki", "read_file", "version_status", ""]) {
      expect(typeof u.isWriteLikeToolPacket(packet({ tool_name: name }))).toBe("boolean");
    }
  });

  it("covers primitive coercion, formatting, error and numeric boundaries", () => {
    expect(u.asRecord(null)).toEqual({});
    expect(u.toRecord([])).toBeNull(); expect(u.toRecord({ a: 1 })).toEqual({ a: 1 });
    expect(u.asString("x")).toBe("x"); expect(u.asString(1)).toBeNull();
    expect(u.asBoolean(false)).toBe(false); expect(u.asBoolean(0)).toBeNull();
    expect(u.asNumber(1)).toBe(1); expect(u.asNumber("1")).toBeNull();
    expect(u.firstString({ a: "", b: " x " }, ["a", "b"])).toBe(" x "); expect(u.firstString({}, ["a"])).toBeNull();
    expect(u.firstNumber({ a: "2" }, ["a"])).toBe(2); expect(u.firstNumber({ a: "bad" }, ["a"])).toBeNull();
    expect(u.clampInteger("bad", 1, 10, 5)).toBe(5); expect(u.clampInteger(99, 1, 10, 5)).toBe(10);
    expect(u.stringify("x")).toBe('"x"'); expect(u.stringify({ a: 1 })).toContain("a"); expect(u.stringify(undefined)).toBeUndefined();
    for (const value of [0, 999, 1000, 1_000_000, -2000]) expect(u.formatTokenCount(value)).toBeTruthy();
    expect(u.normalizeAgentError(new AgentApiError("bad", "code"))).toEqual({ message: "bad", code: "code" });
    expect(u.normalizeAgentError(new Error("plain"))).toEqual({ message: "plain", code: null });
  });

  it("covers fallback branches for defaults, malformed packets and numeric summaries", () => {
    expect(u.normalizeCoomiStatus({})).toMatchObject({ runtime: "coomi", installed: false, toolCount: 0 });
    expect(u.normalizeStoryChapterTemplates([{ id: "x", relativePath: " p ", description: " d ", chapterNamePattern: " n ", chapterMode: "", segmentNaming: "" }])[0])
      .toMatchObject({ relativePath: "p", description: "d", chapterNamePattern: "n", chapterMode: "directory", segmentNaming: "001.md" });
    expect(u.normalizeStoryChapterTemplateError(new Error("request failed with status code 404"))).toBe("");
    expect(u.normalizeStoryChapterTemplateError({})).toBeTruthy();

    const taskSet = [
      { taskId: "a", title: "Specific A", status: "running", order: 1, createdAt: "x", updatedAt: "x" },
      { taskId: "b", title: "Specific B", status: "pending", order: 2, createdAt: "x", updatedAt: "x" },
      { taskId: "c", title: "Specific C", status: "completed", order: 3, createdAt: "x", updatedAt: "x" }
    ] as never;
    expect(u.finalizeTaskStatuses(taskSet, "failed").map((t: any) => t.status)).toEqual(["failed", "skipped", "completed"]);
    expect(u.finalizeTaskStatuses(taskSet, "cancelled").map((t: any) => t.status)).toEqual(["skipped", "skipped", "completed"]);
    expect(u.finalizeTaskStatuses(taskSet, "stopped")).toHaveLength(3);
    expect(u.normalizeTaskStatus("success")).toBe("completed"); expect(u.normalizeTaskStatus("error")).toBe("failed");
    expect(u.statusForTaskEvent("TaskStarted", "success")).toBe("completed");
    const updated = u.upsertTaskEvent(taskSet, packet({ taskId: "a", order: 5, detail: "detail" }), "trace", "session", "TaskStarted");
    expect(updated.find((t: any) => t.taskId === "a")?.detail).toBe("detail");
    expect(u.upsertTaskEvent(taskSet, packet({ title: "analysis" }), "trace", "session", "TaskStarted")).toEqual(taskSet);

    const fallback = { ...u.createEmptyChangeLedger("t", "fallback"), changedFiles: ["old"], changedFileCount: 1, added: 2, removed: 3, commitHash: "abc", diffSource: "commit" };
    expect(u.normalizeChangeLedger(packet({ changedFileCount: "bad", session_id: "packet", added: -1, removed: -1 }), "t", "s", fallback)).toMatchObject({ sessionId: "packet", changedFileCount: 1, added: 0, removed: 0 });
    expect(u.normalizeChangeLedger(packet({ changedFiles: "bad" }), "t", "s", undefined).changedFiles).toEqual([]);
    expect(u.normalizeCommitPrompt(packet({ changedFiles: "bad", changedFileCount: "bad", session_id: "packet" }), "t", "s")).toMatchObject({ sessionId: "packet", changedFiles: [], changedFileCount: 0 });
    expect(u.mergeChangeLedgerPaths(undefined, [], "t", "")).toMatchObject({ sessionId: "", diffSource: "" });
    expect(u.mergeChangeLedgerPaths(fallback, [], "t", "").diffSource).toBe("commit");

    const candidates: string[] = [];
    u.collectPathCandidates(null, candidates); u.collectPathCandidates(undefined, candidates);
    u.collectPathCandidates(["chapters/a.md", 1], candidates, "path");
    u.collectPathCandidates(1, candidates);
    u.collectPathCandidates({ path: "x" }, candidates, "", 7);
    expect(u.looksLikePathText("")).toBe(false);
    expect(u.looksLikePathText("x".repeat(600))).toBe(false);
    expect(u.extractPathsFromPreview("", "")).toEqual([]);
    expect(u.extractPathsFromPreview('{"path":"chapters/a.md"}', "")).toContain("chapters/a.md");
    expect(u.extractPathsFromPreview("{broken\nWrote file: chapters/b.md", "")).toContain("chapters/b.md");
    expect(u.extractPathsFromPreview("C:\\story\\chapters\\a.md", "C:/story").length).toBeGreaterThan(0);
    expect(u.normalizeChangedPath("", "")).toBe("");
    expect(u.normalizeChangedPath("x".repeat(501), "")).toBe("");
    expect(u.normalizeChangedPath("{bad}", "")).toBe("");
    expect(u.normalizeChangedPath("C:/story/a.md", "")).toBe("");
    expect(u.normalizeChangedPath("C:/outside/a.md", "C:/story")).toBe("");

    expect(u.normalizeHistoryChangeLedger(null, u.normalizeTraceEvents([{ event: "GitCommitResult" }]), "t", "s")).toBeTruthy();
    expect(u.normalizeTrace({})).toMatchObject({ traceId: "", durationMs: 0 });
    expect(u.normalizeAudit("bad")).toEqual([]);
    const circular: any = {}; circular.self = circular; expect(u.stringify(circular)).toBe("[object Object]");
    expect(u.summarizeUsagePacket(packet({ usedTokens: 20, contextWindow: 100, usageRatio: 0.2 }))).toContain("20.0%");
    expect(u.summarizeUsagePacket(packet({ used_tokens: 20, context_window: 100 }))).toContain("20.0%");
    expect(u.summarizeCompressionPacket(packet({ estimated_tokens: 20, context_window: 100, messages_before: 4, messages_after: 2 }))).toBeTruthy();
    expect(u.summarizeCompressionPacket(packet({ usage_ratio: 0.3 }))).toContain("30.0%");
    expect(u.firstNumber({ a: undefined, b: 3 }, ["a", "b"])).toBe(3);
  });
});
