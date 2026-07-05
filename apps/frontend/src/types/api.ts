export interface ApiError {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export type ApiAuditRecord = Record<string, unknown>;

export interface ApiTrace {
  traceId: string;
  durationMs: number;
  toolCalls: number;
  llmCalls: number;
  promptTokens: number;
  completionTokens: number;
  estimatedCost: number;
  cacheReadInputTokens: number;
  cacheCreationInputTokens: number;
  cacheHitRatio: number;
  cacheSavings: number;
}

export interface ApiEnvelope<T> {
  ok: boolean;
  data: T | null;
  error: ApiError | null;
  trace: ApiTrace | null;
  audit: ApiAuditRecord[];
}

export interface ApiResult<T> {
  data: T;
  trace: ApiTrace | null;
  audit: ApiAuditRecord[];
}
