import axios from "axios";
import { ApiResponseError, apiClient, describeTransportError, unwrapEnvelope } from "@/api/client";
import type { ApiEnvelope, ApiResult } from "@/types/api";
import type {
  AgentSettingsResponse,
  AgentSettingsUpdateRequest,
  SystemBootstrapResponse,
  SystemHealthResponse,
  UIPreferencesResponse,
  UIPreferencesUpdateRequest
} from "@/types/system";

export class SystemApiError extends ApiResponseError {}

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
    return unwrapEnvelope(response.data, "System health request failed.");
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

