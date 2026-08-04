import { defineStore } from "pinia";
import { ApiResponseError, describeTransportError } from "@/api/client";
import {
  commitWorkspaceGitChanges,
  createWorkspaceGitBranch,
  createWorkspaceWorldline,
  deleteWorkspaceWorldline,
  fetchWorkspaceGitBranches,
  fetchWorkspaceGitSummary,
  fetchWorkspaceGitTimeline,
  initializeWorkspaceGitRepository,
  jumpWorkspaceGitCommit,
  renameWorkspaceWorldline,
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
  /** 平行时空线：全世界线的提交树数据。 */
  timeline: WorkspaceGitTimelineResponse | null;
  isTimelineLoading: boolean;
  isJumping: boolean;
  /** 世界线增删改进行中，用于禁用树上的动作。 */
  isWorldlineBusy: boolean;
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
/**
 * Set while a timeline read is in flight so concurrent callers await it instead
 * of being dropped. The tree is refreshed from a poll, from explicit clicks and
 * after every write, so silently discarding overlapping reads left the graph
 * stale with no error to show for it.
 */
let timelineRequestSeq = 0;
let inFlightTimeline: Promise<void> | null = null;
let inFlightTimelineSeq = 0;

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
    isJumping: false,
    isWorldlineBusy: false
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
    },

    /**
     * 当前所在世界线的名字。观测态（detached）下没有世界线可言，返回空串——
     * 调用方必须自己决定怎么措辞，而不是退回到一个并不成立的默认分支名。
     */
    currentWorldline(state): string {
      if (state.timeline?.detached) {
        return "";
      }
      return String(state.timeline?.currentBranch || state.summary?.branch || "");
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
      this.isWorldlineBusy = false;
      // Invalidate any in-flight read so it cannot repopulate the panel with the
      // previous project's summary after the workspace was closed or switched.
      appliedSummarySeq = ++summaryRequestSeq;
      inFlightSummary = null;
      inFlightSeq = 0;
      timelineRequestSeq += 1;
      inFlightTimeline = null;
      inFlightTimelineSeq = 0;
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
        void this.refreshBranches();
        void this.refreshTimeline({ force: true });
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
        void this.refreshTimeline({ force: true });
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
        void this.refreshTimeline({ force: true });
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
            // 分叉后刷新分支列表，让新世界线立即出现在树状图里。
            void this.refreshBranches();
          } else {
            this.successMessage = shortId ? `已创建本地提交 ${shortId}。` : "已创建本地提交。";
          }
          // 普通提交也会新增节点。强制发起提交后的读取，不能复用提交前开始的
          // 后台轮询，否则树会继续显示旧 HEAD。
          void this.refreshTimeline({ force: true });
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
        void this.refreshTimeline({ force: true });
      } catch (error: unknown) {
        this.error = normalizeGitError(error);
      } finally {
        this.isRestoring = false;
      }
    },

    /**
     * 读取平行时空线数据（全世界线的提交树）。写入操作（commit/restore/jump/
     * 世界线增删改）完成后会自动调用本方法刷新树状图。
     *
     * 并发调用共享同一个请求，而不是像以前那样在 `isTimelineLoading` 为真时
     * 直接 return——那个早退会静默丢掉刷新：后台轮询恰好在跑的时候，用户手点
     * 的刷新、以及写操作后的自动刷新都会被吞掉，树停在旧状态且没有任何错误。
     */
    async refreshTimeline(options?: { force?: boolean }): Promise<void> {
      if (!options?.force && inFlightTimeline) {
        await inFlightTimeline;
        return;
      }

      const seq = ++timelineRequestSeq;
      inFlightTimelineSeq = seq;
      this.isTimelineLoading = true;
      const run = (async () => {
        try {
          const result = await fetchWorkspaceGitTimeline();
          // 只允许最新请求落盘。手动刷新、写操作后的强制刷新和项目 reset 都会
          // 推进序号，因此旧项目或写操作之前开始的响应无法覆盖新状态。
          if (seq !== timelineRequestSeq) {
            return;
          }
          this.timeline = result.data;
        } catch (error: unknown) {
          if (seq !== timelineRequestSeq) {
            return;
          }
          // 时间线加载失败不应阻塞主面板，仅在 error 中记录。
          this.error = normalizeGitError(error);
        } finally {
          if (inFlightTimelineSeq === seq) {
            inFlightTimeline = null;
            inFlightTimelineSeq = 0;
            this.isTimelineLoading = false;
          }
        }
      })();
      inFlightTimeline = run;
      await run;
    },

    /**
     * 跳转到某个版本节点，把工作区恢复成该节点的状态。
     *
     * 目标是某条世界线的最新节点时直接切到那条线；目标是中间的历史节点时进入
     * 观测态，此后的首次提交由后端自动开辟新世界线（延迟分叉）。
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
        const shortId = result.data.commit?.shortId || "";
        const subject = result.data.commit?.subject || "该节点";
        const landedBranch = String(result.data.branch || "");
        this.successMessage = landedBranch
          ? `已切换到世界线 ${landedBranch}（${subject}）。`
          : `已跳转到节点 ${shortId}（${subject}）。这是观测态，在此写入会开辟一条新世界线。`;
        void this.refreshBranches();
        void this.refreshTimeline({ force: true });
        return true;
      } catch (error: unknown) {
        this.error = normalizeGitError(error);
        return false;
      } finally {
        this.isJumping = false;
      }
    },

    /** 从任意版本节点开辟一条命名的新世界线并切换过去。 */
    async createWorldline(fromCommit: string, name: string): Promise<boolean> {
      if (this.isWorldlineBusy) return false;
      this.isWorldlineBusy = true;
      this.error = "";
      this.successMessage = "";
      try {
        const result = await createWorkspaceWorldline(fromCommit, name);
        this.branches = result.data.branches || [];
        if (result.data.summary) this.applySummary(result.data.summary);
        this.successMessage = `已开辟新世界线 ${result.data.worldline || name}，现在写入的内容只会留在这条线上。`;
        await this.refreshTimeline({ force: true });
        return true;
      } catch (error: unknown) {
        this.error = normalizeGitError(error);
        return false;
      } finally {
        this.isWorldlineBusy = false;
      }
    },

    async renameWorldline(name: string, newName: string): Promise<boolean> {
      if (this.isWorldlineBusy) return false;
      this.isWorldlineBusy = true;
      this.error = "";
      this.successMessage = "";
      try {
        const result = await renameWorkspaceWorldline(name, newName);
        this.branches = result.data.branches || [];
        if (result.data.summary) this.applySummary(result.data.summary);
        this.successMessage = `世界线已改名为 ${result.data.renamedTo || newName}。`;
        await this.refreshTimeline({ force: true });
        return true;
      } catch (error: unknown) {
        this.error = normalizeGitError(error);
        return false;
      } finally {
        this.isWorldlineBusy = false;
      }
    },

    /**
     * 删除一条世界线。不可逆：Storydex 只分不合，这条线独有的版本会被永久
     * 丢弃，调用方必须先向用户展示会丢多少个版本并取得确认。
     */
    async deleteWorldline(name: string): Promise<boolean> {
      if (this.isWorldlineBusy) return false;
      this.isWorldlineBusy = true;
      this.error = "";
      this.successMessage = "";
      try {
        const result = await deleteWorkspaceWorldline(name);
        this.branches = result.data.branches || [];
        if (result.data.summary) this.applySummary(result.data.summary);
        const lost = Number(result.data.exclusiveCommits || 0);
        this.successMessage = lost > 0
          ? `已删除世界线 ${name}，随之丢弃了它独有的 ${lost} 个版本。`
          : `已删除世界线 ${name}。`;
        await this.refreshTimeline({ force: true });
        return true;
      } catch (error: unknown) {
        this.error = normalizeGitError(error);
        return false;
      } finally {
        this.isWorldlineBusy = false;
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
