<template>
  <aside class="search-panel">
    <header class="search-head">
      <div class="search-head-copy">
        <h2 class="search-title">搜索</h2>
        <p class="search-subtitle" :title="projectLabel">{{ projectLabel }}</p>
      </div>
    </header>
    <form class="search-form" role="search" @submit.prevent="runSearch">
      <div class="search-input-row">
        <span class="material-symbols-rounded">search</span>
        <input ref="inputRef" v-model="query" type="text" placeholder="搜索文件名或文件内容" aria-label="搜索文件名或文件内容" @input="scheduleSearch" />
        <button v-if="query" type="button" title="清除" aria-label="清除搜索" @click="clearSearch"><span class="material-symbols-rounded">close</span></button>
      </div>
      <div class="search-options">
        <label class="search-option" :class="{ 'is-on': matchCase }">
          <input v-model="matchCase" type="checkbox" @change="scheduleSearch" />
          <span class="material-symbols-rounded">match_case</span>
          <span>区分大小写</span>
        </label>
        <label class="search-option" :class="{ 'is-on': fileNamesOnly }">
          <input v-model="fileNamesOnly" type="checkbox" @change="scheduleSearch" />
          <span class="material-symbols-rounded">draft</span>
          <span>仅文件名</span>
        </label>
      </div>
    </form>
    <div v-if="loading && !results.length" class="search-state">
      <span class="material-symbols-rounded search-state-icon is-spinning">progress_activity</span>
      <p>正在搜索…</p>
    </div>
    <div v-else-if="error" class="search-state is-error">
      <span class="material-symbols-rounded search-state-icon">error</span>
      <p class="search-state-title">搜索失败</p>
      <p>{{ error }}</p>
    </div>
    <div v-else-if="!trimmedQuery" class="search-state">
      <span class="material-symbols-rounded search-state-icon">manage_search</span>
      <p class="search-state-title">在项目里查找</p>
      <p>同时匹配文件名和正文内容，点击结果即可打开文件。</p>
    </div>
    <div v-else-if="!results.length" class="search-state">
      <span class="material-symbols-rounded search-state-icon">search_off</span>
      <p class="search-state-title">没有匹配结果</p>
      <p>{{ fileNamesOnly ? "当前只匹配文件名，可以取消“仅文件名”再试。" : "换个关键词再试试。" }}</p>
    </div>
    <template v-else>
      <div class="search-summary">
        <span>{{ resultSummary }}</span>
        <span v-if="loading" class="search-summary-busy">更新中…</span>
      </div>
      <div class="search-results">
        <button
          v-for="item in results"
          :key="`${item.relativePath}:${item.lineNumber || 0}`"
          type="button"
          :title="item.relativePath"
          @click="openResult(item.relativePath)"
        >
          <span class="search-file">
            <span class="material-symbols-rounded">{{ fileIcon(item.relativePath) }}</span>
            <span class="search-file-name"><template v-for="(part, index) in highlightSegments(fileName(item.relativePath))" :key="index"><mark v-if="part.hit">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></span>
            <span v-if="item.lineNumber" class="search-line">第 {{ item.lineNumber }} 行</span>
          </span>
          <span class="search-path">{{ fileDirectory(item.relativePath) || "项目根目录" }}</span>
          <span v-if="item.snippet" class="search-snippet"><template v-for="(part, index) in highlightSegments(item.snippet.trim())" :key="index"><mark v-if="part.hit">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></span>
        </button>
      </div>
    </template>
  </aside>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { searchWorkspace } from "@/api/workspace";
import { useWorkspaceStore } from "@/stores/workspace";
import type { WorkspaceSearchItem, WorkspaceTreeNode } from "@/types/workspace";

const RESULT_LIMIT = 50;

const workspaceStore = useWorkspaceStore();
const query = ref("");
const loading = ref(false);
const error = ref("");
const contentResults = ref<WorkspaceSearchItem[]>([]);
const matchCase = ref(false);
const fileNamesOnly = ref(false);
const inputRef = ref<HTMLInputElement | null>(null);
let timer: number | null = null;

const projectLabel = computed(() => workspaceStore.projectLabel || "未打开项目");
const trimmedQuery = computed(() => query.value.trim());
const fileResults = computed(() => {
  const needle = matchCase.value ? query.value.trim() : query.value.trim().toLowerCase();
  if (!needle) return [];
  return flattenFiles(workspaceStore.tree).filter((node) => {
    const path = node.relativePath || "";
    return (matchCase.value ? path : path.toLowerCase()).includes(needle);
  }).map((node): WorkspaceSearchItem => ({ relativePath: node.relativePath || "", engine: "filename" }));
});
const uniqueResults = computed(() => {
  const source = fileNamesOnly.value ? fileResults.value : [...fileResults.value, ...contentResults.value];
  const seen = new Set<string>();
  return source.filter((item) => item.relativePath && !seen.has(item.relativePath) && seen.add(item.relativePath));
});
const results = computed(() => uniqueResults.value.slice(0, RESULT_LIMIT));
const resultSummary = computed(() => (
  uniqueResults.value.length > RESULT_LIMIT
    ? `${uniqueResults.value.length} 个文件匹配，显示前 ${RESULT_LIMIT} 个`
    : `${results.value.length} 个文件匹配`
));

onMounted(() => { nextTick(() => inputRef.value?.focus()); });
onBeforeUnmount(() => { if (timer !== null) window.clearTimeout(timer); });

function scheduleSearch(): void {
  if (timer !== null) window.clearTimeout(timer);
  if (fileNamesOnly.value) {
    contentResults.value = [];
    loading.value = false;
    error.value = "";
    return;
  }
  timer = window.setTimeout(runSearch, 220);
}
async function runSearch(): Promise<void> {
  if (!query.value.trim() || fileNamesOnly.value) {
    contentResults.value = [];
    loading.value = false;
    return;
  }
  loading.value = true; error.value = "";
  try { contentResults.value = (await searchWorkspace(query.value.trim(), 40)).data.items || []; }
  catch (reason) { error.value = reason instanceof Error ? reason.message : "搜索失败"; }
  finally { loading.value = false; }
}
function clearSearch(): void { query.value = ""; contentResults.value = []; error.value = ""; inputRef.value?.focus(); }
function openResult(path: string): void { void workspaceStore.openFile(path); }
function fileName(path: string): string { return path.split("/").pop() || path; }
function fileDirectory(path: string): string {
  const index = path.lastIndexOf("/");
  return index > 0 ? path.slice(0, index) : "";
}
function fileIcon(path: string): string {
  return path.toLowerCase().endsWith(".json") ? "data_object" : "description";
}
/** 把文本切成「命中 / 未命中」片段，用于高亮关键词；按当前大小写选项匹配。 */
function highlightSegments(text: string): Array<{ text: string; hit: boolean }> {
  const needle = trimmedQuery.value;
  if (!needle || !text) return [{ text, hit: false }];
  const haystack = matchCase.value ? text : text.toLowerCase();
  const target = matchCase.value ? needle : needle.toLowerCase();
  const parts: Array<{ text: string; hit: boolean }> = [];
  let cursor = 0;
  let index = haystack.indexOf(target);
  while (index !== -1) {
    if (index > cursor) parts.push({ text: text.slice(cursor, index), hit: false });
    parts.push({ text: text.slice(index, index + target.length), hit: true });
    cursor = index + target.length;
    index = haystack.indexOf(target, cursor);
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor), hit: false });
  return parts;
}
function flattenFiles(nodes: WorkspaceTreeNode[], result: WorkspaceTreeNode[] = []): WorkspaceTreeNode[] {
  for (const node of nodes) { if (node.kind === "file") result.push(node); if (node.children?.length) flattenFiles(node.children, result); }
  return result;
}
</script>

<style scoped>
.search-panel { height: 100%; min-height: 0; display: flex; flex-direction: column; background: var(--canvas); color: var(--text-main); }

/* 侧栏统一页头：页面名为主标题，项目名为副标题 */
.search-head { flex: 0 0 auto; min-height: 52px; display: flex; align-items: center; padding: 10px 10px 8px 14px; }
.search-head-copy { min-width: 0; }
.search-title { margin: 0; color: var(--text-main); font-size: 13px; font-weight: 600; line-height: 1.35; }
.search-subtitle { margin: 2px 0 0; overflow: hidden; color: var(--text-muted); font-size: 11px; line-height: 1.4; white-space: nowrap; text-overflow: ellipsis; }

.search-form { flex: 0 0 auto; padding: 0 10px 10px; }
.search-input-row { height: 32px; display: flex; align-items: center; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--surface-1); transition: border-color 140ms ease, box-shadow 140ms ease; }
.search-input-row:hover { border-color: var(--border-strong); }
.search-input-row:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
.search-input-row > .material-symbols-rounded { padding-left: 9px; font-size: 17px; color: var(--text-faint); }
.search-input-row input { min-width: 0; flex: 1; border: 0; outline: 0; background: transparent; color: inherit; padding: 0 8px; font: inherit; font-size: 13px; }
.search-input-row input::placeholder { color: var(--text-faint); }
.search-input-row button { width: 24px; height: 24px; margin-right: 4px; display: grid; place-items: center; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-muted); cursor: pointer; }
.search-input-row button:hover { background: var(--bg-hover); color: var(--text-main); }
.search-input-row button .material-symbols-rounded { font-size: 15px; }

/* 选项做成可切换的小胶囊；保留原生 checkbox 供键盘与读屏使用 */
.search-options { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding-top: 8px; }
.search-option { position: relative; height: 24px; display: inline-flex; align-items: center; gap: 4px; padding: 0 9px 0 7px; border: 1px solid var(--border-subtle); border-radius: var(--radius-full); background: transparent; color: var(--text-muted); font-size: 11px; font-weight: 500; cursor: pointer; user-select: none; transition: background 140ms ease, border-color 140ms ease, color 140ms ease; }
.search-option input { position: absolute; width: 1px; height: 1px; margin: 0; opacity: 0; pointer-events: none; }
.search-option .material-symbols-rounded { font-size: 14px; }
.search-option:hover { border-color: var(--border-strong); background: var(--bg-hover); color: var(--text-main); }
.search-option:focus-within { box-shadow: 0 0 0 1px var(--accent), 0 0 0 3px var(--accent-soft); }
.search-option.is-on { border-color: color-mix(in srgb, var(--accent) 40%, var(--border-subtle)); background: var(--accent-soft); color: var(--accent-strong); }

.search-summary { flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 4px 14px 6px; color: var(--text-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.search-summary-busy { color: var(--text-faint); }

.search-results { overflow: auto; flex: 1; min-height: 0; padding: 0 6px 12px; }
.search-results > button { width: 100%; display: grid; gap: 2px; margin-bottom: 1px; padding: 7px 8px 8px; border: 0; border-radius: var(--radius-md); background: transparent; color: inherit; text-align: left; font: inherit; cursor: pointer; transition: background 120ms ease; }
.search-results > button:hover { background: var(--bg-hover); }
.search-results > button:focus-visible { outline: none; box-shadow: 0 0 0 1px var(--accent), 0 0 0 3px var(--accent-soft); }
.search-file { min-width: 0; display: flex; align-items: center; gap: 6px; }
.search-file > .material-symbols-rounded { flex: 0 0 auto; color: var(--text-muted); font-size: 15px; }
.search-file-name { min-width: 0; flex: 1 1 auto; overflow: hidden; color: var(--text-main); font-size: 13px; font-weight: 500; white-space: nowrap; text-overflow: ellipsis; }
.search-line { flex: 0 0 auto; padding: 0 6px; border-radius: var(--radius-full); background: var(--bg-hover); color: var(--text-muted); font-size: 10px; line-height: 16px; font-variant-numeric: tabular-nums; }
.search-path { overflow: hidden; padding-left: 21px; color: var(--text-faint); font-size: 11px; white-space: nowrap; text-overflow: ellipsis; }
.search-snippet { display: -webkit-box; overflow: hidden; margin-top: 2px; padding-left: 21px; color: var(--text-soft); font-size: 12px; line-height: 1.55; -webkit-line-clamp: 2; -webkit-box-orient: vertical; word-break: break-all; }
.search-results mark { border-radius: 2px; background: color-mix(in srgb, var(--warning-fg) 22%, transparent); color: inherit; font-weight: 600; }

.search-state { display: grid; justify-items: center; gap: 6px; padding: 36px 20px; color: var(--text-muted); font-size: 12px; line-height: 1.6; text-align: center; }
.search-state p { margin: 0; max-width: 240px; }
.search-state .search-state-title { color: var(--text-main); font-size: 13px; font-weight: 600; }
.search-state-icon { width: 40px; height: 40px; display: grid; place-items: center; margin-bottom: 4px; border-radius: var(--radius-full); background: var(--surface-1); box-shadow: var(--shadow-resting); color: var(--text-faint); font-size: 22px; }
.search-state-icon.is-spinning { animation: search-spin 1s linear infinite; }
.search-state.is-error .search-state-icon { background: var(--danger-bg); color: var(--danger-fg); }
@keyframes search-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .search-state-icon.is-spinning { animation: none; } }
</style>
