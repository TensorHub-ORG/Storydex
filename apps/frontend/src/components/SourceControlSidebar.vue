<template>
  <aside class="scm-panel">
    <header class="scm-header">
      <div class="scm-header-copy">
        <h2 class="scm-title">平行时空线</h2>
        <p class="scm-project" :title="projectLabel">{{ projectLabel }}</p>
      </div>

      <button
        class="scm-icon-btn"
        type="button"
        :title="refreshTitle"
        aria-label="刷新平行时空线"
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
          <p class="scm-empty-hint">先打开一个 Storydex 项目，这里会显示它的改动与平行时空线。</p>
        </div>
      </template>

      <template v-else-if="summary && !summary.gitInstalled">
        <div class="scm-empty-state is-warning">
          <span class="material-symbols-rounded scm-empty-icon">warning</span>
          <p class="scm-empty-title">平行时空线不可用</p>
          <p class="scm-empty-hint">{{ summary.message || "当前环境未安装 Git。" }}</p>
        </div>
      </template>

      <template v-else-if="summary && !summary.initialized">
        <div class="scm-empty-state">
          <span class="material-symbols-rounded scm-empty-icon">account_tree</span>
          <p class="scm-empty-title">还没有启用版本记录</p>
          <p class="scm-empty-hint">
            启用后，每次提交都会在时空线上留下一个可回溯的节点，全部保存在本地，不会上传到任何远端。
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
        </div>
        <div v-if="gitStore.error" class="scm-feedback is-error">{{ gitStore.error }}</div>
      </template>

      <template v-else>
        <!-- 我现在在哪条世界线上 -->
        <section class="scm-here">
          <span
            class="scm-here-pill"
            :class="{ 'is-observing': isObserving }"
            :title="hereTitle"
          >
            <span class="material-symbols-rounded">{{ isObserving ? "visibility" : "fork_right" }}</span>
            <span class="scm-here-name">{{ hereLabel }}</span>
          </span>
          <span class="scm-here-state" :title="headTitle">
            <span class="scm-dot" :class="hasChanges ? 'is-dirty' : 'is-clean'"></span>
            <span>{{ changedCountLabel }}</span>
          </span>
        </section>

        <!-- 平行时空线树状图：这个面板的主角 -->
        <section class="scm-graph">
          <TimelineGraph
            :timeline="gitStore.timeline"
            :loading="gitStore.isTimelineLoading"
            :detached-override="gitStore.isDetached"
            :dirty="hasChanges"
            :busy="actions.busy.value"
            density="compact"
            @jump="actions.requestJump"
            @fork="actions.requestFork"
            @inspect="actions.requestInspect"
            @rename-worldline="actions.requestRename"
            @delete-worldline="actions.requestDelete"
            @expand="openWorldlineMap"
          />
        </section>

        <div v-if="gitStore.error" class="scm-feedback is-error">
          <span class="material-symbols-rounded">error</span>
          <span>{{ gitStore.error }}</span>
        </div>
        <div v-else-if="gitStore.successMessage" class="scm-feedback is-success">
          <span class="material-symbols-rounded">check_circle</span>
          <span>{{ gitStore.successMessage }}</span>
        </div>

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
            <span class="scm-pane-title">还没进版本的改动</span>
            <span class="scm-pane-count" :class="{ 'is-active': changedFiles.length > 0 }">
              {{ changedFiles.length }}
            </span>
          </header>

          <div v-if="changesExpanded" class="scm-pane-body">
            <p v-if="changedFiles.length === 0" class="scm-inline-empty">
              工作区是干净的，所有改动都已经在时空线上了。
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

        <!-- 提交：在当前世界线上留下一个新节点 -->
        <section class="scm-compose">
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
            <span class="material-symbols-rounded">add_circle</span>
            <span>{{ commitButtonLabel }}</span>
          </button>
          <p v-if="isObserving" class="scm-compose-hint is-observing">
            你正处于观测态。这次提交会自动开辟一条新的世界线，原线不受影响。
          </p>
        </section>

        <footer class="scm-footer" :title="refreshTitle">
          <span class="material-symbols-rounded">{{ gitStore.isLoading ? "sync" : "schedule" }}</span>
          <span>{{ syncLabel }}</span>
        </footer>
      </template>
    </div>

    <WorldlineDialog
      :state="actions.dialog.value"
      :submitting="actions.isSubmitting.value"
      @confirm="actions.confirm"
      @cancel="actions.close"
      @update:input="onDialogInput"
    />
  </aside>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useGitStore } from "@/stores/git";
import { useWorkspaceStore } from "@/stores/workspace";
import { useWorldlineActions } from "@/composables/useWorldlineActions";
import TimelineGraph from "@/components/TimelineGraph.vue";
import WorldlineDialog from "@/components/WorldlineDialog.vue";

const gitStore = useGitStore();
const workspaceStore = useWorkspaceStore();
const actions = useWorldlineActions();

const commitMessage = ref("");
const changesExpanded = ref(true);
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
const AUTO_REFRESH_INTERVAL_MS = 3000;
/**
 * 树比工作区状态重（要跑 git log --all），所以刷得慢一些；但它必须参与轮询：
 * Agent 的自动提交会凭空长出新节点，只在窗口聚焦时刷新的话，用户盯着面板也
 * 看不到 Agent 刚写进去的版本。
 */
const TIMELINE_REFRESH_INTERVAL_MS = 9000;
let autoRefreshTimer: number | null = null;
let clockTimer: number | null = null;
let timelineTimer: number | null = null;

const summary = computed(() => gitStore.summary);
const projectLabel = computed(() => workspaceStore.projectLabel || "未打开项目");
const changedFiles = computed(() => summary.value?.changedFiles || []);
const headCommit = computed(() => summary.value?.head || null);
const hasChanges = computed(() => changedFiles.value.length > 0);
const isObserving = computed(() => gitStore.isDetached);
/** 观测态下没有世界线可言，不能退回到某个默认分支名假装用户在某条线上。 */
const worldlineName = computed(() => gitStore.currentWorldline);
const hereLabel = computed(() => (isObserving.value ? "观测态（不在任何世界线上）" : worldlineName.value || "尚未确定世界线"));
const hereTitle = computed(() =>
  isObserving.value
    ? "你正停在一条世界线的历史节点上。在这里提交会自动开辟一条新的世界线。"
    : `当前世界线：${worldlineName.value}`
);
const changedCountLabel = computed(() =>
  gitStore.changedCount > 0 ? `${gitStore.changedCount} 个文件还没进版本` : "干净，全部已进版本"
);
const commitPlaceholder = computed(() =>
  hasChanges.value ? "这次改了什么？（Ctrl+Enter 提交）" : "没有待提交的改动"
);
const commitButtonLabel = computed(() => {
  if (gitStore.isCommitting) {
    return "提交中…";
  }
  if (!hasChanges.value) {
    return "没有待提交的改动";
  }
  return isObserving.value
    ? `开辟新世界线并提交 ${changedFiles.value.length} 个文件`
    : `留下新节点（${changedFiles.value.length} 个文件）`;
});
const commitButtonTitle = computed(() => {
  if (!hasChanges.value) return "当前没有待提交的更改";
  return isObserving.value
    ? "在观测态提交会自动开辟一条新的世界线"
    : `在世界线 ${worldlineName.value} 上留下一个新节点`;
});
const headTitle = computed(() =>
  headCommit.value
    ? `最新节点：${headCommit.value.subject}\n${headCommit.value.shortId} · ${headCommit.value.authorName} · ${formatTimestamp(headCommit.value.authoredAt)}`
    : "还没有任何节点"
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
    void gitStore.refreshTimeline();
  }
  autoRefreshTimer = window.setInterval(handleAutoRefresh, AUTO_REFRESH_INTERVAL_MS);
  timelineTimer = window.setInterval(handleTimelineRefresh, TIMELINE_REFRESH_INTERVAL_MS);
  clockTimer = window.setInterval(() => {
    nowTick.value = Date.now();
  }, 1000);
  window.addEventListener("focus", handleWindowFocus);
  document.addEventListener("visibilitychange", handleWindowFocus);
});

onBeforeUnmount(() => {
  for (const timer of [autoRefreshTimer, timelineTimer, clockTimer]) {
    if (timer !== null) {
      window.clearInterval(timer);
    }
  }
  autoRefreshTimer = null;
  timelineTimer = null;
  clockTimer = null;
  window.removeEventListener("focus", handleWindowFocus);
  document.removeEventListener("visibilitychange", handleWindowFocus);
});

watch(
  () => [workspaceStore.projectRootLabel, workspaceStore.treeResetToken],
  () => {
    if (!workspaceStore.launchScreenVisible) {
      void gitStore.refreshSummary({ silent: true });
      void gitStore.refreshBranches();
      void gitStore.refreshTimeline();
    }
  }
);

function isRefreshBlocked(): boolean {
  if (workspaceStore.launchScreenVisible || document.hidden) {
    return true;
  }
  // 只在写操作进行中时跳过（这些操作本身会改变仓库状态并自行刷新），不在
  // isLoading 时跳过——否则服务器慢时每次轮询都被跳过，状态永远不刷新。
  return gitStore.isCommitting || gitStore.isRestoring || gitStore.isInitializing || gitStore.isWorldlineBusy;
}

function handleAutoRefresh(): void {
  if (isRefreshBlocked()) {
    return;
  }
  void gitStore.refreshSummary({ silent: true });
}

function handleTimelineRefresh(): void {
  if (isRefreshBlocked() || gitStore.isJumping) {
    return;
  }
  void gitStore.refreshTimeline();
}

function handleWindowFocus(): void {
  if (workspaceStore.launchScreenVisible || document.hidden) {
    return;
  }
  void gitStore.refreshSummary({ silent: true });
  void gitStore.refreshTimeline();
}

// An explicit click must always hit the backend, even while a background poll is
// running, otherwise the button looks broken.
function refreshSummary(): void {
  void gitStore.refreshSummary({ force: true });
  void gitStore.refreshBranches();
  void gitStore.refreshTimeline({ force: true });
}

/** 侧栏太窄时把树搬到主编辑区，用全宽画布浏览。 */
function openWorldlineMap(): void {
  void workspaceStore.openWorldlineMapDocument();
}

function onDialogInput(value: string): void {
  actions.dialog.value = { ...actions.dialog.value, input: value, error: "" };
}

function initializeRepository(): void {
  void gitStore.initializeRepository();
}

function toggleChanges(): void {
  changesExpanded.value = !changesExpanded.value;
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
    void gitStore.refreshTimeline();
  });
}

function openChangedFile(relativePath: string): void {
  if (!relativePath) {
    return;
  }
  void workspaceStore.openGitReview({ focusPath: relativePath });
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
    commitMessage, changesExpanded, summary, changedFiles, headCommit, hasChanges, syncLabel,
    isObserving, worldlineName, hereLabel, hereTitle, commitButtonLabel, commitButtonTitle,
    refreshSummary, initializeRepository, toggleChanges, handleCommitKeydown, openWorldlineMap,
    onDialogInput, commitAllChanges, openChangedFile, formatStatus, statusClassName, statusTitle,
    fileBaseName, fileDirectory, fileIconName, formatTimestamp, formatRelative,
    isRefreshBlocked, handleAutoRefresh, handleTimelineRefresh, handleWindowFocus
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

/* ---------- 我在哪 ---------- */

.scm-here {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 14px;
  border-bottom: 1px solid var(--border-ghost);
  font-size: 11px;
}

.scm-here-pill {
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 60%;
  height: 21px;
  padding: 0 8px 0 5px;
  border: 1px solid var(--border-subtle);
  border-radius: 11px;
  background: var(--bg-hover);
  color: var(--accent-strong);
  font-weight: 700;
}

.scm-here-pill.is-observing {
  border-color: color-mix(in srgb, var(--warning) 45%, transparent);
  background: color-mix(in srgb, var(--warning) 12%, transparent);
  color: var(--warning);
}

.scm-here-pill .material-symbols-rounded {
  flex: 0 0 auto;
  font-size: 14px;
}

.scm-here-name {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.scm-here-state {
  min-width: 0;
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 5px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
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

/* ---------- 树状图（面板主角） ---------- */

.scm-graph {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  min-height: 0;
  border-bottom: 1px solid var(--border-ghost);
}

/* ---------- compose ---------- */

.scm-compose {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 14px 12px;
  border-top: 1px solid var(--border-ghost);
}

.scm-compose-input {
  width: 100%;
  min-height: 44px;
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

.scm-compose-hint.is-observing {
  color: var(--warning);
}

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
.scm-icon-btn:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

/* ---------- feedback ---------- */

.scm-feedback {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 8px 14px 0;
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

.scm-pane {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
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

.scm-change-row {
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

.scm-row-name {
  min-width: 0;
  max-width: 100%;
  color: var(--text-main);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.scm-row-dir {
  min-width: 0;
  max-width: 100%;
  flex: 1 1 auto;
  color: var(--text-muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
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
