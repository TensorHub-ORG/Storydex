import type { ApiAuditRecord, ApiTrace } from "@/types/api";

export interface AgentChatRequest {
  prompt: string;
  activeFile?: string;
  workspaceRoot?: string;
  storyGeneration?: AgentStoryGenerationOptions;
  confirmNoSnapshot?: boolean;
}

export interface AgentPendingSnapshotConfirmation {
  request: AgentChatRequest;
  traceId: string;
  sessionId: string;
  message: string;
  details: Record<string, unknown>;
}

export interface AgentStoryGenerationOptions {
  fragmentCount: number;
  fragmentWordCount: number;
  chapterTemplateId?: string;
  chapterTemplate?: string;
}

export interface AgentTraceEvent {
  index: number;
  event: string;
  phase: string;
  status: string;
  detail: string;
  timestamp: string;
  data?: Record<string, unknown>;
}

export interface AgentChatResponse {
  route: "coomi";
  reply: string;
  llmModel?: string;
  llmProvider?: string;
  events?: AgentTraceEvent[];
  assistant?: Record<string, unknown>;
}

export interface AgentHistoryResponse {
  items: Record<string, unknown>[];
}

export interface AgentSessionSummary {
  sessionId: string;
  firstPrompt: string;
  createdAt: string;
  updatedAt: string;
  traceCount: number;
}

export interface AgentSessionsResponse {
  items: AgentSessionSummary[];
}

export interface AgentCoomiStatusResponse {
  runtime: string;
  installed: boolean;
  home: string;
  configPath: string;
  sessionsPath: string;
  providerId: string;
  providerType: string;
  model: string;
  display: string;
  permissionMode: string;
  permissionLabel?: string;
  planMode?: boolean;
  toolCount: number;
  contextWindow?: number;
  usedTokens?: number;
  usageRatio?: number;
  cumulativeTokens?: number;
  compactThreshold?: number;
  warningThreshold?: number;
  compressionStatus?: string;
}

export interface AgentCoomiConfigResponse {
  configPath: string;
  content: string;
  parsed: Record<string, unknown>;
  updatedAt: string;
}

export interface AgentCoomiConfigUpdateRequest {
  content: string;
}

export interface AgentCoomiModelListRequest {
  baseUrl: string;
  apiKey: string;
}

export interface AgentCoomiModelListResponse {
  endpoint: string;
  models: string[];
}

export type AgentStreamPacketType =
  | "hello"
  | "final"
  | "error"
  | "done"
  | "RunAccepted"
  | "TextChunk"
  | "TextReset"
  | "ReasoningChunk"
  | "ToolStart"
  | "ToolRunning"
  | "ToolDone"
  | "ToolCacheHit"
  | "UsageUpdate"
  | "CompressionEvent"
  | "PermissionRequest"
  | "GitAutoCommit"
  | "GitCommitPrompt"
  | "GitCommitResult"
  | "TaskPlanCreated"
  | "TaskStarted"
  | "TaskCompleted"
  | "TaskFailed"
  | "TaskSkipped"
  | "TaskPlanUpdated"
  | "TurnContract"
  | "TurnPhase"
  | "StageOutput"
  | "AgentStarted"
  | "AgentCompleted"
  | "AgentError"
  | "AgentCancelled";

export interface AgentStreamPacket {
  type: AgentStreamPacketType | string;
  _type?: string;
  _version?: number;
  trace?: ApiTrace | Record<string, unknown> | null;
  audit?: ApiAuditRecord[] | Record<string, unknown> | null;
  traceId?: string;
  route?: string;
  reply?: string;
  data?: AgentChatResponse;
  error?: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  } | null;
  content?: string;
  reason?: string;
  preserve_visible?: boolean;
  phase?: string;
  label?: string;
  status?: string;
  current?: number;
  total?: number;
  detail?: string;
  startedAt?: string;
  elapsedMs?: number;
  heartbeat?: boolean;
  tool_name?: string;
  tool_call_id?: string;
  arguments?: Record<string, unknown>;
  progress?: string;
  is_error?: boolean;
  result_preview?: string;
  duration_ms?: number;
  metrics?: Record<string, unknown>;
  usage?: Record<string, number>;
  context_window?: number;
  contextWindow?: number;
  used_tokens?: number;
  usedTokens?: number;
  usage_ratio?: number;
  usageRatio?: number;
  cumulative_tokens?: number;
  cumulativeTokens?: number;
  compact_threshold?: number;
  compactThreshold?: number;
  warning_threshold?: number;
  warningThreshold?: number;
  compression_status?: string;
  compressionStatus?: string;
  compact_status?: string;
  compactStatus?: string;
  original_messages?: number;
  compressed_messages?: number;
  estimated_tokens?: number;
  last_total_tokens?: number;
  lastTotalTokens?: number;
  strategy?: string;
  original_tokens?: number;
  compressed_tokens?: number;
  summary?: string;
  approval_id?: string;
  approvalId?: string;
  kind?: string;
  question?: string;
  header?: string;
  options?: AgentApprovalOption[];
  allowText?: boolean;
  multiSelect?: boolean;
  taskId?: string;
  order?: number;
  title?: string;
  tasks?: AgentTaskItem[];
  questionIndex?: number;
  questionTotal?: number;
  updatedAt?: string;
  session_id?: string;
  sessionId?: string;
  mode?: string;
  query?: string;
  error_type?: string;
  code?: string;
  message?: string;
  details?: Record<string, unknown>;
  noRestorePoint?: boolean;
  created?: boolean;
  target?: string;
  targetLabel?: string;
  workspaceRoot?: string;
  changedFileCount?: number;
  changedFiles?: string[];
  added?: number;
  removed?: number;
  diffSource?: "working_tree" | "commit" | string;
  commit?: Record<string, unknown> | null;
  commitHash?: string;
  shortHash?: string;
  initialCommit?: Record<string, unknown> | null;
  promptRequired?: boolean;
  generatedMessage?: boolean;
  intentFrame?: Record<string, unknown>;
  executionPolicy?: Record<string, unknown>;
  turnPlan?: Record<string, unknown>;
  assetTargets?: Record<string, unknown>;
  contextPolicy?: Record<string, unknown>;
  skillRegistry?: Record<string, unknown>;
  toolRegistry?: Record<string, unknown>;
  contextAssembly?: Record<string, unknown>;
  updatePolicy?: Record<string, unknown>;
  requiredQuestions?: Record<string, unknown>[];
  createdAt?: string;
  coomiStatus?: AgentCoomiStatusResponse;
  llmModel?: string;
  llmProvider?: string;
  total_tokens?: number;
  duration_ms_total?: number;
  planMode?: boolean;
}

export interface AgentApprovalOption {
  label: string;
  value: "allow" | "deny" | string;
  description?: string;
  isRecommended?: boolean;
}

export interface AgentPendingApproval {
  approvalId: string;
  kind?: "permission" | "question" | string;
  header: string;
  question: string;
  options: AgentApprovalOption[];
  allowText?: boolean;
  multiSelect?: boolean;
  questionIndex?: number;
  questionTotal?: number;
}

export interface AgentPendingCommitPrompt {
  traceId: string;
  sessionId: string;
  workspaceRoot: string;
  message: string;
  changedFiles: string[];
  changedFileCount: number;
  added: number;
  removed: number;
}

export type AgentCommitDecisionMode = "auto" | "manual" | "skip";

export interface AgentCommitDecisionRequest {
  mode: AgentCommitDecisionMode;
  message?: string;
  sessionId?: string;
}

export type AgentRunStatus =
  | "running"
  | "preview"
  | "completed"
  | "committed"
  | "discarded"
  | "failed"
  | "cancelled"
  | "stopped";
export type AgentTaskStatus = "pending" | "running" | "completed" | "failed" | "skipped";

export interface AgentTaskItem {
  taskId: string;
  traceId: string;
  order: number;
  title: string;
  detail: string;
  status: AgentTaskStatus;
  createdAt: string;
  updatedAt: string;
}

export interface AgentRunChangeLedger {
  traceId: string;
  sessionId: string;
  changedFiles: string[];
  changedFileCount: number;
  added: number;
  removed: number;
  diffSource: "working_tree" | "commit" | "";
  commitHash: string;
  shortHash: string;
  updatedAt: string;
}

export type CoomiWaterfallItemType =
  | "user"
  | "assistant"
  | "reasoning"
  | "tool"
  | "usage"
  | "compression"
  | "phase"
  | "system"
  | "error";

export type CoomiWaterfallItemStatus = "running" | "success" | "error" | "info" | "warning";

export interface CoomiWaterfallItem {
  id: string;
  type: CoomiWaterfallItemType;
  status: CoomiWaterfallItemStatus;
  title: string;
  content: string;
  timestamp: string;
  toolName?: string;
  toolCallId?: string;
  arguments?: Record<string, unknown>;
  resultPreview?: string;
  usage?: Record<string, number>;
  compression?: Record<string, unknown>;
  raw?: Record<string, unknown>;
}

export interface AgentExecutionRun {
  traceId: string;
  sessionId: string;
  prompt: string;
  route: string;
  agentMode: string;
  llmModel: string;
  llmProvider: string;
  status: AgentRunStatus;
  noRestorePoint: boolean;
  createdAt: string;
  updatedAt: string;
  lastAction: "chat";
  reply: string;
  trace: ApiTrace | null;
  audit: ApiAuditRecord[];
  events: AgentTraceEvent[];
  tasks: AgentTaskItem[];
  changeLedger: AgentRunChangeLedger;
  items: CoomiWaterfallItem[];
  errorMessage: string;
  errorCode: string | null;
}
