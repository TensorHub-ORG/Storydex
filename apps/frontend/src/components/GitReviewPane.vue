<template>
  <section class="git-review-pane">
    <header class="git-review-head">
      <div class="git-review-title-block">
        <div class="git-review-title">
          <span class="material-symbols-rounded">difference</span>
          <span>{{ displayTitle }}</span>
        </div>
        <div class="git-review-subtitle">{{ summaryLabel }}</div>
      </div>

      <button class="git-review-refresh" type="button" :disabled="loading" title="刷新 Diff" @click="$emit('refresh')">
        <span class="material-symbols-rounded">refresh</span>
      </button>
    </header>

    <div v-if="loading" class="git-review-state">
      <span class="git-review-spinner"></span>
      <span>正在读取本地变更...</span>
    </div>
    <div v-else-if="error" class="git-review-state is-error">{{ error }}</div>
    <div v-else-if="!diff?.gitInstalled" class="git-review-state">
      {{ diff?.message || "当前环境未安装 Git。" }}
    </div>
    <div v-else-if="!diff?.initialized" class="git-review-state">
      {{ diff?.message || "当前项目尚未初始化本地仓库。" }}
    </div>
    <div v-else-if="files.length === 0" class="git-review-state">
      {{ emptyMessage }}
    </div>

    <div v-else class="git-review-files">
      <section
        v-for="file in files"
        :key="`${file.status}-${file.relativePath}`"
        class="git-review-file"
        :class="{ expanded: isExpanded(file.relativePath) }"
      >
        <button
          class="git-review-file-row"
          type="button"
          :title="file.relativePath"
          :aria-expanded="isExpanded(file.relativePath)"
          @click="toggleFile(file.relativePath)"
        >
          <span class="material-symbols-rounded git-review-caret">
            {{ isExpanded(file.relativePath) ? "expand_more" : "chevron_right" }}
          </span>
          <span class="material-symbols-rounded git-review-file-icon">{{ fileIconName(file.relativePath) }}</span>
          <span class="git-review-path">{{ file.relativePath }}</span>
          <span class="git-review-status" :class="statusClass(file.status)">{{ statusLabel(file.status) }}</span>
          <span class="git-review-stats">
            <span class="is-added">+{{ file.added }}</span>
            <span class="is-removed">-{{ file.removed }}</span>
          </span>
        </button>

        <div v-if="isExpanded(file.relativePath)" class="git-review-hunks">
          <div v-if="file.hunks.length === 0" class="git-review-empty-hunk">
            这个文件没有可展示的文本变更块。
          </div>

          <section v-for="(hunk, index) in file.hunks" :key="`${file.relativePath}-${index}`" class="git-review-hunk">
            <div class="git-review-hunk-head">{{ hunk.header }}</div>
            <div
              v-for="(line, lineIndex) in hunk.lines"
              :key="`${file.relativePath}-${index}-${lineIndex}`"
              class="git-review-line"
              :class="`is-${line.kind}`"
            >
              <span class="git-review-gutter">{{ formatLineNumber(line.oldLine) }}</span>
              <span class="git-review-gutter">{{ formatLineNumber(line.newLine) }}</span>
              <span class="git-review-marker">{{ markerForLine(line.kind) }}</span>
              <pre class="git-review-text">{{ line.content || " " }}</pre>
            </div>
          </section>
        </div>
      </section>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { WorkspaceGitDiffFile, WorkspaceGitDiffLineKind, WorkspaceGitDiffResponse } from "@/types/workspace";

const props = defineProps<{
  diff: WorkspaceGitDiffResponse | null;
  title?: string;
  focusPath?: string;
  loading?: boolean;
  error?: string;
}>();

defineEmits<{
  (event: "refresh"): void;
}>();

const expandedPaths = ref<Set<string>>(new Set());

const files = computed<WorkspaceGitDiffFile[]>(() => props.diff?.files || []);
const displayTitle = computed(() => props.title || "本地变更审阅");
const emptyMessage = computed(() => props.title?.includes("本轮") ? "本轮 Diff 数据不可用。" : "当前没有未提交的项目文件变更。");
const summaryLabel = computed(() => {
  const totals = props.diff?.totals || { files: 0, added: 0, removed: 0 };
  const branch = props.diff?.branch ? ` · ${props.diff.branch}` : "";
  return `${totals.files} 个文件已修改 · +${totals.added} -${totals.removed}${branch}`;
});

watch(
  () => [props.focusPath || "", files.value.map((file) => file.relativePath).join("\u0000")],
  () => {
    const focusPath = normalizePath(props.focusPath || "");
    expandedPaths.value = focusPath ? new Set([focusPath]) : new Set();
  },
  { immediate: true }
);

function toggleFile(relativePath: string): void {
  const normalized = normalizePath(relativePath);
  if (!normalized) {
    return;
  }
  const next = new Set(expandedPaths.value);
  if (next.has(normalized)) {
    next.delete(normalized);
  } else {
    next.add(normalized);
  }
  expandedPaths.value = next;
}

function isExpanded(relativePath: string): boolean {
  return expandedPaths.value.has(normalizePath(relativePath));
}

function statusLabel(status: string): string {
  const compact = String(status || "").trim();
  if (!compact) return "M";
  if (compact === "??") return "U";
  return compact.replace(/\s+/g, "");
}

function statusClass(status: string): string {
  const label = statusLabel(status);
  if (label === "U" || label.includes("A")) {
    return "is-added";
  }
  if (label.includes("D")) {
    return "is-deleted";
  }
  return "is-modified";
}

function fileIconName(relativePath: string): string {
  const normalized = String(relativePath || "").toLowerCase();
  if (normalized.endsWith(".json") || normalized.endsWith(".lock")) return "data_object";
  if (normalized.endsWith(".md") || normalized.endsWith(".txt")) return "article";
  if (normalized.endsWith(".py") || normalized.endsWith(".ts") || normalized.endsWith(".vue")) return "code";
  return "description";
}

function markerForLine(kind: WorkspaceGitDiffLineKind): string {
  if (kind === "added") return "+";
  if (kind === "removed") return "-";
  return " ";
}

function formatLineNumber(value: number | null): string {
  return value === null ? "" : String(value);
}

function normalizePath(value: string): string {
  return String(value || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "").trim();
}
</script>

<style scoped>
.git-review-pane {
  flex: 1 1 auto;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-editor);
  color: var(--text-main);
}

.git-review-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 12px 18px;
  border-bottom: 1px solid var(--border-ghost);
}

.git-review-title-block {
  min-width: 0;
}

.git-review-title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  color: var(--text-main);
  font-size: 13px;
  font-weight: 700;
}

.git-review-title .material-symbols-rounded {
  font-size: 17px;
  color: var(--accent-strong);
}

.git-review-subtitle {
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 12px;
}

.git-review-refresh {
  flex: 0 0 auto;
  width: 28px;
  height: 28px;
  border: 0;
  background: transparent;
  color: var(--text-secondary);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}

.git-review-refresh:hover:not(:disabled) {
  color: var(--text-main);
  background: var(--bg-hover);
}

.git-review-refresh:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.git-review-refresh .material-symbols-rounded {
  font-size: 17px;
}

.git-review-state {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 28px;
  color: var(--text-muted);
  font-size: 13px;
}

.git-review-state.is-error {
  color: var(--danger);
}

.git-review-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid color-mix(in srgb, var(--accent-strong) 28%, transparent);
  border-top-color: var(--accent-strong);
  border-radius: 999px;
  animation: git-review-spin 0.8s linear infinite;
}

.git-review-files {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding-bottom: 28px;
}

.git-review-file {
  border-bottom: 1px solid var(--border-ghost);
}

.git-review-file-row {
  width: 100%;
  min-height: 42px;
  display: grid;
  grid-template-columns: 18px 20px minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 8px;
  padding: 0 18px;
  border: 0;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
  font: inherit;
}

.git-review-file-row:hover,
.git-review-file-row:focus-visible {
  background: var(--bg-hover);
  outline: none;
}

.git-review-caret,
.git-review-file-icon {
  color: var(--text-muted);
  font-size: 16px;
}

.git-review-path {
  min-width: 0;
  color: var(--text-main);
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.git-review-status {
  min-width: 18px;
  text-align: right;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 700;
}

.git-review-status.is-added {
  color: var(--success, #2f8b57);
}

.git-review-status.is-modified {
  color: var(--warning, #b7791f);
}

.git-review-status.is-deleted {
  color: var(--danger);
}

.git-review-stats {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 72px;
  justify-content: flex-end;
  font-size: 12px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.is-added {
  color: var(--success, #2f8b57);
}

.is-removed {
  color: var(--danger);
}

.git-review-hunks {
  background: color-mix(in srgb, var(--bg-panel) 55%, transparent);
}

.git-review-empty-hunk {
  padding: 12px 18px 12px 56px;
  color: var(--text-muted);
  font-size: 12px;
}

.git-review-hunk {
  border-top: 1px solid var(--border-ghost);
}

.git-review-hunk-head {
  padding: 7px 18px 7px 56px;
  color: var(--accent-strong);
  background: color-mix(in srgb, var(--accent-soft) 12%, transparent);
  font-family: var(--font-editor, "Cascadia Mono", "Consolas", monospace);
  font-size: 12px;
  line-height: 1.5;
}

.git-review-line {
  display: grid;
  grid-template-columns: 54px 54px 18px minmax(0, 1fr);
  min-height: 24px;
  align-items: stretch;
  font-family: var(--font-editor, "Cascadia Mono", "Consolas", monospace);
  font-size: 13px;
  line-height: 1.55;
}

.git-review-line.is-added {
  background: color-mix(in srgb, var(--success, #2f8b57) 16%, transparent);
}

.git-review-line.is-removed {
  background: color-mix(in srgb, var(--danger) 16%, transparent);
}

.git-review-gutter {
  padding: 3px 8px;
  color: var(--text-muted);
  text-align: right;
  user-select: none;
  border-right: 1px solid color-mix(in srgb, var(--text-muted) 14%, transparent);
  font-variant-numeric: tabular-nums;
}

.git-review-marker {
  padding: 3px 0;
  text-align: center;
  color: var(--text-muted);
  user-select: none;
}

.git-review-text {
  margin: 0;
  padding: 3px 18px 3px 6px;
  color: var(--text-main);
  white-space: pre-wrap;
  word-break: break-word;
  font: inherit;
}

@keyframes git-review-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
