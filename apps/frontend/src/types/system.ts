import type { WorkspaceRecentProject } from "@/types/workspace";

export type WorkbenchMode = "storydex";

export interface SystemHealthResponse {
  status: string;
  service: string;
  time: string;
  workspaceRoot: string;
  storydexRoot: string;
  projectName: string;
  hasStorydexConfig: boolean;
  requiresInitialization: boolean;
  missingDirectories: string[];
  frontendStaticMode: boolean;
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

export interface SystemBootstrapResponse {
  globalRoot: string;
  uiPreferences: UIPreferencesResponse;
  workspaceState: WorkspaceStateResponse;
}
