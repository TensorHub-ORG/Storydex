import { defineStore } from "pinia";
import { ApiResponseError, describeTransportError } from "@/api/client";
import {
  commitWorkspaceGitChanges,
  createWorkspaceGitBranch,
  fetchWorkspaceGitBranches,
  fetchWorkspaceGitSummary,
  fetchWorkspaceGitTimeline,
  initializeWorkspaceGitRepository,
  jumpWorkspaceGitCommit,
  restoreWorkspaceGitCommit,
  switchWorkspaceGitBranch
} from "@/api/workspace";
import type {
  WorkspaceGitBranchEntry,
  WorkspaceGitSummaryResponse,
  WorkspaceGitTimelineResponse
} from "@/types/workspace";

interface GitState {
  summary: WorkspaceGitSummaryResponse | null;
  isLoading: boolean;
  isInitializing: boolean;
  isCommitting: boolean;
  isRestoring: boolean;
  error: string;
  successMessage: string;
  /** Wall-clock ms of the last summary that was actually applied to the store. */
  lastSyncedAt: number;
  branches: WorkspaceGitBranchEntry[];
  isBranchBusy: boolean;
  /** 平行时空线：全分支提交树数据。 */
  timeline: WorkspaceGitTimelineResponse | null;
  isTimelineLoading: boolean;
  isJumping: boolean;
}

/**
 * Sequence numbers for summary reads. A read may only write to the store if no
 * newer read or write has been applied since it started, so a slow response can
 * never clobber fresher state. Kept outside Pinia state because this is
 * transport bookkeeping, not UI data.
 */
let summaryRequestSeq = 0;
let appliedSummarySeq = 0;
/** Set while a read is in flight so concurrent callers can await it. */
let inFlightSummary: Promise<void> | null = null;
/**
 * Sequence of the read that owns `inFlightSummary`. Ownership is tracked
 * separately from `summaryRequestSeq` because a write (commit/init/restore) also
 * bumps the request counter; without this, the owning read would never release
 * the slot and every later refresh would await an already-settled promise and
 * return without fetching — the exact "refresh button does nothing" symptom.
 */
let inFlightSeq = 0;

export const useGitStore = defineStore("git", {
  state: (): GitState => ({
    summary: null,
    isLoading: false,
    isInitializing: false,
    isCommitting: false,
    isRestoring: false,
    error: "",
    successMessage: "",
    lastSyncedAt: 0,
    branches: [],
    isBranchBusy: false,
    timeline: null,
    isTimelineLoading: false,
    isJumping: false
  }),

  getters: {
    changedCount(state): number {
      return Array.isArray(state.summary?.changedFiles) ? state.summary?.changedFiles.length : 0;
    },

    recentCommits(state) {
      return Array.isArray(state.summary?.recentCommits) ? state.summary?.recentCommits : [];
    },

    /** Commits reachable from HEAD — the project's own timeline. */
    branchCommits(state) {
      const commits = Array.isArray(state.summary?.recentCommits) ? state.summary.recentCommits : [];
      return commits.filter((item) => item.onCurrentBranch !== false);
    },

    /** Commits only reachable from restore backup branches. */
    backupCommits(state) {
      const commits = Array.isArray(state.summary?.recentCommits) ? state.summary.recentCommits : [];
      return commits.filter((item) => item.onCurrentBranch === false);
    },

    isBusy(state): boolean {
      return state.isLoading || state.isInitializing || state.isCommitting || state.isRestoring;
    },

    /** 平行时空线节点（按 column 升序，最新节点在前）。 */
    timelineNodes(state) {
      return Array.isArray(state.timeline?.nodes) ? state.timeline.nodes : [];
    },

    timelineBranches(state) {
      return Array.isArray(state.timeline?.branches) ? state.timeline.branches : [];
    },

    isDetached(state): boolean {
      return Boolean(state.timeline?.detached);
    }
  },

  actions: {
    reset(): void {
      this.summary = null;
      this.error = "";
      this.successMessage = "";
      this.isLoading = false;
      this.isInitializing = false;
      this.isCommitting = false;
      this.isRestoring = false;
      this.lastSyncedAt = 0;
      this.branches = [];
      this.isBranchBusy = false;
      this.timeline = null;
      this.isTimelineLoading = false;
      this.isJumping = false;
      // Invalidate any in-flight read so it cannot repopulate the panel with the
      // previous project's summary after the workspace was closed or switched.
      appliedSummarySeq = ++summaryRequestSeq;
      inFlightSummary = null;
      inFlightSeq = 0;
    },

    /**
     * Re-read the repository summary.
     *
     * Concurrent callers share the in-flight request instead of being dropped.
     * The previous implementation returned early while `isLoading` was true,
     * which silently discarded a refresh the user triggered by hand whenever a
     * background poll happened to be running, leaving stale data and no error —
     * the "refresh button does nothing" and "committed but no history" reports.
     * `force` skips sharing so an explicit click always causes a fresh read.
     */
    async refreshSummary(options?: { silent?: boolean; force?: boolean }): Promise<void> {
      const silent = options?.silent ?? false;
      if (!options?.force && inFlightSummary) {
        await inFlightSummary;
        return;
      }

      const seq = ++summaryRequestSeq;
      // Claim the in-flight slot before issuing the request so the release check
      // below is correct regardless of when the request settles.
      inFlightSeq = seq;
      this.isLoading = true;
      if (!silent) {
        this.error = "";
      }

      const run = (async () => {
        try {
          const result = await fetchWorkspaceGitSummary();
          // Drop a response that lost the race to a newer read or a write.
          if (seq < appliedSummarySeq) {
            return;
          }
          appliedSummarySeq = seq;
          this.summary = result.data;
          this.lastSyncedAt = Date.now();
          this.error = "";
        } catch (error: unknown) {
          if (seq < appliedSummarySeq) {
            return;
          }
          // Surface the failure even for background polls: a silently swallowed
          // error is exactly how "committed but no history" presented itself.
          this.error = normalizeGitError(error);
        } finally {
          // Always release the slot this read owns, even if a write bumped the
          // request counter meanwhile, so the next refresh can start cleanly.
          if (inFlightSeq === seq) {
            inFlightSummary = null;
            inFlightSeq = 0;
            this.isLoading = false;
          }
        }
      })();

      inFlightSummary = run;
      await run;
    },

    async initializeRepository(): Promise<void> {
      if (this.isInitializing) {
        return;
      }
      this.isInitializing = true;
      this.error = "";
      this.successMessage = "";
      try {
        const result = await initializeWorkspaceGitRepository();
        this.applySummary(result.data);
        this.successMessage = "本地仓库已初始化。";
      } catch (error: unknown) {
        this.error = normalizeGitError(error);
      } finally {
        this.isInitializing = false;
      }
    },

    async refreshBranches(): Promise<void> {
      try {
        const result = await fetchWorkspaceGitBranches();
        this.branches = result.data.branches || [];
        if (result.data.summary) this.applySummary(result.data.summary);
      } catch (error: unknown) {
        this.error = normalizeGitError(error);
      }
    },

    async createBranch(name: string): Promise<boolean> {
      if (this.isBranchBusy) return false;
      this.isBranchBusy = true;
      this.error = "";
      try {
        const result = await createWorkspaceGitBranch(name, true);
        this.branches = result.data.branches || [];
        if (result.data.summary) this.applySummary(result.data.summary);
        this.successMessage = `已创建并切换到分支 ${name}`;
        return true;
      } catch (error: unknown) { this.error = normalizeGitError(error); return false; }
      finally { this.isBranchBusy = false; }
    },

    async switchBranch(name: string): Promise<boolean> {
      if (this.isBranchBusy) return false;
      this.isBranchBusy = true;
      this.error = "";
      try {
        const result = await switchWorkspaceGitBranch(name);
        this.branches = result.data.branches || [];
        if (result.data.summary) this.applySummary(result.data.summary);
        this.successMessage = `已切换到分支 ${name}`;
        return true;
      } catch (error: unknown) { this.error = normalizeGitError(error); return false; }
      finally { this.isBranchBusy = false; }
    },

    async commitAll(message: string): Promise<boolean> {
      if (this.isCommitting) {
        return false;
      }
      this.isCommitting = true;
      this.error = "";
      this.successMessage = "";
      try {
        const result = await commitWorkspaceGitChanges({ message });
        this.applySummary(result.data.summary);
        if (result.data.created) {
          const shortId = result.data.commit?.shortId || "";
          const worldline = result.data.worldlineBranch;
          if (worldline) {
            // 延迟分叉：在历史节点上首次提交时自动创建了新世界线分支。
            this.successMessage = `已在新世界线 ${worldline} 上创建提交 ${shortId}。`;
            // 分叉后刷新分支列表和时间线，让新世界线立即出现在树状图里。
            void this.refreshBranches();
            void this.refreshTimeline();
          } else {
            this.successMessage = shortId ? `已创建本地提交 ${shortId}。` : "已创建本地提交。";
          }
        } else {
          this.successMessage = "当前没有可提交的更改。";
        }
        return Boolean(result.data.created);
      } catch (error: unknown) {
        this.error = normalizeGitError(error);
        return false;
      } finally {
        this.isCommitting = false;
      }
    },

    async restoreToCommit(commitId: string, createBackup = true): Promise<void> {
      if (this.isRestoring) {
        return;
      }
      this.isRestoring = true;
      this.error = "";
      this.successMessage = "";
      try {
        const result = await restoreWorkspaceGitCommit({ commitId, createBackup });
        this.applySummary(result.data.summary);
        const restoredSubject = result.data.restoredCommit?.subject || "已恢复到目标版本";
        const backupInfo = result.data.backupRef ? `，已保留备份分支 ${result.data.backupRef}` : "";
        this.successMessage = `${restoredSubject}${backupInfo}`;
        void this.refreshTimeline();
      } catch (error: unknown) {
        this.error = normalizeGitError(error);
      } finally {
        this.isRestoring = false;
      }
    },

    /**
     * 读取平行时空线数据（全分支提交树）。写入操作（commit/restore/jump）
     * 完成后会自动调用本方法刷新树状图。
     */
    async refreshTimeline(): Promise<void> {
      if (this.isTimelineLoading) {
        return;
      }
      this.isTimelineLoading = true;
      try {
        const result = await fetchWorkspaceGitTimeline();
        this.timeline = result.data;
      } catch (error: unknown) {
        // 时间线加载失败不应阻塞主面板，仅在 error 中记录。
        this.error = normalizeGitError(error);
      } finally {
        this.isTimelineLoading = false;
      }
    },

    /**
     * 跳转到历史提交节点（进入 detached HEAD）。后续在 detached HEAD 状态下
     * 首次提交时，后端会自动创建新世界线分支（延迟分叉）。
     */
    async jumpToCommit(commitId: string): Promise<boolean> {
      if (this.isJumping) {
        return false;
      }
      this.isJumping = true;
      this.error = "";
      this.successMessage = "";
      try {
        const result = await jumpWorkspaceGitCommit({ commitId });
        this.applySummary(result.data.summary);
        const subject = result.data.commit?.subject || "历史节点";
        this.successMessage = `已跳转到节点 ${result.data.commit?.shortId || ""}（${subject}）。在此基础上的提交将创建新世界线。`;
        void this.refreshTimeline();
        return true;
      } catch (error: unknown) {
        this.error = normalizeGitError(error);
        return false;
      } finally {
        this.isJumping = false;
      }
    },

    /**
     * Adopt a summary returned by a write (commit/init/restore). Writes carry the
     * post-operation state, so they win over any read still in flight.
     */
    applySummary(summary: WorkspaceGitSummaryResponse | null | undefined): void {
      if (!summary) {
        return;
      }
      appliedSummarySeq = ++summaryRequestSeq;
      this.summary = summary;
      this.lastSyncedAt = Date.now();
    }
  }
});

function normalizeGitError(error: unknown): string {
  if (error instanceof ApiResponseError) {
    return error.message;
  }
  return describeTransportError(error, "本地版本控制请求失败，请稍后重试。");
}
