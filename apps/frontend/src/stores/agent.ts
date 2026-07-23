import { defineStore } from "pinia";
import {
  AgentApiError,
  clearConversation,
  cycleAgentCoomiPermission,
  deleteAgentSession,
  deleteAgentFollowup,
  enqueueAgentFollowup,
  fetchAgentCoomiStatus,
  fetchAgentFollowups,
  fetchAgentHistory,
  fetchAgentSessions,
  rollbackLatestExecution,
  resumeAgentFollowups,
  resolveAgentCoomiApproval,
  setAgentCoomiPermission,
  steerAgentFollowup,
  stopAgentExecution,
  streamAgentPrompt,
  submitAgentRunCommitDecision,
  updateAgentFollowup
} from "@/api/agent";
import { describeTransportError } from "@/api/client";
import { fetchStoryChapterTemplates } from "@/api/workspace";
import { useGitStore } from "@/stores/git";
import { useWorkspaceStore } from "@/stores/workspace";
import type { ApiAuditRecord, ApiTrace } from "@/types/api";
import type {
  AgentCoomiStatusResponse,
  AgentChatRequest,
  AgentCommitDecisionMode,
  AgentExecutionRun,
  AgentFollowupMailboxResponse,
  AgentFollowupMessage,
  AgentFollowupMode,
  AgentPendingSnapshotConfirmation,
  AgentPendingApproval,
  AgentPendingCommitPrompt,
  AgentRunChangeLedger,
  AgentRunStatus,
  AgentSessionSummary,
  AgentStreamPacket,
  AgentTaskItem,
  AgentTaskStatus,
  AgentTraceEvent,
  CoomiWaterfallItem,
  CoomiWaterfallItemStatus,
  CoomiWaterfallItemType
} from "@/types/agent";
import type { StoryChapterTemplate } from "@/types/workspace";

interface AgentState {
  promptInput: string;
  lastPrompt: string;
  lastReply: string;
  lastRoute: string;
  lastTrace: ApiTrace | null;
  lastAudit: ApiAuditRecord[];
  lastEvents: AgentTraceEvent[];
  currentTraceId: string;
  selectedTraceId: string;
  currentSessionId: string;
  availableSessions: AgentSessionSummary[];
  executionHistory: AgentExecutionRun[];
  isRunning: boolean;
  isRollingBack: boolean;
  isStopping: boolean;
  isReexecuting: boolean;
  lastError: string;
  lastErrorCode: string | null;
  lastSuccess: string;
  coomiStatus: AgentCoomiStatusResponse | null;
  contextWindow: number | null;
  usedTokens: number | null;
  usageRatio: number | null;
  cumulativeTokens: number | null;
  compactThreshold: number | null;
  warningThreshold: number | null;
  compressionStatus: string;
  compressionSummary: string;
  pendingApprovals: AgentPendingApproval[];
  pendingSnapshotConfirmation: AgentPendingSnapshotConfirmation | null;
  pendingCommitPrompt: AgentPendingCommitPrompt | null;
  liveChangeLedger: AgentRunChangeLedger | null;
  runStartedAt: number | null;
  isCommittingGit: boolean;
  commitActionLabel: string;
  storyFragmentCount: number;
  storyFragmentWordCount: number;
  storyChapterTemplateId: string;
  storyChapterTemplates: StoryChapterTemplate[];
  storyChapterTemplatesLoading: boolean;
  storyChapterTemplatesError: string;
  followups: AgentFollowupMessage[];
  followupPaused: boolean;
  followupPauseReason: string;
  followupRevision: number;
  editingTraceId: string;
  editingOriginalPrompt: string;
  editingDraftBackup: string;
  editingHasFileChanges: boolean;
}

const MAX_EXECUTION_HISTORY = 40;
const DEFAULT_CHAPTER_TEMPLATE_ID = "default_chapter_directory";
const SINGLE_FILE_CHAPTER_TEMPLATE_ID = "single_file_chapter_directory";
let activeStreamAbortController: AbortController | null = null;
let commitProgressTimer: number | null = null;
let commitActionClearTimer: number | null = null;
let gitSummaryRefreshTimer: number | null = null;

export const useAgentStore = defineStore("agent", {
  state: (): AgentState => ({
    promptInput: "",
    lastPrompt: "",
    lastReply: "",
    lastRoute: "",
    lastTrace: null,
    lastAudit: [],
    lastEvents: [],
    currentTraceId: "",
    selectedTraceId: "",
    currentSessionId: "",
    availableSessions: [],
    executionHistory: [],
    isRunning: false,
    isRollingBack: false,
    isStopping: false,
    isReexecuting: false,
    lastError: "",
    lastErrorCode: null,
    lastSuccess: "",
    coomiStatus: null,
    contextWindow: null,
    usedTokens: null,
    usageRatio: null,
    cumulativeTokens: null,
    compactThreshold: null,
    warningThreshold: null,
    compressionStatus: "idle",
    compressionSummary: "",
    pendingApprovals: [],
    pendingSnapshotConfirmation: null,
    pendingCommitPrompt: null,
    liveChangeLedger: null,
    runStartedAt: null,
    isCommittingGit: false,
    commitActionLabel: "",
    storyFragmentCount: 1,
    storyFragmentWordCount: 2000,
    storyChapterTemplateId: DEFAULT_CHAPTER_TEMPLATE_ID,
    storyChapterTemplates: [],
    storyChapterTemplatesLoading: false,
    storyChapterTemplatesError: "",
    followups: [],
    followupPaused: false,
    followupPauseReason: "",
    followupRevision: 0,
    editingTraceId: "",
    editingOriginalPrompt: "",
    editingDraftBackup: "",
    editingHasFileChanges: false
  }),

  getters: {
    statusLabel(state): string {
      return state.isRunning ? "Coomi · Running" : "Coomi · Ready";
    },

    permissionModeLabel(state): string {
      return state.coomiStatus?.permissionLabel || state.coomiStatus?.permissionMode || "完全访问";
    },

    pendingApproval(state): AgentPendingApproval | null {
      return state.pendingApprovals[0] ?? null;
    },

    activeTraceRun(state): AgentExecutionRun | null {
      const focusTraceId = state.selectedTraceId || state.currentTraceId;
      if (focusTraceId) {
        const match = state.executionHistory.find((run) => run.traceId === focusTraceId);
        if (match) {
          return match;
        }
      }
      return state.executionHistory[0] ?? null;
    }
  },

  actions: {
    selectTraceRun(traceId: string): void {
      this.selectedTraceId = traceId;
    },

    createNewSession(): void {
      if (this.isRunning) {
        return;
      }
      this.resetSession({ clearSessionId: true });
      this.currentSessionId = createSessionId();
    },

    async stopActiveRun(): Promise<void> {
      if (!this.isRunning || this.isStopping) {
        return;
      }
      const workspaceStore = useWorkspaceStore();
      this.isStopping = true;
      this.lastError = "";
      try {
        const result = await stopAgentExecution({
          sessionId: this.currentSessionId || "default",
          expectedTraceId: this.currentTraceId,
          workspaceRoot: workspaceStore.currentProject?.workspaceRoot || workspaceStore.health?.workspaceRoot || ""
        });
        this.followupPaused = Boolean(result.data.mailboxPaused);
        this.followupPauseReason = String(result.data.pauseReason || "manual_stop");
      } catch (error: unknown) {
        this.lastError = describeTransportError(error, "Failed to stop the current Coomi execution.");
        this.lastErrorCode = error instanceof AgentApiError ? error.code ?? null : null;
      } finally {
        this.isStopping = false;
      }
    },

    resetSession(options?: { clearSessionId?: boolean; clearAvailableSessions?: boolean }): void {
      this.promptInput = "";
      this.lastPrompt = "";
      this.lastReply = "";
      this.lastRoute = "";
      this.lastTrace = null;
      this.lastAudit = [];
      this.lastEvents = [];
      this.currentTraceId = "";
      this.selectedTraceId = "";
      this.executionHistory = [];
      this.isRunning = false;
      this.isRollingBack = false;
      this.isStopping = false;
      this.isReexecuting = false;
      this.lastError = "";
      this.lastErrorCode = null;
      this.lastSuccess = "";
      this.pendingApprovals = [];
      this.pendingSnapshotConfirmation = null;
      this.pendingCommitPrompt = null;
      this.liveChangeLedger = null;
      this.runStartedAt = null;
      this.isCommittingGit = false;
      this.commitActionLabel = "";
      this.followups = [];
      this.followupPaused = false;
      this.followupPauseReason = "";
      this.followupRevision = 0;
      this.editingTraceId = "";
      this.editingOriginalPrompt = "";
      this.editingDraftBackup = "";
      this.editingHasFileChanges = false;
      if (commitProgressTimer !== null) {
        window.clearTimeout(commitProgressTimer);
        commitProgressTimer = null;
      }
      if (commitActionClearTimer !== null) {
        window.clearTimeout(commitActionClearTimer);
        commitActionClearTimer = null;
      }
      if (gitSummaryRefreshTimer !== null) {
        window.clearTimeout(gitSummaryRefreshTimer);
        gitSummaryRefreshTimer = null;
      }
      if (options?.clearSessionId) {
        this.currentSessionId = "";
      }
      if (options?.clearAvailableSessions) {
        this.availableSessions = [];
      }
    },

    async refreshCoomiStatus(): Promise<void> {
      try {
        const result = await fetchAgentCoomiStatus();
        this.coomiStatus = normalizeCoomiStatus(result.data);
        this.applyCoomiStatusContext(this.coomiStatus);
      } catch {
        this.coomiStatus = null;
      }
    },

    async cycleCoomiPermission(): Promise<void> {
      const result = await cycleAgentCoomiPermission();
      const permissionMode = String(result.data.permissionMode || "").trim();
      if (this.coomiStatus && permissionMode) {
        this.coomiStatus = {
          ...this.coomiStatus,
          permissionMode,
          permissionLabel: String(result.data.permissionLabel || "").trim()
        };
        return;
      }
      await this.refreshCoomiStatus();
    },

    async setCoomiPermission(mode: string): Promise<void> {
      const result = await setAgentCoomiPermission(mode);
      const permissionMode = String(result.data.permissionMode || "").trim();
      if (this.coomiStatus && permissionMode) {
        this.coomiStatus = {
          ...this.coomiStatus,
          permissionMode,
          permissionLabel: String(result.data.permissionLabel || "").trim()
        };
        return;
      }
      await this.refreshCoomiStatus();
    },

    async resolvePendingApproval(
      decision: "allow" | "deny" | "cancel" | "answer",
      response?: Record<string, unknown>,
      approvalId?: string
    ): Promise<void> {
      const approval = approvalId
        ? this.pendingApprovals.find((item) => item.approvalId === approvalId)
        : this.pendingApprovals[0];
      if (!approval) {
        return;
      }
      this.pendingApprovals = this.pendingApprovals.filter((item) => item.approvalId !== approval.approvalId);
      try {
        await resolveAgentCoomiApproval(approval.approvalId, decision, response);
        if (decision !== "cancel" && this.followupPaused && this.followupPauseReason === "permission_request") {
          await this.resumeFollowups();
        }
      } catch (error: unknown) {
        this.lastError = describeTransportError(error, "Failed to resolve Coomi approval.");
      }
    },

    async resolvePendingCommitPrompt(mode: AgentCommitDecisionMode, message = ""): Promise<void> {
      const prompt = this.pendingCommitPrompt;
      if (!prompt || this.isCommittingGit) {
        return;
      }
      const commitMessage = message.trim();
      if (mode === "manual" && !commitMessage) {
        this.lastError = "请输入提交信息。";
        return;
      }
      this.isCommittingGit = true;
      let resumeQueueAfterDecision = false;
      this.commitActionLabel =
        mode === "auto" ? "正在生成提交说明" : mode === "manual" ? "正在创建本地版本" : "正在保留未提交修改";
      if (commitProgressTimer !== null) {
        window.clearTimeout(commitProgressTimer);
        commitProgressTimer = null;
      }
      if (commitActionClearTimer !== null) {
        window.clearTimeout(commitActionClearTimer);
        commitActionClearTimer = null;
      }
      if (mode === "auto") {
        commitProgressTimer = window.setTimeout(() => {
          if (this.isCommittingGit) {
            this.commitActionLabel = "正在创建本地版本";
          }
        }, 2100);
      }
      this.lastError = "";
      try {
        const result = await submitAgentRunCommitDecision(
          prompt.traceId,
          {
            mode,
            message: commitMessage || undefined,
            sessionId: prompt.sessionId
          },
          prompt.sessionId
        );
        this.applyStreamPacket(prompt.traceId, buildCommitDecisionPacket(prompt, result.data));
        this.pendingCommitPrompt = null;
        resumeQueueAfterDecision = this.followupPaused && this.followupPauseReason === "git_commit_prompt";
        if (mode !== "skip" && Boolean(result.data.created)) {
          this.clearLiveChanges();
        }
        this.commitActionLabel = mode === "skip" ? "已保留未提交修改" : "本地版本已创建";
        void useGitStore().refreshSummary({ silent: true });
      } catch (error: unknown) {
        if (mode === "auto" && shouldRetryCommitWithFallbackMessage(error)) {
          try {
            const result = await submitAgentRunCommitDecision(
              prompt.traceId,
              {
                mode: "manual",
                message: fallbackCommitMessage(prompt),
                sessionId: prompt.sessionId
              },
              prompt.sessionId
            );
            this.applyStreamPacket(prompt.traceId, buildCommitDecisionPacket(prompt, result.data));
            this.pendingCommitPrompt = null;
            resumeQueueAfterDecision = this.followupPaused && this.followupPauseReason === "git_commit_prompt";
            if (Boolean(result.data.created)) {
              this.clearLiveChanges();
            }
            this.commitActionLabel = "本地版本已创建";
            void useGitStore().refreshSummary({ silent: true });
            return;
          } catch (retryError: unknown) {
            this.lastError = describeTransportError(retryError, "提交小说项目修改失败。");
            return;
          }
        }
        this.lastError = describeTransportError(error, "提交小说项目修改失败。");
      } finally {
        if (commitProgressTimer !== null) {
          window.clearTimeout(commitProgressTimer);
          commitProgressTimer = null;
        }
        this.isCommittingGit = false;
        if (resumeQueueAfterDecision) {
          void this.resumeFollowups();
        }
        commitActionClearTimer = window.setTimeout(() => {
          if (!this.isCommittingGit) {
            this.commitActionLabel = "";
          }
          commitActionClearTimer = null;
        }, 1200);
      }
    },

    setStoryGenerationOptions(options: { fragmentCount?: number; fragmentWordCount?: number; chapterTemplateId?: string }): void {
      if (options.chapterTemplateId !== undefined) {
        this.storyChapterTemplateId = String(options.chapterTemplateId || DEFAULT_CHAPTER_TEMPLATE_ID).trim();
      }
      const selectedTemplate = this.storyChapterTemplates.find(
        (item) => item.id === this.storyChapterTemplateId
      );
      const isSingleFileTemplate =
        this.storyChapterTemplateId === SINGLE_FILE_CHAPTER_TEMPLATE_ID
        || selectedTemplate?.contentMode === "single_file";
      if (options.fragmentCount !== undefined) {
        this.storyFragmentCount = isSingleFileTemplate ? 1 : normalizePositiveInteger(options.fragmentCount, 1);
      } else if (isSingleFileTemplate) {
        this.storyFragmentCount = 1;
      }
      if (options.fragmentWordCount !== undefined) {
        this.storyFragmentWordCount = clampInteger(options.fragmentWordCount, 100, 20000, 2000);
      }
    },

    async loadStoryChapterTemplates(options?: { force?: boolean }): Promise<void> {
      if (this.storyChapterTemplatesLoading) {
        return;
      }
      if (this.storyChapterTemplates.length > 0 && !options?.force) {
        return;
      }
      this.storyChapterTemplatesLoading = true;
      this.storyChapterTemplatesError = "";
      try {
        const result = await fetchStoryChapterTemplates();
        this.storyChapterTemplates = normalizeStoryChapterTemplates(result.data.items);
        if (
          this.storyChapterTemplateId &&
          !this.storyChapterTemplates.some((item) => item.id === this.storyChapterTemplateId)
        ) {
          this.storyChapterTemplateId = this.storyChapterTemplates.some((item) => item.id === DEFAULT_CHAPTER_TEMPLATE_ID)
            ? DEFAULT_CHAPTER_TEMPLATE_ID
            : (this.storyChapterTemplates[0]?.id || DEFAULT_CHAPTER_TEMPLATE_ID);
        } else if (!this.storyChapterTemplateId) {
          this.storyChapterTemplateId = this.storyChapterTemplates.some((item) => item.id === DEFAULT_CHAPTER_TEMPLATE_ID)
            ? DEFAULT_CHAPTER_TEMPLATE_ID
            : (this.storyChapterTemplates[0]?.id || DEFAULT_CHAPTER_TEMPLATE_ID);
        }
        if (
          this.storyChapterTemplateId === SINGLE_FILE_CHAPTER_TEMPLATE_ID
          || this.storyChapterTemplates.find((item) => item.id === this.storyChapterTemplateId)?.contentMode === "single_file"
        ) {
          this.storyFragmentCount = 1;
        }
      } catch (error: unknown) {
        if (isStoryChapterTemplateNotFoundError(error)) {
          this.storyChapterTemplates = [];
          this.storyChapterTemplateId = DEFAULT_CHAPTER_TEMPLATE_ID;
          this.storyChapterTemplatesError = "";
          return;
        }
        this.storyChapterTemplatesError = normalizeStoryChapterTemplateError(error);
      } finally {
        this.storyChapterTemplatesLoading = false;
      }
    },

    applyCoomiStatusContext(status: AgentCoomiStatusResponse | null): void {
      if (!status) {
        return;
      }
      this.applyContextMetrics({
        contextWindow: status.contextWindow,
        usedTokens: status.usedTokens,
        usageRatio: status.usageRatio,
        cumulativeTokens: status.cumulativeTokens,
        compactThreshold: status.compactThreshold,
        warningThreshold: status.warningThreshold,
        compressionStatus: status.compressionStatus
      });
    },

    applyContextMetrics(value: unknown): void {
      const record = toRecord(value);
      if (!record) {
        return;
      }
      const contextWindow = firstNumber(record, ["contextWindow", "context_window"]);
      const usedTokens = firstNumber(record, ["usedTokens", "used_tokens", "estimated_tokens", "prompt_tokens", "promptTokens"]);
      const cumulativeTokens = firstNumber(record, ["cumulativeTokens", "cumulative_tokens"]);
      const compactThreshold = firstNumber(record, ["compactThreshold", "compact_threshold"]);
      const warningThreshold = firstNumber(record, ["warningThreshold", "warning_threshold"]);
      const usageRatio = firstNumber(record, ["usageRatio", "usage_ratio"]);
      const compressionStatus = firstString(record, ["compressionStatus", "compression_status", "compactStatus", "compact_status"]);

      if (contextWindow !== null && contextWindow > 0) {
        this.contextWindow = contextWindow;
      }
      if (usedTokens !== null && usedTokens >= 0) {
        this.usedTokens = usedTokens;
      }
      if (cumulativeTokens !== null && cumulativeTokens >= 0) {
        this.cumulativeTokens = cumulativeTokens;
      }
      if (compactThreshold !== null && compactThreshold >= 0) {
        this.compactThreshold = compactThreshold;
      }
      if (warningThreshold !== null && warningThreshold >= 0) {
        this.warningThreshold = warningThreshold;
      }
      if (usageRatio !== null && usageRatio >= 0) {
        this.usageRatio = usageRatio;
      } else if (this.contextWindow && this.usedTokens !== null) {
        this.usageRatio = this.usedTokens / this.contextWindow;
      }
      if (compressionStatus) {
        this.compressionStatus = compressionStatus;
      }
    },

    async loadSessions(): Promise<void> {
      try {
        const result = await fetchAgentSessions();
        this.availableSessions = normalizeSessionSummaries(result.data.items);
        if (!this.currentSessionId && this.availableSessions[0]?.sessionId) {
          this.currentSessionId = this.availableSessions[0].sessionId;
        }
      } catch (error: unknown) {
        this.lastError = describeTransportError(error, "Failed to load Coomi sessions.");
      }
      await this.refreshCoomiStatus();
    },

    async loadHistory(limit = MAX_EXECUTION_HISTORY): Promise<void> {
      const sessionId = this.currentSessionId || this.availableSessions[0]?.sessionId || "default";
      this.currentSessionId = sessionId;
      try {
        const result = await fetchAgentHistory(limit, sessionId);
        // AgentPanel may still be finishing its mount-time history request when
        // the user starts typing.  Never let that stale response replace a live
        // run or history belonging to a newly selected session.
        if (this.isRunning || this.currentSessionId !== sessionId) {
          return;
        }
        this.executionHistory = normalizeHistoryRuns(result.data.items, sessionId);
        const activeRun = this.executionHistory[0] ?? null;
        this.currentTraceId = activeRun?.traceId || "";
        this.selectedTraceId = "";
        this.lastPrompt = activeRun?.prompt || "";
        this.lastReply = activeRun?.reply || "";
        this.lastRoute = activeRun?.route || "";
        this.lastTrace = activeRun?.trace || null;
        this.lastAudit = activeRun?.audit || [];
        this.lastEvents = activeRun?.events || [];
      } catch (error: unknown) {
        this.lastError = describeTransportError(error, "Failed to load Coomi history.");
      }
    },

    applyFollowupMailbox(mailbox: AgentFollowupMailboxResponse): void {
      this.followups = [...(mailbox.messages || [])]
        .filter((item) => item.status !== "sent" && item.status !== "cancelled")
        .sort((left, right) => (left.sequence || 0) - (right.sequence || 0));
      this.followupPaused = Boolean(mailbox.paused);
      this.followupPauseReason = String(mailbox.pauseReason || "");
      this.followupRevision = Number(mailbox.revision || 0);
    },

    async loadFollowups(): Promise<void> {
      const workspaceStore = useWorkspaceStore();
      const sessionId = this.currentSessionId || "default";
      const workspaceRoot = workspaceStore.currentProject?.workspaceRoot || workspaceStore.health?.workspaceRoot || "";
      try {
        const result = await fetchAgentFollowups(sessionId, workspaceRoot);
        if (this.currentSessionId === sessionId || (!this.currentSessionId && sessionId === "default")) {
          this.applyFollowupMailbox(result.data);
        }
      } catch (error: unknown) {
        this.lastError = describeTransportError(error, "Failed to load pending follow-ups.");
      }
    },

    async enqueueFollowup(mode: AgentFollowupMode = "queued"): Promise<boolean> {
      const content = this.promptInput.trim();
      if (!content || !this.isRunning) {
        return false;
      }
      const workspaceStore = useWorkspaceStore();
      const messageId = createFollowupMessageId();
      try {
        const result = await enqueueAgentFollowup({
          messageId,
          sessionId: this.currentSessionId || "default",
          activeTraceId: this.currentTraceId,
          expectedTraceId: mode === "steer" ? this.currentTraceId : "",
          workspaceRoot: workspaceStore.currentProject?.workspaceRoot || workspaceStore.health?.workspaceRoot || "",
          content,
          mode
        });
        this.upsertFollowup(result.data.message);
        this.promptInput = "";
        this.lastSuccess = mode === "steer" ? "引导信息已提交，正在等待安全中断点。" : "信息已加入待发送队列。";
        return true;
      } catch (error: unknown) {
        this.lastError = describeTransportError(error, "Failed to queue the follow-up.");
        this.lastErrorCode = error instanceof AgentApiError ? error.code ?? null : null;
        return false;
      }
    },

    async editFollowup(messageId: string, content: string): Promise<boolean> {
      const workspaceStore = useWorkspaceStore();
      try {
        const result = await updateAgentFollowup(messageId, {
          sessionId: this.currentSessionId || "default",
          workspaceRoot: workspaceStore.currentProject?.workspaceRoot || workspaceStore.health?.workspaceRoot || "",
          content
        });
        this.upsertFollowup(result.data.message);
        return true;
      } catch (error: unknown) {
        this.lastError = describeTransportError(error, "Failed to edit the pending follow-up.");
        return false;
      }
    },

    async deleteFollowup(messageId: string): Promise<boolean> {
      const workspaceStore = useWorkspaceStore();
      try {
        await deleteAgentFollowup(
          messageId,
          this.currentSessionId || "default",
          workspaceStore.currentProject?.workspaceRoot || workspaceStore.health?.workspaceRoot || ""
        );
        this.followups = this.followups.filter((item) => item.messageId !== messageId);
        return true;
      } catch (error: unknown) {
        this.lastError = describeTransportError(error, "Failed to delete the pending follow-up.");
        return false;
      }
    },

    async steerFollowup(messageId: string): Promise<boolean> {
      if (!this.isRunning || !this.currentTraceId) {
        return false;
      }
      const workspaceStore = useWorkspaceStore();
      try {
        const result = await steerAgentFollowup(messageId, {
          sessionId: this.currentSessionId || "default",
          expectedTraceId: this.currentTraceId,
          workspaceRoot: workspaceStore.currentProject?.workspaceRoot || workspaceStore.health?.workspaceRoot || ""
        });
        this.upsertFollowup(result.data.message);
        return true;
      } catch (error: unknown) {
        this.lastError = describeTransportError(error, "Failed to steer the active execution.");
        this.lastErrorCode = error instanceof AgentApiError ? error.code ?? null : null;
        return false;
      }
    },

    async resumeFollowups(): Promise<void> {
      const workspaceStore = useWorkspaceStore();
      try {
        const result = await resumeAgentFollowups(
          this.currentSessionId || "default",
          workspaceStore.currentProject?.workspaceRoot || workspaceStore.health?.workspaceRoot || ""
        );
        this.applyFollowupMailbox(result.data);
        if (this.isRunning) {
          return;
        }
        const next = this.followups.find((item) => item.mode === "queued" && item.status === "pending");
        if (!next) {
          return;
        }
        const latestTraceId = this.executionHistory[0]?.traceId || this.currentTraceId || "";
        await this.executePromptRequest(
          {
            prompt: next.content,
            activeFile: workspaceStore.activeFileBindingOrPath || workspaceStore.activeFile || "",
            workspaceRoot: workspaceStore.currentProject?.workspaceRoot || workspaceStore.health?.workspaceRoot || "",
            storyGeneration: {
              fragmentCount: this.storyFragmentCount,
              fragmentWordCount: this.storyFragmentWordCount,
              chapterTemplateId: this.storyChapterTemplateId || DEFAULT_CHAPTER_TEMPLATE_ID
            },
            sourceFollowupMessageId: next.messageId,
            sourceFollowupExpectedTraceId: latestTraceId
          },
          { sessionId: this.currentSessionId || "default", preserveComposer: true }
        );
      } catch (error: unknown) {
        this.lastError = describeTransportError(error, "Failed to resume pending follow-ups.");
      }
    },

    upsertFollowup(message: AgentFollowupMessage): void {
      const remaining = this.followups.filter((item) => item.messageId !== message.messageId);
      this.followups = message.status === "sent" || message.status === "cancelled"
        ? remaining
        : [...remaining, message].sort((left, right) => (left.sequence || 0) - (right.sequence || 0));
    },

    async selectSession(sessionId: string): Promise<void> {
      const normalized = String(sessionId || "").trim();
      if (!normalized || normalized === this.currentSessionId) {
        return;
      }
      this.currentSessionId = normalized;
      this.resetSession();
      this.currentSessionId = normalized;
      await this.loadHistory();
      await this.loadFollowups();
    },

    async clearConversation(): Promise<void> {
      const sessionId = this.currentSessionId || "default";
      try {
        await clearConversation(sessionId);
        this.resetSession();
        this.currentSessionId = sessionId;
        await this.loadSessions();
      } catch (error: unknown) {
        this.lastError = describeTransportError(error, "Failed to clear Coomi conversation.");
      }
    },

    beginEditLatestRun(run: AgentExecutionRun): boolean {
      const latest = this.executionHistory[0];
      if (
        this.isRunning ||
        this.isRollingBack ||
        this.isReexecuting ||
        !run.traceId ||
        latest?.traceId !== run.traceId ||
        run.status === "running"
      ) {
        return false;
      }
      this.editingTraceId = run.traceId;
      this.editingOriginalPrompt = run.prompt;
      this.editingDraftBackup = this.promptInput;
      this.editingHasFileChanges = Boolean(run.changeLedger?.changedFileCount || run.changeLedger?.changedFiles?.length);
      this.promptInput = run.prompt;
      this.lastError = "";
      this.lastErrorCode = null;
      return true;
    },

    cancelEditLatestRun(): void {
      if (!this.editingTraceId || this.isReexecuting) {
        return;
      }
      this.promptInput = this.editingDraftBackup;
      this.editingTraceId = "";
      this.editingOriginalPrompt = "";
      this.editingDraftBackup = "";
      this.editingHasFileChanges = false;
      this.lastError = "";
      this.lastErrorCode = null;
    },

    async reexecuteEditedLatestRun(): Promise<boolean> {
      const expectedTraceId = this.editingTraceId;
      const prompt = this.promptInput.trim();
      if (!expectedTraceId || !prompt || this.isRunning || this.isReexecuting) {
        return false;
      }
      const target = this.executionHistory.find((run) => run.traceId === expectedTraceId);
      if (!target || this.executionHistory[0]?.traceId !== expectedTraceId || target.status === "running") {
        this.lastError = "最新一轮已发生变化，请重新进入编辑。";
        this.lastErrorCode = "stale_trace";
        return false;
      }

      const workspaceStore = useWorkspaceStore();
      const existingTraceIds = new Set(this.executionHistory.map((run) => run.traceId));
      this.isReexecuting = true;
      this.lastError = "";
      this.lastErrorCode = null;
      const request: AgentChatRequest = {
        prompt,
        activeFile: workspaceStore.activeFileBindingOrPath || workspaceStore.activeFile || "",
        workspaceRoot: workspaceStore.currentProject?.workspaceRoot || workspaceStore.health?.workspaceRoot || "",
        storyGeneration: {
          fragmentCount: this.storyFragmentCount,
          fragmentWordCount: this.storyFragmentWordCount,
          chapterTemplateId: this.storyChapterTemplateId || DEFAULT_CHAPTER_TEMPLATE_ID
        },
        replaceLatestTraceId: expectedTraceId
      };
      try {
        const succeeded = await this.executePromptRequest(request, { sessionId: this.currentSessionId || "default" });
        const replacementRun = this.executionHistory.find((run) => !existingTraceIds.has(run.traceId));
        const accepted = Boolean(
          replacementRun?.events.some(
            (event) => event.event === "TaskPlanCreated" || event.event === "TurnContract" || event.phase === "task_planning"
          )
        );
        if (succeeded || accepted) {
          const original = this.executionHistory.find((run) => run.traceId === expectedTraceId);
          if (original) {
            this.upsertExecutionRun({ ...original, status: "superseded" });
          }
          this.editingTraceId = "";
          this.editingOriginalPrompt = "";
          this.editingDraftBackup = "";
          this.editingHasFileChanges = false;
          this.lastSuccess = "已替换最新一轮对话；项目文件变更未自动撤销。";
          return true;
        }
        if (replacementRun) {
          this.executionHistory = this.executionHistory.filter((run) => run.traceId !== replacementRun.traceId);
        }
        this.currentTraceId = expectedTraceId;
        this.promptInput = prompt;
        return false;
      } finally {
        this.isReexecuting = false;
      }
    },

    async rollbackLatestRun(options: { refillComposer: boolean }): Promise<boolean> {
      if (this.isRunning || this.isRollingBack) {
        return false;
      }

      const sessionId = this.currentSessionId || "default";
      this.isRollingBack = true;
      this.lastError = "";
      this.lastErrorCode = null;
      this.lastSuccess = "";
      try {
        const result = await rollbackLatestExecution(sessionId, this.executionHistory[0]?.traceId || "");
        if (!result.data.rolledBack) {
          return false;
        }

        const removedTraceId = result.data.removedTraceId || this.executionHistory[0]?.traceId || "";
        this.executionHistory = this.executionHistory.filter((run) => run.traceId !== removedTraceId);
        const activeRun = this.executionHistory[0] ?? null;
        this.currentTraceId = activeRun?.traceId || "";
        this.selectedTraceId = "";
        this.lastPrompt = activeRun?.prompt || "";
        this.lastReply = activeRun?.reply || "";
        this.lastRoute = activeRun?.route || "";
        this.lastTrace = activeRun?.trace || null;
        this.lastAudit = activeRun?.audit || [];
        this.lastEvents = activeRun?.events || [];
        if (this.pendingCommitPrompt?.traceId === removedTraceId) {
          this.pendingCommitPrompt = null;
        }
        if (this.liveChangeLedger?.traceId === removedTraceId) {
          this.liveChangeLedger = null;
        }
        if (options.refillComposer) {
          this.promptInput = result.data.prompt;
        }

        await this.loadHistory();
        this.lastSuccess = options.refillComposer ? "已撤回，可重新编辑。" : "已删除本轮对话记录。";
        return true;
      } catch (error: unknown) {
        this.lastError = describeTransportError(error, "Failed to roll back the latest Coomi execution.");
        this.lastErrorCode = error instanceof AgentApiError ? error.code ?? null : null;
        return false;
      } finally {
        this.isRollingBack = false;
      }
    },

    async deleteSession(sessionId: string): Promise<void> {
      const normalized = String(sessionId || "").trim();
      if (!normalized || this.isRunning) {
        return;
      }
      const wasActive = normalized === this.currentSessionId;
      try {
        await deleteAgentSession(normalized);
        this.availableSessions = this.availableSessions.filter((item) => item.sessionId !== normalized);
        if (wasActive) {
          this.resetSession({ clearSessionId: true });
        }
        await this.loadSessions();
        if (wasActive && this.currentSessionId) {
          await this.loadHistory();
        }
      } catch (error: unknown) {
        this.lastError = describeTransportError(error, "Failed to delete Coomi session.");
      }
    },

    async runPrompt(): Promise<void> {
      const prompt = this.promptInput.trim();
      if (!prompt || this.pendingSnapshotConfirmation || this.editingTraceId) {
        return;
      }
      if (this.isRunning) {
        await this.enqueueFollowup("queued");
        return;
      }
      const workspaceStore = useWorkspaceStore();
      const request: AgentChatRequest = {
        prompt,
        activeFile: workspaceStore.activeFileBindingOrPath || workspaceStore.activeFile || "",
        workspaceRoot: workspaceStore.currentProject?.workspaceRoot || workspaceStore.health?.workspaceRoot || "",
        storyGeneration: {
          fragmentCount: this.storyFragmentCount,
          fragmentWordCount: this.storyFragmentWordCount,
          chapterTemplateId: this.storyChapterTemplateId || DEFAULT_CHAPTER_TEMPLATE_ID
        }
      };
      await this.executePromptRequest(request);
    },

    async confirmNoSnapshot(): Promise<void> {
      const pending = this.pendingSnapshotConfirmation;
      if (!pending || this.isRunning) {
        return;
      }
      this.pendingSnapshotConfirmation = null;
      await this.executePromptRequest(
        { ...pending.request, confirmNoSnapshot: true },
        { sessionId: pending.sessionId }
      );
    },

    cancelNoSnapshot(): void {
      if (this.isRunning) {
        return;
      }
      this.pendingSnapshotConfirmation = null;
      this.lastError = "";
      this.lastErrorCode = null;
    },

    async executePromptRequest(
      request: AgentChatRequest,
      options?: { sessionId?: string; preserveComposer?: boolean }
    ): Promise<boolean> {
      const prompt = String(request.prompt || "").trim();
      if (!prompt || this.isRunning) {
        return false;
      }
      const sessionId = options?.sessionId || this.currentSessionId || "default";
      const traceId = createTraceId();
      const now = new Date().toISOString();
      const run: AgentExecutionRun = {
        traceId,
        sessionId,
        prompt,
        route: "coomi",
        agentMode: "coomi",
        llmModel: this.coomiStatus?.model || "",
        llmProvider: this.coomiStatus?.providerId || "",
        status: "running",
        noRestorePoint: Boolean(request.confirmNoSnapshot),
        createdAt: now,
        updatedAt: now,
        lastAction: "chat",
        reply: "",
        trace: null,
        audit: [],
        events: [],
        tasks: [],
        changeLedger: createEmptyChangeLedger(traceId, sessionId),
        items: [
          createWaterfallItem({
            id: `${traceId}-user`,
            type: "user",
            status: "success",
            title: "User",
            content: prompt,
            raw: { prompt }
          })
        ],
        errorMessage: "",
        errorCode: null
      };

      this.pendingSnapshotConfirmation = null;
      this.currentSessionId = sessionId;
      this.currentTraceId = traceId;
      this.selectedTraceId = "";
      this.lastPrompt = prompt;
      this.lastReply = "";
      this.lastRoute = "coomi";
      this.lastTrace = null;
      this.lastAudit = [];
      this.lastEvents = [];
      this.lastError = "";
      this.lastErrorCode = null;
      this.lastSuccess = "";
      this.pendingApprovals = [];
      this.pendingCommitPrompt = null;
      this.runStartedAt = Date.now();
      this.isRunning = true;
      if (!options?.preserveComposer) {
        this.promptInput = "";
      }
      this.upsertExecutionRun(run);

      activeStreamAbortController = new AbortController();
      let succeeded = false;
      try {
        await streamAgentPrompt(
          request,
          (packet) => this.applyStreamPacket(String(packet.traceId || traceId), packet),
          traceId,
          sessionId,
          activeStreamAbortController.signal
        );
        const current = this.executionHistory.find((item) => item.traceId === traceId);
        if (current && current.status === "running") {
          this.finishRun(traceId, "completed");
        }
        const finished = this.executionHistory.find((item) => item.traceId === traceId);
        this.lastSuccess = finished?.status === "failed" ? "" : "Coomi run complete.";
        await this.loadSessions();
        await this.loadFollowups();
        succeeded = true;
      } catch (error: unknown) {
        const normalized = normalizeAgentError(error);
        if (normalized.code === "SNAPSHOT_FAILED") {
          const retryRequest: AgentChatRequest = { ...request };
          delete retryRequest.confirmNoSnapshot;
          this.executionHistory = this.executionHistory.filter((item) => item.traceId !== traceId);
          this.currentTraceId = "";
          this.selectedTraceId = "";
          this.lastReply = "";
          this.lastTrace = null;
          this.lastAudit = [];
          this.lastEvents = [];
          this.pendingSnapshotConfirmation = {
            request: retryRequest,
            traceId,
            sessionId,
            message: normalized.message,
            details: normalized.details || {}
          };
          this.lastError = "";
          this.lastErrorCode = null;
        } else {
          const status: AgentRunStatus = normalized.code === "request_aborted" ? "stopped" : "failed";
          this.lastError = normalized.message;
          this.lastErrorCode = normalized.code;
          this.finishRun(traceId, status, normalized.message, normalized.code);
        }
      } finally {
        this.isRunning = false;
        this.runStartedAt = null;
        activeStreamAbortController = null;
      }
      return succeeded;
    },

    clearLiveChanges(): void {
      this.liveChangeLedger = null;
    },

    applyStreamPacket(traceId: string, packet: AgentStreamPacket): void {
      const eventName = String(packet._type || packet.type || "");
      if (eventName === "ReasoningChunk") {
        return;
      }
      let visiblePacket = packet;
      if (eventName === "TextChunk") {
        const visibleContent = stripDsmlToolText(packet.content);
        if (!visibleContent) {
          return;
        }
        visiblePacket = { ...packet, content: visibleContent };
      }
      let current = this.executionHistory.find((run) => run.traceId === traceId);
      if (!current && eventName === "ContinuationStarted") {
        const now = new Date().toISOString();
        const prompt = String(packet.content || "");
        const continuationSessionId = String(packet.sessionId || this.currentSessionId || "default");
        current = {
          traceId,
          sessionId: continuationSessionId,
          prompt,
          route: "coomi",
          agentMode: "coomi",
          llmModel: this.coomiStatus?.model || "",
          llmProvider: this.coomiStatus?.providerId || "",
          status: "running",
          noRestorePoint: false,
          createdAt: String(packet.createdAt || now),
          updatedAt: now,
          lastAction: "chat",
          reply: "",
          trace: null,
          audit: [],
          events: [],
          tasks: [],
          changeLedger: createEmptyChangeLedger(traceId, continuationSessionId),
          items: [
            createWaterfallItem({
              id: `${traceId}-user`,
              type: "user",
              status: "success",
              title: "User",
              content: prompt,
              raw: { prompt, messageId: packet.messageId, continuationMode: packet.continuationMode }
            })
          ],
          errorMessage: "",
          errorCode: null
        };
        this.currentTraceId = traceId;
        this.selectedTraceId = "";
        this.lastPrompt = prompt;
        this.lastReply = "";
        this.runStartedAt = Date.now();
        this.upsertExecutionRun(current);
      }
      if (!current) {
        return;
      }

      const event = streamPacketToTraceEvent(visiblePacket, current.events.length + 1);
      const nextRun: AgentExecutionRun = {
        ...current,
        events: [...current.events, event],
        updatedAt: new Date().toISOString()
      };

      if (typeof visiblePacket.noRestorePoint === "boolean") {
        nextRun.noRestorePoint = visiblePacket.noRestorePoint;
      }

      if (visiblePacket.coomiStatus) {
        this.coomiStatus = normalizeCoomiStatus(visiblePacket.coomiStatus);
        this.applyCoomiStatusContext(this.coomiStatus);
      }

      const item = streamPacketToWaterfallItem(traceId, visiblePacket, nextRun.items);
      if (item) {
        nextRun.items = mergeWaterfallItem(nextRun.items, item);
      }

      const workspaceStore = useWorkspaceStore();
      const workspaceRoot = workspaceStore.currentProject?.workspaceRoot || workspaceStore.health?.workspaceRoot || "";
      if (eventName === "ToolDone" && !visiblePacket.is_error && isWriteLikeToolPacket(visiblePacket)) {
        const changedPaths = extractChangedPathsFromToolPacket(visiblePacket, nextRun, workspaceRoot);
        if (changedPaths.length > 0) {
          nextRun.changeLedger = mergeChangeLedgerPaths(
            nextRun.changeLedger,
            changedPaths,
            traceId,
            nextRun.sessionId
          );
          this.liveChangeLedger = mergeChangeLedgerPaths(
            this.liveChangeLedger,
            changedPaths,
            traceId,
            nextRun.sessionId
          );
          scheduleGitSummaryRefresh();
        }
      }

      if (eventName === "TextChunk") {
        nextRun.reply += String(visiblePacket.content || "");
        this.lastReply = nextRun.reply;
      } else if (eventName === "TextReset") {
        nextRun.reply = visiblePacket.preserve_visible ? nextRun.reply : "";
        this.lastReply = nextRun.reply;
      } else if (eventName === "UsageUpdate" && visiblePacket.usage) {
        this.applyContextMetrics({ ...visiblePacket, ...visiblePacket.usage });
        nextRun.trace = {
          traceId,
          durationMs: nextRun.trace?.durationMs ?? 0,
          toolCalls: nextRun.trace?.toolCalls ?? 0,
          llmCalls: 1,
          promptTokens: Number(visiblePacket.usage.prompt_tokens || visiblePacket.usage.promptTokens || 0),
          completionTokens: Number(visiblePacket.usage.completion_tokens || visiblePacket.usage.completionTokens || 0),
          estimatedCost: 0,
          cacheReadInputTokens: 0,
          cacheCreationInputTokens: 0,
          cacheHitRatio: 0,
          cacheSavings: 0
        };
      } else if (eventName === "CompressionEvent") {
        this.applyContextMetrics(visiblePacket);
        this.compressionSummary = String(visiblePacket.summary || "");
      } else if (eventName === "TurnContract") {
        const intentFrame = toRecord(visiblePacket.intentFrame) || {};
        const turnPlan = toRecord(visiblePacket.turnPlan) || {};
        const skillRegistry = toRecord(visiblePacket.skillRegistry) || {};
        const toolRegistry = toRecord(visiblePacket.toolRegistry) || {};
        const contextAssembly = toRecord(visiblePacket.contextAssembly) || {};
        const contextBudget = toRecord(contextAssembly.budget) || {};
        nextRun.audit = [
          ...nextRun.audit,
          {
            action: "storydex_turn_contract",
            status: String(visiblePacket.status || ""),
            intent: String(intentFrame.primary || ""),
            requiresChapterTemplateSelection: Boolean(turnPlan.requiresChapterTemplateSelection),
            fragmentCount: Number(turnPlan.fragmentCount || 0),
            fragmentWordCount: Number(turnPlan.fragmentWordCount || 0),
            skillCount: Number(skillRegistry.skillCount || 0),
            toolCount: Number(toolRegistry.toolCount || 0),
            contextBlockCount: Number(contextBudget.blockCount || 0),
            contextTotalChars: Number(contextBudget.totalChars || 0)
          }
        ];
      } else if (eventName === "StoryGenerationValidation") {
        nextRun.audit = [
          ...nextRun.audit,
          {
            action: "story_generation_validation",
            version: Number(visiblePacket._version || 1),
            passed: Boolean(visiblePacket.passed),
            algorithm: String(visiblePacket.algorithm || ""),
            exact: Boolean(visiblePacket.exact),
            fragmentCount: Number(visiblePacket.fragmentCount || 0),
            targetWordCount: Number(visiblePacket.targetWordCount || 0),
            chapterContentMode: String(visiblePacket.chapterContentMode || ""),
            structurePassed: Boolean(visiblePacket.structurePassed),
            writeToolApplied: Boolean(visiblePacket.writeToolApplied),
            correctionAttempt: Number(visiblePacket.correctionAttempt || 0),
            fragments: Array.isArray(visiblePacket.fragments) ? visiblePacket.fragments : []
          }
        ];
      } else if (eventName === "PermissionRequest") {
        const approval = normalizePendingApproval(visiblePacket);
        if (approval) {
          this.pendingApprovals = [
            ...this.pendingApprovals.filter((item) => item.approvalId !== approval.approvalId),
            approval
          ];
        }
        this.followupPaused = true;
        this.followupPauseReason = "permission_request";
      } else if (
        eventName === "FollowupQueued" ||
        eventName === "FollowupUpdated" ||
        eventName === "SteerRequested" ||
        eventName === "SteerApplied" ||
        eventName === "ContinuationStarted"
      ) {
        const followup = normalizeFollowupPacket(visiblePacket, nextRun.sessionId);
        if (followup) {
          this.upsertFollowup(followup);
        }
      } else if (eventName === "TaskPlanCreated" || eventName === "TaskPlanUpdated") {
        nextRun.tasks = normalizeTaskPlan(visiblePacket.tasks, traceId, nextRun.tasks);
      } else if (
        eventName === "TaskStarted" ||
        eventName === "TaskCompleted" ||
        eventName === "TaskFailed" ||
        eventName === "TaskSkipped"
      ) {
        nextRun.tasks = upsertTaskEvent(nextRun.tasks, visiblePacket, traceId, nextRun.sessionId, eventName);
      } else if (eventName === "GitAutoCommit" || eventName === "GitCommitPrompt" || eventName === "GitCommitResult") {
        nextRun.audit = [
          ...nextRun.audit,
          {
            action: "agent_git_commit",
            event: eventName,
            created: Boolean(visiblePacket.created),
            reason: String(visiblePacket.reason || ""),
            target: String(visiblePacket.target || ""),
            workspaceRoot: String(visiblePacket.workspaceRoot || ""),
            commitHash: String(visiblePacket.commitHash || ""),
            changedFileCount: Number(visiblePacket.changedFileCount || 0),
            added: Number(visiblePacket.added || 0),
            removed: Number(visiblePacket.removed || 0),
            diffSource: String(visiblePacket.diffSource || "")
          }
        ];
        nextRun.changeLedger = normalizeChangeLedger(visiblePacket, traceId, nextRun.sessionId, nextRun.changeLedger);
        const created = Boolean(visiblePacket.created);
        if (created && (eventName === "GitAutoCommit" || eventName === "GitCommitResult")) {
          this.clearLiveChanges();
        } else if (eventName === "GitCommitPrompt" || eventName === "GitCommitResult") {
          this.liveChangeLedger = normalizeChangeLedger(
            visiblePacket,
            traceId,
            nextRun.sessionId,
            this.liveChangeLedger || nextRun.changeLedger
          );
        }
        if (eventName === "GitCommitPrompt") {
          this.pendingCommitPrompt = normalizeCommitPrompt(visiblePacket, traceId, nextRun.sessionId);
        } else if (eventName === "GitCommitResult") {
          if (this.pendingCommitPrompt?.traceId === traceId) {
            this.pendingCommitPrompt = null;
          }
          if (visiblePacket.created) {
            nextRun.status = "committed";
          }
        }
        if (String(visiblePacket.status || "") === "error") {
          nextRun.status = "failed";
          nextRun.errorMessage = String(visiblePacket.message || "本地 Git 自动提交失败。");
          nextRun.errorCode = "git_auto_commit_failed";
        }
        void useGitStore().refreshSummary({ silent: true });
      } else if (eventName === "ToolDone") {
        nextRun.audit = [
          ...nextRun.audit,
          {
            action: "coomi_tool_call",
            toolName: String(visiblePacket.tool_name || ""),
            toolCallId: String(visiblePacket.tool_call_id || ""),
            isError: Boolean(visiblePacket.is_error),
            durationMs: Number(visiblePacket.duration_ms || 0),
            resultPreview: String(visiblePacket.result_preview || "")
          }
        ];
      } else if (eventName === "AgentCompleted") {
        nextRun.status = "completed";
        nextRun.tasks = finalizeTaskStatuses(nextRun.tasks, "completed");
        this.pendingApprovals = [];
      } else if (eventName === "AgentCancelled") {
        nextRun.status = "cancelled";
        nextRun.tasks = finalizeTaskStatuses(nextRun.tasks, "cancelled");
        this.pendingApprovals = [];
      } else if (eventName === "AgentError") {
        nextRun.status = "failed";
        nextRun.tasks = finalizeTaskStatuses(nextRun.tasks, "failed");
        nextRun.errorMessage = String(visiblePacket.message || "Coomi execution failed.");
        nextRun.errorCode = String(visiblePacket.error_type || "coomi_error");
        this.pendingApprovals = [];
      }

      this.lastEvents = nextRun.events;
      this.lastAudit = nextRun.audit;
      this.lastTrace = nextRun.trace;
      this.upsertExecutionRun(nextRun);
    },

    finishRun(traceId: string, status: AgentRunStatus, errorMessage = "", errorCode: string | null = null): void {
      const current = this.executionHistory.find((run) => run.traceId === traceId);
      if (!current) {
        return;
      }
      const nextRun = {
        ...current,
        status,
        tasks: finalizeTaskStatuses(current.tasks, status),
        errorMessage,
        errorCode,
        updatedAt: new Date().toISOString()
      };
      this.lastTrace = nextRun.trace;
      this.lastAudit = nextRun.audit;
      this.lastEvents = nextRun.events;
      this.upsertExecutionRun(nextRun);
    },

    upsertExecutionRun(run: AgentExecutionRun): void {
      const remaining = this.executionHistory.filter((item) => item.traceId !== run.traceId);
      this.executionHistory = [run, ...remaining]
        .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt))
        .slice(0, MAX_EXECUTION_HISTORY);
    }
  }
});

function streamPacketToTraceEvent(packet: AgentStreamPacket, index: number): AgentTraceEvent {
  const eventName = String(packet._type || packet.type || "event");
  return {
    index,
    event: eventName,
    phase: phaseForEvent(eventName),
    status: statusForPacket(eventName, packet),
    detail: detailForPacket(eventName, packet),
    timestamp: new Date().toISOString(),
    data: packet as unknown as Record<string, unknown>
  };
}

function streamPacketToWaterfallItem(
  traceId: string,
  packet: AgentStreamPacket,
  existingItems: CoomiWaterfallItem[] = []
): CoomiWaterfallItem | null {
  const eventName = String(packet._type || packet.type || "");
  const raw = packet as unknown as Record<string, unknown>;
  if (eventName === "RunAccepted" || eventName === "TurnPhase") {
    const elapsedMs = Math.max(0, Number(packet.elapsedMs || 0));
    const label = String(packet.label || packet.detail || (eventName === "RunAccepted" ? "请求已接收" : "正在准备"));
    const detail = String(packet.detail || "").trim();
    const elapsedLabel = elapsedMs > 0 ? ` · ${(elapsedMs / 1000).toFixed(1)}s` : "";
    return createWaterfallItem({
      id: `${traceId}-active-phase`,
      type: "phase",
      status: statusForPacket(eventName, packet),
      title: label,
      content: `${detail && detail !== label ? `${label} · ${detail}` : label}${elapsedLabel}`,
      raw
    });
  }
  if (eventName === "TextChunk") {
    const content = stripDsmlToolText(packet.content);
    if (!content) {
      return null;
    }
    const id = segmentItemId(existingItems, traceId, "assistant");
    return createWaterfallItem({
      id,
      type: "assistant",
      status: "running",
      title: "Assistant",
      content,
      raw
    });
  }
  if (eventName === "ReasoningChunk") {
    const id = segmentItemId(existingItems, traceId, "reasoning");
    return createWaterfallItem({
      id,
      type: "reasoning",
      status: "running",
      title: "Reasoning",
      content: String(packet.content || ""),
      raw
    });
  }
  if (eventName === "ConnectionRetry") {
    const attempt = Math.max(1, Number(packet.attempt || 1));
    const maxAttempts = Math.max(attempt, Number(packet.maxAttempts || packet.max_attempts || attempt));
    const message = String(packet.message || "模型连接中断，正在自动重试。");
    return createWaterfallItem({
      id: `${traceId}-connection-retry`,
      type: "system",
      status: "warning",
      title: "模型连接重试",
      content: `${message}（${attempt}/${maxAttempts}）`,
      raw
    });
  }
  if (eventName === "ContinuationStarted" && packet.continuationMode === "steer") {
    const content = String(packet.content || "").trim();
    if (!content) {
      return null;
    }
    const continuationId = String(packet.segmentId || packet.messageId || "steer");
    return createWaterfallItem({
      id: `${traceId}-user-${continuationId}`,
      type: "user",
      status: "success",
      title: "User",
      content,
      raw
    });
  }
  if (eventName === "ContinuationStarted" && packet.continuationMode === "story_generation_correction") {
    const attempt = Math.max(1, Number(packet.correctionAttempt || 1));
    const maximum = Math.max(attempt, Number(packet.maximumCorrectionAttempts || attempt));
    return createWaterfallItem({
      id: `${traceId}-story-generation-correction`,
      type: "system",
      status: "running",
      title: "正在按客观字数自动修订",
      content: `第 ${attempt}/${maximum} 次修订；仍在同一执行中，尚未完成。`,
      raw
    });
  }
  if (eventName === "ToolStart" || eventName === "ToolRunning" || eventName === "ToolDone" || eventName === "ToolCacheHit") {
    const toolCallId = String(packet.tool_call_id || packet.tool_name || `${traceId}-tool`);
    return createWaterfallItem({
      id: `${traceId}-tool-${toolCallId}`,
      type: "tool",
      status: eventName === "ToolDone" ? (packet.is_error ? "error" : "success") : "running",
      title: eventName,
      content: eventName === "ToolDone" ? String(packet.result_preview || "") : String(packet.progress || ""),
      toolName: String(packet.tool_name || ""),
      toolCallId,
      arguments: packet.arguments,
      resultPreview: String(packet.result_preview || ""),
      raw
    });
  }
  if (eventName === "TurnContract") {
    const status = statusForPacket(eventName, packet);
    return createWaterfallItem({
      id: `${traceId}-turn-contract`,
      type: "system",
      status,
      title: "Storydex 执行契约",
      content: summarizeTurnContractPacket(packet),
      raw
    });
  }
  if (eventName === "StoryGenerationValidation") {
    const status = statusForPacket(eventName, packet);
    return createWaterfallItem({
      id: `${traceId}-story-generation-validation`,
      type: status === "error" ? "error" : "system",
      status,
      title: "Storydex 正文客观验收",
      content: summarizeStoryGenerationValidationPacket(packet),
      raw
    });
  }
  if (
      eventName === "UsageUpdate" ||
      eventName === "CompressionEvent" ||
      eventName === "PermissionRequest" ||
      eventName === "TaskPlanCreated" ||
      eventName === "TaskStarted" ||
      eventName === "TaskCompleted" ||
      eventName === "TaskFailed" ||
      eventName === "TaskSkipped" ||
      eventName === "TaskPlanUpdated" ||
    eventName === "StageOutput" ||
    eventName === "AgentStarted" ||
    eventName === "AgentCompleted" ||
    eventName === "AgentCancelled"
    ) {
      return null;
    }
    if (eventName === "GitAutoCommit" || eventName === "GitCommitPrompt" || eventName === "GitCommitResult") {
      const status = statusForPacket(eventName, packet);
      return createWaterfallItem({
        id: `${traceId}-git-version`,
        type: status === "error" ? "error" : "system",
        status,
        title: "小说项目版本记录",
        content: summarizeGitAutoCommitPacket(packet),
        raw
      });
    }
    if (eventName === "AgentError") {
      return createWaterfallItem({
      id: `${traceId}-error`,
      type: "error",
      status: "error",
      title: packet.error_type || "Coomi error",
      content: String(packet.message || "Coomi execution failed."),
      raw
    });
  }
  return null;
}

function segmentItemId(existingItems: CoomiWaterfallItem[], traceId: string, type: "assistant" | "reasoning"): string {
  const last = [...existingItems].reverse().find((item) => item.type !== "usage" && item.type !== "compression" && item.type !== "system" && item.type !== "phase");
  if (last?.type === type) {
    return last.id;
  }
  const nextIndex = existingItems.filter((item) => item.type === type).length + 1;
  return `${traceId}-${type}-${nextIndex}`;
}

function createWaterfallItem(input: {
  id: string;
  type: CoomiWaterfallItemType;
  status: CoomiWaterfallItemStatus;
  title: string;
  content: string;
  raw?: Record<string, unknown>;
  toolName?: string;
  toolCallId?: string;
  arguments?: Record<string, unknown>;
  resultPreview?: string;
  usage?: Record<string, number>;
  compression?: Record<string, unknown>;
}): CoomiWaterfallItem {
  return {
    id: input.id,
    type: input.type,
    status: input.status,
    title: input.title,
    content: input.content,
    timestamp: new Date().toISOString(),
    toolName: input.toolName,
    toolCallId: input.toolCallId,
    arguments: input.arguments,
    resultPreview: input.resultPreview,
    usage: input.usage,
    compression: input.compression,
    raw: input.raw
  };
}

function mergeWaterfallItem(items: CoomiWaterfallItem[], item: CoomiWaterfallItem): CoomiWaterfallItem[] {
  const existing = items.find((candidate) => candidate.id === item.id);
  if (!existing) {
    return [...items, item];
  }
  return items.map((candidate) => {
    if (candidate.id !== item.id) {
      return candidate;
    }
    const shouldAppend = item.type === "assistant" || item.type === "reasoning";
    return {
      ...candidate,
      ...item,
      content: shouldAppend ? `${candidate.content}${item.content}` : item.content || candidate.content,
      arguments: item.arguments ?? candidate.arguments,
      resultPreview: item.resultPreview || candidate.resultPreview,
      raw: item.raw ?? candidate.raw
    };
  });
}

function phaseForEvent(eventName: string): string {
  if (eventName.startsWith("Tool")) return "tool";
  if (eventName === "TextChunk" || eventName === "ReasoningChunk" || eventName === "ConnectionRetry") return "model";
  if (eventName === "GitAutoCommit" || eventName === "GitCommitPrompt" || eventName === "GitCommitResult") return "version_control";
  if (eventName.startsWith("Task")) return "planning";
  if (eventName === "TurnContract" || eventName === "StoryGenerationValidation") return "orchestration";
  if (eventName === "RunAccepted" || eventName === "UsageUpdate" || eventName === "CompressionEvent" || eventName === "TurnPhase") return "runtime";
  if (eventName.startsWith("Agent")) return "agent";
  return "runtime";
}

function statusForPacket(eventName: string, packet: AgentStreamPacket): CoomiWaterfallItemStatus {
  if (eventName === "AgentError" || packet.is_error) return "error";
  if (eventName === "TaskFailed") return "error";
  if (eventName === "TaskSkipped") return "warning";
  if (eventName === "TaskStarted") return "running";
  if (eventName === "TaskCompleted" || eventName === "TaskPlanCreated" || eventName === "TaskPlanUpdated") return "success";
  if (eventName === "GitAutoCommit" || eventName === "GitCommitPrompt" || eventName === "GitCommitResult") {
    const status = String(packet.status || "").trim();
    if (status === "error" || status === "warning" || status === "success" || status === "running") {
      return status;
    }
    if (status === "pending") return "warning";
    return "info";
  }
  if (eventName === "TurnContract") {
    return String(packet.status || "") === "needs_user_input" ? "warning" : "info";
  }
  if (eventName === "StoryGenerationValidation") {
    return packet.passed ? "success" : "error";
  }
  if (eventName === "ConnectionRetry") return "warning";
  if (eventName === "AgentCancelled") return "warning";
  if (eventName === "AgentCompleted" || eventName === "ToolDone") return "success";
  const status = String(packet.status || "").trim();
  if (status === "error" || status === "warning" || status === "success" || status === "running") {
    return status;
  }
  return "info";
}

function detailForPacket(eventName: string, packet: AgentStreamPacket): string {
  if (eventName.startsWith("Task")) return String(packet.title || packet.detail || eventName);
  if (eventName.startsWith("Tool")) return String(packet.tool_name || eventName);
  if (eventName === "TextChunk" || eventName === "ReasoningChunk") return String(packet.content || "").slice(0, 240);
  if (eventName === "ConnectionRetry") {
    const attempt = Math.max(1, Number(packet.attempt || 1));
    const maxAttempts = Math.max(attempt, Number(packet.maxAttempts || packet.max_attempts || attempt));
    return `${String(packet.message || "模型连接中断，正在自动重试。")}（${attempt}/${maxAttempts}）`;
  }
  if (eventName === "AgentError") return String(packet.message || "Coomi error");
  if (eventName === "GitAutoCommit" || eventName === "GitCommitPrompt" || eventName === "GitCommitResult") return summarizeGitAutoCommitPacket(packet);
  if (eventName === "TurnContract") return summarizeTurnContractPacket(packet);
  if (eventName === "StoryGenerationValidation") return summarizeStoryGenerationValidationPacket(packet);
  if (eventName === "RunAccepted" || eventName === "TurnPhase") return String(packet.detail || packet.label || eventName);
  if (eventName === "AgentCompleted") return `tokens ${Number(packet.total_tokens || 0)}`;
  return eventName;
}

function summarizeGitAutoCommitPacket(packet: AgentStreamPacket): string {
  const created = Boolean(packet.created);
  const shortHash = String(packet.shortHash || "").trim();
  const changedFileCount = Number(packet.changedFileCount || 0);
  const message = String(packet.message || "").trim();
  const targetLabel = String(packet.targetLabel || "Storydex 小说项目").trim();
  const workspaceRoot = String(packet.workspaceRoot || "").trim();
  const targetSuffix = workspaceRoot ? ` · ${workspaceRoot}` : "";
  if (packet._type === "GitCommitPrompt" || packet.type === "GitCommitPrompt") {
    return `${targetLabel}检测到 ${changedFileCount} 个未提交文件，等待确认${targetSuffix}`;
  }
  if (created) {
    const suffix = shortHash ? ` ${shortHash}` : "";
    return `${targetLabel}已创建版本控制记录${suffix} · ${changedFileCount} 个文件${targetSuffix}`;
  }
  return `${targetLabel}：${message || "本轮无文件变更，未创建版本记录"}${targetSuffix}`;
}

function summarizeTurnContractPacket(packet: AgentStreamPacket): string {
  const intentFrame = toRecord(packet.intentFrame) || {};
  const turnPlan = toRecord(packet.turnPlan) || {};
  const executionPolicy = toRecord(packet.executionPolicy) || {};
  const updatePolicy = toRecord(packet.updatePolicy) || {};
  const skillRegistry = toRecord(packet.skillRegistry) || {};
  const toolRegistry = toRecord(packet.toolRegistry) || {};
  const contextAssembly = toRecord(packet.contextAssembly) || {};
  const intent = firstString(intentFrame, ["primary"]) || "general";
  const fragmentCount = firstNumber(turnPlan, ["fragmentCount"]) ?? 1;
  const fragmentWordCount = firstNumber(turnPlan, ["fragmentWordCount"]) ?? 2000;
  const requiresTemplate = Boolean(turnPlan.requiresChapterTemplateSelection);
  const selectedTemplate = firstString(turnPlan, ["selectedChapterTemplate"]);
  const selectedTemplateDetail = toRecord(turnPlan.selectedChapterTemplateDetail) || {};
  const selectedTemplateName = firstString(selectedTemplateDetail, ["name"]);
  const invalidTemplate = firstString(turnPlan, ["invalidChapterTemplate"]);
  const nextSegmentPath = firstString(turnPlan, ["nextSegmentPath"]);
  const status = String(packet.status || "ready");
  const pieces = [
    `状态：${status}`,
    `意图：${intent}`,
    `片段：${fragmentCount} 条 x ${fragmentWordCount} 字`,
    `直接写入：${Boolean(executionPolicy.directFileWrites) ? "开启" : "关闭"}`,
    `小说项目 Git：${Boolean(executionPolicy.localGitAutoCommit) ? "自动提交" : "未开启"}`
  ];
  if (requiresTemplate) {
    pieces.push("需要先选择章节目录模板");
  }
  if (invalidTemplate) {
    pieces.push(`模板失效：${invalidTemplate}`);
  } else if (selectedTemplateName || selectedTemplate) {
    pieces.push(`模板：${selectedTemplateName || selectedTemplate}`);
  }
  if (nextSegmentPath) {
    pieces.push(`下一片段：${nextSegmentPath}`);
  }
  const contextSummary = summarizeContextAssembly(contextAssembly);
  if (contextSummary) {
    pieces.push(`上下文：${contextSummary}`);
  }
  const presetCompileWarning = summarizePresetCompileFailures(contextAssembly);
  if (presetCompileWarning) {
    pieces.push(presetCompileWarning);
  }
  const skillCount = firstNumber(skillRegistry, ["skillCount"]) ?? 0;
  if (skillCount) {
    pieces.push(`技能：${skillCount} 个`);
  }
  const toolCount = firstNumber(toolRegistry, ["toolCount"]) ?? 0;
  if (toolCount) {
    pieces.push(`领域工具：${toolCount} 个`);
  }
  const variableUpdateLabel = Boolean(updatePolicy.autoUpdateVariables)
    ? "自动更新"
    : intent === "story_generation"
      ? "正文生成后直接整理"
      : "生成后询问";
  pieces.push(`变量：${variableUpdateLabel}`);
  pieces.push(`WIKI：${Boolean(updatePolicy.autoUpdateWiki) ? "自动更新" : "变量后询问"}`);
  return pieces.join(" · ");
}

function summarizeStoryGenerationValidationPacket(packet: AgentStreamPacket): string {
  const fragments = Array.isArray(packet.fragments) ? packet.fragments : [];
  const summaries = fragments.slice(0, 6).map((value, index) => {
    const fragment = toRecord(value) || {};
    const path = firstString(fragment, ["path"]) || `片段 ${index + 1}`;
    const actual = firstNumber(fragment, ["generatedWordCount", "actualWordCount", "fileWordCount"]) ?? 0;
    const target = firstNumber(fragment, ["targetWordCount"]) ?? Number(packet.targetWordCount || 0);
    const difference = firstNumber(fragment, ["difference"]) ?? actual - target;
    const differenceLabel = difference === 0 ? "" : `，差 ${difference > 0 ? "+" : ""}${difference}`;
    return `${path}：${actual}/${target} 字${differenceLabel}`;
  });
  if (fragments.length > summaries.length) {
    summaries.push(`另有 ${fragments.length - summaries.length} 个片段`);
  }
  const result = packet.passed ? "通过" : "未通过";
  const structure = packet.structurePassed === false ? "；章节结构不符合模板" : "";
  const writeTool = packet.writeToolApplied === false ? "；本轮未成功执行受约束正文写入" : "";
  const detail = summaries.length ? `；${summaries.join("；")}` : "";
  return `${result}：按 Storydex 非空白字符统计精确验收${structure}${writeTool}${detail}`;
}

function summarizePresetCompileFailures(contextAssembly: Record<string, unknown>): string {
  const notes = Array.isArray(contextAssembly.notes) ? contextAssembly.notes : [];
  const failures = notes
    .map((item) => String(item || ""))
    .filter((note) => note.startsWith("preset_compile_failed:"))
    .map((note) => note.slice("preset_compile_failed:".length).trim())
    .filter(Boolean);
  if (!failures.length) {
    return "";
  }
  return `⚠ 预设编译失败（已回退为原文摘要）：${failures.slice(0, 2).join("；")}`;
}

function summarizeContextAssembly(contextAssembly: Record<string, unknown>): string {
  const budget = toRecord(contextAssembly.budget) || {};
  const blockCount = firstNumber(budget, ["blockCount"]) ?? 0;
  const totalChars = firstNumber(budget, ["totalChars"]) ?? 0;
  const sources = Array.isArray(contextAssembly.sources) ? contextAssembly.sources : [];
  const sourcePieces = sources
    .slice(0, 5)
    .map((item) => {
      const source = toRecord(item) || {};
      const kind = firstString(source, ["kind"]);
      if (!kind) return "";
      const count = firstNumber(source, ["count"]) ?? 0;
      return `${kind}=${count}`;
    })
    .filter(Boolean);
  if (!blockCount && !sourcePieces.length) {
    return "";
  }
  const head = `${blockCount} 块/${totalChars} 字`;
  return sourcePieces.length ? `${head} (${sourcePieces.join(", ")})` : head;
}

function stripDsmlToolText(value: unknown): string {
  const text = stripTextualToolBlocks(String(value || ""));
  if (!text.toLowerCase().includes("dsml")) {
    return text;
  }
  const lines = text.match(/[^\r\n]*(?:\r\n|\n|\r|$)/g) || [text];
  const kept = lines.filter((line) => {
    if (!line) {
      return false;
    }
    const compact = line.toLowerCase().replace(/\s+/g, "");
    if (!compact.includes("dsml")) {
      return true;
    }
    return !(
      compact.includes("tool_calls") ||
      compact.includes("tool_call") ||
      compact.includes("invoke") ||
      compact.includes("parameter") ||
      compact.startsWith("<||dsml") ||
      compact.startsWith("&lt;||dsml")
    );
  });
  const cleaned = kept.join("");
  const compactCleaned = cleaned.toLowerCase().replace(/\s+/g, "");
  if (
    compactCleaned.includes("dsml") &&
    (compactCleaned.includes("tool_calls") ||
      compactCleaned.includes("tool_call") ||
      compactCleaned.includes("invoke") ||
      compactCleaned.includes("parameter"))
  ) {
    return "";
  }
  return cleaned;
}

function stripTextualToolBlocks(text: string): string {
  if (!text) {
    return "";
  }
  const toolTagPattern =
    "read|read_file|readfile|glob|grep|bash|powershell|web_search|websearch|web_fetch|webfetch|write|edit|todo|todowrite|todo_write|ask_user|ask_user_question|askuserquestion|enter_plan_mode|enterplanmode|exit_plan_mode|exitplanmode";
  const blockPattern = new RegExp(`<\\s*(${toolTagPattern})\\b[^>]*>[\\s\\S]*?<\\/\\s*\\1\\s*>`, "gi");
  const tagLinePattern = new RegExp(`^\\s*<\\/?\\s*(${toolTagPattern})\\b[^>]*>\\s*$`, "i");
  const paramLinePattern =
    /^\s*<\s*(path|pattern|file_path|command|query|url|prompt|offset|limit|content|old_string|new_string|todos)\b[^>]*>[\s\S]*?<\/\s*\1\s*>\s*$/i;
  const cleaned = text.replace(blockPattern, "");
  if (cleaned === text && !looksLikeToolXmlFragment(text, tagLinePattern, paramLinePattern)) {
    return text;
  }
  return cleaned
    .split(/(\r\n|\n|\r)/)
    .reduce((parts: string[], part, index, source) => {
      if (part === "\r\n" || part === "\n" || part === "\r") {
        return parts;
      }
      const newline = source[index + 1] === "\r\n" || source[index + 1] === "\n" || source[index + 1] === "\r" ? source[index + 1] : "";
      if (tagLinePattern.test(part) || paramLinePattern.test(part)) {
        return parts;
      }
      parts.push(`${part}${newline}`);
      return parts;
    }, [])
    .join("");
}

function looksLikeToolXmlFragment(text: string, tagLinePattern: RegExp, paramLinePattern: RegExp): boolean {
  return text
    .split(/\r?\n/)
    .some((line) => tagLinePattern.test(line) || paramLinePattern.test(line));
}

function normalizeHistoryRuns(items: unknown, sessionId: string): AgentExecutionRun[] {
  if (!Array.isArray(items)) {
    return [];
  }
  return items
    .map((item) => normalizeHistoryRun(item, sessionId))
    .filter((item): item is AgentExecutionRun => item !== null)
    .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt));
}

function normalizeHistoryRun(value: unknown, fallbackSessionId: string): AgentExecutionRun | null {
  const record = toRecord(value);
  if (!record) {
    return null;
  }
  const traceId = asString(record.traceId) || createTraceId();
  const prompt = asString(record.prompt) || "";
  const reply = asString(record.reply) || "";
  const events = normalizeTraceEvents(record.events);
  const sessionId = asString(record.sessionId) || fallbackSessionId;
  const status = normalizeRunStatus(record.status, asString(record.errorMessage) || "");
  const tasks = finalizeTaskStatuses(
    normalizeTaskPlan(record.tasks, traceId, deriveTasksFromEvents(events, traceId)),
    status
  );
  const changeLedger = normalizeHistoryChangeLedger(record.changeLedger, events, traceId, sessionId);
  const createdAt = asString(record.createdAt) || new Date().toISOString();
  const updatedAt = asString(record.updatedAt) || createdAt;
  return {
    traceId,
    sessionId,
    prompt,
    route: asString(record.route) || "coomi",
    agentMode: asString(record.agentMode) || "coomi",
    llmModel: asString(record.llmModel) || "",
    llmProvider: asString(record.llmProvider) || "",
    status,
    noRestorePoint:
      asBoolean(record.noRestorePoint) ??
      asBoolean(toRecord(record.execution)?.noRestorePoint) ??
      false,
    createdAt,
    updatedAt,
    lastAction: "chat",
    reply,
    trace: normalizeTrace(record.trace),
    audit: normalizeAudit(record.audit),
    events,
    tasks,
    changeLedger,
    items: buildHistoryWaterfallItems(traceId, prompt, reply, events),
    errorMessage: asString(record.errorMessage) || "",
    errorCode: asString(record.errorCode)
  };
}

function buildHistoryWaterfallItems(
  traceId: string,
  prompt: string,
  reply: string,
  events: AgentTraceEvent[]
): CoomiWaterfallItem[] {
  const items: CoomiWaterfallItem[] = [];
  if (prompt) {
    items.push(createWaterfallItem({ id: `${traceId}-user`, type: "user", status: "success", title: "User", content: prompt }));
  }
  for (const event of events) {
    const packet = event.data ? ({ type: event.event, ...event.data } as AgentStreamPacket) : ({ type: event.event } as AgentStreamPacket);
    const item = streamPacketToWaterfallItem(traceId, packet, items);
    if (item) {
      items.splice(0, items.length, ...mergeWaterfallItem(items, item));
    }
  }
  if (reply && !items.some((item) => item.type === "assistant")) {
    items.push(createWaterfallItem({ id: `${traceId}-assistant`, type: "assistant", status: "success", title: "Assistant", content: reply }));
  }
  return items;
}

function normalizeCoomiStatus(value: unknown): AgentCoomiStatusResponse | null {
  const record = toRecord(value);
  if (!record) {
    return null;
  }
  return {
    runtime: asString(record.runtime) || "coomi",
    installed: asBoolean(record.installed) ?? false,
    home: asString(record.home) || "",
    configPath: asString(record.configPath) || "",
    sessionsPath: asString(record.sessionsPath) || "",
    providerId: asString(record.providerId) || "",
    providerType: asString(record.providerType) || "",
    model: asString(record.model) || "",
    display: asString(record.display) || "",
    permissionMode: asString(record.permissionMode) || "",
    permissionLabel: asString(record.permissionLabel) || "",
    planMode: asBoolean(record.planMode) ?? false,
    toolCount: asNumber(record.toolCount) || 0,
    contextWindow: firstNumber(record, ["contextWindow", "context_window"]) ?? undefined,
    usedTokens: firstNumber(record, ["usedTokens", "used_tokens"]) ?? undefined,
    usageRatio: firstNumber(record, ["usageRatio", "usage_ratio"]) ?? undefined,
    cumulativeTokens: firstNumber(record, ["cumulativeTokens", "cumulative_tokens"]) ?? undefined,
    compactThreshold: firstNumber(record, ["compactThreshold", "compact_threshold"]) ?? undefined,
    warningThreshold: firstNumber(record, ["warningThreshold", "warning_threshold"]) ?? undefined,
    compressionStatus: firstString(record, ["compressionStatus", "compression_status"]) || undefined
  };
}

function normalizeSessionSummaries(items: unknown): AgentSessionSummary[] {
  if (!Array.isArray(items)) {
    return [];
  }
  return items
    .map((item) => {
      const record = toRecord(item);
      const sessionId = asString(record?.sessionId);
      if (!sessionId) {
        return null;
      }
      const updatedAt = asString(record?.updatedAt) || asString(record?.createdAt) || new Date().toISOString();
      return {
        sessionId,
        firstPrompt: asString(record?.firstPrompt) || "",
        createdAt: asString(record?.createdAt) || updatedAt,
        updatedAt,
        traceCount: asNumber(record?.traceCount) || 0
      };
    })
    .filter((item): item is AgentSessionSummary => item !== null)
    .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt));
}

function normalizeStoryChapterTemplates(items: unknown): StoryChapterTemplate[] {
  if (!Array.isArray(items)) {
    return [];
  }
  return items
    .map((item) => {
      const record = toRecord(item);
      const id = asString(record?.id)?.trim() || "";
      if (!id) {
        return null;
      }
      return {
        id,
        name: asString(record?.name)?.trim() || id,
        relativePath: asString(record?.relativePath)?.trim() || "",
        description: asString(record?.description)?.trim() || "",
        chapterMode: asString(record?.chapterMode)?.trim() || "directory",
        contentMode: asString(record?.contentMode)?.trim() || "multi_fragment",
        chapterNamePattern: asString(record?.chapterNamePattern)?.trim() || "",
        segmentNaming: asString(record?.segmentNaming)?.trim() || "001.md"
      };
    })
    .filter((item): item is StoryChapterTemplate => item !== null);
}

function normalizeStoryChapterTemplateError(error: unknown): string {
  const message = describeTransportError(error, "章节模板暂时无法读取。");
  if (/request failed with status code 404|404|not found/i.test(message)) {
    return "";
  }
  return message || "章节模板暂时无法读取。";
}

function isStoryChapterTemplateNotFoundError(error: unknown): boolean {
  const record = error as { response?: { status?: number }; status?: number; code?: string; message?: string };
  if (record?.response?.status === 404 || record?.status === 404) {
    return true;
  }
  if (String(record?.code || "").toLowerCase().includes("not_found")) {
    return true;
  }
  return /request failed with status code 404|404|not found/i.test(String(record?.message || ""));
}

function createEmptyChangeLedger(traceId: string, sessionId: string): AgentRunChangeLedger {
  return {
    traceId,
    sessionId,
    changedFiles: [],
    changedFileCount: 0,
    added: 0,
    removed: 0,
    diffSource: "",
    commitHash: "",
    shortHash: "",
    updatedAt: ""
  };
}

function finalizeTaskStatuses(tasks: AgentTaskItem[], status: AgentRunStatus): AgentTaskItem[] {
  const visibleTasks = sanitizeTaskList(tasks);
  if (!visibleTasks.length) {
    return visibleTasks;
  }
  const now = new Date().toISOString();
  return visibleTasks.map((task) => {
    if (task.status === "completed" || task.status === "failed" || task.status === "skipped") {
      return task;
    }
    if (status === "completed" || status === "committed") {
      return { ...task, status: "completed", updatedAt: now };
    }
    if (status === "failed") {
      return { ...task, status: task.status === "running" ? "failed" : "skipped", updatedAt: now };
    }
    if (status === "cancelled" || status === "stopped") {
      return { ...task, status: "skipped", updatedAt: now };
    }
    return task;
  });
}

function normalizeTaskPlan(value: unknown, traceId: string, fallback: AgentTaskItem[] = []): AgentTaskItem[] {
  if (!Array.isArray(value)) {
    return sanitizeTaskList(fallback);
  }
  const now = new Date().toISOString();
  const tasks = value
    .map((item, index) => {
      const record = toRecord(item);
      if (!record) {
        return null;
      }
      const taskId = asString(record.taskId) || asString(record.id) || `${traceId}-task-${index + 1}`;
      const title = (asString(record.title) || "").trim();
      if (!title || isGenericTaskTitle(title)) {
        return null;
      }
      return {
        taskId,
        traceId: asString(record.traceId) || traceId,
        order: Math.max(1, asNumber(record.order) || index + 1),
        title,
        detail: (asString(record.detail) || "").trim(),
        status: normalizeTaskStatus(record.status),
        createdAt: asString(record.createdAt) || now,
        updatedAt: asString(record.updatedAt) || asString(record.createdAt) || now
      };
    })
    .filter((item): item is AgentTaskItem => item !== null)
    .slice(0, 10);
  return sanitizeTaskList(tasks);
}

function upsertTaskEvent(
  tasks: AgentTaskItem[],
  packet: AgentStreamPacket,
  traceId: string,
  sessionId: string,
  eventName: string
): AgentTaskItem[] {
  void sessionId;
  const now = new Date().toISOString();
  const taskId = String(packet.taskId || "").trim() || `${traceId}-task-${Number(packet.order || tasks.length + 1)}`;
  const existing = tasks.find((item) => item.taskId === taskId);
  const order = Math.max(1, Number(packet.order || existing?.order || tasks.length + 1));
  const nextTask: AgentTaskItem = {
    taskId,
    traceId: String(packet.traceId || traceId),
    order,
    title: String(packet.title || existing?.title || `任务 ${order}`).trim(),
    detail: String(packet.detail || existing?.detail || "").trim(),
    status: statusForTaskEvent(eventName, packet.status),
    createdAt: existing?.createdAt || now,
    updatedAt: String(packet.updatedAt || packet.createdAt || now)
  };
  if (isGenericTaskTitle(nextTask.title)) {
    return sanitizeTaskList(tasks.filter((item) => item.taskId !== taskId));
  }
  return sanitizeTaskList([...tasks.filter((item) => item.taskId !== taskId), nextTask]);
}

function deriveTasksFromEvents(events: AgentTraceEvent[], traceId: string): AgentTaskItem[] {
  let tasks: AgentTaskItem[] = [];
  for (const event of events) {
    const packet = event.data
      ? ({ type: event.event, ...event.data } as AgentStreamPacket)
      : ({ type: event.event } as AgentStreamPacket);
    if (event.event === "TaskPlanCreated" || event.event === "TaskPlanUpdated") {
      tasks = normalizeTaskPlan(packet.tasks, traceId, tasks);
    } else if (
      event.event === "TaskStarted" ||
      event.event === "TaskCompleted" ||
      event.event === "TaskFailed" ||
      event.event === "TaskSkipped"
    ) {
      tasks = upsertTaskEvent(tasks, packet, traceId, "", event.event);
    }
  }
  return tasks;
}

function sanitizeTaskList(tasks: AgentTaskItem[]): AgentTaskItem[] {
  return tasks
    .filter((task) => !isGenericTaskTitle(task.title))
    .sort((left, right) => left.order - right.order)
    .slice(0, 10);
}

function isGenericTaskTitle(title: string): boolean {
  const compact = String(title || "")
    .toLowerCase()
    .replace(/[\s:：，。,.;；、\-_/?？!！]+/g, "");
  if (!compact) {
    return true;
  }
  const exactGenericTitles = new Set([
    "分析需求",
    "执行任务",
    "完成回复",
    "确认需求",
    "处理请求",
    "任务执行",
    "analysis",
    "analyzerequest",
    "executetask",
    "finishreply"
  ]);
  if (exactGenericTitles.has(compact)) {
    return true;
  }
  const genericTokenGroups = [
    ["确认", "目标", "影响", "范围"],
    ["执行", "本轮", "请求"],
    ["检查", "结果", "文件", "状态"],
    ["执行", "修改", "检查", "结果"],
    ["检查", "记录", "本轮", "版本"]
  ];
  return genericTokenGroups.some((group) => group.every((token) => compact.includes(token)));
}

function normalizeTaskStatus(value: unknown): AgentTaskStatus {
  if (value === "pending" || value === "running" || value === "completed" || value === "failed" || value === "skipped") {
    return value;
  }
  if (value === "success") {
    return "completed";
  }
  if (value === "error") {
    return "failed";
  }
  return "pending";
}

function statusForTaskEvent(eventName: string, status: unknown): AgentTaskStatus {
  const normalized = normalizeTaskStatus(status);
  if (normalized !== "pending") {
    return normalized;
  }
  if (eventName === "TaskStarted") return "running";
  if (eventName === "TaskCompleted") return "completed";
  if (eventName === "TaskFailed") return "failed";
  if (eventName === "TaskSkipped") return "skipped";
  return "pending";
}

function normalizeChangeLedger(
  packet: AgentStreamPacket,
  traceId: string,
  sessionId: string,
  fallback?: AgentRunChangeLedger
): AgentRunChangeLedger {
  const changedFiles = Array.isArray(packet.changedFiles)
    ? packet.changedFiles.map((item) => String(item || "").replace(/\\/g, "/").trim()).filter(Boolean)
    : fallback?.changedFiles || [];
  const rawCount = Number(packet.changedFileCount ?? changedFiles.length ?? fallback?.changedFileCount ?? 0);
  const commitHash = String(packet.commitHash || fallback?.commitHash || "").trim();
  const source = String(
    packet.diffSource || fallback?.diffSource || (commitHash ? "commit" : rawCount > 0 ? "working_tree" : "")
  ).trim();
  return {
    traceId: String(packet.traceId || traceId),
    sessionId: String(packet.session_id || packet.sessionId || sessionId || fallback?.sessionId || ""),
    changedFiles,
    changedFileCount: Math.max(0, Number.isFinite(rawCount) ? Math.round(rawCount) : changedFiles.length),
    added: Math.max(0, Math.round(Number(packet.added ?? fallback?.added ?? 0) || 0)),
    removed: Math.max(0, Math.round(Number(packet.removed ?? fallback?.removed ?? 0) || 0)),
    diffSource: source === "commit" || source === "working_tree" ? source : "",
    commitHash,
    shortHash: String(packet.shortHash || fallback?.shortHash || "").trim(),
    updatedAt: String(packet.updatedAt || packet.createdAt || fallback?.updatedAt || new Date().toISOString())
  };
}

function normalizeCommitPrompt(
  packet: AgentStreamPacket,
  traceId: string,
  sessionId: string
): AgentPendingCommitPrompt {
  const changedFiles = Array.isArray(packet.changedFiles)
    ? packet.changedFiles.map((item) => String(item || "").replace(/\\/g, "/").trim()).filter(Boolean)
    : [];
  const rawCount = Number(packet.changedFileCount ?? changedFiles.length);
  return {
    traceId: String(packet.traceId || traceId),
    sessionId: String(packet.session_id || packet.sessionId || sessionId || ""),
    workspaceRoot: String(packet.workspaceRoot || ""),
    message: String(packet.message || ""),
    changedFiles,
    changedFileCount: Math.max(0, Number.isFinite(rawCount) ? Math.round(rawCount) : changedFiles.length),
    added: Math.max(0, Math.round(Number(packet.added ?? 0) || 0)),
    removed: Math.max(0, Math.round(Number(packet.removed ?? 0) || 0))
  };
}

function buildCommitDecisionPacket(prompt: AgentPendingCommitPrompt, data: AgentStreamPacket): AgentStreamPacket {
  return {
    ...data,
    type: data.type || data._type || "GitCommitResult",
    _type: data._type || data.type || "GitCommitResult",
    traceId: prompt.traceId,
    sessionId: prompt.sessionId
  };
}

function fallbackCommitMessage(prompt: AgentPendingCommitPrompt): string {
  const count = Math.max(0, Math.round(Number(prompt.changedFileCount || prompt.changedFiles.length || 0)));
  return count > 0 ? `agent: update project files (${count} files)` : "agent: update project files";
}

function shouldRetryCommitWithFallbackMessage(error: unknown): boolean {
  if (error instanceof AgentApiError && error.code === "commit_message_generation_failed") {
    return true;
  }

  const root = asRecord(error);
  const response = asRecord(root.response);
  const responseData = asRecord(response.data);
  const envelopeError = asRecord(responseData.error);
  const details = asRecord(envelopeError.details);
  const status = Number(response.status || 0);
  const code = String(envelopeError.code || "");
  const message = `${String(envelopeError.message || "")} ${String(details.message || "")} ${String(root.message || "")}`;
  return code === "commit_message_generation_failed" || (status === 502 && /commit message/i.test(message));
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

function mergeChangeLedgerPaths(
  fallback: AgentRunChangeLedger | null | undefined,
  paths: string[],
  traceId: string,
  sessionId: string
): AgentRunChangeLedger {
  const changedFiles = uniqueStrings([...(fallback?.changedFiles || []), ...paths]);
  const commitHash = String(fallback?.commitHash || "").trim();
  return {
    traceId,
    sessionId: sessionId || fallback?.sessionId || "",
    changedFiles,
    changedFileCount: changedFiles.length,
    added: Math.max(0, Math.round(Number(fallback?.added || 0) || 0)),
    removed: Math.max(0, Math.round(Number(fallback?.removed || 0) || 0)),
    diffSource: commitHash ? "commit" : changedFiles.length > 0 ? "working_tree" : fallback?.diffSource || "",
    commitHash,
    shortHash: String(fallback?.shortHash || "").trim(),
    updatedAt: new Date().toISOString()
  };
}

function scheduleGitSummaryRefresh(): void {
  if (gitSummaryRefreshTimer !== null) {
    window.clearTimeout(gitSummaryRefreshTimer);
  }
  gitSummaryRefreshTimer = window.setTimeout(() => {
    gitSummaryRefreshTimer = null;
    void useGitStore().refreshSummary({ silent: true });
  }, 350);
}

function isWriteLikeToolPacket(packet: AgentStreamPacket): boolean {
  const toolName = String(packet.tool_name || "").replace(/[_\s-]+/g, "").toLowerCase();
  if (!toolName) {
    return false;
  }
  if (toolName.includes("versionstatus") || toolName.includes("runtimepresetstatus")) {
    return false;
  }
  return (
    toolName.includes("write") ||
    toolName.includes("edit") ||
    toolName.includes("patch") ||
    toolName.includes("save") ||
    toolName.includes("create") ||
    toolName.includes("delete") ||
    toolName.includes("move") ||
    toolName.includes("rename") ||
    toolName.includes("mkdir") ||
    toolName.includes("applystoryincrement") ||
    toolName.includes("syncwiki")
  );
}

function extractChangedPathsFromToolPacket(
  packet: AgentStreamPacket,
  run: AgentExecutionRun,
  workspaceRoot: string
): string[] {
  const candidates: string[] = [];
  if (Array.isArray(packet.changedFiles)) {
    candidates.push(...packet.changedFiles.map((item) => String(item || "")));
  }
  const packetArguments = toRecord(packet.arguments);
  if (packetArguments) {
    collectPathCandidates(packetArguments, candidates);
  } else {
    const previousArguments = findToolArgumentsForPacket(run, packet);
    if (previousArguments) {
      collectPathCandidates(previousArguments, candidates);
    }
  }
  candidates.push(...extractPathsFromPreview(String(packet.result_preview || ""), workspaceRoot));
  return uniqueStrings(candidates.map((path) => normalizeChangedPath(path, workspaceRoot)).filter(Boolean));
}

function findToolArgumentsForPacket(run: AgentExecutionRun, packet: AgentStreamPacket): Record<string, unknown> | null {
  const toolCallId = String(packet.tool_call_id || "").trim();
  const toolName = String(packet.tool_name || "").trim();
  const item = [...run.items].reverse().find((candidate) => {
    if (!candidate.arguments) {
      return false;
    }
    if (toolCallId && candidate.toolCallId === toolCallId) {
      return true;
    }
    return Boolean(toolName && candidate.toolName === toolName);
  });
  return toRecord(item?.arguments);
}

function collectPathCandidates(value: unknown, output: string[], key = "", depth = 0): void {
  if (depth > 6 || value === null || value === undefined) {
    return;
  }
  if (typeof value === "string") {
    if (isPathLikeKey(key) || looksLikePathText(value)) {
      output.push(value);
    }
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      collectPathCandidates(item, output, key, depth + 1);
    }
    return;
  }
  const record = toRecord(value);
  if (!record) {
    return;
  }
  for (const [childKey, childValue] of Object.entries(record)) {
    collectPathCandidates(childValue, output, childKey, depth + 1);
  }
}

function isPathLikeKey(key: string): boolean {
  const normalized = key.replace(/[_\s-]+/g, "").toLowerCase();
  return (
    normalized === "path" ||
    normalized.endsWith("path") ||
    normalized.endsWith("paths") ||
    normalized === "file" ||
    normalized === "files" ||
    normalized === "filename" ||
    normalized === "filepath" ||
    normalized === "relativepath" ||
    normalized === "target" ||
    normalized === "targetfile" ||
    normalized === "segmentpath" ||
    normalized === "sourcepath" ||
    normalized === "outputpath"
  );
}

function looksLikePathText(value: string): boolean {
  const text = String(value || "").trim();
  if (!text || text.length > 500 || /[\r\n{}]/.test(text)) {
    return false;
  }
  const normalized = text.replace(/\\/g, "/").replace(/^['"`]+|['"`]+$/g, "");
  return (
    /^[A-Za-z]:\//.test(normalized) ||
    normalized.startsWith("./") ||
    normalized.startsWith(".storydex/") ||
    /^[^/]+\/.+\.[A-Za-z0-9]{1,8}$/.test(normalized) ||
    /\.(md|markdown|json|jsonl|txt|yml|yaml|csv|toml)$/i.test(normalized)
  );
}

function extractPathsFromPreview(text: string, workspaceRoot: string): string[] {
  const candidates: string[] = [];
  const trimmed = text.trim();
  if (!trimmed) {
    return candidates;
  }
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      collectPathCandidates(JSON.parse(trimmed), candidates);
    } catch {
      // Tool previews are often clipped; line-based extraction below still handles the common cases.
    }
  }
  for (const line of text.split(/\r?\n/)) {
    const match = line.match(
      /(?:File written to|Wrote file|Updated file|Created file|Modified file|Deleted file|写入(?:文件)?|更新(?:文件)?)[：:\s]+(.+)$/i
    );
    if (match?.[1]) {
      candidates.push(match[1]);
    }
  }
  const absoluteMatches = text.match(/[A-Za-z]:[\\/][^\r\n"'<>|]+/g) || [];
  candidates.push(...absoluteMatches);
  if (workspaceRoot.trim()) {
    const normalizedRoot = workspaceRoot.replace(/\\/g, "/").replace(/\/+$/, "");
    const rootPattern = new RegExp(`${escapeRegExp(normalizedRoot)}[/\\\\][^\\r\\n"'<>|]+`, "gi");
    candidates.push(...(text.match(rootPattern) || []));
  }
  return candidates;
}

function normalizeChangedPath(value: string, workspaceRoot: string): string {
  let text = String(value || "")
    .replace(/\0/g, "")
    .replace(/\\/g, "/")
    .trim()
    .replace(/^['"`]+|['"`]+$/g, "");
  text = text
    .replace(/^(?:File written to|Wrote file|Updated file|Created file|Modified file|Deleted file)\s+/i, "")
    .replace(/\s+\((?:\d+|[\d.]+)\s*(?:bytes|chars|characters|字节|字符).*?\)$/i, "")
    .replace(/[。；;，,]+$/g, "")
    .trim();
  const filePathMatch = text.match(/^(.+\.(?:md|markdown|json|jsonl|txt|yml|yaml|csv|toml))(?:\s+.*)?$/i);
  if (filePathMatch?.[1]) {
    text = filePathMatch[1].trim();
  }
  if (!text || text.length > 500 || /[\r\n{}]/.test(text)) {
    return "";
  }

  const normalizedRoot = workspaceRoot.replace(/\\/g, "/").replace(/\/+$/, "");
  if (/^[A-Za-z]:\//.test(text)) {
    if (!normalizedRoot) {
      return "";
    }
    const rootPrefix = `${normalizedRoot}/`.toLowerCase();
    const lower = `${text}/`.toLowerCase();
    if (!lower.startsWith(rootPrefix)) {
      return "";
    }
    text = text.slice(normalizedRoot.length).replace(/^\/+/, "");
  }

  text = text.replace(/^\.\/+/, "").replace(/^\/+|\/+$/g, "");
  if (!text || text === "." || text.split("/").includes("..")) {
    return "";
  }
  if (!looksLikePathText(text)) {
    return "";
  }
  return text;
}

function uniqueStrings(values: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    const normalized = String(value || "").replace(/\\/g, "/").trim();
    if (!normalized || seen.has(normalized)) {
      continue;
    }
    seen.add(normalized);
    result.push(normalized);
  }
  return result;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function normalizeHistoryChangeLedger(
  value: unknown,
  events: AgentTraceEvent[],
  traceId: string,
  sessionId: string
): AgentRunChangeLedger {
  const record = toRecord(value);
  let ledger = createEmptyChangeLedger(traceId, sessionId);
  if (record) {
    ledger = normalizeChangeLedger(record as unknown as AgentStreamPacket, traceId, sessionId, ledger);
  }
  for (const event of events) {
    if (event.event !== "GitAutoCommit") {
      continue;
    }
    const packet = event.data
      ? ({ type: event.event, ...event.data } as AgentStreamPacket)
      : ({ type: event.event } as AgentStreamPacket);
    ledger = normalizeChangeLedger(packet, traceId, sessionId, ledger);
  }
  return ledger;
}

function normalizeTraceEvents(value: unknown): AgentTraceEvent[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const events: AgentTraceEvent[] = [];
  value.forEach((item, index) => {
      const record = toRecord(item);
      if (!record) {
        return;
      }
      events.push({
        index: asNumber(record.index) || index + 1,
        event: asString(record.event) || "event",
        phase: asString(record.phase) || "runtime",
        status: asString(record.status) || "info",
        detail: asString(record.detail) || "",
        timestamp: asString(record.timestamp) || "",
        data: toRecord(record.data) || {}
      });
    });
  return events;
}

function normalizeTrace(value: unknown): ApiTrace | null {
  const record = toRecord(value);
  if (!record) {
    return null;
  }
  return {
    traceId: asString(record.traceId) || "",
    durationMs: asNumber(record.durationMs) || 0,
    toolCalls: asNumber(record.toolCalls) || 0,
    llmCalls: asNumber(record.llmCalls) || 0,
    promptTokens: asNumber(record.promptTokens) || 0,
    completionTokens: asNumber(record.completionTokens) || 0,
    estimatedCost: asNumber(record.estimatedCost) || 0,
    cacheReadInputTokens: asNumber(record.cacheReadInputTokens) || 0,
    cacheCreationInputTokens: asNumber(record.cacheCreationInputTokens) || 0,
    cacheHitRatio: asNumber(record.cacheHitRatio) || 0,
    cacheSavings: asNumber(record.cacheSavings) || 0
  };
}

function normalizeAudit(value: unknown): ApiAuditRecord[] {
  if (Array.isArray(value)) {
    return value.filter((item): item is ApiAuditRecord => Boolean(toRecord(item)));
  }
  const record = toRecord(value);
  return record ? [record as ApiAuditRecord] : [];
}

function normalizeRunStatus(value: unknown, errorMessage: string): AgentRunStatus {
  if (errorMessage) return "failed";
  if (
    value === "completed" ||
    value === "committed" ||
    value === "discarded" ||
    value === "preview" ||
    value === "failed" ||
    value === "cancelled" ||
    value === "stopped" ||
    value === "superseded" ||
    value === "running"
  ) {
    return value;
  }
  return "completed";
}

function normalizeAgentError(error: unknown): {
  message: string;
  code: string | null;
  details?: Record<string, unknown>;
} {
  if (error instanceof AgentApiError) {
    return { message: error.message, code: error.code ?? null, details: error.details };
  }
  return { message: describeTransportError(error, "Coomi execution failed."), code: null, details: undefined };
}

function normalizePendingApproval(packet: AgentStreamPacket): AgentPendingApproval | null {
  const approvalId = String(packet.approvalId || packet.approval_id || "").trim();
  if (!approvalId) {
    return null;
  }
  const kind = asString((packet as unknown as Record<string, unknown>).kind) || undefined;
  const rawOptions = Array.isArray(packet.options) ? packet.options : [];
  const options: AgentPendingApproval["options"] = [];
  for (const option of rawOptions) {
    const record = toRecord(option);
    const label = asString(record?.label) || "";
    const value = asString(record?.value) || label;
    if (!value.trim() && !label.trim()) {
      continue;
    }
    options.push({
      label: label || value,
      value,
      description: asString(record?.description) || "",
      isRecommended: asBoolean(record?.isRecommended) ?? asBoolean(record?.is_recommended) ?? false
    });
  }
  const packetRecord = packet as unknown as Record<string, unknown>;
  const questionIndex = firstNumber(packetRecord, ["questionIndex", "question_index"]);
  const questionTotal = firstNumber(packetRecord, ["questionTotal", "question_total"]);
  return {
    approvalId,
    kind,
    header: String(packet.header || "权限确认"),
    question: String(packet.question || "允许 Coomi 执行这个操作吗？"),
    options: options.length
      ? options
      : kind === "question"
        ? [{ label: "回复", value: "answer", description: "输入回复后确认。", isRecommended: true }]
        : [
          { label: "允许", value: "allow", description: "仅批准本次工具调用。", isRecommended: true },
          { label: "拒绝", value: "deny", description: "将拒绝结果返回给 Coomi。" }
        ],
    allowText: asBoolean((packet as unknown as Record<string, unknown>).allowText) ?? false,
    multiSelect: asBoolean((packet as unknown as Record<string, unknown>).multiSelect) ?? false,
    questionIndex: questionIndex ?? undefined,
    questionTotal: questionTotal ?? undefined
  };
}

function stringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value ?? "");
  }
}

function summarizeUsagePacket(packet: AgentStreamPacket): string {
  const usage = toRecord(packet.usage) || {};
  const prompt = firstNumber(usage, ["prompt_tokens", "promptTokens"]) ?? 0;
  const completion = firstNumber(usage, ["completion_tokens", "completionTokens"]) ?? 0;
  const total = firstNumber(usage, ["total_tokens", "totalTokens"]) ?? prompt + completion;
  const used = firstNumber(packet as unknown as Record<string, unknown>, ["usedTokens", "used_tokens", "estimated_tokens"]) ?? prompt;
  const windowSize = firstNumber(packet as unknown as Record<string, unknown>, ["contextWindow", "context_window"]);
  const ratio =
    firstNumber(packet as unknown as Record<string, unknown>, ["usageRatio", "usage_ratio"]) ??
    (windowSize && windowSize > 0 ? used / windowSize : null);
  const parts = [`tokens ${formatTokenCount(total)}`];
  if (prompt || completion) {
    parts.push(`prompt ${formatTokenCount(prompt)}`, `completion ${formatTokenCount(completion)}`);
  }
  if (ratio !== null && windowSize) {
    parts.push(`ctx ${(ratio * 100).toFixed(1)}% (${formatTokenCount(used)} / ${formatTokenCount(windowSize)})`);
  }
  return parts.join(" | ");
}

function summarizeCompressionPacket(packet: AgentStreamPacket): string {
  const record = packet as unknown as Record<string, unknown>;
  const strategy = firstString(record, ["strategy"]) || "coomi";
  const status = firstString(record, ["compact_status", "compactStatus", "compression_status", "compressionStatus"]) || "completed";
  const estimated = firstNumber(record, ["estimated_tokens", "usedTokens", "used_tokens"]);
  const windowSize = firstNumber(record, ["contextWindow", "context_window"]);
  const ratio =
    firstNumber(record, ["usageRatio", "usage_ratio"]) ??
    (estimated !== null && windowSize && windowSize > 0 ? estimated / windowSize : null);
  const messagesBefore = firstNumber(record, ["original_messages"]);
  const messagesAfter = firstNumber(record, ["compressed_messages"]);
  const pieces = [`strategy ${strategy}`, `status ${status}`];
  if (ratio !== null) {
    pieces.push(`ctx ${(ratio * 100).toFixed(1)}%`);
  }
  if (estimated !== null) {
    pieces.push(`estimated ${formatTokenCount(estimated)}`);
  }
  if (messagesBefore !== null && messagesAfter !== null) {
    pieces.push(`messages ${messagesBefore} -> ${messagesAfter}`);
  }
  if (packet.summary) {
    pieces.push(packet.summary);
  }
  return pieces.join(" | ");
}

function extractCompressionMeta(packet: AgentStreamPacket): Record<string, unknown> {
  const record = packet as unknown as Record<string, unknown>;
  const keys = [
    "strategy",
    "compact_status",
    "compactStatus",
    "usageRatio",
    "usage_ratio",
    "estimated_tokens",
    "usedTokens",
    "used_tokens",
    "contextWindow",
    "context_window",
    "compactThreshold",
    "compact_threshold",
    "warningThreshold",
    "warning_threshold",
    "original_messages",
    "compressed_messages",
    "summary"
  ];
  return keys.reduce<Record<string, unknown>>((result, key) => {
    if (record[key] !== undefined) {
      result[key] = record[key];
    }
    return result;
  }, {});
}

function firstString(record: Record<string, unknown>, keys: string[]): string | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }
  return null;
}

function firstNumber(record: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
    if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) {
      return Number(value);
    }
  }
  return null;
}

function formatTokenCount(value: number): string {
  if (!Number.isFinite(value)) {
    return "unknown";
  }
  const absolute = Math.abs(value);
  if (absolute >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1)}M`;
  }
  if (absolute >= 1_000) {
    return `${(value / 1_000).toFixed(1)}K`;
  }
  return String(Math.round(value));
}

function createTraceId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `trace-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function createFollowupMessageId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `followup-${crypto.randomUUID()}`;
  }
  return `followup-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function createSessionId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `session-${crypto.randomUUID()}`;
  }
  return `session-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function normalizeFollowupPacket(packet: AgentStreamPacket, fallbackSessionId: string): AgentFollowupMessage | null {
  const messageId = String(packet.messageId || "").trim();
  const content = String(packet.content || "").trim();
  const mode = packet.mode === "steer" ? "steer" : "queued";
  const rawStatus = String(packet.status || "pending");
  const status = (
    ["pending", "steering", "dispatching", "sent", "cancelled", "failed"].includes(rawStatus)
      ? rawStatus
      : "pending"
  ) as AgentFollowupMessage["status"];
  if (!messageId || !content) {
    return null;
  }
  const now = new Date().toISOString();
  return {
    messageId,
    sessionId: String(packet.sessionId || fallbackSessionId || "default"),
    activeTraceId: String(packet.activeTraceId || ""),
    expectedTraceId: String(packet.expectedTraceId || ""),
    content,
    mode,
    status,
    statusDetail: String(packet.statusDetail || ""),
    createdAt: String(packet.createdAt || now),
    updatedAt: String(packet.updatedAt || now),
    dispatchTraceId: String(packet.traceId || ""),
    segmentId: String(packet.segmentId || "")
  };
}

function toRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function clampInteger(value: unknown, minimum: number, maximum: number, fallback: number): number {
  const parsed = Number.parseInt(String(value ?? "").trim(), 10);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.max(minimum, Math.min(maximum, parsed));
}

function normalizePositiveInteger(value: unknown, fallback: number): number {
  const parsed = Number.parseInt(String(value ?? "").trim(), 10);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return Math.max(1, parsed);
}

// Deterministic normalization helpers are exposed only to Vitest. Keeping these
// assertions close to the store lets security-sensitive path/session behavior be
// tested directly without changing the production component API.
export const __agentStoreTestUtils = import.meta.env.MODE === "test" ? {
  streamPacketToTraceEvent,
  streamPacketToWaterfallItem,
  segmentItemId,
  createWaterfallItem,
  mergeWaterfallItem,
  phaseForEvent,
  statusForPacket,
  detailForPacket,
  summarizeGitAutoCommitPacket,
  summarizeTurnContractPacket,
  summarizeStoryGenerationValidationPacket,
  summarizePresetCompileFailures,
  summarizeContextAssembly,
  stripDsmlToolText,
  stripTextualToolBlocks,
  looksLikeToolXmlFragment,
  normalizeHistoryRuns,
  normalizeHistoryRun,
  buildHistoryWaterfallItems,
  normalizeCoomiStatus,
  normalizeSessionSummaries,
  normalizeStoryChapterTemplates,
  normalizeStoryChapterTemplateError,
  isStoryChapterTemplateNotFoundError,
  createEmptyChangeLedger,
  finalizeTaskStatuses,
  normalizeTaskPlan,
  upsertTaskEvent,
  deriveTasksFromEvents,
  sanitizeTaskList,
  isGenericTaskTitle,
  normalizeTaskStatus,
  statusForTaskEvent,
  normalizeChangeLedger,
  normalizeCommitPrompt,
  buildCommitDecisionPacket,
  fallbackCommitMessage,
  shouldRetryCommitWithFallbackMessage,
  asRecord,
  mergeChangeLedgerPaths,
  isWriteLikeToolPacket,
  extractChangedPathsFromToolPacket,
  findToolArgumentsForPacket,
  collectPathCandidates,
  isPathLikeKey,
  looksLikePathText,
  extractPathsFromPreview,
  normalizeChangedPath,
  uniqueStrings,
  escapeRegExp,
  normalizeHistoryChangeLedger,
  normalizeTraceEvents,
  normalizeTrace,
  normalizeAudit,
  normalizeRunStatus,
  normalizeAgentError,
  normalizePendingApproval,
  stringify,
  summarizeUsagePacket,
  summarizeCompressionPacket,
  extractCompressionMeta,
  firstString,
  firstNumber,
  formatTokenCount,
  toRecord,
  asString,
  asBoolean,
  asNumber,
  clampInteger
} : null;
