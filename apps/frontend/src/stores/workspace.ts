import { defineStore } from "pinia";
import { ApiResponseError, describeTransportError } from "@/api/client";
import { fetchAgentRunDiff } from "@/api/agent";
import { fetchHelpGuide } from "@/api/help";
import { fetchSystemBootstrap, fetchSystemHealth } from "@/api/system";
import {
  copyWorkspacePath,
  createWorkspaceDirectory,
  createWorkspaceFile,
  deleteWorkspacePath,
  createWorkspaceProject,
  fetchStoryProjectSettings,
  fetchCurrentProject,
  fetchWorkspaceGitDiff,
  fetchWorkspaceDiagnostics,
  fetchWorkspaceTree,
  importWorkspaceFiles,
  initializeWorkspaceProject,
  moveWorkspacePath,
  openWorkspaceProject,
  readWorkspaceFile,
  renameWorkspacePath,
  updateStoryChapterCompletion,
  updateStoryProjectSettings,
  writeWorkspaceFile
} from "@/api/workspace";
import { useAgentStore } from "@/stores/agent";
import { useGitStore } from "@/stores/git";
import { useUiStore } from "@/stores/ui";
import type { ApiTrace } from "@/types/api";
import type { SystemHealthResponse } from "@/types/system";
import type {
  WorkspaceEditorTab,
  WorkspaceDiagnosticItem,
  WorkspaceFileDocument,
  WorkspaceGitDiffLineKind,
  WorkspaceGitDiffResponse,
  WorkspaceImportFileItem,
  WorkspacePathInfo,
  WorkspacePreviewLine,
  WorkspaceProjectInfo,
  WorkspaceRecentProject,
  StoryProjectSettings,
  StoryProjectSettingsResponse,
  StoryProjectSettingsUpdateRequest,
  StorySegmentExtension,
  WorkspaceTreeNode
} from "@/types/workspace";

interface CachedWorkspaceDocument extends WorkspaceFileDocument {
  savedContent: string;
  dirty: boolean;
}

interface WorkspaceState {
  initialized: boolean;
  launchScreenVisible: boolean;
  isBootstrapping: boolean;
  isTreeLoading: boolean;
  isFileLoading: boolean;
  isSaving: boolean;
  isDirty: boolean;
  isProjectSwitching: boolean;
  isProjectCreating: boolean;
  isProjectInitializing: boolean;
  isStorySettingsLoading: boolean;
  isDiagnosticsLoading: boolean;
  isGitReviewLoading: boolean;
  tree: WorkspaceTreeNode[];
  openTabs: WorkspaceEditorTab[];
  documents: Record<string, CachedWorkspaceDocument>;
  diagnostics: WorkspaceDiagnosticItem[];
  activeFile: string;
  activeFileContent: string;
  editorContent: string;
  activeFileUpdatedAt: string;
  activeFileExtension: string;
  activeFileSize: number;
  activeFileKind: string;
  activeFileMedia: Record<string, unknown>;
  editorMode: "preview" | "edit";
  lastSavedAt: string;
  workspaceError: string;
  lastProjectAction: string;
  currentProject: WorkspaceProjectInfo | null;
  treeTrace: ApiTrace | null;
  fileTrace: ApiTrace | null;
  saveTrace: ApiTrace | null;
  healthTrace: ApiTrace | null;
  health: SystemHealthResponse | null;
  lastProjectPath: string;
  treeResetToken: number;
  recentProjects: WorkspaceRecentProject[];
  diagnosticsTrace: ApiTrace | null;
  storySettingsTrace: ApiTrace | null;
  storySettings: StoryProjectSettings;
  storySettingsError: string;
  diagnosticsError: string;
  gitReviewDiff: WorkspaceGitDiffResponse | null;
  gitReviewFocusPath: string;
  gitReviewTraceId: string;
  gitReviewSessionId: string;
  gitReviewError: string;
}

const MAX_RECENT_PROJECTS = 8;

export const useWorkspaceStore = defineStore("workspace", {
  state: (): WorkspaceState => ({
    initialized: false,
    launchScreenVisible: true,
    isBootstrapping: false,
    isTreeLoading: false,
    isFileLoading: false,
    isSaving: false,
    isDirty: false,
    isProjectSwitching: false,
    isProjectCreating: false,
    isProjectInitializing: false,
    isStorySettingsLoading: false,
    isDiagnosticsLoading: false,
    isGitReviewLoading: false,
    tree: [],
    openTabs: [],
    documents: {},
    diagnostics: [],
    activeFile: "",
    activeFileContent: "",
    editorContent: "",
    activeFileUpdatedAt: "",
    activeFileExtension: "",
    activeFileSize: 0,
    activeFileKind: "file",
    activeFileMedia: {},
    editorMode: "preview",
    lastSavedAt: "",
    workspaceError: "",
    lastProjectAction: "",
    currentProject: null,
    treeTrace: null,
    fileTrace: null,
    saveTrace: null,
    healthTrace: null,
    health: null,
    lastProjectPath: "",
    treeResetToken: 0,
    recentProjects: [],
    diagnosticsTrace: null,
    storySettingsTrace: null,
    storySettings: defaultStoryProjectSettings(),
    storySettingsError: "",
    diagnosticsError: "",
    gitReviewDiff: null,
    gitReviewFocusPath: "",
    gitReviewTraceId: "",
    gitReviewSessionId: "",
    gitReviewError: ""
  }),

  getters: {
    activeFileName(state): string {
      if (!state.activeFile) {
        return "未选择文件";
      }
      const activeDocument = state.documents[state.activeFile];
      if (activeDocument?.title) {
        return activeDocument.title;
      }
      const parts = state.activeFile.split("/");
      return parts[parts.length - 1] ?? state.activeFile;
    },

    activeDisplayPath(state): string {
      if (!state.activeFile) {
        return "";
      }
      const activeDocument = state.documents[state.activeFile];
      if (typeof activeDocument?.displayPath === "string" && activeDocument.displayPath.trim()) {
        return activeDocument.displayPath.trim();
      }
      return state.activeFile;
    },

    activeFileBinding(state): string {
      if (!state.activeFile) {
        return "";
      }
      const activeDocument = state.documents[state.activeFile];
      if (typeof activeDocument?.boundRelativePath === "string" && activeDocument.boundRelativePath.trim()) {
        return activeDocument.boundRelativePath.trim();
      }
      return state.activeFile;
    },

    activePreviewLines(state): WorkspacePreviewLine[] {
      const activeDocument = state.activeFile ? state.documents[state.activeFile] : null;
      return Array.isArray(activeDocument?.previewLines) ? activeDocument.previewLines : [];
    },

    activeDocumentReadOnly(state): boolean {
      const activeDocument = state.activeFile ? state.documents[state.activeFile] : null;
      return Boolean(activeDocument?.readOnly);
    },

    isAgentPreviewActive(state): boolean {
      const activeDocument = state.activeFile ? state.documents[state.activeFile] : null;
      return Boolean(activeDocument?.transient && activeDocument?.kind === "agent-preview");
    },

    isGitReviewActive(state): boolean {
      const activeDocument = state.activeFile ? state.documents[state.activeFile] : null;
      return Boolean(activeDocument?.transient && activeDocument?.kind === "git-review");
    },

    isHelpGuideActive(state): boolean {
      const activeDocument = state.activeFile ? state.documents[state.activeFile] : null;
      return Boolean(activeDocument?.transient && activeDocument?.kind === "help-guide");
    },

    activeGitReviewDiff(state): WorkspaceGitDiffResponse | null {
      return state.gitReviewDiff;
    },

    activeGitReviewFocusPath(state): string {
      return state.gitReviewFocusPath;
    },

    projectLabel(state): string {
      if (state.launchScreenVisible) {
        return "尚未打开文件夹";
      }
      return state.currentProject?.projectName || state.health?.projectName || "未打开项目";
    },

    projectRootLabel(state): string {
      if (state.launchScreenVisible) {
        return "";
      }
      return state.currentProject?.workspaceRoot || state.health?.workspaceRoot || "";
    },

    requiresInitialization(state): boolean {
      if (state.launchScreenVisible) {
        return false;
      }
      return Boolean(state.currentProject?.requiresInitialization || state.health?.requiresInitialization);
    },

    projectMissingDirectories(state): string[] {
      return state.currentProject?.missingDirectories || state.health?.missingDirectories || [];
    },

    wordCount(state): number {
      return countVisibleCharacters(state.editorContent);
    },

    lineCount(state): number {
      if (!state.editorContent) {
        return 0;
      }
      return state.editorContent.split(/\r?\n/).length;
    },

    connectionLabel(state): string {
      if (state.isBootstrapping) return "后端连接中";
      if (state.health?.status === "ok") return "后端已连接";
      if (state.workspaceError) return "后端连接异常";
      return "等待初始化";
    },

    isJsonFile(state): boolean {
      return state.activeFileExtension === ".json";
    },

    isMarkdownFile(state): boolean {
      return state.activeFileExtension === ".md";
    },

    isPreviewUnsupported(state): boolean {
      return Boolean(state.activeFileMedia?.previewUnsupported);
    },

    previewUnsupportedMessage(state): string {
      const message = state.activeFileMedia?.message;
      return typeof message === "string" && message.trim() ? message.trim() : "此文件无法展示";
    },

    storySegmentExtension(state): StorySegmentExtension {
      return state.storySettings.segmentExtension;
    },

    storyMaxSegmentsPerChapter(state): number {
      return state.storySettings.maxSegmentsPerChapter;
    },

    autoNameChapterDirectories(state): boolean {
      return state.storySettings.autoNameChapterDirectories;
    },

    storySettingsPath(state): string {
      return state.storySettings.settingsPath;
    },

    activeFileBindingOrPath(state): string {
      if (!state.activeFile) {
        return "";
      }
      const activeDocument = state.documents[state.activeFile];
      if (typeof activeDocument?.boundRelativePath === "string" && activeDocument.boundRelativePath.trim()) {
        return activeDocument.boundRelativePath.trim();
      }
      return state.activeFile;
    },

    isActiveStorySegment(state): boolean {
      const activePath = (() => {
        if (!state.activeFile) {
          return "";
        }
        const activeDocument = state.documents[state.activeFile];
        if (typeof activeDocument?.boundRelativePath === "string" && activeDocument.boundRelativePath.trim()) {
          return activeDocument.boundRelativePath.trim();
        }
        return state.activeFile;
      })();
      return isStorySegmentPath(activePath, state.storySettings.segmentExtension);
    },

    chapterCompletionMap(state): Record<string, boolean> {
      return state.storySettings.chapterCompletion;
    },

    isChapterCompleted(state): (chapterPath: string) => boolean {
      return (chapterPath: string) => Boolean(state.storySettings.chapterCompletion[normalizeRelativePath(chapterPath)]);
    },

    diagnosticsForPath(state): (relativePath: string) => WorkspaceDiagnosticItem[] {
      return (relativePath: string) => {
        const normalized = normalizeRelativePath(relativePath);
        if (!normalized) {
          return [];
        }
        return state.diagnostics.filter((item) => {
          const itemPath = normalizeRelativePath(item.relativePath);
          return itemPath === normalized || itemPath.startsWith(`${normalized}/`);
        });
      };
    },

    diagnosticCountForPath(): (relativePath: string) => number {
      return (relativePath: string) => this.diagnosticsForPath(relativePath).length;
    }
  },

  actions: {
    async bootstrapGlobalState(): Promise<void> {
      try {
        const result = await fetchSystemBootstrap();
        this.lastProjectPath = String(result.data.workspaceState.lastProjectPath || "").trim();
        this.recentProjects = Array.isArray(result.data.workspaceState.recentProjects)
          ? result.data.workspaceState.recentProjects.slice(0, MAX_RECENT_PROJECTS)
          : [];
        useUiStore().applyPersistedState(result.data.uiPreferences);
      } catch {
        this.recentProjects = [];
      }
    },

    async bootstrap(force = false): Promise<void> {
      if (this.isBootstrapping) {
        return;
      }
      if (this.initialized && !force) {
        return;
      }

      this.isBootstrapping = true;
      this.workspaceError = "";
      if (force && !this.launchScreenVisible) {
        this.treeResetToken += 1;
      }

      try {
        const agentStore = useAgentStore();
        const healthResult = await fetchSystemHealth();
        this.health = healthResult.data;
        this.healthTrace = healthResult.trace;
        this.workspaceError = "";

        if (shouldRestoreProjectFromHealth(healthResult.data, this.lastProjectPath)) {
          this.launchScreenVisible = false;
        }

        if (this.launchScreenVisible) {
          agentStore.resetSession({ clearSessionId: true, clearAvailableSessions: true });
          useGitStore().reset();
          this.resetStoryWorkspaceState();
          this.initialized = true;
          return;
        }

        const [treeResult, projectResult] = await Promise.all([fetchWorkspaceTree(), fetchCurrentProject()]);

        this.tree = treeResult.data.roots;
        this.treeTrace = treeResult.trace;
        this.currentProject = projectResult.data;
        await Promise.all([this.refreshStorySettings(), this.refreshDiagnostics()]);
        if (projectResult.data.requiresInitialization) {
          agentStore.resetSession({ clearSessionId: true, clearAvailableSessions: true });
        } else {
          await agentStore.loadSessions();
          await agentStore.loadHistory();
        }

        const targetFile =
          this.activeFile ||
          this.openTabs.find((tab) => Boolean(this.documents[tab.relativePath]))?.relativePath ||
          "";

        if (targetFile) {
          await this.openFile(targetFile);
        } else {
          this.clearActiveFile();
        }

        this.initialized = true;
      } catch (error: unknown) {
        this.workspaceError = normalizeWorkspaceError(error);
      } finally {
        this.isBootstrapping = false;
      }
    },

    async refreshHealth(): Promise<void> {
      try {
        const result = await fetchSystemHealth();
        this.health = result.data;
        this.healthTrace = result.trace;
        this.workspaceError = "";
      } catch (error: unknown) {
        this.workspaceError = normalizeWorkspaceError(error);
      }
    },

    async refreshProject(): Promise<void> {
      try {
        const result = await fetchCurrentProject();
        this.currentProject = result.data;
        this.workspaceError = "";
      } catch (error: unknown) {
        this.workspaceError = normalizeWorkspaceError(error);
      }
    },

    async refreshTree(options?: { silent?: boolean }): Promise<void> {
      const silent = options?.silent ?? true;
      if (!silent) {
        this.isTreeLoading = true;
      }
      try {
        const result = await fetchWorkspaceTree();
        this.tree = result.data.roots;
        this.treeTrace = result.trace;
        this.applyTreeProjectInfo(result.data);
        this.reconcileWorkspaceStateWithTree();
        await Promise.all([this.refreshStorySettings(), this.refreshDiagnostics()]);
        this.workspaceError = "";
        if (!this.launchScreenVisible) {
          void useGitStore().refreshSummary({ silent: true });
        }
      } catch (error: unknown) {
        this.workspaceError = normalizeWorkspaceError(error);
      } finally {
        if (!silent) {
          this.isTreeLoading = false;
        }
      }
    },

    async refreshStorySettings(): Promise<void> {
      if (this.launchScreenVisible || !this.currentProject) {
        this.storySettings = defaultStoryProjectSettings();
        this.storySettingsTrace = null;
        this.storySettingsError = "";
        return;
      }

      this.isStorySettingsLoading = true;
      this.storySettingsError = "";
      try {
        const result = await fetchStoryProjectSettings();
        let fallbackSettings: StoryProjectSettings | null = null;
        if (!hasExtendedStorySettingsPayload(result.data)) {
          try {
            fallbackSettings = await this.readStorySettingsFromProjectFile();
          } catch {
            fallbackSettings = null;
          }
        }
        this.storySettings = normalizeStorySettingsResponse(result.data, {
          source: "api",
          fallbackPath: storyProjectConfigRelativePath(this.currentProject.storydexDirName),
          fallbackSettings,
          currentSettings: this.storySettings
        });
        this.storySettingsTrace = result.trace;
      } catch (error: unknown) {
        try {
          this.storySettings = await this.readStorySettingsFromProjectFile();
          this.storySettingsTrace = null;
        } catch (fallbackError: unknown) {
          this.storySettings = defaultStoryProjectSettings(
            storyProjectConfigRelativePath(this.currentProject.storydexDirName)
          );
          this.storySettingsTrace = null;
          this.storySettingsError = normalizeWorkspaceError(fallbackError ?? error);
        }
      } finally {
        this.isStorySettingsLoading = false;
      }
    },

    async updateStorySettings(
      patch: Partial<StoryProjectSettingsUpdateRequest>
    ): Promise<StoryProjectSettings> {
      if (this.launchScreenVisible || !this.currentProject) {
        return this.storySettings;
      }

        const payload = normalizeStorySettingsPayload({
          segmentExtension: patch.segmentExtension ?? this.storySettings.segmentExtension,
          maxSegmentsPerChapter: patch.maxSegmentsPerChapter ?? this.storySettings.maxSegmentsPerChapter,
          storyFragmentCount: patch.storyFragmentCount ?? this.storySettings.storyFragmentCount,
          storyFragmentWordCount: patch.storyFragmentWordCount ?? this.storySettings.storyFragmentWordCount,
          autoUpdateVariables: patch.autoUpdateVariables ?? this.storySettings.autoUpdateVariables,
          autoUpdateWiki: patch.autoUpdateWiki ?? this.storySettings.autoUpdateWiki,
          agentCommitPromptEnabled:
            patch.agentCommitPromptEnabled ?? this.storySettings.agentCommitPromptEnabled,
          autoNameChapterDirectories:
            patch.autoNameChapterDirectories ?? this.storySettings.autoNameChapterDirectories,
          contextConcisionMinCalls: patch.contextConcisionMinCalls ?? this.storySettings.contextConcisionMinCalls,
          contextConcisionMaxCalls: patch.contextConcisionMaxCalls ?? this.storySettings.contextConcisionMaxCalls,
          contextConcisionMaxInputTokens:
            patch.contextConcisionMaxInputTokens ?? this.storySettings.contextConcisionMaxInputTokens,
          chapterCompletion: patch.chapterCompletion ?? this.storySettings.chapterCompletion
        });

      this.isStorySettingsLoading = true;
      this.storySettingsError = "";
      try {
        const result = await updateStoryProjectSettings(payload);
        const fileSettings = await this.writeStorySettingsToProjectFile(payload);
        const nextSettings = normalizeStorySettingsResponse(result.data, {
          source: "api",
          fallbackPath: storyProjectConfigRelativePath(this.currentProject.storydexDirName),
          fallbackSettings: fileSettings,
          currentSettings: this.storySettings
        });
        this.storySettings = nextSettings;
        this.storySettingsTrace = result.trace;
        return nextSettings;
      } catch (error: unknown) {
        try {
          const nextSettings = await this.writeStorySettingsToProjectFile(payload);
          this.storySettings = nextSettings;
          this.storySettingsTrace = null;
          return nextSettings;
        } catch (fallbackError: unknown) {
          this.storySettingsError = normalizeWorkspaceError(fallbackError ?? error);
          throw fallbackError;
        }
      } finally {
        this.isStorySettingsLoading = false;
      }
    },

    async setChapterCompletion(chapterPath: string, completed: boolean): Promise<StoryProjectSettings> {
      if (this.launchScreenVisible || !this.currentProject) {
        return this.storySettings;
      }

      const normalizedChapterPath = normalizeRelativePath(chapterPath);
      if (!normalizedChapterPath) {
        return this.storySettings;
      }

      try {
        const result = await updateStoryChapterCompletion({
          chapterPath: normalizedChapterPath,
          completed
        });
        const nextSettings = normalizeStorySettingsResponse(result.data, {
          source: "api",
          fallbackPath: storyProjectConfigRelativePath(this.currentProject.storydexDirName)
        });
        this.storySettings = nextSettings;
        this.storySettingsTrace = result.trace;
        return nextSettings;
      } catch {
        const nextChapterCompletion = {
          ...this.storySettings.chapterCompletion,
          [normalizedChapterPath]: completed
        };
        return this.updateStorySettings({ chapterCompletion: nextChapterCompletion });
      }
    },

    async refreshDiagnostics(): Promise<void> {
      if (this.launchScreenVisible || !this.currentProject) {
        this.diagnostics = [];
        this.diagnosticsTrace = null;
        this.diagnosticsError = "";
        return;
      }

      const relativePaths = collectDiagnosticCandidatePaths(this.tree);
      if (!relativePaths.length) {
        this.diagnostics = [];
        this.diagnosticsTrace = null;
        this.diagnosticsError = "";
        return;
      }

      this.isDiagnosticsLoading = true;
      this.diagnosticsError = "";
      try {
        const result = await fetchWorkspaceDiagnostics({ relativePaths });
        this.diagnostics = normalizeDiagnostics(result.data.items);
        this.diagnosticsTrace = result.trace;
      } catch (error: unknown) {
        this.diagnostics = [];
        this.diagnosticsTrace = null;
        this.diagnosticsError = normalizeWorkspaceError(error);
      } finally {
        this.isDiagnosticsLoading = false;
      }
    },

    async openProject(projectPath: string): Promise<WorkspaceProjectInfo> {
      this.isProjectSwitching = true;
      this.workspaceError = "";
      this.lastProjectAction = "";

      try {
        const result = await openWorkspaceProject({ projectPath });
        this.launchScreenVisible = false;
        this.currentProject = result.data;
        this.rememberRecentProject(result.data);
        this.lastProjectAction = `已打开项目：${result.data.projectName}`;
        await this.reloadProjectContext();
        return result.data;
      } catch (error: unknown) {
        this.workspaceError = normalizeWorkspaceError(error);
        throw error;
      } finally {
        this.isProjectSwitching = false;
      }
    },

    async openProjectTarget(projectPath: string, options?: { isFile?: boolean }): Promise<WorkspaceProjectInfo> {
      const project = await this.openProject(projectPath);
      if (!options?.isFile) {
        return project;
      }

      const relativePath = resolveRelativeProjectFilePath(projectPath, project.workspaceRoot);
      if (relativePath) {
        await this.openFile(relativePath);
      }
      return project;
    },

    async createProject(projectPath: string): Promise<WorkspaceProjectInfo> {
      this.isProjectCreating = true;
      this.workspaceError = "";
      this.lastProjectAction = "";

      try {
        const result = await createWorkspaceProject({ projectPath });
        this.launchScreenVisible = false;
        this.currentProject = result.data;
        this.rememberRecentProject(result.data);
        this.lastProjectAction = `已创建项目：${result.data.projectName}`;
        await this.reloadProjectContext();
        return result.data;
      } catch (error: unknown) {
        this.workspaceError = normalizeWorkspaceError(error);
        throw error;
      } finally {
        this.isProjectCreating = false;
      }
    },

    async initializeCurrentProject(projectPath = ""): Promise<WorkspaceProjectInfo> {
      this.isProjectInitializing = true;
      this.workspaceError = "";
      this.lastProjectAction = "";

      try {
        const result = await initializeWorkspaceProject(projectPath ? { projectPath } : undefined);
        this.launchScreenVisible = false;
        this.currentProject = result.data;
        this.rememberRecentProject(result.data);
        this.lastProjectAction = `已完成项目初始化：${result.data.projectName}`;
        await this.reloadProjectContext();
        return result.data;
      } catch (error: unknown) {
        this.workspaceError = normalizeWorkspaceError(error);
        throw error;
      } finally {
        this.isProjectInitializing = false;
      }
    },

    async openFile(relativePath: string, options?: { forceReload?: boolean }): Promise<void> {
      if (!relativePath) {
        return;
      }

      const switchingTarget = relativePath !== this.activeFile || Boolean(options?.forceReload);
      if (switchingTarget) {
        const saved = await this.saveDirtyActiveFileIfNeeded();
        if (!saved) {
          return;
        }
      }

      this.syncActiveDocument();
      const cached = this.documents[relativePath];
      if (cached && !options?.forceReload) {
        this.activateCachedDocument(relativePath);
        this.workspaceError = "";
        return;
      }

      this.isFileLoading = true;
      try {
        const result = await readWorkspaceFile({ relativePath });
        this.applyFileDocument(result.data);
        this.fileTrace = result.trace;
        this.workspaceError = "";
      } catch (error: unknown) {
        this.workspaceError = normalizeWorkspaceError(error);
      } finally {
        this.isFileLoading = false;
      }
    },

    async activateTab(relativePath: string): Promise<void> {
      if (!relativePath || !this.documents[relativePath]) {
        return;
      }
      if (relativePath !== this.activeFile) {
        const saved = await this.saveDirtyActiveFileIfNeeded();
        if (!saved) {
          return;
        }
      }
      this.syncActiveDocument();
      this.activateCachedDocument(relativePath);
    },

    async closeTab(relativePath: string): Promise<void> {
      if (!relativePath) {
        return;
      }

      if (this.activeFile === relativePath) {
        const saved = await this.saveDirtyActiveFileIfNeeded();
        if (!saved) {
          return;
        }
      }

      this.syncActiveDocument();
      const closingIndex = this.openTabs.findIndex((tab) => tab.relativePath === relativePath);
      if (closingIndex < 0) {
        return;
      }

      const isActiveTab = this.activeFile === relativePath;
      this.openTabs.splice(closingIndex, 1);
      delete this.documents[relativePath];

      if (!isActiveTab) {
        return;
      }

      const nextTab =
        this.openTabs[closingIndex] ??
        this.openTabs[closingIndex - 1] ??
        this.openTabs[0] ??
        null;

      if (!nextTab) {
        this.clearActiveFile();
        return;
      }

      this.activateCachedDocument(nextTab.relativePath);
    },

    setEditorContent(content: string): void {
      this.editorContent = content;
      if (!this.activeFile || !this.documents[this.activeFile]) {
        this.isDirty = false;
        return;
      }

      const document = this.documents[this.activeFile];
      if (document.readOnly) {
        this.editorContent = document.content;
        this.isDirty = false;
        return;
      }
      document.content = content;
      document.dirty = content !== document.savedContent;
      document.size = estimateUtf8Size(content);
      this.isDirty = document.dirty;
      this.updateTabState(this.activeFile);
    },

    async saveActiveFile(): Promise<void> {
      if (!this.activeFile || !this.isDirty || this.isSaving) {
        return;
      }
      const activeDocument = this.documents[this.activeFile];
      if (activeDocument?.readOnly) {
        return;
      }

      this.isSaving = true;
      try {
        const result = await writeWorkspaceFile({
          relativePath: this.activeFile,
          content: this.editorContent
        });
        this.applyFileDocument(result.data);
        this.saveTrace = result.trace;
        this.lastSavedAt = result.data.updatedAt;
        this.workspaceError = "";
        await this.refreshTree();
      } catch (error: unknown) {
        this.workspaceError = normalizeWorkspaceError(error);
      } finally {
        this.isSaving = false;
      }
    },

    async reloadActiveFile(): Promise<void> {
      if (!this.activeFile) {
        return;
      }
      const activeDocument = this.documents[this.activeFile];
      if (activeDocument?.transient) {
        return;
      }
      await this.openFile(this.activeFile, { forceReload: true });
    },

    async setEditorMode(mode: "preview" | "edit"): Promise<void> {
      if (mode === this.editorMode) {
        return;
      }
      if (mode === "edit" && this.activeDocumentReadOnly) {
        return;
      }
      if (mode === "preview") {
        const saved = await this.saveDirtyActiveFileIfNeeded();
        if (!saved) {
          return;
        }
      }
      this.editorMode = mode;
    },

    async createFile(relativePath: string, content = ""): Promise<WorkspaceFileDocument> {
      const result = await createWorkspaceFile({ relativePath, content });
      this.workspaceError = "";
      await this.refreshTree();
      this.applyFileDocument(result.data);
      return result.data;
    },

    async createDirectory(relativePath: string): Promise<WorkspacePathInfo> {
      const result = await createWorkspaceDirectory({ relativePath });
      this.workspaceError = "";
      await this.refreshTree();
      return result.data;
    },

    async importFiles(targetDirectory: string, files: WorkspaceImportFileItem[]): Promise<WorkspacePathInfo[]> {
      const result = await importWorkspaceFiles({ targetDirectory, files });
      this.workspaceError = "";
      await this.refreshTree();
      return result.data.items;
    },

    async renamePath(fromRelativePath: string, toRelativePath: string): Promise<WorkspacePathInfo> {
      const result = await renameWorkspacePath({ fromRelativePath, toRelativePath });
      this.workspaceError = "";
      this.remapPathState(fromRelativePath, toRelativePath);
      await this.refreshTree();
      return result.data;
    },

    async deletePath(relativePath: string): Promise<WorkspacePathInfo> {
      const result = await deleteWorkspacePath({ relativePath });
      this.workspaceError = "";
      this.removePathState(relativePath);
      await this.refreshTree();
      return result.data;
    },

    async copyPath(fromRelativePath: string, toRelativePath: string): Promise<WorkspacePathInfo> {
      const result = await copyWorkspacePath({ fromRelativePath, toRelativePath });
      this.workspaceError = "";
      await this.refreshTree();
      if (result.data.kind === "file") {
        await this.openFile(toRelativePath);
      }
      return result.data;
    },

    async movePath(fromRelativePath: string, toRelativePath: string): Promise<WorkspacePathInfo> {
      const result = await moveWorkspacePath({ fromRelativePath, toRelativePath });
      this.workspaceError = "";
      this.remapPathState(fromRelativePath, toRelativePath);
      await this.refreshTree();
      return result.data;
    },

    async refreshAfterCommit(segmentPath: string): Promise<void> {
      this.clearTransientPreviews();
      await Promise.all([this.refreshHealth(), this.refreshProject(), this.refreshTree()]);
      const preferredPath = this.resolveExistingPath(segmentPath);
      if (preferredPath) {
        await this.openFile(preferredPath, { forceReload: true });
        return;
      }

      if (this.activeFile && this.pathExistsInTree(this.activeFile)) {
        this.workspaceError = "";
        return;
      }

      const fallback = findFirstFile(this.tree);
      if (fallback) {
        await this.openFile(fallback, { forceReload: true });
      } else {
        this.clearActiveFile();
      }
    },

    openAgentPreview(payload: {
      sourcePath: string;
      title?: string;
      displayPath?: string;
      extension?: string;
      content: string;
      previewLines: WorkspacePreviewLine[];
    }): void {
      this.syncActiveDocument();
      this.clearTransientPreviews();

      const sourcePath = normalizeRelativePath(payload.sourcePath) || payload.sourcePath.trim();
      const previewId = buildAgentPreviewId(sourcePath || payload.displayPath || payload.title || "preview");
      const extension = payload.extension || extensionFromPath(sourcePath || payload.displayPath || "");
      const title = payload.title?.trim() || `预览 · ${fileNameFromPath(sourcePath || payload.displayPath || "未命名文件")}`;
      const nowIso = new Date().toISOString();

      const document: CachedWorkspaceDocument = {
        relativePath: previewId,
        content: payload.content,
        savedContent: payload.content,
        dirty: false,
        size: estimateUtf8Size(payload.content),
        updatedAt: nowIso,
        extension,
        kind: "agent-preview",
        title,
        displayPath: payload.displayPath?.trim() || sourcePath,
        readOnly: true,
        transient: true,
        boundRelativePath: sourcePath,
        previewLines: payload.previewLines,
        media: {
          previewSource: "agent",
        },
      };

      this.documents[previewId] = document;
      this.activeFile = previewId;
      this.activeFileContent = document.content;
      this.editorContent = document.content;
      this.activeFileUpdatedAt = document.updatedAt;
      this.activeFileExtension = document.extension;
      this.activeFileSize = document.size;
      this.activeFileKind = document.kind;
      this.activeFileMedia = document.media || {};
      this.isDirty = false;
      this.editorMode = "preview";
      this.ensureOpenTab(previewId, document.extension, false, title);
    },

    async refreshGitReviewDiff(options?: {
      focusPath?: string;
      silent?: boolean;
      traceId?: string;
      sessionId?: string;
      changedFiles?: string[];
      commitHash?: string;
    }): Promise<WorkspaceGitDiffResponse | null> {
      if (this.launchScreenVisible) {
        this.gitReviewDiff = null;
        this.gitReviewFocusPath = "";
        this.gitReviewTraceId = "";
        this.gitReviewSessionId = "";
        this.gitReviewError = "";
        return null;
      }

      const focusPath = normalizeRelativePath(options?.focusPath || "");
      const traceId = String(options?.traceId || this.gitReviewTraceId || "").trim();
      const sessionId = String(options?.sessionId || this.gitReviewSessionId || "").trim();
      const changedFiles = normalizePathList(options?.changedFiles || []);
      const commitHash = String(options?.commitHash || "").trim();
      this.gitReviewFocusPath = focusPath;
      this.gitReviewTraceId = traceId;
      this.gitReviewSessionId = sessionId;
      this.isGitReviewLoading = true;
      if (!options?.silent) {
        this.gitReviewError = "";
      }
      try {
        const result = traceId
          ? await fetchAgentRunDiff(traceId, sessionId || undefined, changedFiles, commitHash)
          : await fetchWorkspaceGitDiff();
        this.gitReviewDiff = normalizeGitDiffResponse(result.data);
        this.gitReviewError = "";
        this.syncActiveGitReviewDocument();
        return this.gitReviewDiff;
      } catch (error: unknown) {
        const message = traceId ? normalizeAgentRunDiffError(error) : normalizeWorkspaceError(error);
        if (!options?.silent) {
          this.gitReviewError = message;
        }
        this.gitReviewDiff = null;
        this.syncActiveGitReviewDocument();
        return null;
      } finally {
        this.isGitReviewLoading = false;
      }
    },

    async openGitReview(options?: { focusPath?: string }): Promise<void> {
      await this.openGitReviewDocument({ focusPath: options?.focusPath || "" });
    },

    async openAgentRunDiff(options: {
      traceId: string;
      sessionId?: string;
      focusPath?: string;
      changedFiles?: string[];
      commitHash?: string;
    }): Promise<void> {
      const traceId = String(options.traceId || "").trim();
      if (!traceId) {
        this.gitReviewError = "本轮 Diff 数据不可用。";
        return;
      }
      await this.openGitReviewDocument({
        focusPath: options.focusPath || "",
        traceId,
        sessionId: options.sessionId || "",
        changedFiles: options.changedFiles || [],
        commitHash: options.commitHash || ""
      });
    },

    async openGitReviewDocument(options?: {
      focusPath?: string;
      traceId?: string;
      sessionId?: string;
      changedFiles?: string[];
      commitHash?: string;
    }): Promise<void> {
      const saved = await this.saveDirtyActiveFileIfNeeded();
      if (!saved) {
        return;
      }
      this.syncActiveDocument();
      this.clearTransientPreviews();

      const focusPath = normalizeRelativePath(options?.focusPath || "");
      const traceId = String(options?.traceId || "").trim();
      const sessionId = String(options?.sessionId || "").trim();
      const changedFiles = normalizePathList(options?.changedFiles || []);
      const commitHash = String(options?.commitHash || "").trim();
      const diff = await this.refreshGitReviewDiff({ focusPath, traceId, sessionId, changedFiles, commitHash });
      const reviewId = buildGitReviewId(traceId);
      const nowIso = new Date().toISOString();
      const content = buildGitReviewContent(diff, this.gitReviewError);
      const document: CachedWorkspaceDocument = {
        relativePath: reviewId,
        content,
        savedContent: content,
        dirty: false,
        size: estimateUtf8Size(content),
        updatedAt: nowIso,
        extension: ".diff",
        kind: "git-review",
        title: traceId ? "本轮修改审阅" : "变更审阅",
        displayPath: traceId
          ? (focusPath ? `本轮修改 · ${focusPath}` : "本轮修改")
          : (focusPath ? `本地变更 · ${focusPath}` : "本地变更"),
        readOnly: true,
        transient: true,
        boundRelativePath: focusPath,
        media: {
          reviewSource: traceId ? "agent-run" : "git",
          gitDiff: diff,
          focusPath,
          traceId,
          sessionId,
          changedFiles,
          commitHash,
        },
      };

      this.documents[reviewId] = document;
      this.activeFile = reviewId;
      this.activeFileContent = document.content;
      this.editorContent = document.content;
      this.activeFileUpdatedAt = document.updatedAt;
      this.activeFileExtension = document.extension;
      this.activeFileSize = document.size;
      this.activeFileKind = document.kind;
      this.activeFileMedia = document.media || {};
      this.isDirty = false;
      this.editorMode = "preview";
      this.ensureOpenTab(reviewId, document.extension, false, document.title);
    },

    async openHelpGuideDocument(): Promise<void> {
      const saved = await this.saveDirtyActiveFileIfNeeded();
      if (!saved) {
        return;
      }
      this.syncActiveDocument();
      this.clearTransientPreviews();

      this.isFileLoading = true;
      try {
        const result = await fetchHelpGuide();
        const content = result.data.content || "# 使用指南\n\n暂未找到 Storydex 使用指南。";
        const guideId = ".storydex/help/usage-guide.md";
        const nowIso = new Date().toISOString();
        const document: CachedWorkspaceDocument = {
          relativePath: guideId,
          content,
          savedContent: content,
          dirty: false,
          size: estimateUtf8Size(content),
          updatedAt: result.data.items?.[0]?.updatedAt || nowIso,
          extension: ".md",
          kind: "help-guide",
          title: "使用指南",
          displayPath: "Storydex 内置使用指南",
          readOnly: true,
          transient: true,
          media: {
            guideRoot: result.data.root || "",
            guideItemCount: result.data.items?.length || 0,
          },
        };

        this.documents[guideId] = document;
        this.activeFile = guideId;
        this.activeFileContent = document.content;
        this.editorContent = document.content;
        this.activeFileUpdatedAt = document.updatedAt;
        this.activeFileExtension = document.extension;
        this.activeFileSize = document.size;
        this.activeFileKind = document.kind;
        this.activeFileMedia = document.media || {};
        this.isDirty = false;
        this.editorMode = "preview";
        this.ensureOpenTab(guideId, document.extension, false, document.title);
        this.workspaceError = "";
      } catch (error: unknown) {
        this.workspaceError = normalizeWorkspaceError(error);
      } finally {
        this.isFileLoading = false;
      }
    },

    clearTransientPreviews(): void {
      const transientPaths = Object.entries(this.documents)
        .filter(([, document]) => Boolean(document?.transient))
        .map(([relativePath]) => relativePath);

      if (!transientPaths.length) {
        return;
      }

      const activeWasTransient = this.activeFile && transientPaths.includes(this.activeFile);
      for (const relativePath of transientPaths) {
        delete this.documents[relativePath];
      }
      this.openTabs = this.openTabs.filter((tab) => !transientPaths.includes(tab.relativePath));

      if (activeWasTransient) {
        const nextTab = this.openTabs.find((tab) => Boolean(this.documents[tab.relativePath])) ?? null;
        if (nextTab) {
          this.activateCachedDocument(nextTab.relativePath);
        } else {
          this.clearActiveFile();
        }
      }
    },

    async reloadProjectContext(): Promise<void> {
      const agentStore = useAgentStore();
      agentStore.resetSession({ clearSessionId: true, clearAvailableSessions: true });
      this.launchScreenVisible = false;
      this.treeResetToken += 1;

      this.openTabs = [];
      this.documents = {};
      this.resetStoryWorkspaceState();
      this.clearActiveFile();

      await Promise.all([this.refreshHealth(), this.refreshProject(), this.refreshTree()]);
      if (!this.currentProject?.requiresInitialization) {
        await agentStore.loadSessions();
        await agentStore.loadHistory();
      }

      this.clearActiveFile();
      this.initialized = true;
    },

    collapseTree(): void {
      this.treeResetToken += 1;
    },

    enterLaunchScreen(): void {
      useAgentStore().resetSession({ clearSessionId: true, clearAvailableSessions: true });
      useGitStore().reset();
      this.launchScreenVisible = true;
      this.currentProject = null;
      this.tree = [];
      this.treeTrace = null;
      this.openTabs = [];
      this.documents = {};
      this.resetStoryWorkspaceState();
      this.treeResetToken += 1;
      this.clearActiveFile();
    },

    resetStoryWorkspaceState(): void {
      this.diagnostics = [];
      this.diagnosticsTrace = null;
      this.storySettingsTrace = null;
      this.storySettings = defaultStoryProjectSettings();
      this.storySettingsError = "";
      this.diagnosticsError = "";
      this.gitReviewDiff = null;
      this.gitReviewFocusPath = "";
      this.gitReviewError = "";
      this.isStorySettingsLoading = false;
      this.isDiagnosticsLoading = false;
      this.isGitReviewLoading = false;
    },

    async readStorySettingsFromProjectFile(): Promise<StoryProjectSettings> {
      const configPath = storyProjectConfigRelativePath(this.currentProject?.storydexDirName);
      const progressPath = storyChapterProgressRelativePath(this.currentProject?.storydexDirName);
      const cachedConfigContent = this.documents[configPath]?.content;
      const cachedProgressContent = this.documents[progressPath]?.content;
      const configPayload = cachedConfigContent
        ? parseJsonObject(cachedConfigContent)
        : parseJsonObject((await readWorkspaceFile({ relativePath: configPath })).data.content);
      let progressPayload: Record<string, unknown> = {};
      try {
        progressPayload = cachedProgressContent
          ? parseJsonObject(cachedProgressContent)
          : parseJsonObject((await readWorkspaceFile({ relativePath: progressPath })).data.content);
      } catch {
        progressPayload = {};
      }
      return normalizeStorySettingsFromProjectFile(configPayload, configPath, progressPayload);
    },

    async writeStorySettingsToProjectFile(
      payload: StoryProjectSettingsUpdateRequest
    ): Promise<StoryProjectSettings> {
      const configPath = storyProjectConfigRelativePath(this.currentProject?.storydexDirName);
      const progressPath = storyChapterProgressRelativePath(this.currentProject?.storydexDirName);
      let projectPayload = this.documents[configPath]?.content
        ? parseJsonObject(this.documents[configPath].content)
        : {};
      if (!Object.keys(projectPayload).length) {
        try {
          projectPayload = parseJsonObject((await readWorkspaceFile({ relativePath: configPath })).data.content);
        } catch {
          projectPayload = {};
        }
      }
      let progressPayload = this.documents[progressPath]?.content
        ? parseJsonObject(this.documents[progressPath].content)
        : {};
      if (!Object.keys(progressPayload).length) {
        try {
          progressPayload = parseJsonObject((await readWorkspaceFile({ relativePath: progressPath })).data.content);
        } catch {
          progressPayload = {};
        }
      }
      const updatedAt = new Date().toISOString();
      const nextProjectPayload = {
        ...projectPayload,
        version: Number(projectPayload.version || 1) || 1,
        storySegmentFormat: payload.segmentExtension.replace(/^\./, ""),
        autoNameChapterTitle: payload.autoNameChapterDirectories,
        auto_name_chapter_title: payload.autoNameChapterDirectories,
        maxSegmentsPerChapter: payload.maxSegmentsPerChapter,
        max_segments_per_chapter: payload.maxSegmentsPerChapter,
        chapterSegmentLimit: payload.maxSegmentsPerChapter,
        chapter_segment_limit: payload.maxSegmentsPerChapter,
        storyFragmentCount: payload.storyFragmentCount,
        story_fragment_count: payload.storyFragmentCount,
        storyFragmentWordCount: payload.storyFragmentWordCount,
        story_fragment_word_count: payload.storyFragmentWordCount,
        autoUpdateVariables: payload.autoUpdateVariables,
        auto_update_variables: payload.autoUpdateVariables,
        autoUpdateWiki: payload.autoUpdateWiki,
        auto_update_wiki: payload.autoUpdateWiki,
        autoUpdateVariablesNote: "自动更新变量需要较多耗时，建议每次仅生成单条剧情片段。",
        auto_update_variables_note: "自动更新变量需要较多耗时，建议每次仅生成单条剧情片段。",
        agentCommitPromptEnabled: payload.agentCommitPromptEnabled,
        agent_commit_prompt_enabled: payload.agentCommitPromptEnabled,
        autoNameChapterDirectories: payload.autoNameChapterDirectories,
        auto_name_chapter_directories: payload.autoNameChapterDirectories,
        chapterDirectoryNamingMode: payload.autoNameChapterDirectories ? "auto" : "manual",
        chapter_directory_naming_mode: payload.autoNameChapterDirectories ? "auto" : "manual",
        chapterNamingMode: payload.autoNameChapterDirectories ? "auto" : "manual",
        chapter_naming_mode: payload.autoNameChapterDirectories ? "auto" : "manual",
        contextConcisionMinCalls: payload.contextConcisionMinCalls,
        context_concision_min_calls: payload.contextConcisionMinCalls,
        contextConcisionMaxCalls: payload.contextConcisionMaxCalls,
        context_concision_max_calls: payload.contextConcisionMaxCalls,
        contextConcisionMaxInputTokens: payload.contextConcisionMaxInputTokens,
        context_concision_max_input_tokens: payload.contextConcisionMaxInputTokens,
        defaultDialogueQuote: String(projectPayload.defaultDialogueQuote || "cn_double"),
        segmentNamingMode: String(projectPayload.segmentNamingMode || "auto"),
        updatedAt
      };
      const nextProgressPayload = {
        version: Number(progressPayload.version || 1) || 1,
        updatedAt,
        chapters: Object.fromEntries(
          Object.entries(payload.chapterCompletion).map(([relativePath, completed]) => [
            relativePath,
            {
              completed: Boolean(completed),
              updatedAt,
              displayName: fileNameFromPath(relativePath)
            }
          ])
        )
      };

      const serialized = `${JSON.stringify(nextProjectPayload, null, 2)}\n`;
      const progressSerialized = `${JSON.stringify(nextProgressPayload, null, 2)}\n`;
      const updatedDocument = await writeWorkspaceFile({
        relativePath: configPath,
        content: serialized
      });
      const updatedProgressDocument = await writeWorkspaceFile({
        relativePath: progressPath,
        content: progressSerialized
      });
      this.syncAuxiliaryDocument(updatedDocument.data);
      this.syncAuxiliaryDocument(updatedProgressDocument.data);
      return normalizeStorySettingsFromProjectFile(nextProjectPayload, configPath, nextProgressPayload);
    },

    syncAuxiliaryDocument(document: WorkspaceFileDocument): void {
      const existing = this.documents[document.relativePath];
      if (!existing) {
        return;
      }

      const nextDocument: CachedWorkspaceDocument = {
        ...document,
        savedContent: document.content,
        dirty: false
      };

      this.documents[document.relativePath] = nextDocument;
      this.updateTabState(document.relativePath);

      if (this.activeFile === document.relativePath) {
        this.activeFileContent = document.content;
        this.editorContent = document.content;
        this.activeFileUpdatedAt = document.updatedAt;
        this.activeFileExtension = document.extension;
        this.activeFileSize = document.size;
        this.activeFileKind = document.kind;
        this.activeFileMedia = (document.media as Record<string, unknown>) || {};
        this.isDirty = false;
      }
    },

    syncActiveGitReviewDocument(): void {
      const reviewId = buildGitReviewId(this.gitReviewTraceId);
      const document = this.documents[reviewId];
      if (!document || document.kind !== "git-review") {
        return;
      }
      const content = buildGitReviewContent(this.gitReviewDiff, this.gitReviewError);
      const updatedDocument: CachedWorkspaceDocument = {
        ...document,
        content,
        savedContent: content,
        dirty: false,
        size: estimateUtf8Size(content),
        updatedAt: new Date().toISOString(),
        boundRelativePath: this.gitReviewFocusPath,
        displayPath: this.gitReviewTraceId
          ? (this.gitReviewFocusPath ? `本轮修改 · ${this.gitReviewFocusPath}` : "本轮修改")
          : (this.gitReviewFocusPath ? `本地变更 · ${this.gitReviewFocusPath}` : "本地变更"),
        media: {
          ...(document.media || {}),
          gitDiff: this.gitReviewDiff,
          focusPath: this.gitReviewFocusPath,
          traceId: this.gitReviewTraceId,
          sessionId: this.gitReviewSessionId,
          error: this.gitReviewError,
        },
      };
      this.documents[reviewId] = updatedDocument;
      this.updateTabState(reviewId);
      if (this.activeFile === reviewId) {
        this.activeFileContent = updatedDocument.content;
        this.editorContent = updatedDocument.content;
        this.activeFileUpdatedAt = updatedDocument.updatedAt;
        this.activeFileExtension = updatedDocument.extension;
        this.activeFileSize = updatedDocument.size;
        this.activeFileKind = updatedDocument.kind;
        this.activeFileMedia = (updatedDocument.media as Record<string, unknown>) || {};
        this.isDirty = false;
      }
    },

    rememberRecentProject(project: WorkspaceProjectInfo): void {
      const projectRoot = project.workspaceRoot.trim();
      if (!projectRoot) {
        return;
      }

      const nextProject: WorkspaceRecentProject = {
        projectName: project.projectName,
        workspaceRoot: projectRoot,
        openedAt: new Date().toISOString()
      };

      this.recentProjects = [
        nextProject,
        ...this.recentProjects.filter((item) => item.workspaceRoot !== projectRoot)
      ].slice(0, MAX_RECENT_PROJECTS);
    },

    clearActiveFile(): void {
      this.activeFile = "";
      this.activeFileContent = "";
      this.editorContent = "";
      this.activeFileUpdatedAt = "";
      this.activeFileExtension = "";
      this.activeFileSize = 0;
      this.activeFileKind = "file";
      this.activeFileMedia = {};
      this.isDirty = false;
      this.editorMode = "preview";
    },

    applyFileDocument(document: WorkspaceFileDocument): void {
      const cachedDocument: CachedWorkspaceDocument = {
        ...document,
        savedContent: document.content,
        dirty: false
      };

      this.documents[document.relativePath] = cachedDocument;
      this.activeFile = document.relativePath;
      this.activeFileContent = document.content;
      this.editorContent = document.content;
      this.activeFileUpdatedAt = document.updatedAt;
      this.activeFileExtension = document.extension;
      this.activeFileSize = document.size;
      this.activeFileKind = document.kind;
      this.activeFileMedia = (document.media as Record<string, unknown>) || {};
      this.isDirty = false;
      this.ensureOpenTab(document.relativePath, document.extension, false, document.title);
    },

    applyTreeProjectInfo(tree: {
      workspaceRoot: string;
      storydexRoot: string;
      projectName: string;
      hasStorydexConfig: boolean;
      requiresInitialization: boolean;
      missingDirectories: string[];
      openedAt: string;
    }): void {
      this.currentProject = {
        projectName: tree.projectName,
        workspaceRoot: tree.workspaceRoot,
        storydexRoot: tree.storydexRoot,
        storydexDirName: tree.storydexRoot.split("/").pop() || ".storydex",
        hasStorydexConfig: tree.hasStorydexConfig,
        requiresInitialization: tree.requiresInitialization,
        missingDirectories: tree.missingDirectories,
        projectState: tree.requiresInitialization ? "needs_init" : "ready",
        openedAt: tree.openedAt
      };
    },

    syncActiveDocument(): void {
      if (!this.activeFile || !this.documents[this.activeFile]) {
        return;
      }

      const document = this.documents[this.activeFile];
      if (document.readOnly) {
        this.isDirty = false;
        return;
      }
      document.content = this.editorContent;
      document.dirty = this.editorContent !== document.savedContent;
      document.size = estimateUtf8Size(this.editorContent);
      this.isDirty = document.dirty;
      this.updateTabState(this.activeFile);
    },

    activateCachedDocument(relativePath: string): void {
      const document = this.documents[relativePath];
      if (!document) {
        return;
      }

      this.activeFile = document.relativePath;
      this.activeFileContent = document.savedContent;
      this.editorContent = document.content;
      this.activeFileUpdatedAt = document.updatedAt;
      this.activeFileExtension = document.extension;
      this.activeFileSize = document.size;
      this.activeFileKind = document.kind;
      this.activeFileMedia = (document.media as Record<string, unknown>) || {};
      this.isDirty = document.dirty;
      this.ensureOpenTab(document.relativePath, document.extension, document.dirty, document.title);
    },

    ensureOpenTab(relativePath: string, extension: string, dirty: boolean, titleOverride = ""): void {
      const title = titleOverride || fileNameFromPath(relativePath);
      const existingIndex = this.openTabs.findIndex((tab) => tab.relativePath === relativePath);
      const nextTab: WorkspaceEditorTab = {
        relativePath,
        title,
        extension,
        dirty
      };

      if (existingIndex >= 0) {
        this.openTabs.splice(existingIndex, 1, nextTab);
      } else {
        this.openTabs.push(nextTab);
      }
    },

    updateTabState(relativePath: string): void {
      const document = this.documents[relativePath];
      if (!document) {
        return;
      }
      this.ensureOpenTab(relativePath, document.extension, document.dirty, document.title);
    },

    pathExistsInTree(relativePath: string): boolean {
      return treeContainsPath(this.tree, relativePath);
    },

    resolveExistingPath(relativePath: string): string {
      const normalized = typeof relativePath === "string" ? relativePath.trim() : "";
      if (!normalized) {
        return "";
      }
      return this.pathExistsInTree(normalized) ? normalized : "";
    },

    reconcileWorkspaceStateWithTree(): void {
      const existingPaths = collectFilePaths(this.tree);
      const transientPaths = new Set(
        Object.entries(this.documents)
          .filter(([, document]) => Boolean(document?.transient))
          .map(([relativePath]) => relativePath)
      );
      const stalePaths = Object.keys(this.documents).filter(
        (relativePath) => !existingPaths.has(relativePath) && !transientPaths.has(relativePath)
      );

      for (const relativePath of stalePaths) {
        delete this.documents[relativePath];
      }

      this.openTabs = this.openTabs.filter(
        (tab) => existingPaths.has(tab.relativePath) || transientPaths.has(tab.relativePath)
      );

      if (this.activeFile && !existingPaths.has(this.activeFile) && !transientPaths.has(this.activeFile)) {
        const nextTab =
          this.openTabs.find((tab) => Boolean(this.documents[tab.relativePath])) ??
          null;

        if (nextTab && this.documents[nextTab.relativePath]) {
          this.activateCachedDocument(nextTab.relativePath);
        } else {
          this.clearActiveFile();
        }
      }
    },

    remapPathState(fromRelativePath: string, toRelativePath: string): void {
      const normalizedFrom = normalizeRelativePath(fromRelativePath);
      const normalizedTo = normalizeRelativePath(toRelativePath);
      if (!normalizedFrom || !normalizedTo || normalizedFrom === normalizedTo) {
        return;
      }

      const remappedDocuments: Record<string, CachedWorkspaceDocument> = {};
      for (const [relativePath, document] of Object.entries(this.documents)) {
        const nextPath = rebaseRelativePath(relativePath, normalizedFrom, normalizedTo);
        remappedDocuments[nextPath] =
          nextPath === relativePath
            ? document
            : {
                ...document,
                relativePath: nextPath
              };
      }
      this.documents = remappedDocuments;

      this.openTabs = this.openTabs.map((tab) => {
        const nextPath = rebaseRelativePath(tab.relativePath, normalizedFrom, normalizedTo);
        return nextPath === tab.relativePath
          ? tab
          : {
              ...tab,
              relativePath: nextPath,
              title: fileNameFromPath(nextPath)
            };
      });

      if (this.activeFile) {
        const nextActiveFile = rebaseRelativePath(this.activeFile, normalizedFrom, normalizedTo);
        if (nextActiveFile !== this.activeFile && this.documents[nextActiveFile]) {
          this.activateCachedDocument(nextActiveFile);
        }
      }
    },

    removePathState(relativePath: string): void {
      const normalizedPath = normalizeRelativePath(relativePath);
      if (!normalizedPath) {
        return;
      }

      for (const key of Object.keys(this.documents)) {
        if (isSameOrNestedPath(key, normalizedPath)) {
          delete this.documents[key];
        }
      }

      this.openTabs = this.openTabs.filter((tab) => !isSameOrNestedPath(tab.relativePath, normalizedPath));

      if (this.activeFile && isSameOrNestedPath(this.activeFile, normalizedPath)) {
        const nextTab = this.openTabs.find((tab) => Boolean(this.documents[tab.relativePath])) ?? null;
        if (nextTab) {
          this.activateCachedDocument(nextTab.relativePath);
        } else {
          this.clearActiveFile();
        }
      }
    },

    async saveDirtyActiveFileIfNeeded(): Promise<boolean> {
      if (!this.activeFile || !this.isDirty || this.isSaving) {
        return true;
      }
      await this.saveActiveFile();
      return !this.isDirty;
    }
  }
});

function defaultStoryProjectSettings(settingsPath = ".storydex/config/project-settings.json"): StoryProjectSettings {
    return {
      segmentExtension: ".md",
      maxSegmentsPerChapter: 3,
      storyFragmentCount: 1,
      storyFragmentWordCount: 2000,
      autoUpdateVariables: false,
      autoUpdateWiki: false,
      autoUpdateVariablesNote: "自动更新变量需要较多耗时，建议每次仅生成单条剧情片段。",
      agentCommitPromptEnabled: true,
      autoNameChapterDirectories: false,
    contextConcisionMinCalls: 1,
    contextConcisionMaxCalls: 2,
    contextConcisionMaxInputTokens: 32000,
    chapterCompletion: {},
    updatedAt: "",
    settingsPath,
    source: "default"
  };
}

function storyProjectConfigRelativePath(storydexDirName = ".storydex"): string {
  return normalizeRelativePath(`${storydexDirName || ".storydex"}/config/project-settings.json`)
    || ".storydex/config/project-settings.json";
}

function storyChapterProgressRelativePath(storydexDirName = ".storydex"): string {
  return normalizeRelativePath(`${storydexDirName || ".storydex"}/memory/chapter-progress.json`)
    || ".storydex/memory/chapter-progress.json";
}

function normalizeStorySettingsPayload(
  payload: Partial<StoryProjectSettingsUpdateRequest>
): StoryProjectSettingsUpdateRequest {
  const maxSegmentsPerChapter = normalizeStoryMaxSegmentsPerChapter(payload.maxSegmentsPerChapter);
  const storyFragmentCount = normalizeStoryFragmentCount(payload.storyFragmentCount);
  const storyFragmentWordCount = normalizeStoryFragmentWordCount(payload.storyFragmentWordCount);
  const autoUpdateVariables = normalizeBooleanFlag(payload.autoUpdateVariables, false);
  const autoUpdateWiki = normalizeBooleanFlag(payload.autoUpdateWiki, false);
  const agentCommitPromptEnabled = normalizeBooleanFlag(payload.agentCommitPromptEnabled, true);
  const autoNameChapterDirectories = normalizeStoryAutoNameChapterDirectories(payload.autoNameChapterDirectories);
  const contextConcisionMinCalls = normalizeStoryCallCount(payload.contextConcisionMinCalls, 1);
  const contextConcisionMaxCalls = Math.max(
    contextConcisionMinCalls,
    normalizeStoryCallCount(payload.contextConcisionMaxCalls, 2)
  );
  const contextConcisionMaxInputTokens = normalizeStoryContextTokens(payload.contextConcisionMaxInputTokens, 32000);
  return {
    segmentExtension: normalizeStorySegmentExtension(payload.segmentExtension),
    storySegmentFormat: normalizeStorySegmentExtension(payload.segmentExtension).replace(/^\./, ""),
    maxSegmentsPerChapter,
    max_segments_per_chapter: maxSegmentsPerChapter,
    chapterSegmentLimit: maxSegmentsPerChapter,
    chapter_segment_limit: maxSegmentsPerChapter,
    storyFragmentCount,
    story_fragment_count: storyFragmentCount,
    storyFragmentWordCount,
    story_fragment_word_count: storyFragmentWordCount,
    autoUpdateVariables,
    auto_update_variables: autoUpdateVariables,
    autoUpdateWiki,
    auto_update_wiki: autoUpdateWiki,
    agentCommitPromptEnabled,
    agent_commit_prompt_enabled: agentCommitPromptEnabled,
    autoNameChapterTitle: autoNameChapterDirectories,
    auto_name_chapter_title: autoNameChapterDirectories,
    autoNameChapterDirectories,
    auto_name_chapter_directories: autoNameChapterDirectories,
    chapterDirectoryNamingMode: autoNameChapterDirectories ? "auto" : "manual",
    chapter_directory_naming_mode: autoNameChapterDirectories ? "auto" : "manual",
    chapterNamingMode: autoNameChapterDirectories ? "auto" : "manual",
    chapter_naming_mode: autoNameChapterDirectories ? "auto" : "manual",
    contextConcisionMinCalls,
    context_concision_min_calls: contextConcisionMinCalls,
    contextConcisionMaxCalls,
    context_concision_max_calls: contextConcisionMaxCalls,
    contextConcisionMaxInputTokens,
    context_concision_max_input_tokens: contextConcisionMaxInputTokens,
    chapterCompletion: normalizeChapterCompletionMap(payload.chapterCompletion)
  };
}

function normalizeStorySettingsResponse(
  payload: StoryProjectSettingsResponse,
  options?: {
    source?: StoryProjectSettings["source"];
    fallbackPath?: string;
    fallbackSettings?: StoryProjectSettings | null;
    currentSettings?: StoryProjectSettings | null;
  }
): StoryProjectSettings {
  const fallbackSettings = options?.fallbackSettings ?? null;
  const currentSettings = options?.currentSettings ?? null;
  const contextConcisionMinCalls = normalizeStoryCallCount(
    payload.contextConcisionMinCalls
      ?? payload.context_concision_min_calls
      ?? fallbackSettings?.contextConcisionMinCalls
      ?? currentSettings?.contextConcisionMinCalls,
    1
  );
  return {
    segmentExtension: normalizeStorySegmentExtension(payload.segmentExtension ?? payload.storySegmentFormat),
      maxSegmentsPerChapter: normalizeStoryMaxSegmentsPerChapter(
        payload.maxSegmentsPerChapter
          ?? payload.max_segments_per_chapter
          ?? payload.chapterSegmentLimit
          ?? payload.chapter_segment_limit
          ?? fallbackSettings?.maxSegmentsPerChapter
          ?? currentSettings?.maxSegmentsPerChapter
      ),
      storyFragmentCount: normalizeStoryFragmentCount(
        payload.storyFragmentCount
          ?? payload.story_fragment_count
          ?? fallbackSettings?.storyFragmentCount
          ?? currentSettings?.storyFragmentCount
      ),
      storyFragmentWordCount: normalizeStoryFragmentWordCount(
        payload.storyFragmentWordCount
          ?? payload.story_fragment_word_count
          ?? fallbackSettings?.storyFragmentWordCount
          ?? currentSettings?.storyFragmentWordCount
      ),
      autoUpdateVariables: normalizeBooleanFlag(
        payload.autoUpdateVariables
          ?? payload.auto_update_variables
          ?? fallbackSettings?.autoUpdateVariables
          ?? currentSettings?.autoUpdateVariables,
        false
      ),
      autoUpdateWiki: normalizeBooleanFlag(
        payload.autoUpdateWiki
          ?? payload.auto_update_wiki
          ?? fallbackSettings?.autoUpdateWiki
          ?? currentSettings?.autoUpdateWiki,
        false
      ),
      autoUpdateVariablesNote: String(
        payload.autoUpdateVariablesNote
          ?? payload.auto_update_variables_note
          ?? fallbackSettings?.autoUpdateVariablesNote
          ?? currentSettings?.autoUpdateVariablesNote
          ?? "自动更新变量需要较多耗时，建议每次仅生成单条剧情片段。"
      ).trim(),
      agentCommitPromptEnabled: normalizeBooleanFlag(
        payload.agentCommitPromptEnabled
          ?? payload.agent_commit_prompt_enabled
          ?? fallbackSettings?.agentCommitPromptEnabled
          ?? currentSettings?.agentCommitPromptEnabled,
        true
      ),
      autoNameChapterDirectories: normalizeStoryAutoNameChapterDirectories(
        payload.autoNameChapterTitle
        ?? payload.auto_name_chapter_title
        ?? payload.autoNameChapterDirectories
        ?? payload.auto_name_chapter_directories
        ?? payload.chapterDirectoryNamingMode
        ?? payload.chapter_directory_naming_mode
        ?? payload.chapterNamingMode
        ?? payload.chapter_naming_mode
        ?? payload.segmentNamingMode
        ?? payload.segment_naming_mode
        ?? fallbackSettings?.autoNameChapterDirectories
        ?? currentSettings?.autoNameChapterDirectories
    ),
    contextConcisionMinCalls,
    contextConcisionMaxCalls: Math.max(
      contextConcisionMinCalls,
      normalizeStoryCallCount(
        payload.contextConcisionMaxCalls
          ?? payload.context_concision_max_calls
          ?? fallbackSettings?.contextConcisionMaxCalls
          ?? currentSettings?.contextConcisionMaxCalls,
        2
      )
    ),
    contextConcisionMaxInputTokens: normalizeStoryContextTokens(
      payload.contextConcisionMaxInputTokens
        ?? payload.context_concision_max_input_tokens
        ?? fallbackSettings?.contextConcisionMaxInputTokens
        ?? currentSettings?.contextConcisionMaxInputTokens,
      32000
    ),
    chapterCompletion: normalizeChapterCompletionMap(
      payload.chapterCompletion
        ?? fallbackSettings?.chapterCompletion
        ?? currentSettings?.chapterCompletion
    ),
    updatedAt: String(payload.updatedAt ?? fallbackSettings?.updatedAt ?? currentSettings?.updatedAt ?? "").trim(),
    settingsPath: String(
      payload.settingsPath
        || fallbackSettings?.settingsPath
        || options?.fallbackPath
        || ".storydex/config/project-settings.json"
    ).trim(),
    source: options?.source || "api"
  };
}

function normalizeStorySettingsFromProjectFile(
  payload: Record<string, unknown>,
  settingsPath: string,
  progressPayload?: Record<string, unknown>
): StoryProjectSettings {
  const storySettings = parseJsonObject(payload.story_settings ?? payload.storySettings ?? payload);
  const chapterCompletionSource =
    parseJsonObject(progressPayload?.chapters)
    || parseJsonObject(progressPayload?.chapterCompletion)
    || parseJsonObject(storySettings.chapter_completion ?? storySettings.chapterCompletion);
  const contextConcisionMinCalls = normalizeStoryCallCount(
    storySettings.contextConcisionMinCalls
      ?? storySettings.context_concision_min_calls,
    1
  );
  return {
    segmentExtension: normalizeStorySegmentExtension(
      (storySettings.story_segment_format
        ?? storySettings.storySegmentFormat
        ?? storySettings.segment_extension
        ?? storySettings.segmentExtension) as StorySegmentExtension | string | undefined
    ),
      maxSegmentsPerChapter: normalizeStoryMaxSegmentsPerChapter(
        storySettings.maxSegmentsPerChapter
          ?? storySettings.max_segments_per_chapter
          ?? storySettings.chapterSegmentLimit
          ?? storySettings.chapter_segment_limit
      ),
      storyFragmentCount: normalizeStoryFragmentCount(
        storySettings.storyFragmentCount
          ?? storySettings.story_fragment_count
      ),
      storyFragmentWordCount: normalizeStoryFragmentWordCount(
        storySettings.storyFragmentWordCount
          ?? storySettings.story_fragment_word_count
      ),
      autoUpdateVariables: normalizeBooleanFlag(
        storySettings.autoUpdateVariables
          ?? storySettings.auto_update_variables,
        false
      ),
      autoUpdateWiki: normalizeBooleanFlag(
        storySettings.autoUpdateWiki
          ?? storySettings.auto_update_wiki,
        false
      ),
      autoUpdateVariablesNote: String(
        storySettings.autoUpdateVariablesNote
          ?? storySettings.auto_update_variables_note
          ?? "自动更新变量需要较多耗时，建议每次仅生成单条剧情片段。"
      ).trim(),
      agentCommitPromptEnabled: normalizeBooleanFlag(
        storySettings.agentCommitPromptEnabled
          ?? storySettings.agent_commit_prompt_enabled,
        true
      ),
      autoNameChapterDirectories: normalizeStoryAutoNameChapterDirectories(
        storySettings.autoNameChapterTitle
        ?? storySettings.auto_name_chapter_title
        ?? storySettings.autoNameChapterDirectories
        ?? storySettings.auto_name_chapter_directories
        ?? storySettings.chapterDirectoryNamingMode
        ?? storySettings.chapter_directory_naming_mode
        ?? storySettings.chapterNamingMode
        ?? storySettings.chapter_naming_mode
        ?? storySettings.segmentNamingMode
        ?? storySettings.segment_naming_mode
    ),
    contextConcisionMinCalls,
    contextConcisionMaxCalls: Math.max(
      contextConcisionMinCalls,
      normalizeStoryCallCount(
        storySettings.contextConcisionMaxCalls
          ?? storySettings.context_concision_max_calls,
        2
      )
    ),
    contextConcisionMaxInputTokens: normalizeStoryContextTokens(
      storySettings.contextConcisionMaxInputTokens
        ?? storySettings.context_concision_max_input_tokens,
      32000
    ),
    chapterCompletion: normalizeChapterCompletionMap(chapterCompletionSource),
    updatedAt: String(
      progressPayload?.updatedAt
      ?? progressPayload?.updated_at
      ?? storySettings.updated_at
      ?? storySettings.updatedAt
      ?? ""
    ).trim(),
    settingsPath,
    source: "project_file"
  };
}

function normalizeStorySegmentExtension(value: unknown): StorySegmentExtension {
  const normalized = String(value ?? "").trim().toLowerCase();
  return normalized === ".txt" || normalized === "txt" ? ".txt" : ".md";
}

function normalizeStoryMaxSegmentsPerChapter(value: unknown): number {
  const parsed = Number.parseInt(String(value ?? "").trim(), 10);
  if (!Number.isFinite(parsed)) {
    return 3;
  }
  return Math.max(1, Math.min(99, parsed));
}

function normalizeStoryFragmentCount(value: unknown): number {
  const parsed = Number.parseInt(String(value ?? "").trim(), 10);
  if (!Number.isFinite(parsed)) {
    return 1;
  }
  return Math.max(1, Math.min(20, parsed));
}

function normalizeStoryFragmentWordCount(value: unknown): number {
  const parsed = Number.parseInt(String(value ?? "").trim(), 10);
  if (!Number.isFinite(parsed)) {
    return 2000;
  }
  return Math.max(100, Math.min(20000, parsed));
}

function normalizeStoryCallCount(value: unknown, fallback: number): number {
  const parsed = Number.parseInt(String(value ?? "").trim(), 10);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.max(1, Math.min(8, parsed));
}

function normalizeStoryContextTokens(value: unknown, fallback: number): number {
  const parsed = Number.parseInt(String(value ?? "").trim(), 10);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.max(4000, Math.min(256000, parsed));
}

function normalizeStoryAutoNameChapterDirectories(value: unknown): boolean {
  return normalizeBooleanFlag(value, false, { trueAliases: ["auto"], falseAliases: ["manual"] });
}

function normalizeBooleanFlag(
  value: unknown,
  fallback: boolean,
  aliases?: { trueAliases?: string[]; falseAliases?: string[] }
): boolean {
  if (typeof value === "boolean") {
    return value;
  }
  const normalized = String(value ?? "").trim().toLowerCase();
  if (!normalized) {
    return fallback;
  }
  if (["false", "0", "off", "disabled", ...(aliases?.falseAliases || [])].includes(normalized)) {
    return false;
  }
  if (["true", "1", "on", "enabled", ...(aliases?.trueAliases || [])].includes(normalized)) {
    return true;
  }
  return fallback;
}

function normalizeChapterCompletionMap(value: unknown): Record<string, boolean> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return {};
  }

  const normalized: Record<string, boolean> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    const normalizedKey = normalizeRelativePath(key);
    if (!normalizedKey) {
      continue;
    }
    if (typeof item === "object" && item !== null && !Array.isArray(item)) {
      normalized[normalizedKey] = Boolean((item as Record<string, unknown>).completed);
      continue;
    }
    normalized[normalizedKey] = Boolean(item);
  }
  return normalized;
}

function parseJsonObject(value: unknown): Record<string, unknown> {
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value) as unknown;
      return parseJsonObject(parsed);
    } catch {
      return {};
    }
  }
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
}

function hasExtendedStorySettingsPayload(payload: StoryProjectSettingsResponse): boolean {
  return Boolean(
    payload.maxSegmentsPerChapter !== undefined
        || payload.max_segments_per_chapter !== undefined
        || payload.chapterSegmentLimit !== undefined
        || payload.chapter_segment_limit !== undefined
        || payload.storyFragmentCount !== undefined
        || payload.story_fragment_count !== undefined
        || payload.storyFragmentWordCount !== undefined
        || payload.story_fragment_word_count !== undefined
        || payload.autoUpdateVariables !== undefined
        || payload.auto_update_variables !== undefined
        || payload.autoUpdateWiki !== undefined
        || payload.auto_update_wiki !== undefined
        || payload.agentCommitPromptEnabled !== undefined
        || payload.agent_commit_prompt_enabled !== undefined
        || payload.autoNameChapterTitle !== undefined
      || payload.auto_name_chapter_title !== undefined
      || payload.autoNameChapterDirectories !== undefined
      || payload.auto_name_chapter_directories !== undefined
      || payload.chapterDirectoryNamingMode !== undefined
      || payload.chapter_directory_naming_mode !== undefined
      || payload.chapterNamingMode !== undefined
      || payload.chapter_naming_mode !== undefined
      || payload.segmentNamingMode !== undefined
      || payload.segment_naming_mode !== undefined
      || payload.contextConcisionMinCalls !== undefined
      || payload.context_concision_min_calls !== undefined
      || payload.contextConcisionMaxCalls !== undefined
      || payload.context_concision_max_calls !== undefined
      || payload.contextConcisionMaxInputTokens !== undefined
      || payload.context_concision_max_input_tokens !== undefined
  );
}

function collectDiagnosticCandidatePaths(nodes: WorkspaceTreeNode[]): string[] {
  const supportedExtensions = new Set([".json", ".ipynb", ".py"]);
  const paths: string[] = [];

  for (const node of nodes) {
    if (
      node.kind === "file"
      && node.relativePath
      && (
        supportedExtensions.has(String(node.extension || "").toLowerCase())
        || isStoryDiagnosticCandidate(node.relativePath)
      )
    ) {
      paths.push(node.relativePath);
      continue;
    }
    if (node.kind === "directory" && Array.isArray(node.children) && node.children.length > 0) {
      paths.push(...collectDiagnosticCandidatePaths(node.children));
    }
  }

  return paths;
}

function isStoryDiagnosticCandidate(relativePath: string): boolean {
  const normalized = normalizeRelativePath(relativePath);
  if (!normalized || !normalized.startsWith("chapters/") || normalized.endsWith(".variables.json")) {
    return false;
  }
  return /\.(md|txt)$/i.test(normalized);
}

function normalizeDiagnostics(items: WorkspaceDiagnosticItem[] | undefined): WorkspaceDiagnosticItem[] {
  if (!Array.isArray(items)) {
    return [];
  }

  return items
    .filter((item) => typeof item?.relativePath === "string")
    .map((item) => ({
      source: String(item.source || "").trim() || "workspace",
      severity: String(item.severity || "").trim() || "error",
      relativePath: normalizeRelativePath(item.relativePath),
      line: Number(item.line || 0),
      column: Number(item.column || 0),
      message: String(item.message || "").trim()
    }))
    .filter((item) => Boolean(item.relativePath) && Boolean(item.message));
}

function isStorySegmentPath(relativePath: string, preferredExtension: StorySegmentExtension): boolean {
  const normalized = normalizeRelativePath(relativePath);
  if (!normalized || !normalized.startsWith("chapters/") || normalized.endsWith(".variables.json")) {
    return false;
  }

  const name = fileNameFromPath(normalized).toLowerCase();
  return /^seg-[^/]+\.(md|txt)$/i.test(name) || name.endsWith(preferredExtension);
}

function findFirstFile(nodes: WorkspaceTreeNode[]): string {
  for (const node of nodes) {
    if (node.kind === "file" && node.relativePath) {
      return node.relativePath;
    }
    if (node.kind === "directory" && Array.isArray(node.children)) {
      const nested = findFirstFile(node.children);
      if (nested) {
        return nested;
      }
    }
  }
  return "";
}

function collectFilePaths(nodes: WorkspaceTreeNode[]): Set<string> {
  const paths = new Set<string>();
  for (const node of nodes) {
    if (node.kind === "file" && node.relativePath) {
      paths.add(node.relativePath);
      continue;
    }
    if (node.kind === "directory" && Array.isArray(node.children)) {
      for (const path of collectFilePaths(node.children)) {
        paths.add(path);
      }
    }
  }
  return paths;
}

function treeContainsPath(nodes: WorkspaceTreeNode[], relativePath: string): boolean {
  if (!relativePath) {
    return false;
  }
  for (const node of nodes) {
    if (node.kind === "file" && node.relativePath === relativePath) {
      return true;
    }
    if (node.kind === "directory" && Array.isArray(node.children) && treeContainsPath(node.children, relativePath)) {
      return true;
    }
  }
  return false;
}

function buildAgentPreviewId(relativePath: string): string {
  const normalized = normalizeRelativePath(relativePath) || "preview";
  return `.storydex/preview/${normalized.replace(/\//g, "__")}`;
}

function buildGitReviewId(traceId = ""): string {
  const normalizedTraceId = String(traceId || "").trim();
  return normalizedTraceId ? `agent-run-diff:${normalizedTraceId}` : "workspace-git-diff";
}

function buildGitReviewContent(diff: WorkspaceGitDiffResponse | null, error = ""): string {
  if (error) {
    return `Diff review unavailable\n\n${error}`;
  }
  if (!diff) {
    return "Diff review is not loaded.";
  }
  const totals = diff.totals || { files: 0, added: 0, removed: 0 };
  const lines = [
    `Git diff review (${totals.files} files, +${totals.added} -${totals.removed})`,
    `Branch: ${diff.branch || "-"}`,
    "",
  ];
  for (const file of diff.files || []) {
    lines.push(`${file.relativePath} +${file.added} -${file.removed}`);
  }
  return lines.join("\n").trim();
}

function extensionFromPath(relativePath: string): string {
  const normalized = normalizeRelativePath(relativePath);
  if (!normalized) {
    return ".txt";
  }
  const name = fileNameFromPath(normalized);
  const dotIndex = name.lastIndexOf(".");
  if (dotIndex < 0) {
    return ".txt";
  }
  return name.slice(dotIndex).toLowerCase();
}

function normalizeWorkspaceError(error: unknown): string {
  if (error instanceof ApiResponseError) {
    return error.message;
  }
  return describeTransportError(error, "工作区请求失败，请稍后重试。");
}

function normalizeAgentRunDiffError(error: unknown): string {
  if (error instanceof ApiResponseError) {
    if (error.code === "agent_run_not_found" || error.code === "agent_run_diff_unavailable") {
      return "本轮 Diff 数据不可用。";
    }
    return error.message || "本轮 Diff 数据不可用。";
  }
  const message = describeTransportError(error, "本轮 Diff 数据不可用。");
  return /404|not found/i.test(message) ? "本轮 Diff 数据不可用。" : message;
}

function normalizeGitDiffResponse(payload: WorkspaceGitDiffResponse): WorkspaceGitDiffResponse {
  const files = Array.isArray(payload.files)
    ? payload.files.map((file) => ({
        relativePath: normalizeRelativePath(String(file.relativePath || "")),
        status: String(file.status || "").trim() || "M",
        added: normalizeCount(file.added),
        removed: normalizeCount(file.removed),
        truncated: Boolean(file.truncated),
        hunks: Array.isArray(file.hunks)
          ? file.hunks.map((hunk) => ({
              header: String(hunk.header || ""),
              oldStart: normalizeCount(hunk.oldStart),
              oldLines: normalizeCount(hunk.oldLines),
              newStart: normalizeCount(hunk.newStart),
              newLines: normalizeCount(hunk.newLines),
              lines: Array.isArray(hunk.lines)
                ? hunk.lines.map((line) => ({
                    kind: normalizeGitDiffLineKind(line.kind),
                    oldLine: normalizeNullableCount(line.oldLine),
                    newLine: normalizeNullableCount(line.newLine),
                    content: String(line.content ?? ""),
                  }))
                : [],
            }))
          : [],
      }))
    : [];
  const totals = payload.totals || { files: files.length, added: 0, removed: 0 };
  return {
    available: Boolean(payload.available),
    gitInstalled: Boolean(payload.gitInstalled),
    initialized: Boolean(payload.initialized),
    branch: String(payload.branch || ""),
    files,
    totals: {
      files: normalizeCount(totals.files || files.length),
      added: normalizeCount(totals.added),
      removed: normalizeCount(totals.removed),
    },
    message: String(payload.message || ""),
  };
}

function normalizeCount(value: unknown): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return 0;
  }
  return Math.max(0, Math.round(parsed));
}

function normalizeNullableCount(value: unknown): number | null {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  return normalizeCount(value);
}

function normalizeGitDiffLineKind(value: unknown): WorkspaceGitDiffLineKind {
  return value === "added" || value === "removed" ? value : "context";
}

function countVisibleCharacters(content: string): number {
  return content.replace(/\s+/g, "").length;
}

function fileNameFromPath(relativePath: string): string {
  const parts = relativePath.split("/");
  return parts[parts.length - 1] ?? relativePath;
}

function estimateUtf8Size(content: string): number {
  if (typeof TextEncoder !== "undefined") {
    return new TextEncoder().encode(content).length;
  }
  return content.length;
}

function normalizeRelativePath(relativePath: string): string {
  return relativePath.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "").trim();
}

function normalizePathList(paths: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const path of paths) {
    const normalized = normalizeRelativePath(String(path || ""));
    if (!normalized || seen.has(normalized)) {
      continue;
    }
    seen.add(normalized);
    result.push(normalized);
  }
  return result;
}

function normalizeFilesystemPath(value: string): string {
  return String(value || "").trim().replace(/\\/g, "/").replace(/\/+$/g, "");
}

function shouldRestoreProjectFromHealth(health: SystemHealthResponse | null, lastProjectPath: string): boolean {
  const healthWorkspaceRoot = normalizeFilesystemPath(health?.workspaceRoot || "");
  const rememberedProjectPath = normalizeFilesystemPath(lastProjectPath);
  return Boolean(
    healthWorkspaceRoot &&
    rememberedProjectPath &&
    healthWorkspaceRoot === rememberedProjectPath &&
    health?.hasStorydexConfig
  );
}

function resolveRelativeProjectFilePath(targetPath: string, workspaceRoot: string): string {
  const normalizedTarget = normalizeFilesystemPath(targetPath);
  const normalizedWorkspaceRoot = normalizeFilesystemPath(workspaceRoot);
  if (!normalizedTarget || !normalizedWorkspaceRoot || normalizedTarget === normalizedWorkspaceRoot) {
    return "";
  }

  const targetLower = normalizedTarget.toLowerCase();
  const rootLower = normalizedWorkspaceRoot.toLowerCase();
  if (!targetLower.startsWith(`${rootLower}/`)) {
    return "";
  }

  return normalizeRelativePath(normalizedTarget.slice(normalizedWorkspaceRoot.length + 1));
}

function isSameOrNestedPath(candidate: string, prefix: string): boolean {
  return candidate === prefix || candidate.startsWith(`${prefix}/`);
}

function rebaseRelativePath(candidate: string, fromPrefix: string, toPrefix: string): string {
  if (!isSameOrNestedPath(candidate, fromPrefix)) {
    return candidate;
  }
  if (candidate === fromPrefix) {
    return toPrefix;
  }
  return `${toPrefix}/${candidate.slice(fromPrefix.length + 1)}`;
}

export const __workspaceStoreTestUtils = import.meta.env.MODE === "test" ? {
  defaultStoryProjectSettings,
  storyProjectConfigRelativePath,
  storyChapterProgressRelativePath,
  normalizeStorySettingsPayload,
  normalizeStorySettingsResponse,
  normalizeStorySettingsFromProjectFile,
  normalizeStorySegmentExtension,
  normalizeStoryMaxSegmentsPerChapter,
  normalizeStoryFragmentCount,
  normalizeStoryFragmentWordCount,
  normalizeStoryCallCount,
  normalizeStoryContextTokens,
  normalizeStoryAutoNameChapterDirectories,
  normalizeBooleanFlag,
  normalizeChapterCompletionMap,
  parseJsonObject,
  hasExtendedStorySettingsPayload,
  collectDiagnosticCandidatePaths,
  isStoryDiagnosticCandidate,
  normalizeDiagnostics,
  isStorySegmentPath,
  findFirstFile,
  collectFilePaths,
  treeContainsPath,
  buildAgentPreviewId,
  buildGitReviewId,
  buildGitReviewContent,
  extensionFromPath,
  normalizeWorkspaceError,
  normalizeAgentRunDiffError,
  normalizeGitDiffResponse,
  normalizeCount,
  normalizeNullableCount,
  normalizeGitDiffLineKind,
  countVisibleCharacters,
  fileNameFromPath,
  estimateUtf8Size,
  normalizeRelativePath,
  normalizePathList,
  normalizeFilesystemPath,
  shouldRestoreProjectFromHealth,
  resolveRelativeProjectFilePath,
  isSameOrNestedPath,
  rebaseRelativePath
} : null;
