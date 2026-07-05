import axios, { AxiosHeaders } from "axios";
import type { ApiAuditRecord, ApiEnvelope, ApiResult, ApiTrace } from "@/types/api";

let currentAuthToken = "";

function resolveApiBaseUrl(): string {
  const envBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim();
  if (envBaseUrl) {
    return envBaseUrl;
  }

  const desktopBaseUrl =
    typeof window !== "undefined" ? window.storydexDesktop?.backendBaseUrl?.trim() || "" : "";
  if (desktopBaseUrl) {
    return desktopBaseUrl;
  }

  if (typeof window !== "undefined" && window.location.protocol === "file:") {
    return "http://127.0.0.1:18081/api/v1";
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
  if (!currentAuthToken) {
    return config;
  }

  const headers = AxiosHeaders.from(config.headers ?? {});
  headers.set("Authorization", `Bearer ${currentAuthToken}`);
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
    return error.message || fallbackMessage;
  }

  if (error instanceof Error) {
    return error.message;
  }

  return fallbackMessage;
}
