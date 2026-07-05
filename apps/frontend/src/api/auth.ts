import axios from "axios";
import { ApiResponseError, apiClient, describeTransportError, unwrapEnvelope } from "@/api/client";
import type { ApiEnvelope, ApiResult } from "@/types/api";
import type {
  AccountMessageResponse,
  AccountSummaryResponse,
  AuthUser,
  ChangePasswordRequest,
  CheckUsernameResponse,
  LoginAccountRequest,
  LoginAccountResponse,
  PersistedSessionResponse,
  RegisterAccountRequest,
  RegisterAccountResponse,
  UpdatePasswordRequest,
  UpdateProfileRequest
} from "@/types/auth";

export class AuthApiError extends ApiResponseError {
  status?: number;
}

function throwAuthApiError(error: unknown, fallbackMessage: string): never {
  if (error instanceof AuthApiError) {
    throw error;
  }

  if (error instanceof ApiResponseError) {
    const next = new AuthApiError(error.message, error.code, error.details, error.trace, error.audit);
    throw next;
  }

  if (axios.isAxiosError(error)) {
    const body = error.response?.data as ApiEnvelope<unknown> | undefined;
    const next = new AuthApiError(
      body?.error?.message ?? describeTransportError(error, fallbackMessage),
      body?.error?.code,
      body?.error?.details,
      body?.trace ?? null,
      body?.audit ?? []
    );
    next.status = error.response?.status;
    throw next;
  }

  throw error;
}

export async function registerAccount(
  payload: RegisterAccountRequest
): Promise<ApiResult<RegisterAccountResponse>> {
  try {
    const response = await apiClient.post<ApiEnvelope<RegisterAccountResponse>>("/auth/register", payload);
    return unwrapEnvelope(response.data, "Register request failed.");
  } catch (error: unknown) {
    throwAuthApiError(error, "Register request failed.");
  }
}

export async function loginAccount(payload: LoginAccountRequest): Promise<ApiResult<LoginAccountResponse>> {
  try {
    const response = await apiClient.post<ApiEnvelope<LoginAccountResponse>>("/auth/login", payload);
    return unwrapEnvelope(response.data, "Login request failed.");
  } catch (error: unknown) {
    throwAuthApiError(error, "Login request failed.");
  }
}

export async function fetchPersistedSession(): Promise<ApiResult<PersistedSessionResponse>> {
  try {
    const response = await apiClient.get<ApiEnvelope<PersistedSessionResponse>>("/auth/session");
    return unwrapEnvelope(response.data, "Persisted session request failed.");
  } catch (error: unknown) {
    throwAuthApiError(error, "Persisted session request failed.");
  }
}

export async function fetchCurrentAccount(): Promise<ApiResult<AuthUser>> {
  try {
    const response = await apiClient.get<ApiEnvelope<AuthUser>>("/auth/me");
    return unwrapEnvelope(response.data, "Current account request failed.");
  } catch (error: unknown) {
    throwAuthApiError(error, "Current account request failed.");
  }
}

export async function fetchAccountSummary(): Promise<ApiResult<AccountSummaryResponse>> {
  try {
    const response = await apiClient.get<ApiEnvelope<AccountSummaryResponse>>("/auth/account-summary");
    return unwrapEnvelope(response.data, "Account summary request failed.");
  } catch (error: unknown) {
    throwAuthApiError(error, "Account summary request failed.");
  }
}

export async function updateAccountProfile(payload: UpdateProfileRequest): Promise<ApiResult<AuthUser>> {
  try {
    const response = await apiClient.put<ApiEnvelope<AuthUser>>("/auth/profile", payload);
    return unwrapEnvelope(response.data, "Profile update failed.");
  } catch (error: unknown) {
    throwAuthApiError(error, "Profile update failed.");
  }
}

export async function changeAccountPassword(
  payload: ChangePasswordRequest | UpdatePasswordRequest
): Promise<ApiResult<AccountMessageResponse>> {
  try {
    const response = await apiClient.put<ApiEnvelope<AccountMessageResponse>>("/auth/password", payload);
    return unwrapEnvelope(response.data, "Password update failed.");
  } catch (error: unknown) {
    throwAuthApiError(error, "Password update failed.");
  }
}

export async function logoutAccount(): Promise<ApiResult<AccountMessageResponse>> {
  try {
    const response = await apiClient.post<ApiEnvelope<AccountMessageResponse>>("/auth/logout");
    return unwrapEnvelope(response.data, "Logout request failed.");
  } catch (error: unknown) {
    throwAuthApiError(error, "Logout request failed.");
  }
}

export async function checkUsernameAvailability(username: string): Promise<ApiResult<CheckUsernameResponse>> {
  try {
    const response = await apiClient.get<ApiEnvelope<CheckUsernameResponse>>(
      `/auth/check-username/${encodeURIComponent(username)}`
    );
    return unwrapEnvelope(response.data, "Username availability request failed.");
  } catch (error: unknown) {
    throwAuthApiError(error, "Username availability request failed.");
  }
}
