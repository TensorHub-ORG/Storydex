import axios from "axios";
import { ApiResponseError, apiClient, describeTransportError, unwrapEnvelope } from "@/api/client";
import { isTauriRuntime } from "@/desktop/tauriDesktop";
import type { ApiEnvelope, ApiResult } from "@/types/api";
import type {
  AgentSettingsResponse,
  AgentSettingsUpdateRequest,
  FeedbackSubmitRequest,
  FeedbackSubmitResponse,
  SystemBootstrapResponse,
  SystemHealthResponse,
  ToolFailureAnalysisRequest,
  ToolFailureAnalysisResponse,
  UIPreferencesResponse,
  UIPreferencesUpdateRequest
} from "@/types/system";

export class SystemApiError extends ApiResponseError {}

export const STORYDEX_AGENTD_RUNTIME = "storydex-agentd";

/**
 * A Tauri window must only talk to the Rust sidecar started by its shell.
 * Browser development keeps the legacy Python endpoint compatible so the
 * backend test and development workflows remain usable.
 */
export function validateSystemHealthRuntime(
  data: SystemHealthResponse,
  desktopRuntime = isTauriRuntime()
): void {
  if (!desktopRuntime) {
    return;
  }

  const actualRuntime = String(data.runtime || "").trim();
  const actualVersion = String(data.version || "").trim();
  const bridgeRuntime = typeof window !== "undefined"
    ? String(window.storydexDesktop?.backendRuntime || "").trim()
    : "";
  const bridgeVersion = typeof window !== "undefined"
    ? String(window.storydexDesktop?.backendRuntimeVersion || "").trim()
    : "";
  const expectedRuntime = bridgeRuntime || STORYDEX_AGENTD_RUNTIME;
  const runtimeMatches = actualRuntime === STORYDEX_AGENTD_RUNTIME && actualRuntime === expectedRuntime;
  const versionMatches = !bridgeVersion || !actualVersion || bridgeVersion === actualVersion;

  if (runtimeMatches && actualVersion && versionMatches) {
    return;
  }

  const desktopVersion = typeof window !== "undefined"
    ? String(window.storydexDesktop?.versions?.tauri || "").trim()
    : "";
  throw new SystemApiError(
    actualRuntime
      ? `桌面端连接到了不兼容的后端运行时（${actualRuntime}${actualVersion ? ` ${actualVersion}` : ""}）。请完全退出 Storydex 后重新启动。`
      : "桌面端未连接到 Storydex Rust 后端。请完全退出 Storydex 后重新启动，避免继续使用旧版 Python 后端。",
    "runtime_mismatch",
    {
      expectedRuntime: STORYDEX_AGENTD_RUNTIME,
      actualRuntime: actualRuntime || null,
      actualVersion: actualVersion || null,
      expectedBridgeRuntime: bridgeRuntime || null,
      expectedBridgeVersion: bridgeVersion || null,
      desktopVersion: desktopVersion || null,
      backendBaseUrl: typeof window !== "undefined" ? window.storydexDesktop?.backendBaseUrl || null : null
    }
  );
}

function rethrowSystemError(error: unknown, fallbackMessage: string): never {
  if (error instanceof SystemApiError) {
    throw error;
  }

  if (error instanceof ApiResponseError) {
    throw new SystemApiError(error.message, error.code, error.details, error.trace, error.audit);
  }

  if (axios.isAxiosError(error)) {
    const body = error.response?.data as ApiEnvelope<unknown> | undefined;
    throw new SystemApiError(
      body?.error?.message ?? describeTransportError(error, fallbackMessage),
      body?.error?.code,
      body?.error?.details,
      body?.trace ?? null,
      body?.audit ?? []
    );
  }

  throw error;
}

export async function fetchSystemHealth(): Promise<ApiResult<SystemHealthResponse>> {
  try {
    const response = await apiClient.get<ApiEnvelope<SystemHealthResponse>>("/sys/health");
    const result = unwrapEnvelope(response.data, "System health request failed.");
    validateSystemHealthRuntime(result.data);
    return result;
  } catch (error: unknown) {
    rethrowSystemError(error, "System health request failed.");
  }
}

export async function fetchSystemBootstrap(): Promise<ApiResult<SystemBootstrapResponse>> {
  try {
    const response = await apiClient.get<ApiEnvelope<SystemBootstrapResponse>>("/sys/bootstrap");
    return unwrapEnvelope(response.data, "System bootstrap request failed.");
  } catch (error: unknown) {
    rethrowSystemError(error, "System bootstrap request failed.");
  }
}

export async function fetchUiPreferences(): Promise<ApiResult<UIPreferencesResponse>> {
  try {
    const response = await apiClient.get<ApiEnvelope<UIPreferencesResponse>>("/sys/ui-preferences");
    return unwrapEnvelope(response.data, "UI preferences request failed.");
  } catch (error: unknown) {
    rethrowSystemError(error, "UI preferences request failed.");
  }
}

export async function updateUiPreferences(
  payload: UIPreferencesUpdateRequest
): Promise<ApiResult<UIPreferencesResponse>> {
  try {
    const response = await apiClient.put<ApiEnvelope<UIPreferencesResponse>>("/sys/ui-preferences", payload);
    return unwrapEnvelope(response.data, "UI preferences update failed.");
  } catch (error: unknown) {
    rethrowSystemError(error, "UI preferences update failed.");
  }
}

export async function fetchAgentSettings(): Promise<ApiResult<AgentSettingsResponse>> {
  try {
    const response = await apiClient.get<ApiEnvelope<AgentSettingsResponse>>("/sys/agent-settings");
    return unwrapEnvelope(response.data, "Agent settings request failed.");
  } catch (error: unknown) {
    rethrowSystemError(error, "Agent settings request failed.");
  }
}

export async function updateAgentSettings(
  payload: AgentSettingsUpdateRequest
): Promise<ApiResult<AgentSettingsResponse>> {
  try {
    const response = await apiClient.put<ApiEnvelope<AgentSettingsResponse>>("/sys/agent-settings", payload);
    return unwrapEnvelope(response.data, "Agent settings update failed.");
  } catch (error: unknown) {
    rethrowSystemError(error, "Agent settings update failed.");
  }
}

export async function submitFeedback(
  payload: FeedbackSubmitRequest
): Promise<ApiResult<FeedbackSubmitResponse>> {
  try {
    const response = await apiClient.post<ApiEnvelope<FeedbackSubmitResponse>>("/sys/feedback", payload);
    return unwrapEnvelope(response.data, "Feedback submission failed.");
  } catch (error: unknown) {
    rethrowSystemError(error, "Feedback submission failed.");
  }
}

export async function analyzeToolFailures(
  payload: ToolFailureAnalysisRequest
): Promise<ApiResult<ToolFailureAnalysisResponse>> {
  try {
    const response = await apiClient.post<ApiEnvelope<ToolFailureAnalysisResponse>>(
      "/sys/feedback/tool-analysis",
      payload
    );
    return unwrapEnvelope(response.data, "Tool failure analysis failed.");
  } catch (error: unknown) {
    rethrowSystemError(error, "Tool failure analysis failed.");
  }
}

