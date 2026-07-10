import { describe, expect, it } from "vitest";
import { parseSseFrameData, splitSseFrames } from "@/api/sseParser.mjs";

describe("SSE parser", () => {
  it("emits completed LF and CRLF frames without waiting for stream completion", () => {
    expect(splitSseFrames('data: {"type":"RunAccepted"}\n\ndata: partial')).toEqual({
      frames: ['data: {"type":"RunAccepted"}'],
      rest: "data: partial"
    });
    expect(splitSseFrames('data: {"type":"TurnPhase"}\r\n\r\n')).toEqual({
      frames: ['data: {"type":"TurnPhase"}'],
      rest: ""
    });
  });

  it("joins multiple data lines and preserves explicit event names", () => {
    expect(parseSseFrameData('event: heartbeat\ndata: {"elapsedMs":500,\ndata: "heartbeat":true}')).toEqual({
      type: "heartbeat",
      elapsedMs: 500,
      heartbeat: true
    });
  });

  it.each(["RunAccepted", "TurnPhase", "TextChunk", "heartbeat"])("parses %s packets", (type) => {
    expect(parseSseFrameData(`data: {"type":"${type}","content":"ok"}`)).toMatchObject({ type, content: "ok" });
  });

  it("ignores comments, empty data, and invalid JSON", () => {
    expect(splitSseFrames(undefined)).toEqual({ frames: [], rest: "" });
    expect(parseSseFrameData(undefined)).toBeNull();
    expect(parseSseFrameData(": keep-alive\n\n")).toBeNull();
    expect(parseSseFrameData("event: phase")).toBeNull();
    expect(parseSseFrameData("data: {broken")).toBeNull();
  });
});
