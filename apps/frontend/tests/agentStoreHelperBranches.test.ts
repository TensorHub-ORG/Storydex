import { describe, expect, it } from "vitest";
import { AgentApiError } from "@/api/agent";
import { __agentStoreTestUtils } from "@/stores/agent";

// Targets the remaining uncovered RHS "|| default" / ternary branches inside the
// pure helpers. Each assertion checks a specific fallback so the branch that
// was previously only exercised on its left-hand side gets its right-hand side hit.
const u = __agentStoreTestUtils!;
const packet = (value: Record<string, unknown>) => value as never;

describe("agent store helper fallback branches", () => {
  it("statusForPacket TurnContract / StoryGenerationValidation fallbacks", () => {
    // 1704/1705: status !== needs_user_input -> "info"
    expect(u.statusForPacket("TurnContract", packet({ status: "ready" }))).toBe("info");
    expect(u.statusForPacket("TurnContract", packet({ status: "needs_user_input" }))).toBe("warning");
    // A rejected candidate is an error; a committed tier miss is only a warning.
    expect(u.statusForPacket("StoryGenerationValidation", packet({ passed: true }))).toBe("success");
    expect(u.statusForPacket("StoryGenerationValidation", packet({ passed: false }))).toBe("error");
    expect(u.statusForPacket("StoryGenerationValidation", packet({
      passed: true,
      chapterLengthTier: "medium",
      tierHit: false
    }))).toBe("warning");
  });

  it("detailForPacket branches for tools, chunks, retries and errors", () => {
    // 1723: Tool* with no tool_name -> eventName
    expect(u.detailForPacket("ToolStart", packet({}))).toBe("ToolStart");
    expect(u.detailForPacket("ToolStart", packet({ tool_name: "read_file" }))).toBe("read_file");
    // 1724: chunk content fallback empty
    expect(u.detailForPacket("TextChunk", packet({}))).toBe("");
    expect(u.detailForPacket("ReasoningChunk", packet({ content: "why" }))).toBe("why");
    // 1726/1727/1728: ConnectionRetry attempt/max fallbacks and message fallback
    expect(u.detailForPacket("ConnectionRetry", packet({}))).toContain("模型连接中断");
    expect(u.detailForPacket("ConnectionRetry", packet({ max_attempts: 4 }))).toContain("1/4");
    // 1730: AgentError message fallback
    expect(u.detailForPacket("AgentError", packet({}))).toBe("Coomi error");
    expect(u.detailForPacket("AgentError", packet({ message: "boom" }))).toBe("boom");
  });

  it("streamPacketToWaterfallItem fallbacks for chunks, retries, and steer continuation", () => {
    // 1457: eventName from packet.type instead of _type
    const fromType = u.streamPacketToWaterfallItem("t", packet({ type: "RunAccepted", elapsedMs: 0 }), []);
    expect(fromType?.type).toBe("phase");
    // 1495: ReasoningChunk content fallback empty string
    const reasoning = u.streamPacketToWaterfallItem("t", packet({ _type: "ReasoningChunk" }), []);
    expect(reasoning?.content).toBe("");
    // 1500/1501/1502: ConnectionRetry attempt/max/message fallbacks
    const retry = u.streamPacketToWaterfallItem("t", packet({ _type: "ConnectionRetry" }), []);
    expect(retry?.content).toContain("模型连接中断");
    expect(retry?.content).toContain("1/1");
    const retryMax = u.streamPacketToWaterfallItem("t", packet({ _type: "ConnectionRetry", max_attempts: 5 }), []);
    expect(retryMax?.content).toContain("1/5");
    // 1513/1514: steer continuation empty content -> null
    expect(u.streamPacketToWaterfallItem("t", packet({ _type: "ContinuationStarted", continuationMode: "steer", content: "   " }), [])).toBeNull();
    // 1517: continuationId falls back to messageId then "steer"
    const steerByMessage = u.streamPacketToWaterfallItem("t", packet({ _type: "ContinuationStarted", continuationMode: "steer", content: "go", messageId: "m1" }), []);
    expect(steerByMessage?.id).toBe("t-user-m1");
    const steerDefault = u.streamPacketToWaterfallItem("t", packet({ _type: "ContinuationStarted", continuationMode: "steer", content: "go" }), []);
    expect(steerDefault?.id).toBe("t-user-steer");
    // 1528/1529: correction attempt/max fallbacks
    const correction = u.streamPacketToWaterfallItem("t", packet({ _type: "ContinuationStarted", continuationMode: "story_generation_correction" }), []);
    expect(correction?.content).toContain("1/1");
    // Rejected candidates stay system errors; committed tier misses are notices.
    const validationError = u.streamPacketToWaterfallItem("t", packet({ _type: "StoryGenerationValidation", passed: false }), []);
    expect(validationError?.type).toBe("system");
    const validationWarning = u.streamPacketToWaterfallItem("t", packet({
      _type: "StoryGenerationValidation",
      passed: true,
      chapterLengthTier: "medium",
      tierHit: false
    }), []);
    expect(validationWarning?.type).toBe("notice");
    const validationOk = u.streamPacketToWaterfallItem("t", packet({ _type: "StoryGenerationValidation", passed: true }), []);
    expect(validationOk?.type).toBe("system");
    // 1610/1611: AgentError title + message fallbacks
    const errorDefaultTitle = u.streamPacketToWaterfallItem("t", packet({ _type: "AgentError" }), []);
    expect(errorDefaultTitle?.title).toBe("Coomi error");
    expect(errorDefaultTitle?.content).toBe("Coomi execution failed.");
    const errorNamed = u.streamPacketToWaterfallItem("t", packet({ _type: "AgentError", error_type: "provider", message: "bad" }), []);
    expect(errorNamed?.title).toBe("provider");
  });

  it("streamPacketToTraceEvent uses the 'event' fallback name", () => {
    // 1440: no _type / type -> "event"
    expect(u.streamPacketToTraceEvent(packet({}), 1).event).toBe("event");
  });

  it("mergeWaterfallItem uses candidate content when a non-append item has empty content", () => {
    // 1671: non-append (system) item with empty content keeps candidate content
    const existing = [u.createWaterfallItem({ id: "sys", type: "system", status: "info", title: "S", content: "keep" })];
    const merged = u.mergeWaterfallItem(existing, u.createWaterfallItem({ id: "sys", type: "system", status: "info", title: "S", content: "" }));
    expect(merged[0].content).toBe("keep");
    const replaced = u.mergeWaterfallItem(existing, u.createWaterfallItem({ id: "sys", type: "system", status: "info", title: "S", content: "new" }));
    expect(replaced[0].content).toBe("new");
  });

  it("summarizeTurnContractPacket word-count and operation fallbacks", () => {
    // 1773: min === max -> "N 字"
    const equalRange = u.summarizeTurnContractPacket(packet({
      turnPlan: { fragmentWordCountMin: 2000, fragmentWordCountMax: 2000 }
    }));
    expect(equalRange).toContain("2000 字");
    // 1795: unknown operationType -> raw value; 1798: unknown complexity -> raw value
    const unknownLabels = u.summarizeTurnContractPacket(packet({
      intentFrame: { operationType: "weird_op", complexity: "medium" }
    }));
    expect(unknownLabels).toContain("weird_op");
    expect(unknownLabels).toContain("复杂度：medium");
    // 1811: selectedTemplate used when no detail name
    const templateOnly = u.summarizeTurnContractPacket(packet({
      turnPlan: { selectedChapterTemplate: "tmpl-id" }
    }));
    expect(templateOnly).toContain("模板：tmpl-id");
    // 1838: autoUpdateWiki false -> 变量后询问
    expect(u.summarizeTurnContractPacket(packet({ updatePolicy: { autoUpdateWiki: false } }))).toContain("WIKI：变量后询问");
    expect(u.summarizeTurnContractPacket(packet({ updatePolicy: { autoUpdateWiki: true } }))).toContain("WIKI：自动更新");
  });

  it("summarizeStoryGenerationValidationPacket fragment and difference fallbacks", () => {
    // 1848/1849: non-record fragment + path fallback; 1859 difference 0 -> no diff label
    const summary = u.summarizeStoryGenerationValidationPacket(packet({
      passed: true,
      fragments: [null, { generatedWordCount: 100, difference: 0 }]
    }));
    expect(summary).toContain("片段 1");
    expect(summary).toContain("片段 2");
    expect(summary).not.toContain("差");
    // 1864: more than 6 fragments -> "另有 N 个片段"
    const many = u.summarizeStoryGenerationValidationPacket(packet({
      passed: false,
      fragments: Array.from({ length: 8 }, (_, i) => ({ path: `p${i}`, generatedWordCount: 10 }))
    }));
    expect(many).toContain("另有 2 个片段");
    // 1869: writeToolApplied false note
    const writeNote = u.summarizeStoryGenerationValidationPacket(packet({ passed: false, writeToolApplied: false, fragments: [] }));
    expect(writeNote).toContain("未成功执行受约束正文写入");
  });

  it("summarizeContextAssembly source-kind fallback", () => {
    // 1898: source without kind is dropped, count defaults to 0
    const summary = u.summarizeContextAssembly({
      budget: { blockCount: 2, totalChars: 10 },
      sources: [{ kind: "chapter" }, { count: 5 }]
    });
    expect(summary).toContain("chapter=0");
    expect(summary).not.toContain("=5");
  });

  it("stripDsmlToolText keeps ordinary lines and clears full tool payloads", () => {
    // 1910: value falls back to "" for null
    expect(u.stripDsmlToolText(null)).toBe("");
    // 1940: cleaned still contains dsml tool markup -> ""
    expect(u.stripDsmlToolText("<||dsml tool_calls invoke parameter")).toBe("");
    // keeps a plain dsml-mention line
    expect(u.stripDsmlToolText("talking about dsml here")).toContain("dsml");
  });

  it("normalizeHistoryRun prompt and noRestorePoint execution fallback", () => {
    // 1998: prompt fallback ""; 2021: execution.noRestorePoint fallback
    const run = u.normalizeHistoryRun({
      traceId: "t",
      reply: "r",
      status: "completed",
      execution: { noRestorePoint: true }
    }, "session");
    expect(run?.prompt).toBe("");
    expect(run?.noRestorePoint).toBe(true);
  });

  it("normalizeChangeLedger and normalizeCommitPrompt sessionId + count fallbacks", () => {
    // 2349: changedFileCount NaN falls back to changedFiles.length; 2356 sessionId fallback chain
    const ledger = u.normalizeChangeLedger(packet({ changedFiles: ["a\\b.md"] }), "trace", "", { sessionId: "fb" } as never);
    expect(ledger.sessionId).toBe("fb");
    expect(ledger.changedFiles).toEqual(["a/b.md"]);
    // 2374: backslash normalization + filter empties; 2379 sessionId fallback ""
    const prompt = u.normalizeCommitPrompt(packet({ changedFiles: ["x\\y.md", "", null] }), "trace", "");
    expect(prompt.changedFiles).toEqual(["x/y.md"]);
    expect(prompt.sessionId).toBe("");
    // 2439: diffSource working_tree when files present but no commit hash
    const merged = u.mergeChangeLedgerPaths(null, ["a.md"], "t", "s");
    expect(merged.diffSource).toBe("working_tree");
  });

  it("clampInteger and normalizePositiveInteger fallbacks", () => {
    // 3003/3004: unparsable -> fallback
    expect(u.clampInteger(undefined, 1, 10, 7)).toBe(7);
    expect(u.clampInteger("5", 1, 10, 7)).toBe(5);
    // 3011/3012: unparsable -> fallback; parsable clamped to >= 1
    expect(u.normalizePositiveInteger("bad", 3)).toBe(3);
    expect(u.normalizePositiveInteger("0", 3)).toBe(1);
    expect(u.normalizePositiveInteger("8", 3)).toBe(8);
  });

  it("normalizeFollowupPacket status normalization and id guards", () => {
    // missing messageId/content -> null
    expect(u.normalizeFollowupPacket(packet({ content: "x" }), "s")).toBeNull();
    expect(u.normalizeFollowupPacket(packet({ messageId: "m" }), "s")).toBeNull();
    // 2959: mode steer, 2960/2963 unknown status -> pending
    const steer = u.normalizeFollowupPacket(packet({ messageId: "m", content: "c", mode: "steer", status: "weird" }), "s");
    expect(steer?.mode).toBe("steer");
    expect(steer?.status).toBe("pending");
    // 2972 sessionId fallback chain, 2973 activeTraceId fallback ""
    const queued = u.normalizeFollowupPacket(packet({ messageId: "m", content: "c" }), "");
    expect(queued?.sessionId).toBe("default");
    expect(queued?.activeTraceId).toBe("");
  });
});
