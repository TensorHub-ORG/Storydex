export interface WorkspaceFileReadRequest {
  relativePath: string;
}

export interface WorkspaceFileWindowRequest {
  relativePath: string;
  startLine: number;
  lineCount?: number;
}

export interface WorkspaceFileWindowResponse {
  relativePath: string;
  content: string;
  size: number;
  mtimeMs: number;
  startLine: number;
  loadedLines: number;
  lineCount: number;
  lineCountExact: boolean;
  hasPrevious: boolean;
  hasNext: boolean;
  mode: "full" | "progressive" | "large-readonly";
  readOnly: boolean;
  initialChunkBytes: number;
}

export interface WorkspaceFileWriteRequest {
  relativePath: string;
  content: string;
}

export interface WorkspaceCreateFileRequest {
  relativePath: string;
  content?: string;
}

export interface WorkspaceCreateDirectoryRequest {
  relativePath: string;
}

export interface WorkspaceRenameRequest {
  fromRelativePath: string;
  toRelativePath: string;
}

export interface WorkspaceDeleteRequest {
  relativePath: string;
}

export interface WorkspaceTransferRequest {
  fromRelativePath: string;
  toRelativePath: string;
}

export interface WorkspaceImportFileItem {
  name: string;
  contentBase64: string;
}

export interface WorkspaceImportFilesRequest {
  targetDirectory: string;
  files: WorkspaceImportFileItem[];
}

export interface WorkspaceProjectPathRequest {
  projectPath: string;
}

export type StorySegmentExtension = ".md" | ".txt";

export interface WorkspaceTreeNode {
  name: string;
  relativePath: string | null;
  kind: "directory" | "file";
  children: WorkspaceTreeNode[];
  extension?: string;
  size?: number;
  updatedAt?: string;
}

export interface WorkspaceProjectInfo {
  projectName: string;
  workspaceRoot: string;
  storydexRoot: string;
  storydexDirName: string;
  hasStorydexConfig: boolean;
  requiresInitialization: boolean;
  missingDirectories: string[];
  projectState: string;
  openedAt: string;
}

export interface WorkspaceRecentProject {
  projectName: string;
  workspaceRoot: string;
  openedAt: string;
}

export interface WorkspaceTreeResponse {
  workspaceRoot: string;
  storydexRoot: string;
  projectName: string;
  hasStorydexConfig: boolean;
  requiresInitialization: boolean;
  missingDirectories: string[];
  openedAt: string;
  defaultFile: string | null;
  roots: WorkspaceTreeNode[];
}

export interface WorkspaceImportFilesResponse {
  items: WorkspacePathInfo[];
}

export interface WorkspaceFileDocument {
  relativePath: string;
  content: string;
  size: number;
  wordCount?: number;
  lineCount?: number;
  updatedAt: string;
  extension: string;
  kind: string;
  title?: string;
  displayPath?: string;
  readOnly?: boolean;
  transient?: boolean;
  boundRelativePath?: string;
  previewLines?: WorkspacePreviewLine[];
  media?: Record<string, unknown>;
  isPartialView?: boolean;
  lineCountExact?: boolean;
  offset?: number | null;
  limit?: number | null;
}

export interface WorkspacePathInfo {
  relativePath: string;
  exists: boolean;
  kind: "directory" | "file" | string;
  size: number;
  mtimeMs: number | null;
  sha256: string;
}

export interface WorkspaceEditorTab {
  relativePath: string;
  title: string;
  extension: string;
  dirty: boolean;
}

export type WorkspacePreviewLineKind = "context" | "added" | "removed";

export interface WorkspacePreviewLine {
  id: string;
  kind: WorkspacePreviewLineKind;
  content: string;
  lineNumber: number | null;
}

export interface WorkspaceDiagnosticsRequest {
  relativePaths: string[];
}

export interface WorkspaceDiagnosticItem {
  code?: string;
  source: string;
  severity: string;
  relativePath: string;
  line: number;
  column: number;
  message: string;
  evidence?: string;
  fixes?: Array<{ id: string; label: string }>;
}

export interface WorkspaceDiagnosticsResponse {
  items: WorkspaceDiagnosticItem[];
}

export interface WorkspaceDiagnosticFixRequest {
  relativePath: string;
  fixId: string;
}

export interface WorkspaceDiagnosticFixResponse extends WorkspaceDiagnosticFixRequest {
  changed: boolean;
}

export interface WorkspaceGitChangedFile {
  status: string;
  relativePath: string;
  staged: boolean;
  unstaged: boolean;
}

export type WorkspaceGitDiffLineKind = "context" | "added" | "removed";

export interface WorkspaceGitDiffLine {
  kind: WorkspaceGitDiffLineKind;
  oldLine: number | null;
  newLine: number | null;
  content: string;
}

export interface WorkspaceGitDiffHunk {
  header: string;
  oldStart: number;
  oldLines: number;
  newStart: number;
  newLines: number;
  lines: WorkspaceGitDiffLine[];
}

export interface WorkspaceGitDiffFile {
  relativePath: string;
  status: string;
  added: number;
  removed: number;
  hunks: WorkspaceGitDiffHunk[];
  truncated: boolean;
}

export interface WorkspaceGitDiffTotals {
  files: number;
  added: number;
  removed: number;
}

export interface WorkspaceGitDiffResponse {
  available: boolean;
  gitInstalled: boolean;
  initialized: boolean;
  branch: string;
  files: WorkspaceGitDiffFile[];
  totals: WorkspaceGitDiffTotals;
  message: string;
}

export interface WorkspaceGitCommitEntry {
  id: string;
  shortId: string;
  authorName: string;
  authoredAt: string;
  refs: string;
  subject: string;
}

export interface WorkspaceGitSummaryResponse {
  available: boolean;
  gitInstalled: boolean;
  initialized: boolean;
  branch: string;
  clean: boolean;
  changedFiles: WorkspaceGitChangedFile[];
  recentCommits: WorkspaceGitCommitEntry[];
  graphLines: string[];
  defaultBranch: string;
  message: string;
  head?: WorkspaceGitCommitEntry | null;
}

export interface WorkspaceGitCommitResponse {
  created: boolean;
  commit: WorkspaceGitCommitEntry | null;
  summary: WorkspaceGitSummaryResponse;
}

export interface WorkspaceGitCommitRequest {
  message: string;
}

export interface WorkspaceGitRestoreRequest {
  commitId: string;
  createBackup?: boolean;
}

export interface WorkspaceGitRestoreResponse {
  restored: boolean;
  restoredCommit: WorkspaceGitCommitEntry | null;
  backupCommit: WorkspaceGitCommitEntry | null;
  backupRef: string;
  summary: WorkspaceGitSummaryResponse;
}

export type StorySettingsSource = "api" | "project_file" | "default";

export interface StoryProjectSettings {
  segmentExtension: StorySegmentExtension;
  maxSegmentsPerChapter: number;
  storyFragmentCount: number;
  storyFragmentWordCount: number;
  storyChapterTemplateId: string;
  autoUpdateVariables: boolean;
  autoUpdateWiki: boolean;
  autoUpdateVariablesNote: string;
  agentCommitPromptEnabled: boolean;
  autoNameChapterDirectories: boolean;
  contextConcisionMinCalls: number;
  contextConcisionMaxCalls: number;
  contextConcisionMaxInputTokens: number;
  chapterCompletion: Record<string, boolean>;
  updatedAt: string;
  settingsPath: string;
  source: StorySettingsSource;
}

export interface StoryProjectSettingsResponse {
  segmentExtension?: StorySegmentExtension | string;
  storySegmentFormat?: string;
  chapterCompletion?: Record<string, boolean | { completed?: boolean }>;
  maxSegmentsPerChapter?: number | string;
  max_segments_per_chapter?: number | string;
  chapterSegmentLimit?: number | string;
  chapter_segment_limit?: number | string;
  storyFragmentCount?: number | string;
  story_fragment_count?: number | string;
  storyFragmentWordCount?: number | string;
  story_fragment_word_count?: number | string;
  storyChapterTemplateId?: string;
  story_chapter_template_id?: string;
  autoUpdateVariables?: boolean | string;
  auto_update_variables?: boolean | string;
  autoUpdateWiki?: boolean | string;
  auto_update_wiki?: boolean | string;
  autoUpdateVariablesNote?: string;
  auto_update_variables_note?: string;
  agentCommitPromptEnabled?: boolean | string;
  agent_commit_prompt_enabled?: boolean | string;
  autoNameChapterTitle?: boolean | string;
  auto_name_chapter_title?: boolean | string;
  autoNameChapterDirectories?: boolean | string;
  auto_name_chapter_directories?: boolean | string;
  chapterDirectoryNamingMode?: string;
  chapter_directory_naming_mode?: string;
  chapterNamingMode?: string;
  chapter_naming_mode?: string;
  segmentNamingMode?: string;
  segment_naming_mode?: string;
  contextConcisionMinCalls?: number | string;
  context_concision_min_calls?: number | string;
  contextConcisionMaxCalls?: number | string;
  context_concision_max_calls?: number | string;
  contextConcisionMaxInputTokens?: number | string;
  context_concision_max_input_tokens?: number | string;
  updatedAt?: string;
  settingsPath?: string;
}

export interface StoryProjectSettingsUpdateRequest {
  segmentExtension: StorySegmentExtension;
  storySegmentFormat?: string;
  maxSegmentsPerChapter: number;
  max_segments_per_chapter?: number;
  chapterSegmentLimit?: number;
  chapter_segment_limit?: number;
  storyFragmentCount?: number;
  story_fragment_count?: number;
  storyFragmentWordCount?: number;
  story_fragment_word_count?: number;
  storyChapterTemplateId?: string;
  story_chapter_template_id?: string;
  autoUpdateVariables?: boolean;
  auto_update_variables?: boolean;
  autoUpdateWiki?: boolean;
  auto_update_wiki?: boolean;
  agentCommitPromptEnabled?: boolean;
  agent_commit_prompt_enabled?: boolean;
  autoNameChapterTitle?: boolean;
  auto_name_chapter_title?: boolean;
  autoNameChapterDirectories: boolean;
  auto_name_chapter_directories?: boolean;
  chapterDirectoryNamingMode?: string;
  chapter_directory_naming_mode?: string;
  chapterNamingMode?: string;
  chapter_naming_mode?: string;
  contextConcisionMinCalls?: number;
  context_concision_min_calls?: number;
  contextConcisionMaxCalls?: number;
  context_concision_max_calls?: number;
  contextConcisionMaxInputTokens?: number;
  context_concision_max_input_tokens?: number;
  chapterCompletion: Record<string, boolean>;
}

export interface StoryChapterCompletionRequest {
  chapterPath: string;
  completed: boolean;
}

export interface StoryChapterState {
  relativePath: string;
  name: string;
  displayName: string;
  chapterNumber: number;
  completed: boolean;
  updatedAt: string;
}

export interface StoryChapterListResponse {
  items: StoryChapterState[];
}

export interface StoryChapterTemplate {
  id: string;
  name: string;
  relativePath: string;
  description: string;
  chapterMode: string;
  contentMode: "multi_fragment" | "single_file" | string;
  chapterNamePattern: string;
  segmentNaming: string;
}

export interface StoryChapterTemplateListResponse {
  items: StoryChapterTemplate[];
}

export interface StoryCurrentStateData {
  updatedAt?: string;
  latestSnapshotPath?: string;
  fullState?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface StoryCurrentStateResponse {
  currentStatePath: string;
  latestSnapshotIndexPath: string;
  data: StoryCurrentStateData;
}

export interface StorySnapshotOperation extends Record<string, unknown> {
  op?: string;
  path?: string;
  value?: unknown;
  evidence?: string;
}

export interface StorySnapshotMemoryUpdate extends Record<string, unknown> {
  memory?: string;
  evidence?: string;
}

export interface StorySnapshotCharacterUpdate extends Record<string, unknown> {
  character?: string;
  changes?: unknown[];
  evidence?: string;
}

export interface StorySnapshotEventUpdate extends Record<string, unknown> {
  event?: string;
  impact?: string;
  evidence?: string;
}

export interface StoryLatestSnapshotPayload {
  chapter_id?: string;
  segment_id?: string;
  snapshot_order?: number;
  created_at?: string;
  parent_snapshot?: string;
  operations?: StorySnapshotOperation[];
  full_state?: Record<string, unknown>;
  memory_updates?: StorySnapshotMemoryUpdate[];
  character_updates?: StorySnapshotCharacterUpdate[];
  event_updates?: StorySnapshotEventUpdate[];
  snapshot_comment?: string;
  [key: string]: unknown;
}

export interface StoryLatestSnapshotResponse {
  relativePath: string;
  snapshot: StoryLatestSnapshotPayload;
}

export interface StoryCurrentStateModel {
  currentStatePath: string;
  latestSnapshotIndexPath: string;
  data: StoryCurrentStateData;
  updatedAt: string;
  latestSnapshotPath: string;
  fullState: Record<string, unknown>;
  raw: StoryCurrentStateData;
}

export interface StoryLatestSnapshotModel {
  relativePath: string;
  snapshot: StoryLatestSnapshotPayload;
  chapterId: string;
  segmentId: string;
  snapshotOrder: number | null;
  createdAt: string;
  parentSnapshot: string;
  operations: StorySnapshotOperation[];
  fullState: Record<string, unknown>;
  memoryUpdates: StorySnapshotMemoryUpdate[];
  characterUpdates: StorySnapshotCharacterUpdate[];
  eventUpdates: StorySnapshotEventUpdate[];
  snapshotComment: string;
  raw: StoryLatestSnapshotPayload;
}

