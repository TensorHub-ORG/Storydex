import axios, { AxiosHeaders } from "axios";
import type { ApiAuditRecord, ApiEnvelope, ApiResult, ApiTrace } from "@/types/api";

let currentAuthToken = "";

export function getRuntimeAuthToken(): string {
  return typeof window !== "undefined"
    ? window.storydexDesktop?.backendAuthToken?.trim() || ""
    : "";
}

function resolveApiBaseUrl(): string {
  // The Tauri sidecar owns the dynamically assigned Rust runtime endpoint.
  // Prefer it whenever the desktop shell injects one so stale developer
  // environment variables cannot redirect the app to the legacy backend.
  const desktopBaseUrl =
    typeof window !== "undefined" ? window.storydexDesktop?.backendBaseUrl?.trim() || "" : "";
  if (desktopBaseUrl) {
    return desktopBaseUrl;
  }

  // Browser-only development may still opt into an explicit API server.
  const envBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim();
  if (envBaseUrl) {
    return envBaseUrl;
  }

  return "/api/v1";
}

export const apiClient = axios.create({
  baseURL: resolveApiBaseUrl(),
  timeout: 180000
});

export function setApiAuthToken(token: string): void {
  currentAuthToken = token.trim();
}

export function getApiAuthToken(): string {
  return currentAuthToken;
}

apiClient.interceptors.request.use((config) => {
  const runtimeAuthToken = getRuntimeAuthToken();
  if (!currentAuthToken && !runtimeAuthToken) {
    return config;
  }

  const headers = AxiosHeaders.from(config.headers ?? {});
  if (runtimeAuthToken) {
    headers.set("X-Storydex-Runtime-Token", runtimeAuthToken);
  }
  if (currentAuthToken) {
    headers.set("Authorization", `Bearer ${currentAuthToken}`);
  }
  config.headers = headers;
  return config;
});

export class ApiResponseError extends Error {
  code?: string;
  details?: Record<string, unknown>;
  trace: ApiTrace | null;
  audit: ApiAuditRecord[];

  constructor(
    message: string,
    code?: string,
    details?: Record<string, unknown>,
    trace: ApiTrace | null = null,
    audit: ApiAuditRecord[] = []
  ) {
    super(message);
    this.name = "ApiResponseError";
    this.code = code;
    this.details = details;
    this.trace = trace;
    this.audit = audit;
  }
}

export function unwrapEnvelope<T>(body: ApiEnvelope<T>, fallbackMessage: string): ApiResult<T> {
  if (!body.ok || !body.data) {
    throw new ApiResponseError(
      body.error?.message ?? fallbackMessage,
      body.error?.code,
      body.error?.details,
      body.trace,
      body.audit ?? []
    );
  }

  return {
    data: body.data,
    trace: body.trace,
    audit: body.audit ?? []
  };
}

export function describeTransportError(error: unknown, fallbackMessage: string): string {
  if (axios.isAxiosError(error)) {
    if (!error.response) {
      if (error.code === "ECONNABORTED") {
        return "后端请求超时，请稍后重试。";
      }
      return "无法连接后端服务，请确认应用后端已经启动。";
    }
    // 后端 StorydexError 会把真实错误消息放在 envelope.error.message 里。
    // axios 收到 4xx/5xx 会直接抛 AxiosError，跳过 unwrapEnvelope，
    // 所以需要从 error.response.data 读取 envelope 里的消息展示给用户。
    const envelopeMessage = readEnvelopeErrorMessage(error.response.data);
    if (envelopeMessage) {
      return envelopeMessage;
    }
    return error.message || fallbackMessage;
  }

  if (error instanceof Error) {
    return error.message;
  }

  return fallbackMessage;
}

function readEnvelopeErrorMessage(data: unknown): string | null {
  if (!data || typeof data !== "object") return null;
  const envelope = data as { error?: { message?: unknown } };
  const message = envelope.error?.message;
  return typeof message === "string" && message.trim() ? message : null;
}
