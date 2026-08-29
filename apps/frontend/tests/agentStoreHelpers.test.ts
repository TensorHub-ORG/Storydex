import { describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { AgentApiError } from "@/api/agent";
import { __agentStoreTestUtils, useAgentStore } from "@/stores/agent";

const u = __agentStoreTestUtils!;
const packet = (value: Record<string, unknown>) => value as never;

describe("agent store deterministic helpers", () => {
  it("uses the medium tier as the generation default and keeps legacy migration local", () => {
    setActivePinia(createPinia());
    const store = useAgentStore();
    expect(store.chapterLengthTier).toBe("medium");
    expect(store.chapterWordCountTarget).toBe(3000);
    expect(store.storyFragmentWordCount).toBe(3000);
    expect(store.storyFragmentWordCountMin).toBe(3000);
    expect(store.storyFragmentWordCountMax).toBe(3000);
    expect(store.preciseWordCountEnabled).toBe(false);
    store.setStoryGenerationOptions({ chapterWordCountTarget: 2000 });
    expect(store.chapterLengthTier).toBe("short");
    store.setStoryGenerationOptions({ chapterLengthTier: "long", chapterWordCountTarget: 1000 });
    expect(store.chapterLengthTier).toBe("long");
    store.setStoryGenerationOptions({ preciseWordCountEnabled: true });
    expect(store.preciseWordCountEnabled).toBe(false);
  });

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
    expect(u.streamPacketToWaterfallItem("t", packet({
      _type: "ConnectionRetry",
      attempt: 2,
      maxAttempts: 3,
      resetTextCharacters: 4,
      message: "retry"
    }), existing)?.content).toContain("4");
  });

  it("rolls back partial and multi-item provider output by Unicode code point", () => {
    expect(u.dropTrailingCodePoints("ab😀cd", 2)).toBe("ab😀");
    expect(u.dropTrailingCodePoints("ab", 20)).toBe("");
    expect(u.dropTrailingCodePoints("ab", 0)).toBe("ab");
    expect(u.dropTrailingCodePoints("", 1)).toBe("");
    expect(u.rollbackTraceTextEvents([], 0)).toEqual([]);
    expect(u.rollbackAssistantItems([], 0)).toEqual([]);

    const traceEvents = [
      { index: 1, event: "TextChunk", phase: "model", status: "running", detail: "abc", data: { content: "abc" }, timestamp: "1" },
      { index: 2, event: "TurnPhase", phase: "model", status: "running", detail: "phase", data: {}, timestamp: "2" },
      { index: 3, event: "TextChunk", phase: "model", status: "running", detail: "defgh", data: { content: "defgh" }, timestamp: "3" }
    ] as never;
    const rolledEvents = u.rollbackTraceTextEvents(traceEvents, 7);
    expect(rolledEvents.map((event: { event: string }) => event.event)).toEqual(["TextChunk", "TurnPhase"]);
    expect(rolledEvents[0].detail).toBe("a");
    expect(rolledEvents[0].data.content).toBe("a");
    expect(rolledEvents.map((event: { index: number }) => event.index)).toEqual([1, 2]);

    const assistantItems = [
      u.createWaterfallItem({ id: "a1", type: "assistant", status: "running", title: "A", content: "abc" }),
      u.createWaterfallItem({ id: "s1", type: "system", status: "info", title: "S", content: "keep" }),
      u.createWaterfallItem({ id: "a2", type: "assistant", status: "running", title: "A", content: "defgh" })
    ];
    const rolledItems = u.rollbackAssistantItems(assistantItems, 7);
    expect(rolledItems.map((item: { id: string }) => item.id)).toEqual(["a1", "s1"]);
    expect(rolledItems[0].content).toBe("a");
  });

  it("formats structured Coomi diagnostics for the Agent and Trace panels", () => {
    const message = u.formatAgentErrorPacket(packet({
      _type: "AgentError",
      error_type: "CoomiBridgeError",
      message: "Rust bridge could not start",
      details: {
        stage: "bridge_start",
        runtime: "storydex-coomi-rs",
        runtimeVersion: "2.0.0-storydex.2",
        providerId: "opencode-go",
        model: "deepseek-v4-pro",
        traceId: "trace-1",
        sessionId: "session-1",
        origin: { file: "services/coomi_bridge_client.py", line: 101, function: "start" },
        exceptionChain: [
          { type: "CoomiBridgeError", message: "Rust bridge could not start" },
          { type: "NotImplementedError", message: "" }
        ]
      }
    }));
    expect(message).toContain("Stage: bridge_start");
    expect(message).toContain("services/coomi_bridge_client.py:101 in start");
    expect(message).toContain("CoomiBridgeError");
    expect(message).toContain("opencode-go / deepseek-v4-pro");
    expect(message).toContain("Trace: trace-1");
    expect(message).not.toContain("api_key");
  });

  it("surfaces structured provider HTTP failures without replacing the provider message", () => {
    const forbidden = u.formatAgentErrorPacket(packet({
      _type: "AgentError",
      message: "provider returned HTTP 403: Forbidden",
      details: { statusCode: 403 }
    }));
    const gateway = u.formatAgentErrorPacket(packet({
      _type: "AgentError",
      message: "Provider request failed",
      details: { providerHttpStatus: 502 }
    }));

    expect(forbidden).toContain("provider returned HTTP 403: Forbidden");
    expect(forbidden.match(/HTTP 403/g)).toHaveLength(1);
    expect(gateway).toContain("Provider request failed");
    expect(gateway).toContain("HTTP 502");
  });

  it("summarizes Git, contracts, presets, context, usage, and compression", () => {
    expect(u.summarizeGitAutoCommitPacket(packet({ _type: "GitCommitPrompt", changedFileCount: 2, workspaceRoot: "C:/story" }))).toContain("2");
    expect(u.summarizeGitAutoCommitPacket(packet({ _type: "GitCommitResult", created: true, shortHash: "abc", changedFileCount: 1 }))).toContain("abc");
    expect(u.summarizeGitAutoCommitPacket(packet({ _type: "GitAutoCommit", message: "none" }))).toContain("none");
    const contract = packet({
      status: "ready", intentFrame: { primary: "story" },
      turnPlan: { fragmentCount: 2, fragmentWordCount: 1200, requiresChapterTemplateSelection: true, invalidChapterTemplate: "bad", nextSegmentPath: "chapters/2.md" },
      executionPolicy: { directFileWrites: true, localGitAutoCommit: false, localGitCommitMode: "explicit" }, updatePolicy: { autoUpdateVariables: true, autoUpdateWiki: false },
      skillRegistry: { skillCount: 2 }, toolRegistry: { toolCount: 3 },
      contextAssembly: { budget: { blockCount: 4, totalChars: 500 }, sources: [{ kind: "chapter", count: 2 }, {}, null], notes: ["preset_compile_failed: demo", "other"] }
    });
    expect(u.summarizeTurnContractPacket(contract)).toContain("chapters/2.md");
    expect(u.summarizeTurnContractPacket(contract)).toContain("章目标：1200 字");
    expect(u.summarizeTurnContractPacket(contract)).toContain("可接受：840-1560 字");
    expect(u.summarizeTurnContractPacket(contract)).toContain("小说项目 Git：按需提交");
    const calibratedSummary = u.summarizeTurnContractPacket(packet({
      status: "ready",
      turnPlan: {
        fragmentCount: 1,
        chapterWordCountTarget: 3000,
        wordCountPolicy: {
          target: 3000,
          modelReferenceWordCount: 2500,
          acceptanceMinimum: 2100,
          acceptanceMaximum: 3900,
          calibration: {
            status: "applied",
            provider: "chy",
            model: "deepseek-v4-flash",
            sampleCount: 3
          }
        }
      }
    }));
    expect(calibratedSummary).toContain("章目标：3000 字");
    expect(calibratedSummary).toContain("模型参考：2500 字");
    expect(calibratedSummary).toContain("校准：chy/deepseek-v4-flash · 3 个样本");
    const modifySummary = u.summarizeTurnContractPacket(packet({
      status: "ready",
      intentFrame: { primary: "story_generation", operationType: "modify_existing", complexity: "complex" }
    }));
    expect(modifySummary).toContain("修改现有文件");
    expect(modifySummary).toContain("复杂度：复杂");
    expect(u.summarizeTurnContractPacket(packet({ turnPlan: { selectedChapterTemplate: "id", selectedChapterTemplateDetail: { name: "Template" } } }))).toContain("Template");
    expect(u.summarizeTurnContractPacket(packet({
      intentFrame: { primary: "story_generation" },
      updatePolicy: { autoUpdateVariables: false }
    }))).toContain("正文生成后直接整理");
    const legacyValidationSummary = u.summarizeStoryGenerationValidationPacket(packet({
      passed: false,
      targetWordCount: 100,
      structurePassed: true,
      fragments: [{ path: "chapters/1/001.md", generatedWordCount: 90, targetWordCount: 100, difference: -10 }]
    }));
    expect(legacyValidationSummary).toContain("待复核");
    expect(legacyValidationSummary).toContain("90 字（目标 100）");
    expect(legacyValidationSummary).toContain("差 -10");
    const rangeValidationSummary = u.summarizeStoryGenerationValidationPacket(packet({
      passed: false,
      targetWordCountMin: 2000,
      targetWordCountMax: 2500,
      structurePassed: false,
      fragments: [{ path: "chapters/第1章/001.md", actualWordCount: 2173, difference: 0 }]
    }));
    expect(rangeValidationSummary).toContain("2173 字（目标 2000-2500）");
    expect(rangeValidationSummary).toContain("本章 2173 字 · 章目标 2250 字 · 可接受 1400-3250 字");
    expect(rangeValidationSummary).toContain("章节结构与模板不一致");
    expect(rangeValidationSummary).not.toContain("2173/0");
    const overBudgetSummary = u.summarizeStoryGenerationValidationPacket(packet({
      passed: true,
      overBudget: true,
      generatedWordCount: 3126,
      chapterWordCountTarget: 2500,
      acceptWordCountMin: 1875,
      acceptWordCountMax: 3125
    }));
    expect(overBudgetSummary).toContain("建议作者按需裁剪");
    expect(u.statusForPacket("StoryGenerationValidation", packet({ passed: true, overBudget: true }))).toBe("warning");
    const tierSummary = u.summarizeStoryGenerationValidationPacket(packet({
      passed: true,
      writeToolApplied: true,
      chapterLengthTier: "short",
      actualWordCount: 1500,
      tierHit: false,
      structurePassed: true,
      machineQualityPassed: true
    }));
    expect(tierSummary).toContain("章节已写入 · 本次续写：1500 字");
    expect(tierSummary).toContain("短档");
    expect(tierSummary).toContain("档位未命中");
    expect(tierSummary).not.toContain("目标");
    expect(tierSummary).not.toContain("可接受");
    expect(u.statusForPacket("StoryGenerationValidation", packet({
      passed: true,
      chapterLengthTier: "short",
      tierHit: false
    }))).toBe("warning");
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

  it("shows a committed short draft as a visible warning", () => {
    const validation = packet({
      _type: "StoryGenerationValidation",
      passed: true,
      belowBudget: true,
      overBudget: false,
      generatedWordCount: 1900,
      chapterWordCountTarget: 3000,
      acceptWordCountMin: 2100,
      acceptWordCountMax: 3900
    });

    expect(u.statusForPacket("StoryGenerationValidation", validation)).toBe("warning");
    expect(u.summarizeStoryGenerationValidationPacket(validation)).toContain("已保留结构完整首稿");
  });

  it("shows a precise miss as a written chapter warning, not a generation failure", () => {
    const validation = packet({
      _type: "StoryGenerationValidation",
      passed: true,
      writeToolApplied: true,
      finalWordCount: 3707,
      canonicalWordCount: 3707,
      chapterWordCountTarget: 3000,
      preciseWordCountEnabled: true,
      normalBandPassed: true,
      precisionAchieved: false,
      lengthControlStrategy: "elastic_manuscript_v1",
      selectedEditIds: [],
      lengthFallbackReason: "repair_outside_band",
      generatedOverheadRatio: null
    });

    expect(u.statusForPacket("StoryGenerationValidation", validation)).toBe("warning");
    const summary = u.summarizeStoryGenerationValidationPacket(validation);
    expect(summary).toContain("章节已写入，字数 3707");
    expect(summary).toContain("未达到精确范围 2700-3300");
    expect(summary).not.toContain("生成失败");
    expect(summary).not.toContain("待复核");
  });

  it("shows whole-chapter measurements, revision outcome, and prose-only call accounting", () => {
    const tierDraft = u.streamPacketToWaterfallItem("tier-trace", packet({
      _type: "StoryDraftMeasured",
      wordCountScope: "candidate",
      actualWordCount: 1200,
      initialWordCount: 1200,
      generatedWordCount: 1200,
      retainedWordCount: 2600,
      resultingWordCount: 3800,
      chapterLengthTier: "short",
      tierHit: true
    }), []);
    expect(tierDraft?.content).toContain("本次续写：1200 字");
    expect(tierDraft?.content).toContain("落盘后本章：3800 字");
    expect(tierDraft?.content).not.toContain("首稿整章");

    const draft = u.streamPacketToWaterfallItem("trace", packet({
      _type: "StoryDraftMeasured",
      initialWordCount: 2860,
      retainedWordCount: 2600,
      generatedWordCount: 260
    }), []);
    expect(draft?.content).toContain("首稿整章：2860 字");
    expect(draft?.content).toContain("保留正文：2600 字");
    expect(draft?.content).toContain("本轮新增：260 字");

    const revision = u.streamPacketToWaterfallItem("trace", packet({
      _type: "StoryLengthRevisionResult",
      candidateWordCount: 350,
      accepted: true
    }), []);
    expect(revision?.content).toContain("字数修订：已触发");
    expect(revision?.content).toContain("候选新增正文：350 字");

    const validation = u.summarizeStoryGenerationValidationPacket(packet({
      passed: true,
      initialWordCount: 2860,
      finalWordCount: 2950,
      revisionApplied: true,
      callAccounting: { lengthRevisionCalls: 1 }
    }));
    expect(validation).toContain("首稿整章 2860 字");
    expect(validation).toContain("最终整章 2950 字");
    expect(validation).toContain("字数修订：已触发并采用");

    const noRevision = u.summarizeStoryGenerationValidationPacket(packet({
      passed: true,
      initialWordCount: 3010,
      finalWordCount: 3010,
      revisionApplied: false,
      callAccounting: { lengthRevisionCalls: 0 }
    }));
    expect(noRevision).toContain("字数修订：未触发");

    const accounting = u.streamPacketToWaterfallItem("trace", packet({
      _type: "StoryCallAccounting",
      logicalStoryCalls: 2,
      initialGenerationCalls: 1,
      lengthRevisionCalls: 1,
      providerAttempts: 3,
      transportRetries: 1,
      nonProseCalls: { intent_recognition: 1 }
    }), []);
    expect(accounting?.content).toContain("正文逻辑调用：2 次");
    expect(accounting?.content).toContain("首稿 1");
    expect(accounting?.content).toContain("长度修订 1");
    expect(accounting?.content).toContain("正文 Provider 尝试：3 次");
    expect(accounting?.content).toContain("传输重试：1 次");
    expect(accounting?.content).not.toContain("意图识别");
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
