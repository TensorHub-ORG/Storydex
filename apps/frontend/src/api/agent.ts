import { ApiResponseError, apiClient, getApiAuthToken, unwrapEnvelope } from "@/api/client";
import type {
  AgentChatRequest,
  AgentChatResponse,
  AgentCommitDecisionRequest,
  AgentCoomiConfigResponse,
  AgentCoomiModelListRequest,
  AgentCoomiModelListResponse,
  AgentCoomiConfigUpdateRequest,
  AgentCoomiStatusResponse,
  AgentHistoryResponse,
  AgentSessionsResponse,
  AgentStreamPacket
} from "@/types/agent";
import type { ApiEnvelope, ApiResult, ApiTrace } from "@/types/api";
import type { WorkspaceGitDiffResponse } from "@/types/workspace";

export class AgentApiError extends ApiResponseError {}

function buildTraceHeaders(traceId?: string): Record<string, string> | undefined {
  return traceId ? { "x-trace-id": traceId } : undefined;
}

function buildSessionParams(sessionId?: string): Record<string, string> | undefined {
  const normalizedSessionId = String(sessionId || "").trim();
  return normalizedSessionId ? { sessionId: normalizedSessionId } : undefined;
}

function appendSessionQuery(url: string, sessionId?: string): string {
  const normalizedSessionId = String(sessionId || "").trim();
  if (!normalizedSessionId) {
    return url;
  }
  const parsed = new URL(url, window.location.origin);
  parsed.searchParams.set("sessionId", normalizedSessionId);
  return parsed.toString();
}

export async function sendAgentPrompt(
  payload: AgentChatRequest,
  traceId?: string,
  sessionId?: string
): Promise<ApiResult<AgentChatResponse>> {
  const response = await apiClient.post<ApiEnvelope<AgentChatResponse>>("/agent/chat", payload, {
    timeout: 180000,
    headers: buildTraceHeaders(traceId),
    params: buildSessionParams(sessionId)
  });

  try {
    return unwrapEnvelope(response.data, "Coomi request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new AgentApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function streamAgentPrompt(
  payload: AgentChatRequest,
  onMessage: (packet: AgentStreamPacket) => void,
  traceId?: string,
  sessionId?: string,
  signal?: AbortSignal
): Promise<void> {
  try {
    const authToken = getApiAuthToken();
    const response = await fetch(appendSessionQuery(resolveApiUrl("/agent/chat/stream"), sessionId), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
        ...(buildTraceHeaders(traceId) ?? {})
      },
      body: JSON.stringify(payload),
      signal
    });

    if (!response.ok) {
      throw await buildStreamResponseError(response);
    }
    if (!response.body) {
      throw new AgentApiError("Coomi stream response is unavailable.");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let sawTerminalPacket = false;
    let deferredAgentError: AgentApiError | null = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        if (sawTerminalPacket) {
          return;
        }
        throw new AgentApiError("Coomi stream ended before completion.", "stream_incomplete");
      }

      buffer += decoder.decode(value, { stream: true });
      const parsed = splitSseFrames(buffer);
      buffer = parsed.rest;

      for (const frame of parsed.frames) {
        const packet = parseSseFrame(frame);
        if (!packet) {
          continue;
        }

        if (packet.type === "error" && packet.error) {
          sawTerminalPacket = true;
          throw new AgentApiError(
            packet.error.message,
            packet.error.code,
            packet.error.details,
            normalizeTrace(packet.trace),
            normalizeAudit(packet.audit)
          );
        }

        if (packet.type === "AgentError") {
          sawTerminalPacket = true;
          onMessage(packet);
          deferredAgentError = new AgentApiError(
            String(packet.message || "Coomi execution failed."),
            String(packet.error_type || "coomi_error")
          );
          continue;
        }

        if (packet.type === "AgentCompleted" || packet.type === "AgentCancelled" || packet.type === "final") {
          sawTerminalPacket = true;
        }

        if (packet.type === "done") {
          if (deferredAgentError) {
            throw deferredAgentError;
          }
          return;
        }

        onMessage(packet);
      }
    }
  } catch (error: unknown) {
    if (isAbortError(error)) {
      throw new AgentApiError("Current Coomi run was stopped.", "request_aborted");
    }
    throw error;
  }
}

export async function fetchAgentHistory(limit = 40, sessionId?: string): Promise<ApiResult<AgentHistoryResponse>> {
  const response = await apiClient.get<ApiEnvelope<AgentHistoryResponse>>("/agent/history", {
    params: { limit, sessionId: sessionId || undefined }
  });

  try {
    return unwrapEnvelope(response.data, "Coomi history request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new AgentApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function fetchAgentSessions(): Promise<ApiResult<AgentSessionsResponse>> {
  const response = await apiClient.get<ApiEnvelope<AgentSessionsResponse>>("/agent/sessions");

  try {
    return unwrapEnvelope(response.data, "Coomi sessions request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new AgentApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function deleteAgentSession(
  sessionId: string
): Promise<ApiResult<{ deleted: boolean; sessionId: string; removedCount: number }>> {
  const response = await apiClient.post<ApiEnvelope<{ deleted: boolean; sessionId: string; removedCount: number }>>(
    "/agent/sessions/delete",
    { sessionId }
  );

  try {
    return unwrapEnvelope(response.data, "Coomi session delete request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new AgentApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function fetchAgentCoomiStatus(): Promise<ApiResult<AgentCoomiStatusResponse>> {
  const response = await apiClient.get<ApiEnvelope<AgentCoomiStatusResponse>>("/agent/coomi/status");

  try {
    return unwrapEnvelope(response.data, "Coomi status request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new AgentApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function fetchAgentCoomiConfig(): Promise<ApiResult<AgentCoomiConfigResponse>> {
  const response = await apiClient.get<ApiEnvelope<AgentCoomiConfigResponse>>("/agent/coomi/config");

  try {
    return unwrapEnvelope(response.data, "Coomi config request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new AgentApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function updateAgentCoomiConfig(
  payload: AgentCoomiConfigUpdateRequest
): Promise<ApiResult<AgentCoomiConfigResponse>> {
  const response = await apiClient.put<ApiEnvelope<AgentCoomiConfigResponse>>("/agent/coomi/config", payload);

  try {
    return unwrapEnvelope(response.data, "Coomi config update failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new AgentApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function fetchAgentCoomiModels(
  payload: AgentCoomiModelListRequest
): Promise<ApiResult<AgentCoomiModelListResponse>> {
  const response = await apiClient.post<ApiEnvelope<AgentCoomiModelListResponse>>("/agent/coomi/models", payload, {
    timeout: 30000
  });

  try {
    return unwrapEnvelope(response.data, "Coomi model list request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new AgentApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function cycleAgentCoomiPermission(): Promise<ApiResult<{ permissionMode: string; permissionLabel: string }>> {
  const response = await apiClient.post<ApiEnvelope<{ permissionMode: string; permissionLabel: string }>>(
    "/agent/coomi/permission/cycle",
    {}
  );

  try {
    return unwrapEnvelope(response.data, "Coomi permission cycle request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new AgentApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function setAgentCoomiPermission(
  permissionMode: string
): Promise<ApiResult<{ permissionMode: string; permissionLabel: string }>> {
  const response = await apiClient.post<ApiEnvelope<{ permissionMode: string; permissionLabel: string }>>(
    "/agent/coomi/permission",
    { permissionMode }
  );

  try {
    return unwrapEnvelope(response.data, "Coomi permission update request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new AgentApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function resolveAgentCoomiApproval(
  approvalId: string,
  decision: "allow" | "deny" | "cancel" | "answer",
  approvalResponse?: Record<string, unknown>
): Promise<ApiResult<{ accepted: boolean; approvalId: string; decision: string }>> {
  const response = await apiClient.post<ApiEnvelope<{ accepted: boolean; approvalId: string; decision: string }>>(
    "/agent/coomi/approval",
    { approvalId, decision, response: approvalResponse || {} }
  );

  try {
    return unwrapEnvelope(response.data, "Coomi approval request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new AgentApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function clearConversation(sessionId?: string): Promise<ApiResult<{ cleared: boolean }>> {
  const response = await apiClient.post<ApiEnvelope<{ cleared: boolean }>>(
    "/agent/clear-conversation",
    {},
    { params: { sessionId: sessionId || undefined } }
  );

  try {
    return unwrapEnvelope(response.data, "Clear Coomi conversation request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new AgentApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function fetchAgentRunDiff(
  traceId: string,
  sessionId?: string,
  changedFiles?: string[],
  commitHash?: string
): Promise<ApiResult<WorkspaceGitDiffResponse>> {
  const normalizedChangedFiles = Array.isArray(changedFiles)
    ? changedFiles.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
  const response = await apiClient.get<ApiEnvelope<WorkspaceGitDiffResponse>>(
    `/agent/runs/${encodeURIComponent(traceId)}/diff`,
    {
      params: {
        sessionId: sessionId || undefined,
        changedFiles: normalizedChangedFiles.length ? normalizedChangedFiles.join("\n") : undefined,
        commitHash: commitHash || undefined
      }
    }
  );

  try {
    return unwrapEnvelope(response.data, "Agent run diff request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new AgentApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

export async function submitAgentRunCommitDecision(
  traceId: string,
  payload: AgentCommitDecisionRequest,
  sessionId?: string
): Promise<ApiResult<AgentStreamPacket>> {
  const response = await apiClient.post<ApiEnvelope<AgentStreamPacket>>(
    `/agent/runs/${encodeURIComponent(traceId)}/commit`,
    payload,
    {
      params: { sessionId: sessionId || payload.sessionId || undefined }
    }
  );

  try {
    return unwrapEnvelope(response.data, "Agent commit decision request failed.");
  } catch (error: unknown) {
    if (error instanceof ApiResponseError) {
      throw new AgentApiError(error.message, error.code, error.details, error.trace, error.audit);
    }
    throw error;
  }
}

function resolveApiUrl(path: string): string {
  const rawBase = String(apiClient.defaults.baseURL || "/api/v1").trim() || "/api/v1";
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;

  if (/^https?:\/\//i.test(rawBase)) {
    const base = rawBase.endsWith("/") ? rawBase : `${rawBase}/`;
    return new URL(normalizedPath.slice(1), base).toString();
  }

  const basePath = rawBase.startsWith("/") ? rawBase : `/${rawBase}`;
  return `${window.location.origin}${basePath.replace(/\/$/, "")}${normalizedPath}`;
}

async function buildStreamResponseError(response: Response): Promise<AgentApiError> {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      const body = (await response.json()) as ApiEnvelope<unknown>;
      return new AgentApiError(
        body.error?.message || `Coomi request failed (${response.status}).`,
        body.error?.code,
        body.error?.details,
        body.trace ?? null,
        body.audit ?? []
      );
    } catch {
      return new AgentApiError(`Coomi request failed (${response.status}).`);
    }
  }

  const text = await response.text();
  return new AgentApiError(text || `Coomi request failed (${response.status}).`);
}

function splitSseFrames(buffer: string): { frames: string[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const parts = normalized.split("\n\n");
  return {
    frames: parts.slice(0, -1),
    rest: parts[parts.length - 1] || ""
  };
}

function parseSseFrame(frame: string): AgentStreamPacket | null {
  const lines = frame.split("\n");
  let eventName = "message";
  const dataLines: string[] = [];

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    if (!line || line.startsWith(":")) {
      continue;
    }
    if (line.startsWith("event:")) {
      eventName = line.slice("event:".length).trim();
      continue;
    }
    if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }

  if (dataLines.length === 0) {
    return null;
  }

  try {
    const parsed = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;
    return {
      type: (typeof parsed.type === "string" ? parsed.type : eventName) as AgentStreamPacket["type"],
      ...parsed
    } as AgentStreamPacket;
  } catch {
    return null;
  }
}

function isAbortError(error: unknown): boolean {
  if (typeof DOMException !== "undefined" && error instanceof DOMException) {
    return error.name === "AbortError";
  }
  return error instanceof Error && error.name === "AbortError";
}

function normalizeTrace(value: unknown): ApiTrace | null {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  const record = value as Record<string, unknown>;
  if (typeof record.traceId !== "string") {
    return null;
  }
  return {
    traceId: record.traceId,
    durationMs: typeof record.durationMs === "number" ? record.durationMs : 0,
    toolCalls: typeof record.toolCalls === "number" ? record.toolCalls : 0,
    llmCalls: typeof record.llmCalls === "number" ? record.llmCalls : 0,
    promptTokens: typeof record.promptTokens === "number" ? record.promptTokens : 0,
    completionTokens: typeof record.completionTokens === "number" ? record.completionTokens : 0,
    estimatedCost: typeof record.estimatedCost === "number" ? record.estimatedCost : 0,
    cacheReadInputTokens: typeof record.cacheReadInputTokens === "number" ? record.cacheReadInputTokens : 0,
    cacheCreationInputTokens: typeof record.cacheCreationInputTokens === "number" ? record.cacheCreationInputTokens : 0,
    cacheHitRatio: typeof record.cacheHitRatio === "number" ? record.cacheHitRatio : 0,
    cacheSavings: typeof record.cacheSavings === "number" ? record.cacheSavings : 0
  };
}

function normalizeAudit(value: unknown): Record<string, unknown>[] {
  if (Array.isArray(value)) {
    return value.filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null);
  }
  if (typeof value === "object" && value !== null) {
    return [value as Record<string, unknown>];
  }
  return [];
}
