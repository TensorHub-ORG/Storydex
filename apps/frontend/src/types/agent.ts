import type { ApiAuditRecord, ApiTrace } from "@/types/api";
import type { ChapterLengthTier } from "@/types/workspace";

export type AgentReasoningEffort = "auto" | "low" | "medium" | "high" | "xhigh" | "max";
export type AgentReasoningSupport = "supported" | "unsupported" | "unknown";
export type AgentReasoningControlMode = "auto" | "native" | "prompt";
export type AgentReasoningCapabilitySource = "model_config" | "provider_config" | "model_rule" | "unknown";

export interface AgentReasoningWireField {
  path: string;
  value: unknown;
}

export interface AgentReasoningLevelCapability {
  effort: AgentReasoningEffort;
  control: AgentReasoningControlMode;
  wireFields: AgentReasoningWireField[];
  routeSensitive: boolean;
}

export interface AgentReasoningCapability {
  support: AgentReasoningSupport;
  levels: AgentReasoningLevelCapability[];
  source: AgentReasoningCapabilitySource;
  promptFallback: boolean;
  routeSensitive: boolean;
  fallbackReason?: string;
}

export interface AgentReasoningRequestPlan {
  requested: AgentReasoningEffort;
  control: AgentReasoningControlMode;
  sent: boolean;
  promptApplied: boolean;
  wireFields: AgentReasoningWireField[];
  support: AgentReasoningSupport;
  source: AgentReasoningCapabilitySource;
  routeSensitive: boolean;
  fallbackReason?: string;
}

export interface AgentCoomiModelChoice {
  selector: string;
  providerId: string;
  providerDisplay: string;
  model: string;
  isFast: boolean;
  reasoningCapability: AgentReasoningCapability;
}

export interface AgentChatRequest {
  prompt: string;
  activeFile?: string;
  workspaceRoot?: string;
  reasoningEffort?: AgentReasoningEffort;
  storyGeneration?: AgentStoryGenerationOptions;
  confirmNoSnapshot?: boolean;
  replaceLatestTraceId?: string;
  sourceFollowupMessageId?: string;
  sourceFollowupExpectedTraceId?: string;
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
  chapterLengthTier: ChapterLengthTier;
  chapterWordCountTarget?: number;
  preciseWordCountEnabled?: boolean;
  fragmentWordCount?: number;
  fragmentWordCountMin?: number;
  fragmentWordCountMax?: number;
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

export interface AgentExecutionRollbackResponse {
  rolledBack: boolean;
  sessionId: string;
  removedTraceId: string;
  prompt: string;
}

export type AgentFollowupMode = "queued" | "steer";
export type AgentFollowupStatus = "pending" | "steering" | "dispatching" | "sent" | "cancelled" | "failed";

export interface AgentFollowupMessage {
  messageId: string;
  sessionId: string;
  activeTraceId: string;
  expectedTraceId?: string;
  content: string;
  mode: AgentFollowupMode;
  status: AgentFollowupStatus;
  statusDetail?: string;
  createdAt: string;
  updatedAt: string;
  sequence?: number;
  dispatchTraceId?: string;
  segmentId?: string;
  error?: string;
}

export interface AgentFollowupMailboxResponse {
  _type: "FollowupMailbox" | string;
  _version: number;
  revision: number;
  workspaceRoot: string;
  sessionId: string;
  activeTraceId: string;
  paused: boolean;
  pauseReason: string;
  messages: AgentFollowupMessage[];
  events?: Record<string, unknown>[];
  createdAt: string;
  updatedAt: string;
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
  reasoningCapability?: AgentReasoningCapability;
  reasoningRequestPlan?: AgentReasoningRequestPlan;
  models?: AgentCoomiModelChoice[];
  providerCapabilities?: Record<string, unknown>;
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
  providerType: string;
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
  | "ConnectionRetry"
  | "ToolStart"
  | "ToolRunning"
  | "ToolDone"
  | "ToolCacheHit"
  | "UsageUpdate"
  | "ReasoningPlan"
  | "ModelCompleted"
  | "CompressionEvent"
  | "PlanModeChanged"
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
  | "StoryDraftMeasured"
  | "StoryLengthRevisionResult"
  | "StoryCallAccounting"
  | "StoryGenerationValidation"
  | "TurnPhase"
  | "StageOutput"
  | "AgentStarted"
  | "AgentCompleted"
  | "AgentWarning"
  | "AgentNotice"
  | "AgentError"
  | "AgentCancelled"
  | "FollowupQueued"
  | "FollowupUpdated"
  | "SteerRequested"
  | "SteerApplied"
  | "ContinuationStarted";

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
  resetTextCharacters?: number;
  providerResetTextCharacters?: number;
  phase?: string;
  label?: string;
  status?: string;
  permissionMode?: string;
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
  reasoning_tokens?: number;
  reasoningTokens?: number;
  reasoningRequestPlan?: AgentReasoningRequestPlan;
  plan?: AgentReasoningRequestPlan;
  round?: number;
  upstreamResponded?: boolean;
  responseModel?: string;
  finishReason?: string;
  responseStatus?: string;
  nativeReasoning?: boolean;
  metadata?: Record<string, unknown>;
  provider?: string;
  model?: string;
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
  request?: Record<string, unknown>;
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
  messageId?: string;
  activeTraceId?: string;
  expectedTraceId?: string;
  previousTraceId?: string;
  segmentId?: string;
  previousSegmentId?: string;
  continuationMode?: AgentFollowupMode | string;
  statusDetail?: string;
  attempt?: number;
  max_attempts?: number;
  maxAttempts?: number;
  delay?: number;
  delaySeconds?: number;
  mode?: string;
  query?: string;
  error_type?: string;
  statusCode?: number;
  code?: string;
  message?: string;
  details?: Record<string, unknown>;
  noRestorePoint?: boolean;
  created?: boolean;
  target?: string | number;
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
  passed?: boolean;
  algorithm?: string;
  exact?: boolean;
  countingRule?: string;
  fragmentCount?: number;
  targetWordCount?: number;
  targetWordCountMin?: number;
  targetWordCountMax?: number;
  generatedWordCount?: number;
  retainedWordCount?: number;
  resultingWordCount?: number;
  wordCountScope?: "candidate" | "chapter" | "fragment" | string;
  initialWordCount?: number;
  finalWordCount?: number;
  candidateWordCount?: number;
  chapterWordCountTarget?: number;
  chapterLengthTier?: ChapterLengthTier;
  tierHit?: boolean | null;
  tierDeviation?: string;
  actualWordCount?: number;
  machineQualityPassed?: boolean | null;
  acceptWordCountMin?: number;
  acceptWordCountMax?: number;
  belowBudget?: boolean;
  overBudget?: boolean;
  chapterContentMode?: string;
  structurePassed?: boolean;
  writeToolApplied?: boolean;
  preciseWordCountEnabled?: boolean;
  revisionApplied?: boolean;
  lengthControlStrategy?: string;
  canonicalWordCount?: number;
  precisionAchieved?: boolean | null;
  selectedEditIds?: string[];
  rejectedEditIds?: string[];
  rejectedEditReasonCounts?: Record<string, number>;
  evaluatedCombinationCount?: number;
  lengthFallbackReason?: string;
  generatedOverheadRatio?: number | null;
  accepted?: boolean;
  outcome?: string;
  rejectionReasons?: string[];
  revisionOutcomeReason?: string;
  revisionRejectionReasons?: string[];
  completionTokens?: number | null;
  capApplied?: boolean;
  providerDurationMs?: number;
  normalBand?: number[];
  precisionBand?: number[];
  normalBandPassed?: boolean | null;
  precisionBandPassed?: boolean;
  calibrationStatus?: string;
  logicalStoryCalls?: number;
  providerAttempts?: number;
  transportRetries?: number;
  initialGenerationCalls?: number;
  lengthRevisionCalls?: number;
  secondDraftCalls?: number;
  nonProseCalls?: Record<string, number>;
  contractViolations?: string[];
  callAccounting?: Record<string, unknown>;
  correctionAttempt?: number;
  maximumCorrectionAttempts?: number;
  fragments?: Record<string, unknown>[];
  validation?: Record<string, unknown>;
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
  source?: string;
  warning_type?: string;
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
  | "stopped"
  | "superseded";
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
  | "notice"
  | "info"
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
  /** 本轮消耗的 token（AgentCompleted.total_tokens），与 session 级上下文占用无关 */
  turnTokens: number | null;
  /** 本轮耗时毫秒（AgentCompleted.duration_ms），收敛后固定 */
  turnDurationMs: number | null;
}
