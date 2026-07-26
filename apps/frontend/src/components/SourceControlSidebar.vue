<template>
  <aside class="source-control-panel">
    <header class="source-header">
      <div class="source-header-copy">
        <h2 class="source-title">版本控制</h2>
        <p class="source-subtitle">{{ projectLabel }}</p>
      </div>

      <button
        class="source-icon-btn"
        type="button"
        title="刷新"
        :disabled="gitStore.isLoading || workspaceStore.launchScreenVisible"
        @click="refreshSummary"
      >
        <span class="material-symbols-rounded">refresh</span>
      </button>
    </header>

    <div class="source-body">
      <template v-if="workspaceStore.launchScreenVisible">
        <div class="source-empty-state">先打开一个 Storydex 项目，再查看版本控制。</div>
      </template>

      <template v-else-if="summary && !summary.gitInstalled">
        <div class="source-empty-state is-warning">{{ summary.message || "当前环境未安装 Git。" }}</div>
      </template>

      <template v-else-if="summary && !summary.initialized">
        <section class="scm-compose">
          <div class="scm-compose-head">
            <span class="scm-section-label">提交仓库</span>
            <span class="scm-branch-pill">{{ branchName }}</span>
          </div>
          <div class="scm-compose-meta">初始化后，每次 Agent 修改项目文件都会自动提交到本地仓库。</div>
          <button class="scm-commit-btn" type="button" :disabled="gitStore.isInitializing" @click="initializeRepository">
            <span class="material-symbols-rounded">account_tree</span>
            <span>{{ gitStore.isInitializing ? "初始化中..." : "初始化本地仓库" }}</span>
          </button>
        </section>
      </template>

      <template v-else>
        <section class="scm-compose">
          <div class="scm-compose-head">
            <span class="scm-section-label">提交仓库</span>
            <span class="scm-branch-pill" :title="`当前分支：${branchName}`">
              <span class="material-symbols-rounded">fork_right</span>
              <span class="scm-branch-name">{{ branchName }}</span>
            </span>
          </div>
          <div class="scm-compose-meta">
            <span class="scm-head-subject" :title="`最新提交：${headSubject}`">最新：{{ headSubject }}</span>
            <span class="scm-head-separator">·</span>
            <span class="scm-head-count">{{ changedCountLabel }}</span>
          </div>
          <textarea
            v-model.trim="commitMessage"
            class="commit-message-input"
            :placeholder="commitPlaceholder"
            rows="2"
            @keydown="handleCommitKeydown"
          ></textarea>
          <button
            class="scm-commit-btn"
            type="button"
            :disabled="gitStore.isCommitting || changedFiles.length === 0"
            :title="changedFiles.length === 0 ? '当前没有待提交的更改' : `提交全部更改到 ${branchName}`"
            @click="commitAllChanges"
          >
            <span class="material-symbols-rounded">check_circle</span>
            <span>{{ gitStore.isCommitting ? "提交中..." : "提交" }}</span>
          </button>
        </section>

        <div v-if="gitStore.error" class="scm-feedback is-error">{{ gitStore.error }}</div>
        <div v-else-if="gitStore.successMessage" class="scm-feedback is-success">{{ gitStore.successMessage }}</div>

        <div class="scm-split-view">
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
              <div class="scm-pane-title">
                <span class="scm-pane-caret material-symbols-rounded">
                  {{ changesExpanded ? "expand_more" : "chevron_right" }}
                </span>
                <span>更改</span>
              </div>
              <span class="scm-pane-count">{{ changedFiles.length }}</span>
            </header>

            <div v-if="changesExpanded" class="scm-pane-body">
              <div v-if="changedFiles.length === 0" class="scm-inline-empty">当前没有待提交的更改。</div>

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
                <span class="scm-row-status" :class="statusClassName(item.status)" :title="statusTitle(item.status)">
                  {{ formatStatus(item.status) }}
                </span>
              </button>
            </div>
          </section>

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
              <div class="scm-pane-title">
                <span class="scm-pane-caret material-symbols-rounded">
                  {{ historyExpanded ? "expand_more" : "chevron_right" }}
                </span>
                <span>提交历史</span>
              </div>
              <span class="scm-pane-count">{{ recentCommits.length }}</span>
            </header>

            <div v-if="historyExpanded" class="scm-pane-body">
              <div v-if="recentCommits.length === 0" class="scm-inline-empty">仓库已就绪，等待首个本地提交。</div>

              <!-- 行本身只做展示；回退是 hover 浮现的显式按钮，避免点一下就进入危险操作 -->
              <div
                v-for="(item, index) in recentCommits"
                :key="item.id"
                class="scm-history-row"
                :class="{ current: isCurrentCommit(item.id) }"
                :title="historyRowTitle(item)"
              >
                <span class="scm-graph-lane" :class="{ tail: index === recentCommits.length - 1 }">
                  <span class="scm-graph-node"></span>
                </span>
                <span class="scm-row-line scm-row-line-history">
                  <span class="scm-history-subject">{{ item.subject }}</span>
                  <span class="scm-history-meta">{{ historyMetaText(item) }}</span>
                  <span v-if="historyRefLabel(item)" class="scm-history-ref">{{ historyRefLabel(item) }}</span>
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
            </div>
          </section>
        </div>
      </template>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useGitStore } from "@/stores/git";
import { useWorkspaceStore } from "@/stores/workspace";
import type { WorkspaceGitCommitEntry } from "@/types/workspace";

const gitStore = useGitStore();
const workspaceStore = useWorkspaceStore();

const commitMessage = ref("");
const changesExpanded = ref(true);
const historyExpanded = ref(true);

const summary = computed(() => gitStore.summary);
const projectLabel = computed(() => workspaceStore.projectLabel || "未打开项目");
const branchName = computed(() => summary.value?.branch || summary.value?.defaultBranch || "develop");
const changedFiles = computed(() => summary.value?.changedFiles || []);
const recentCommits = computed(() => summary.value?.recentCommits || []);
const headSubject = computed(() => summary.value?.head?.subject || "暂无提交");
const changedCountLabel = computed(() => (gitStore.changedCount > 0 ? `${gitStore.changedCount} 项更改` : "工作区干净"));
const commitPlaceholder = computed(() => `消息(Ctrl+Enter 在 "${branchName.value}" 提交)`);
onMounted(() => {
  if (!workspaceStore.launchScreenVisible) {
    void gitStore.refreshSummary({ silent: true });
  }
});

watch(
  () => [workspaceStore.projectRootLabel, workspaceStore.treeResetToken],
  () => {
    if (!workspaceStore.launchScreenVisible) {
      void gitStore.refreshSummary({ silent: true });
    }
  }
);

function refreshSummary(): void {
  void gitStore.refreshSummary();
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
  void gitStore.commitAll(message).then(() => {
    if (!gitStore.error) {
      commitMessage.value = "";
    }
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
  return "is-modified";
}

// 状态字母对写作者不自解释，悬浮时给中文含义
function statusTitle(status: string): string {
  const compact = formatStatus(status);
  if (compact === "U") {
    return "新文件（尚未跟踪）";
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
    return ".";
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
  return `${item.authorName} ${formatTimestamp(item.authoredAt)}`;
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
defineExpose({
  __testUtils: import.meta.env.MODE === "test" ? {
    commitMessage, changesExpanded, historyExpanded, summary, changedFiles, recentCommits,
    refreshSummary, initializeRepository, toggleChanges, toggleHistory, handleCommitKeydown,
    commitAllChanges, openChangedFile, restoreCommit, isCurrentCommit, formatStatus, statusClassName, statusTitle,
    fileBaseName, fileDirectory, fileIconName, historyMetaText, historyRefLabel, historyRowTitle, formatTimestamp
  } : null
});
</script>

<style scoped>
.source-control-panel,
.source-control-panel * {
  box-sizing: border-box;
}

.source-control-panel {
  width: 100%;
  max-width: 100%;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-panel);
  color: var(--text-main);
}

.source-header {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 18px 12px;
  border-bottom: 1px solid var(--border-ghost);
  background: var(--bg-panel);
}

.source-header-copy {
  min-width: 0;
  flex: 1 1 auto;
}

.source-title {
  margin: 0;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: var(--text-main);
}

.source-subtitle {
  margin: 4px 0 0;
  color: var(--text-muted);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.source-icon-btn {
  flex: 0 0 auto;
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 3px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.source-icon-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-main);
}

.source-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 0;
  overflow: hidden;
  padding: 0;
}

.source-empty-state {
  padding: 18px 16px;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.7;
}

.source-empty-state.is-warning {
  color: var(--warning, #b46c08);
}

.scm-compose {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 10px 14px 12px;
  border-bottom: 1px solid var(--border-ghost);
  background: transparent;
  box-shadow: none;
  overflow: hidden;
}

.scm-compose-head,
.scm-pane-header,
.scm-pane-title,
.scm-compose-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.scm-section-label {
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

/* 分支徽标：chip 样式，与导入预览一致 */
.scm-branch-pill {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  max-width: 132px;
  height: 20px;
  padding: 0 7px 0 4px;
  border: 1px solid var(--border-subtle);
  border-radius: 3px;
  background: var(--bg-card);
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

/* 历史行的 ref/短哈希：辅助信息，弱化为灰色等宽字 */
.scm-history-ref {
  flex: 0 0 auto;
  max-width: 112px;
  color: var(--text-faint);
  font-family: var(--font-mono);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.scm-compose-meta {
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.45;
}

.scm-head-subject {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.scm-head-separator,
.scm-head-count {
  flex: 0 0 auto;
}

/* 提交输入框：盒式输入，易识别为可输入区域 */
.commit-message-input {
  width: 100%;
  min-height: 46px;
  max-height: 56px;
  padding: 6px 8px;
  border: 1px solid var(--border-subtle);
  border-radius: 3px;
  background: var(--bg-input);
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
  line-height: 1.45;
  resize: none;
}

.commit-message-input::placeholder {
  color: var(--text-faint);
  font-size: 12px;
}

.commit-message-input:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: none;
}

/* 提交按钮：全宽主按钮，动作醒目（VSCode SCM 同款布局） */
.scm-commit-btn {
  width: 100%;
  align-self: stretch;
  height: 28px;
  padding: 0 10px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  border: 0;
  border-radius: 3px;
  background: var(--accent);
  color: var(--accent-contrast);
  font: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

.scm-commit-btn:hover:not(:disabled) {
  background: var(--accent-strong);
}

.scm-commit-btn .material-symbols-rounded {
  font-size: 15px;
}

.scm-commit-btn:disabled,
.source-icon-btn:disabled,
.scm-change-row:disabled,
.scm-restore-btn:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.scm-feedback {
  flex: 0 0 auto;
  padding: 8px 16px;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 12px;
  line-height: 1.6;
}

.scm-feedback.is-success {
  color: var(--success, #1d7b50);
}

.scm-feedback.is-error {
  color: var(--danger);
}

.scm-split-view {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 0;
  overflow: hidden;
}

.scm-pane {
  flex: 1 1 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 0;
  border-bottom: 1px solid var(--border-ghost);
  background: transparent;
  box-shadow: none;
}

.scm-pane.collapsed {
  flex: 0 0 auto;
  min-height: auto;
}

.scm-pane-header {
  flex: 0 0 auto;
  justify-content: space-between;
  padding: 10px 18px;
  border-bottom: 1px solid var(--border-ghost);
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

.scm-pane-caret {
  width: 15px;
  height: 15px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-muted);
  font-size: 15px;
  line-height: 1;
}

.scm-pane-count {
  flex: 0 0 auto;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  background: var(--bg-hover);
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 700;
}

.scm-pane-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  padding: 4px 10px 8px;
}

.scm-inline-empty {
  padding: 12px;
  color: var(--text-muted);
  font-size: 12px;
}

.scm-change-row,
.scm-history-row {
  width: 100%;
  max-width: 100%;
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  padding: 8px 8px;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-align: left;
  font: inherit;
  overflow: hidden;
}

.scm-change-row:hover:not(:disabled),
.scm-history-row:hover:not(:disabled) {
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
  gap: 8px;
}

.scm-row-name,
.scm-history-subject {
  min-width: 0;
  flex: 0 1 auto;
  color: var(--text-main);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.scm-row-dir,
.scm-history-meta {
  min-width: 0;
  flex: 1 1 auto;
  color: var(--text-muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.scm-row-status {
  min-width: 18px;
  text-align: right;
  font-size: 12px;
  font-weight: 700;
}

.scm-row-status.is-added {
  color: var(--success, #2f8b57);
}

.scm-row-status.is-modified {
  color: var(--warning, #b7791f);
}

.scm-row-status.is-deleted {
  color: var(--danger);
}

/* 历史行：纯展示行，回退按钮 hover 才浮现，避免误触危险操作 */
.scm-history-row {
  grid-template-columns: 18px minmax(0, 1fr) auto;
  cursor: default;
}

.scm-history-row.current {
  background: color-mix(in srgb, var(--accent-soft) 16%, transparent);
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
  height: 100%;
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

</style>
