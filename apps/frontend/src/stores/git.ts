import { defineStore } from "pinia";
import { ApiResponseError, describeTransportError } from "@/api/client";
import {
  commitWorkspaceGitChanges,
  fetchWorkspaceGitSummary,
  initializeWorkspaceGitRepository,
  restoreWorkspaceGitCommit
} from "@/api/workspace";
import type { WorkspaceGitSummaryResponse } from "@/types/workspace";

interface GitState {
  summary: WorkspaceGitSummaryResponse | null;
  isLoading: boolean;
  isInitializing: boolean;
  isCommitting: boolean;
  isRestoring: boolean;
  error: string;
  successMessage: string;
}

export const useGitStore = defineStore("git", {
  state: (): GitState => ({
    summary: null,
    isLoading: false,
    isInitializing: false,
    isCommitting: false,
    isRestoring: false,
    error: "",
    successMessage: ""
  }),

  getters: {
    changedCount(state): number {
      return Array.isArray(state.summary?.changedFiles) ? state.summary?.changedFiles.length : 0;
    },

    recentCommits(state) {
      return Array.isArray(state.summary?.recentCommits) ? state.summary?.recentCommits : [];
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
    },

    async refreshSummary(options?: { silent?: boolean }): Promise<void> {
      if (this.isLoading) {
        return;
      }
      this.isLoading = true;
      if (!options?.silent) {
        this.error = "";
      }
      try {
        const result = await fetchWorkspaceGitSummary();
        this.summary = result.data;
        if (!options?.silent) {
          this.error = "";
        }
      } catch (error: unknown) {
        if (!options?.silent) {
          this.error = normalizeGitError(error);
        }
      } finally {
        this.isLoading = false;
      }
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
        this.summary = result.data;
        this.successMessage = "本地仓库已初始化。";
      } catch (error: unknown) {
        this.error = normalizeGitError(error);
      } finally {
        this.isInitializing = false;
      }
    },

    async commitAll(message: string): Promise<void> {
      if (this.isCommitting) {
        return;
      }
      this.isCommitting = true;
      this.error = "";
      this.successMessage = "";
      try {
        const result = await commitWorkspaceGitChanges({ message });
        this.summary = result.data.summary;
        this.successMessage = result.data.created ? "已创建本地提交。" : "当前没有可提交的更改。";
      } catch (error: unknown) {
        this.error = normalizeGitError(error);
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
        this.summary = result.data.summary;
        const restoredSubject = result.data.restoredCommit?.subject || "已恢复到目标版本";
        const backupInfo = result.data.backupRef ? `，已保留备份分支 ${result.data.backupRef}` : "";
        this.successMessage = `${restoredSubject}${backupInfo}`;
      } catch (error: unknown) {
        this.error = normalizeGitError(error);
      } finally {
        this.isRestoring = false;
      }
    }
  }
});

function normalizeGitError(error: unknown): string {
  if (error instanceof ApiResponseError) {
    return error.message;
  }
  return describeTransportError(error, "本地版本控制请求失败，请稍后重试。");
}
