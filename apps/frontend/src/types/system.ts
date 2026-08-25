import type { WorkspaceRecentProject } from "@/types/workspace";

export type WorkbenchMode = "storydex";

export interface SystemHealthResponse {
  status: string;
  service: string;
  time: string;
  runtime?: string;
  version?: string;
  protocolVersion?: number;
  workspaceRoot?: string;
  storydexRoot?: string;
  projectName?: string;
  hasStorydexConfig?: boolean;
  requiresInitialization?: boolean;
  missingDirectories?: string[];
  frontendStaticMode?: boolean;
  memoryUsageMb?: number | null;
}

export interface UIPreferencesResponse {
  theme: string;
  activeActivity: string;
  workbenchMode: WorkbenchMode;
  sidebarWidth: number;
  sidebarCollapsed: boolean;
  agentCollapsed: boolean;
  agentWidth: number;
  leftPaneFontScale: number;
  centerPaneFontScale: number;
  rightPaneFontScale: number;
  fontFamily?: string;
  /** @deprecated Kept for compatibility with older Storydex preferences. */
  fileFontSize?: number;
  /** @deprecated Kept for compatibility with older Storydex preferences. */
  playerFontSize?: number;
  updatedAt: string;
}

export interface UIPreferencesUpdateRequest {
  theme: string;
  activeActivity: string;
  workbenchMode: WorkbenchMode;
  sidebarWidth: number;
  sidebarCollapsed: boolean;
  agentCollapsed: boolean;
  agentWidth: number;
  leftPaneFontScale: number;
  centerPaneFontScale: number;
  rightPaneFontScale: number;
  fontFamily?: string;
}

export interface WorkspaceStateResponse {
  lastProjectPath: string;
  recentProjects: WorkspaceRecentProject[];
  updatedAt: string;
}

export interface AgentSettingsResponse {
  coomiMemoryEnabled: boolean;
  wikiContextEnabled: boolean;
  updatedAt: string;
}

export interface AgentSettingsUpdateRequest {
  coomiMemoryEnabled: boolean;
  wikiContextEnabled: boolean;
}

export interface FeedbackImagePayload {
  name: string;
  mimeType: string;
  dataUrl: string;
}

export interface FeedbackSubmitRequest {
  source: "error" | "settings";
  category: string;
  description: string;
  contact?: string;
  errorMessage?: string;
  errorType?: string;
  errorDetails?: Record<string, unknown>;
  diagnostics?: Record<string, unknown>;
  images?: FeedbackImagePayload[];
}

export interface FeedbackSubmitResponse {
  feedbackId: string;
}

export interface ToolFailureTracePayload {
  sequence: number;
  tool: string;
  status: "success" | "error" | "unknown";
  argumentShape: unknown;
  elapsedMs?: number;
  category?: string;
  errorSummary?: string;
}

export interface ToolFailureAnalysisRequest {
  providerId?: string;
  trace: ToolFailureTracePayload[];
}

export interface ToolFailureAnalysisResponse {
  analysis: string;
  programEvidence: string;
  failureCount: number;
  requestId: string;
  elapsedMs: number;
  responseCategory: string;
  redactionVersion: string;
}

export interface SystemBootstrapResponse {
  globalRoot: string;
  uiPreferences: UIPreferencesResponse;
  workspaceState: WorkspaceStateResponse;
}
