import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiClient, setApiAuthToken } from "@/api/client";
import { AgentApiError, streamAgentPrompt } from "@/api/agent";

function streamResponse(frames: string[], options: ResponseInit = {}): Response {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame));
      controller.close();
    }
  }), { status: 200, headers: { "content-type": "text/event-stream" }, ...options });
}

const sse = (packet: Record<string, unknown>) => `data: ${JSON.stringify(packet)}\n\n`;

describe("Coomi streaming API contract", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    setApiAuthToken("");
    apiClient.defaults.baseURL = "/api/v1";
  });

  it("streams fragmented packets with auth, trace and session context", async () => {
    setApiAuthToken("secret");
    const body = sse({ type: "RunAccepted", traceId: "t" }) + sse({ type: "TextChunk", content: "hi" }) + sse({ type: "AgentCompleted" }) + sse({ type: "done" });
    const response = streamResponse([body.slice(0, 17), body.slice(17)]);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(response);
    const packets: Record<string, unknown>[] = [];
    await streamAgentPrompt({ prompt: "hello" }, (packet) => packets.push(packet), "trace-1", "session 1");
    expect(packets.map((packet) => packet.type)).toEqual(["RunAccepted", "TextChunk", "AgentCompleted"]);
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("sessionId=session+1"), expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer secret", "x-trace-id": "trace-1" })
    }));
  });

  it("supports absolute API bases and terminal final packets", async () => {
    apiClient.defaults.baseURL = "https://api.example.test/v1";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(streamResponse([sse({ type: "final" })]));
    await streamAgentPrompt({ prompt: "x" }, vi.fn());
    expect(fetchMock.mock.calls[0][0]).toBe("https://api.example.test/v1/agent/chat/stream");
  });

  it("maps JSON, malformed JSON and text HTTP failures", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(JSON.stringify({ ok: false, error: { message: "denied", code: "no" }, trace: { traceId: "t" }, audit: [{}] }), { status: 403, headers: { "content-type": "application/json" } }));
    await expect(streamAgentPrompt({ prompt: "x" }, vi.fn())).rejects.toMatchObject({ message: "denied", code: "no" });
    vi.mocked(fetch).mockResolvedValueOnce(new Response("{", { status: 500, headers: { "content-type": "application/json" } }));
    await expect(streamAgentPrompt({ prompt: "x" }, vi.fn())).rejects.toThrow("Coomi request failed (500)");
    vi.mocked(fetch).mockResolvedValueOnce(new Response("gateway down", { status: 502, headers: { "content-type": "text/plain" } }));
    await expect(streamAgentPrompt({ prompt: "x" }, vi.fn())).rejects.toThrow("gateway down");
    vi.mocked(fetch).mockResolvedValueOnce(new Response(null, { status: 503 }));
    await expect(streamAgentPrompt({ prompt: "x" }, vi.fn())).rejects.toThrow("Coomi request failed (503)");
  });

  it("rejects missing and incomplete bodies", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(new Response(null, { status: 200 }));
    await expect(streamAgentPrompt({ prompt: "x" }, vi.fn())).rejects.toThrow("stream response is unavailable");
    vi.mocked(fetch).mockResolvedValueOnce(streamResponse([sse({ type: "TextChunk", content: "partial" })]));
    await expect(streamAgentPrompt({ prompt: "x" }, vi.fn())).rejects.toMatchObject({ code: "stream_incomplete" });
  });

  it("maps protocol error packets and normalizes trace/audit details", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(streamResponse([sse({
      type: "error", error: { message: "failed", code: "bad", details: { field: "x" } },
      trace: { traceId: "trace", durationMs: 2, toolCalls: 1, llmCalls: 1, promptTokens: 2, completionTokens: 3, estimatedCost: 4, cacheReadInputTokens: 5, cacheCreationInputTokens: 6, cacheHitRatio: 0.5, cacheSavings: 7 },
      audit: [{ event: "x" }, null, "bad"]
    })]));
    await expect(streamAgentPrompt({ prompt: "x" }, vi.fn())).rejects.toMatchObject({
      message: "failed", code: "bad", trace: { traceId: "trace", durationMs: 2 }, audit: [{ event: "x" }]
    });
  });

  it("defers AgentError until done and maps abort errors", async () => {
    const onMessage = vi.fn();
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(streamResponse([sse({ type: "AgentError", message: "tool failed", error_type: "tool" }) + sse({ type: "done" })]));
    await expect(streamAgentPrompt({ prompt: "x" }, onMessage)).rejects.toMatchObject({ message: "tool failed", code: "tool" });
    expect(onMessage).toHaveBeenCalled();

    vi.mocked(fetch).mockRejectedValueOnce(new DOMException("aborted", "AbortError"));
    await expect(streamAgentPrompt({ prompt: "x" }, vi.fn())).rejects.toMatchObject({ code: "request_aborted" });
    const abort = new Error("aborted"); abort.name = "AbortError";
    vi.mocked(fetch).mockRejectedValueOnce(abort);
    await expect(streamAgentPrompt({ prompt: "x" }, vi.fn())).rejects.toBeInstanceOf(AgentApiError);
  });
});
