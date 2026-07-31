<template>
  <aside class="scm-panel">
    <header class="scm-header">
      <div class="scm-header-copy">
        <h2 class="scm-title">版本控制</h2>
        <p class="scm-project" :title="projectLabel">{{ projectLabel }}</p>
      </div>

      <button
        class="scm-icon-btn"
        type="button"
        :title="refreshTitle"
        aria-label="刷新版本控制状态"
        :disabled="workspaceStore.launchScreenVisible"
        @click="refreshSummary"
      >
        <span class="material-symbols-rounded" :class="{ spinning: gitStore.isLoading }">refresh</span>
      </button>
    </header>

    <div class="scm-body">
      <template v-if="workspaceStore.launchScreenVisible">
        <div class="scm-empty-state">
          <span class="material-symbols-rounded scm-empty-icon">folder_open</span>
          <p class="scm-empty-title">尚未打开项目</p>
          <p class="scm-empty-hint">先打开一个 Storydex 项目，这里会显示它的改动与历史版本。</p>
        </div>
      </template>

      <template v-else-if="summary && !summary.gitInstalled">
        <div class="scm-empty-state is-warning">
          <span class="material-symbols-rounded scm-empty-icon">warning</span>
          <p class="scm-empty-title">版本控制不可用</p>
          <p class="scm-empty-hint">{{ summary.message || "当前环境未安装 Git。" }}</p>
        </div>
      </template>

      <template v-else-if="summary && !summary.initialized">
        <div class="scm-empty-state">
          <span class="material-symbols-rounded scm-empty-icon">account_tree</span>
          <p class="scm-empty-title">还没有启用版本记录</p>
          <p class="scm-empty-hint">
            初始化后，每次改动都可以留下一个可回退的本地版本，不会上传到任何远端。
          </p>
          <button
            class="scm-primary-btn"
            type="button"
            :disabled="gitStore.isInitializing"
            @click="initializeRepository"
          >
            <span class="material-symbols-rounded">play_arrow</span>
            <span>{{ gitStore.isInitializing ? "初始化中…" : "启用版本记录" }}</span>
          </button>
          <form class="scm-empty-branch-form" @submit.prevent="handleCreateBranch">
            <input v-model.trim="newBranchName" placeholder="无提交也可以先创建分支" :disabled="gitStore.isBranchBusy" />
            <button class="scm-primary-btn" type="submit" :disabled="gitStore.isBranchBusy || !newBranchName">创建分支</button>
          </form>
        </div>
        <div v-if="gitStore.error" class="scm-feedback is-error">{{ gitStore.error }}</div>
      </template>

      <template v-else>
        <!-- 当前状态卡：直接回答“我刚才提交成功了吗” -->
        <section class="scm-state-card">
          <div class="scm-state-row">
            <span class="scm-state-label">分支</span>
            <span class="scm-branch-pill" :title="`当前分支：${branchName}`">
              <span class="material-symbols-rounded">fork_right</span>
              <span class="scm-branch-name">{{ branchName }}</span>
            </span>
          </div>
          <div class="scm-state-row">
            <span class="scm-state-label">最新版本</span>
            <span class="scm-state-value" :title="headTitle">
              <template v-if="headCommit">
                <span class="scm-state-subject">{{ headCommit.subject }}</span>
                <code class="scm-hash">{{ headCommit.shortId }}</code>
              </template>
              <span v-else class="scm-state-muted">暂无提交</span>
            </span>
          </div>
          <div class="scm-state-row">
            <span class="scm-state-label">工作区</span>
            <span class="scm-state-value">
              <span class="scm-dot" :class="hasChanges ? 'is-dirty' : 'is-clean'"></span>
              <span>{{ changedCountLabel }}</span>
            </span>
          </div>
        </section>

        <section class="scm-branch-manager">
          <div class="scm-branch-manager-row">
            <label for="scm-branch-select">当前分支</label>
            <select
              id="scm-branch-select"
              :value="branchName"
              :disabled="gitStore.isBranchBusy || hasChanges"
              title="有未提交修改时不能切换分支"
              @change="handleBranchSelect"
            >
              <option v-for="branch in gitStore.branches" :key="branch.name" :value="branch.name">{{ branch.name }}</option>
            </select>
          </div>
          <form class="scm-new-branch" @submit.prevent="handleCreateBranch">
            <input v-model.trim="newBranchName" placeholder="新分支名称，例如 draft/chapter-3" :disabled="gitStore.isBranchBusy" />
            <button class="scm-icon-btn" type="submit" title="新建并切换分支" :disabled="gitStore.isBranchBusy || !newBranchName">
              <span class="material-symbols-rounded">add</span>
            </button>
          </form>
          <p v-if="hasChanges" class="scm-compose-hint">提交当前修改后即可切换分支。</p>
        </section>

        <!-- 提交区 -->
        <section class="scm-compose">
          <label class="scm-compose-label" for="scm-commit-message">本次改动说明</label>
          <textarea
            id="scm-commit-message"
            ref="commitInputRef"
            v-model="commitMessage"
            class="scm-compose-input"
            :placeholder="commitPlaceholder"
            rows="2"
            :disabled="!hasChanges || gitStore.isCommitting"
            @keydown="handleCommitKeydown"
          ></textarea>
          <button
            class="scm-primary-btn is-block"
            type="button"
            :disabled="gitStore.isCommitting || !hasChanges"
            :title="commitButtonTitle"
            @click="commitAllChanges"
          >
            <span class="material-symbols-rounded">check</span>
            <span>{{ commitButtonLabel }}</span>
          </button>
          <p class="scm-compose-hint">留空也可以提交，系统会自动生成带时间的说明。</p>
        </section>

        <div v-if="gitStore.error" class="scm-feedback is-error">
          <span class="material-symbols-rounded">error</span>
          <span>{{ gitStore.error }}</span>
        </div>
        <div v-else-if="gitStore.successMessage" class="scm-feedback is-success">
          <span class="material-symbols-rounded">check_circle</span>
          <span>{{ gitStore.successMessage }}</span>
        </div>

        <div class="scm-split-view">
          <!-- 未提交的更改 -->
          <section class="scm-pane" :class="{ collapsed: !changesExpanded }">
            <header
              class="scm-pane-header"
              role="button"
              tabindex="0"
              :aria-expanded="changesExpanded"
              @click="toggleChanges"
              @keydown.enter.prevent="toggleChanges"
              @keydown.space.prevent="toggleChanges"
            >
              <span class="scm-pane-caret material-symbols-rounded">
                {{ changesExpanded ? "expand_more" : "chevron_right" }}
              </span>
              <span class="scm-pane-title">未提交的更改</span>
              <span class="scm-pane-count" :class="{ 'is-active': changedFiles.length > 0 }">
                {{ changedFiles.length }}
              </span>
            </header>

            <div v-if="changesExpanded" class="scm-pane-body">
              <p v-if="changedFiles.length === 0" class="scm-inline-empty">
                所有改动都已提交，工作区是干净的。
              </p>

              <button
                v-for="item in changedFiles"
                :key="`${item.status}-${item.relativePath}`"
                class="scm-change-row"
                type="button"
                :title="`查看差异：${item.relativePath}`"
                @click="openChangedFile(item.relativePath)"
              >
                <span class="scm-row-icon material-symbols-rounded">{{ fileIconName(item.relativePath) }}</span>
                <span class="scm-row-line">
                  <span class="scm-row-name">{{ fileBaseName(item.relativePath) }}</span>
                  <span class="scm-row-dir">{{ fileDirectory(item.relativePath) }}</span>
                </span>
                <span class="scm-status-chip" :class="statusClassName(item.status)" :title="statusTitle(item.status)">
                  {{ statusTitle(item.status) }}
                </span>
              </button>
            </div>
          </section>

          <!-- 历史版本 -->
          <section class="scm-pane" :class="{ collapsed: !historyExpanded }">
            <header
              class="scm-pane-header"
              role="button"
              tabindex="0"
              :aria-expanded="historyExpanded"
              @click="toggleHistory"
              @keydown.enter.prevent="toggleHistory"
              @keydown.space.prevent="toggleHistory"
            >
              <span class="scm-pane-caret material-symbols-rounded">
                {{ historyExpanded ? "expand_more" : "chevron_right" }}
              </span>
              <span class="scm-pane-title">历史版本</span>
              <span class="scm-pane-count">{{ branchCommits.length }}</span>
            </header>

            <div v-if="historyExpanded" class="scm-pane-body">
              <p v-if="branchCommits.length === 0" class="scm-inline-empty">
                还没有任何版本记录，先写下改动说明并提交一次。
              </p>

              <!-- 行本身只做展示；回退是显式按钮，避免点一下就进入危险操作 -->
              <div
                v-for="(item, index) in branchCommits"
                :key="item.id"
                class="scm-history-row"
                :class="{ current: isCurrentCommit(item.id) }"
                :title="historyRowTitle(item)"
              >
                <span class="scm-graph-lane" :class="{ tail: index === branchCommits.length - 1 }">
                  <span class="scm-graph-node"></span>
                </span>
                <span class="scm-row-line scm-row-line-history">
                  <span class="scm-history-subject">{{ item.subject }}</span>
                  <span class="scm-history-meta">{{ historyMetaText(item) }}</span>
                </span>
                <span v-if="isCurrentCommit(item.id)" class="scm-current-badge">当前</span>
                <button
                  v-else
                  class="scm-restore-btn"
                  type="button"
                  title="回退到此版本（会先自动保留当前状态的备份分支）"
                  aria-label="回退到此版本"
                  :disabled="gitStore.isRestoring"
                  @click="restoreCommit(item.id, item.subject)"
                >
                  <span class="material-symbols-rounded">history</span>
                </button>
              </div>

              <!-- 回退产生的备份提交单独归组，否则它们会按时间混进主线，看起来像凭空多出的记录 -->
              <template v-if="backupCommits.length > 0">
                <div class="scm-subgroup-header">
                  <span class="material-symbols-rounded">inventory_2</span>
                  <span>回退前保留的备份（{{ backupCommits.length }}）</span>
                </div>
                <div
                  v-for="item in backupCommits"
                  :key="item.id"
                  class="scm-history-row is-backup"
                  :title="historyRowTitle(item)"
                >
                  <span class="scm-graph-lane is-backup">
                    <span class="scm-graph-node"></span>
                  </span>
                  <span class="scm-row-line scm-row-line-history">
                    <span class="scm-history-subject">{{ item.subject }}</span>
                    <span class="scm-history-meta">{{ historyMetaText(item) }}</span>
                    <span v-if="historyRefLabel(item)" class="scm-history-ref">{{ historyRefLabel(item) }}</span>
                  </span>
                  <button
                    class="scm-restore-btn"
                    type="button"
                    title="恢复到这个备份版本"
                    aria-label="恢复到这个备份版本"
                    :disabled="gitStore.isRestoring"
                    @click="restoreCommit(item.id, item.subject)"
                  >
                    <span class="material-symbols-rounded">history</span>
                  </button>
                </div>
              </template>
            </div>
          </section>
        </div>

        <footer class="scm-footer" :title="refreshTitle">
          <span class="material-symbols-rounded">{{ gitStore.isLoading ? "sync" : "schedule" }}</span>
          <span>{{ syncLabel }}</span>
        </footer>
      </template>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useGitStore } from "@/stores/git";
import { useWorkspaceStore } from "@/stores/workspace";
import type { WorkspaceGitCommitEntry } from "@/types/workspace";

const gitStore = useGitStore();
const workspaceStore = useWorkspaceStore();

const commitMessage = ref("");
const newBranchName = ref("");
const changesExpanded = ref(true);
const historyExpanded = ref(true);
const commitInputRef = ref<HTMLTextAreaElement | null>(null);
/** Ticks once a second so the "last synced" label stays truthful without a store write. */
const nowTick = ref(Date.now());

/**
 * The panel polls on its own instead of relying on other components. Commits can
 * come from the Agent, the explorer, or an external editor, and previously the
 * only refreshes were driven by ExplorerSidebar's timer — which stops existing
 * the moment the user switches to this panel, so a fresh commit could stay
 * invisible until the project was reopened.
 */
const AUTO_REFRESH_INTERVAL_MS = 5000;
let autoRefreshTimer: number | null = null;
let clockTimer: number | null = null;

const summary = computed(() => gitStore.summary);
const projectLabel = computed(() => workspaceStore.projectLabel || "未打开项目");
const branchName = computed(() => summary.value?.branch || summary.value?.defaultBranch || "develop");
const changedFiles = computed(() => summary.value?.changedFiles || []);
const recentCommits = computed(() => gitStore.recentCommits);
const branchCommits = computed(() => gitStore.branchCommits);
const backupCommits = computed(() => gitStore.backupCommits);
const headCommit = computed(() => summary.value?.head || null);
const headSubject = computed(() => headCommit.value?.subject || "暂无提交");
const hasChanges = computed(() => changedFiles.value.length > 0);
const changedCountLabel = computed(() =>
  gitStore.changedCount > 0 ? `${gitStore.changedCount} 个文件待提交` : "干净，无待提交改动"
);
const commitPlaceholder = computed(() =>
  hasChanges.value ? `例如：修改第三章结尾（Ctrl+Enter 提交到 ${branchName.value}）` : "没有待提交的改动"
);
const commitButtonLabel = computed(() => {
  if (gitStore.isCommitting) {
    return "提交中…";
  }
  return hasChanges.value ? `提交 ${changedFiles.value.length} 个文件` : "没有待提交的改动";
});
const commitButtonTitle = computed(() =>
  hasChanges.value ? `提交全部改动到 ${branchName.value}` : "当前没有待提交的更改"
);
const headTitle = computed(() =>
  headCommit.value
    ? `${headCommit.value.subject}\n${headCommit.value.shortId} · ${headCommit.value.authorName} · ${formatTimestamp(headCommit.value.authoredAt)}`
    : "暂无提交"
);
const syncLabel = computed(() => {
  if (gitStore.isLoading) {
    return "正在读取仓库状态…";
  }
  if (!gitStore.lastSyncedAt) {
    return "尚未同步";
  }
  return `已同步 · ${formatRelative(gitStore.lastSyncedAt, nowTick.value)}`;
});
const refreshTitle = computed(() =>
  gitStore.lastSyncedAt
    ? `刷新（上次同步：${new Date(gitStore.lastSyncedAt).toLocaleTimeString("zh-CN")}）`
    : "刷新"
);

onMounted(() => {
  if (!workspaceStore.launchScreenVisible) {
    void gitStore.refreshSummary({ silent: true });
    void gitStore.refreshBranches();
  }
  autoRefreshTimer = window.setInterval(handleAutoRefresh, AUTO_REFRESH_INTERVAL_MS);
  clockTimer = window.setInterval(() => {
    nowTick.value = Date.now();
  }, 1000);
  window.addEventListener("focus", handleWindowFocus);
  document.addEventListener("visibilitychange", handleWindowFocus);
});

onBeforeUnmount(() => {
  if (autoRefreshTimer !== null) {
    window.clearInterval(autoRefreshTimer);
    autoRefreshTimer = null;
  }
  if (clockTimer !== null) {
    window.clearInterval(clockTimer);
    clockTimer = null;
  }
  window.removeEventListener("focus", handleWindowFocus);
  document.removeEventListener("visibilitychange", handleWindowFocus);
});

watch(
  () => [workspaceStore.projectRootLabel, workspaceStore.treeResetToken],
  () => {
    if (!workspaceStore.launchScreenVisible) {
      void gitStore.refreshSummary({ silent: true });
    }
  }
);

function handleAutoRefresh(): void {
  if (workspaceStore.launchScreenVisible || document.hidden || gitStore.isBusy) {
    return;
  }
  void gitStore.refreshSummary({ silent: true });
}

function handleWindowFocus(): void {
  if (workspaceStore.launchScreenVisible || document.hidden) {
    return;
  }
  void gitStore.refreshSummary({ silent: true });
}

// An explicit click must always hit the backend, even while a background poll is
// running, otherwise the button looks broken.
function refreshSummary(): void {
  void gitStore.refreshSummary({ force: true });
  void gitStore.refreshBranches();
}

function handleBranchSelect(event: Event): void {
  const name = (event.target as HTMLSelectElement).value;
  if (name && name !== branchName.value) void gitStore.switchBranch(name);
}

function handleCreateBranch(): void {
  const name = newBranchName.value.trim();
  if (!name) return;
  void gitStore.createBranch(name).then((created) => {
    if (created) newBranchName.value = "";
  });
}

function initializeRepository(): void {
  void gitStore.initializeRepository();
}

function toggleChanges(): void {
  changesExpanded.value = !changesExpanded.value;
}

function toggleHistory(): void {
  historyExpanded.value = !historyExpanded.value;
}

function handleCommitKeydown(event: KeyboardEvent): void {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    commitAllChanges();
  }
}

function commitAllChanges(): void {
  const message = commitMessage.value.trim();
  void gitStore.commitAll(message).then((created) => {
    if (created) {
      commitMessage.value = "";
    }
    // Re-read after the write so history reflects the new commit even if the
    // commit response raced with an in-flight poll.
    void gitStore.refreshSummary({ silent: true, force: true });
  });
}

function openChangedFile(relativePath: string): void {
  if (!relativePath) {
    return;
  }
  void workspaceStore.openGitReview({ focusPath: relativePath });
}

async function restoreCommit(commitId: string, subject: string): Promise<void> {
  const confirmed = window.confirm(
    `确认回退到这个版本吗？\n\n${subject}\n\n系统会先自动保留当前状态的本地备份分支，然后恢复到所选提交。`
  );
  if (!confirmed) {
    return;
  }

  await gitStore.restoreToCommit(commitId, true);
  if (gitStore.error) {
    return;
  }
  await workspaceStore.reloadProjectContext();
}

function isCurrentCommit(commitId: string): boolean {
  return String(summary.value?.head?.id || "") === String(commitId || "");
}

function formatStatus(status: string): string {
  const compact = String(status || "").trim();
  if (!compact) {
    return "M";
  }
  if (compact === "??") {
    return "U";
  }
  return compact.replace(/\s+/g, "");
}

function statusClassName(status: string): string {
  const compact = formatStatus(status);
  if (compact.includes("A") || compact === "U") {
    return "is-added";
  }
  if (compact.includes("D")) {
    return "is-deleted";
  }
  if (compact.includes("R")) {
    return "is-renamed";
  }
  return "is-modified";
}

// 状态字母对写作者不自解释，直接显示中文
function statusTitle(status: string): string {
  const compact = formatStatus(status);
  if (compact === "U") {
    return "新文件";
  }
  if (compact.includes("A")) {
    return "新增";
  }
  if (compact.includes("D")) {
    return "已删除";
  }
  if (compact.includes("R")) {
    return "已重命名";
  }
  return "已修改";
}

function fileBaseName(relativePath: string): string {
  const normalized = String(relativePath || "").replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  return parts[parts.length - 1] || normalized;
}

function fileDirectory(relativePath: string): string {
  const normalized = String(relativePath || "").replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  if (parts.length <= 1) {
    return "项目根目录";
  }
  return parts.slice(0, -1).join("/");
}

function fileIconName(relativePath: string): string {
  const normalized = String(relativePath || "").toLowerCase();
  if (normalized.endsWith(".json") || normalized.endsWith(".lock")) {
    return "data_object";
  }
  if (normalized.endsWith(".md")) {
    return "article";
  }
  if (normalized.endsWith(".py") || normalized.endsWith(".ts") || normalized.endsWith(".vue")) {
    return "code";
  }
  return "description";
}

function historyMetaText(item: WorkspaceGitCommitEntry): string {
  const when = formatRelative(new Date(item.authoredAt).getTime(), nowTick.value);
  return `${item.authorName} · ${when}`;
}

function historyRefLabel(item: WorkspaceGitCommitEntry): string {
  const refs = String(item.refs || "")
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
  // 当前提交由“当前”徽章标识，这里不再重复分支名
  if (isCurrentCommit(item.id)) {
    return "";
  }
  const directRef = refs.find((part) => !part.startsWith("HEAD ->"));
  if (directRef) {
    return directRef.replace(/^origin\//, "");
  }
  const headRef = refs.find((part) => part.startsWith("HEAD ->"));
  if (headRef) {
    return headRef.replace(/^HEAD ->\s*/u, "");
  }
  return item.shortId;
}

function historyRowTitle(item: WorkspaceGitCommitEntry): string {
  return `${item.subject}\n${item.shortId} · ${item.authorName} · ${formatTimestamp(item.authoredAt)}`;
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

/** Relative wording keeps "did it just commit?" answerable at a glance. */
function formatRelative(value: number, reference: number): string {
  if (!Number.isFinite(value) || value <= 0) {
    return "未知时间";
  }
  const diffSeconds = Math.round((reference - value) / 1000);
  if (diffSeconds < 5) {
    return "刚刚";
  }
  if (diffSeconds < 60) {
    return `${diffSeconds} 秒前`;
  }
  if (diffSeconds < 3600) {
    return `${Math.floor(diffSeconds / 60)} 分钟前`;
  }
  if (diffSeconds < 86400) {
    return `${Math.floor(diffSeconds / 3600)} 小时前`;
  }
  const days = Math.floor(diffSeconds / 86400);
  if (days <= 30) {
    return `${days} 天前`;
  }
  return new Date(value).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

defineExpose({
  __testUtils: import.meta.env.MODE === "test" ? {
    commitMessage, changesExpanded, historyExpanded, summary, changedFiles, recentCommits,
    branchCommits, backupCommits, headCommit, headSubject, hasChanges, syncLabel,
    refreshSummary, initializeRepository, toggleChanges, toggleHistory, handleCommitKeydown,
    commitAllChanges, openChangedFile, restoreCommit, isCurrentCommit, formatStatus, statusClassName, statusTitle,
    fileBaseName, fileDirectory, fileIconName, historyMetaText, historyRefLabel, historyRowTitle, formatTimestamp,
    formatRelative, handleAutoRefresh, handleWindowFocus
  } : null
});
</script>

<style scoped>
.scm-panel,
.scm-panel * {
  box-sizing: border-box;
}

.scm-panel {
  width: 100%;
  max-width: 100%;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  /* --bg-panel is not a defined theme token; fall back to the sidebar surface so
     the panel never renders on a transparent background. */
  background: var(--bg-panel, var(--bg-sidebar));
  color: var(--text-main);
}

/* ---------- header ---------- */

.scm-header {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px 12px;
  border-bottom: 1px solid var(--border-ghost);
}

.scm-header-copy {
  min-width: 0;
  flex: 1 1 auto;
}

.scm-title {
  margin: 0;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.02em;
}

.scm-project {
  margin: 3px 0 0;
  color: var(--text-muted);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.scm-icon-btn {
  flex: 0 0 auto;
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--radius-sm, 4px);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.scm-icon-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-main);
}

.spinning {
  animation: scm-spin 0.9s linear infinite;
}

@keyframes scm-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .spinning {
    animation: none;
  }
}

.scm-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ---------- empty states ---------- */

.scm-empty-state {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
  padding: 22px 16px;
  color: var(--text-muted);
  font-size: 12px;
}

.scm-empty-icon {
  font-size: 26px;
  color: var(--text-faint);
}

.scm-empty-state.is-warning .scm-empty-icon {
  color: var(--warning, #b46c08);
}

.scm-empty-title {
  margin: 0;
  color: var(--text-main);
  font-size: 13px;
  font-weight: 600;
}

.scm-empty-hint {
  margin: 0;
  line-height: 1.7;
}

/* ---------- current state card ---------- */

.scm-state-card {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 12px 14px 0;
  padding: 10px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md, 6px);
  background: var(--bg-card);
}

.scm-branch-manager {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-ghost);
  display: grid;
  gap: 8px;
  margin: 10px 14px 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md, 6px);
  background: var(--bg-card);
}

.scm-branch-manager-row,
.scm-new-branch {
  display: flex;
  align-items: center;
  gap: 8px;
}

.scm-branch-manager-row label {
  color: var(--text-muted);
  font-size: 12px;
  flex: 0 0 auto;
}

.scm-branch-manager select,
.scm-new-branch input,
.scm-empty-branch-form input {
  min-width: 0;
  flex: 1 1 auto;
  height: 30px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-editor);
  color: var(--text-main);
  padding: 0 8px;
  font: inherit;
}

.scm-empty-branch-form { display: flex; gap: 8px; width: 100%; margin-top: 10px; }
.scm-empty-branch-form input { min-width: 0; flex: 1; height: 32px; border: 1px solid var(--border-subtle); border-radius: 5px; padding: 0 8px; background: var(--bg-editor); color: var(--text-main); font: inherit; font-size: 11px; }
.scm-empty-branch-form input:focus { outline: none; border-color: var(--accent); }
.scm-new-branch input::placeholder, .scm-empty-branch-form input::placeholder { font-size: 10px; }

.scm-branch-manager select:focus,
.scm-new-branch input:focus {
  border-color: var(--accent);
  outline: none;
}

.scm-state-row {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  font-size: 12px;
}

.scm-state-label {
  flex: 0 0 52px;
  color: var(--text-muted);
  font-size: 11px;
}

.scm-state-value {
  min-width: 0;
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  gap: 6px;
  overflow: hidden;
}

.scm-state-subject {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.scm-state-muted {
  color: var(--text-faint);
}

.scm-hash {
  flex: 0 0 auto;
  padding: 0 4px;
  border-radius: 3px;
  background: var(--bg-hover);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 11px;
}

.scm-dot {
  flex: 0 0 auto;
  width: 7px;
  height: 7px;
  border-radius: 999px;
}

.scm-dot.is-clean {
  background: var(--success, #2f8b57);
}

.scm-dot.is-dirty {
  background: var(--warning, #b7791f);
}

.scm-branch-pill {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  max-width: 150px;
  height: 20px;
  padding: 0 7px 0 4px;
  border: 1px solid var(--border-subtle);
  border-radius: 3px;
  background: var(--bg-hover);
  color: var(--accent-strong);
  font-size: 11px;
  font-weight: 700;
}

.scm-branch-pill .material-symbols-rounded {
  flex: 0 0 auto;
  font-size: 13px;
}

.scm-branch-name {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ---------- compose ---------- */

.scm-compose {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 14px;
}

.scm-compose-label {
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 600;
}

.scm-compose-input {
  width: 100%;
  min-height: 48px;
  max-height: 90px;
  padding: 7px 8px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm, 4px);
  background: var(--bg-input);
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
  line-height: 1.5;
  resize: vertical;
}

.scm-compose-input::placeholder {
  color: var(--text-faint);
}

.scm-compose-input:focus {
  outline: none;
  border-color: var(--accent);
}

.scm-compose-input:disabled {
  background: var(--bg-card-muted, var(--bg-card));
  cursor: not-allowed;
}

.scm-compose-hint {
  margin: 0;
  color: var(--text-faint);
  font-size: 11px;
  line-height: 1.5;
}

.scm-branch-manager .scm-compose-hint { font-size: 10px; }

.scm-primary-btn {
  height: 28px;
  padding: 0 12px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  border: 0;
  border-radius: var(--radius-sm, 4px);
  background: var(--accent);
  color: var(--accent-contrast);
  font: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

.scm-primary-btn.is-block {
  width: 100%;
}

.scm-primary-btn:hover:not(:disabled) {
  background: var(--accent-strong);
}

.scm-primary-btn .material-symbols-rounded {
  font-size: 16px;
}

.scm-primary-btn:disabled,
.scm-icon-btn:disabled,
.scm-restore-btn:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

/* ---------- feedback ---------- */

.scm-feedback {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 0 14px 10px;
  padding: 7px 9px;
  border-radius: var(--radius-sm, 4px);
  font-size: 12px;
  line-height: 1.6;
}

.scm-feedback .material-symbols-rounded {
  flex: 0 0 auto;
  font-size: 15px;
}

.scm-feedback.is-success {
  background: color-mix(in srgb, var(--success, #2f8b57) 10%, transparent);
  color: var(--success, #1d7b50);
}

.scm-feedback.is-error {
  background: color-mix(in srgb, var(--danger) 10%, transparent);
  color: var(--danger);
}

/* ---------- panes ---------- */

.scm-split-view {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-top: 1px solid var(--border-ghost);
}

.scm-pane {
  flex: 1 1 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-bottom: 1px solid var(--border-ghost);
}

.scm-pane.collapsed {
  flex: 0 0 auto;
  min-height: auto;
}

.scm-pane-header {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 9px 14px;
  color: var(--text-soft);
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  outline: none;
  user-select: none;
}

.scm-pane-header:hover,
.scm-pane-header:focus-visible {
  background: var(--bg-hover);
}

.scm-pane-title {
  flex: 1 1 auto;
  min-width: 0;
}

.scm-pane-caret {
  flex: 0 0 auto;
  font-size: 16px;
  color: var(--text-muted);
}

.scm-pane-count {
  flex: 0 0 auto;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 9px;
  background: var(--bg-hover);
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.scm-pane-count.is-active {
  background: color-mix(in srgb, var(--accent) 16%, transparent);
  color: var(--accent-strong);
}

.scm-pane-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  padding: 2px 8px 8px;
}

.scm-inline-empty {
  margin: 0;
  padding: 10px 6px;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.6;
}

/* ---------- rows ---------- */

.scm-change-row,
.scm-history-row {
  width: 100%;
  max-width: 100%;
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  padding: 7px 6px;
  border: 0;
  border-radius: var(--radius-sm, 4px);
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-align: left;
  font: inherit;
  overflow: hidden;
}

.scm-change-row:hover:not(:disabled) {
  background: var(--bg-hover);
}

.scm-row-icon {
  color: var(--text-muted);
  font-size: 16px;
}

.scm-row-line {
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 6px;
  overflow: hidden;
}

.scm-row-line-history {
  flex-direction: column;
  align-items: flex-start;
  gap: 1px;
}

.scm-row-name,
.scm-history-subject {
  min-width: 0;
  max-width: 100%;
  color: var(--text-main);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.scm-row-dir,
.scm-history-meta {
  min-width: 0;
  max-width: 100%;
  color: var(--text-muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.scm-row-dir {
  flex: 1 1 auto;
}

/* 状态改为中文短标签，写作者不需要记 Git 字母 */
.scm-status-chip {
  flex: 0 0 auto;
  height: 18px;
  padding: 0 6px;
  display: inline-flex;
  align-items: center;
  border-radius: 3px;
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
}

.scm-status-chip.is-added {
  background: color-mix(in srgb, var(--success, #2f8b57) 14%, transparent);
  color: var(--success, #2f8b57);
}

.scm-status-chip.is-modified {
  background: color-mix(in srgb, var(--warning, #b7791f) 16%, transparent);
  color: var(--warning, #b7791f);
}

.scm-status-chip.is-deleted {
  background: color-mix(in srgb, var(--danger) 12%, transparent);
  color: var(--danger);
}

.scm-status-chip.is-renamed {
  background: color-mix(in srgb, var(--info, #2f6feb) 12%, transparent);
  color: var(--info, #2f6feb);
}

/* ---------- history ---------- */

.scm-history-row {
  cursor: default;
}

.scm-history-row:hover {
  background: var(--bg-hover);
}

.scm-history-row.current {
  background: color-mix(in srgb, var(--accent-soft) 16%, transparent);
}

.scm-history-row.is-backup .scm-history-subject {
  color: var(--text-muted);
}

.scm-history-ref {
  max-width: 100%;
  color: var(--text-faint);
  font-family: var(--font-mono);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.scm-subgroup-header {
  display: flex;
  align-items: center;
  gap: 5px;
  margin: 8px 0 2px;
  padding: 5px 6px;
  border-top: 1px dashed var(--border-subtle);
  color: var(--text-faint);
  font-size: 11px;
  font-weight: 600;
}

.scm-subgroup-header .material-symbols-rounded {
  font-size: 14px;
}

.scm-restore-btn {
  flex: 0 0 auto;
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 3px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.12s ease;
}

.scm-history-row:hover .scm-restore-btn,
.scm-restore-btn:focus-visible {
  opacity: 1;
}

.scm-restore-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-main);
}

.scm-restore-btn .material-symbols-rounded {
  font-size: 16px;
}

.scm-current-badge {
  flex: 0 0 auto;
  height: 18px;
  padding: 0 6px;
  display: inline-flex;
  align-items: center;
  border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--border-subtle));
  border-radius: 3px;
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  color: var(--accent-strong);
  font-size: 11px;
  font-weight: 700;
}

.scm-graph-lane {
  position: relative;
  width: 18px;
  align-self: stretch;
  min-height: 22px;
}

.scm-graph-lane::before {
  content: "";
  position: absolute;
  left: 8px;
  top: 0;
  bottom: 0;
  width: 1px;
  background: color-mix(in srgb, var(--text-muted) 28%, transparent);
}

.scm-graph-lane.tail::before {
  bottom: 50%;
}

.scm-graph-node {
  position: absolute;
  left: 4px;
  top: 50%;
  width: 9px;
  height: 9px;
  margin-top: -4.5px;
  border-radius: 999px;
  border: 2px solid color-mix(in srgb, var(--accent-strong) 86%, transparent);
  background: var(--bg-card);
}

.scm-history-row.current .scm-graph-node {
  background: color-mix(in srgb, var(--accent-strong) 86%, transparent);
}

.scm-graph-lane.is-backup .scm-graph-node {
  border-color: color-mix(in srgb, var(--text-muted) 55%, transparent);
}

/* ---------- footer ---------- */

.scm-footer {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 7px 14px;
  border-top: 1px solid var(--border-ghost);
  color: var(--text-faint);
  font-size: 11px;
}

.scm-footer .material-symbols-rounded {
  font-size: 13px;
}
</style>
