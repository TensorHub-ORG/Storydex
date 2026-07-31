<template>
  <aside class="search-panel">
    <header class="search-head"><strong>搜索</strong></header>
    <form class="search-form" @submit.prevent="runSearch">
      <div class="search-input-row">
        <span class="material-symbols-rounded">search</span>
        <input ref="inputRef" v-model="query" type="text" placeholder="搜索文件名或文件内容" @input="scheduleSearch" />
        <button v-if="query" type="button" title="清除" @click="clearSearch"><span class="material-symbols-rounded">close</span></button>
      </div>
      <div class="search-options">
        <label class="search-option"><input v-model="matchCase" type="checkbox" @change="scheduleSearch" /><span>区分大小写</span></label>
        <label class="search-option"><input v-model="fileNamesOnly" type="checkbox" @change="scheduleSearch" /><span>仅文件名</span></label>
      </div>
    </form>
    <div v-if="loading" class="search-state">正在搜索...</div>
    <div v-else-if="error" class="search-state is-error">{{ error }}</div>
    <div v-else-if="query && !results.length" class="search-state">没有匹配结果</div>
    <div v-else class="search-results">
      <button v-for="item in results" :key="`${item.relativePath}:${item.lineNumber || 0}`" type="button" @click="openResult(item.relativePath)">
        <span class="search-file"><span class="material-symbols-rounded">description</span>{{ fileName(item.relativePath) }}</span>
        <span class="search-path">{{ item.relativePath }}<template v-if="item.lineNumber">:{{ item.lineNumber }}</template></span>
        <span v-if="item.snippet" class="search-snippet">{{ item.snippet }}</span>
      </button>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { searchWorkspace } from "@/api/workspace";
import { useWorkspaceStore } from "@/stores/workspace";
import type { WorkspaceSearchItem, WorkspaceTreeNode } from "@/types/workspace";

const workspaceStore = useWorkspaceStore();
const query = ref("");
const loading = ref(false);
const error = ref("");
const contentResults = ref<WorkspaceSearchItem[]>([]);
const matchCase = ref(false);
const fileNamesOnly = ref(false);
const inputRef = ref<HTMLInputElement | null>(null);
let timer: number | null = null;

const fileResults = computed(() => {
  const needle = matchCase.value ? query.value.trim() : query.value.trim().toLowerCase();
  if (!needle) return [];
  return flattenFiles(workspaceStore.tree).filter((node) => {
    const path = node.relativePath || "";
    return (matchCase.value ? path : path.toLowerCase()).includes(needle);
  }).map((node): WorkspaceSearchItem => ({ relativePath: node.relativePath || "", engine: "filename" }));
});
const results = computed(() => {
  const source = fileNamesOnly.value ? fileResults.value : [...fileResults.value, ...contentResults.value];
  const seen = new Set<string>();
  return source.filter((item) => item.relativePath && !seen.has(item.relativePath) && seen.add(item.relativePath)).slice(0, 50);
});

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
function flattenFiles(nodes: WorkspaceTreeNode[], result: WorkspaceTreeNode[] = []): WorkspaceTreeNode[] {
  for (const node of nodes) { if (node.kind === "file") result.push(node); if (node.children?.length) flattenFiles(node.children, result); }
  return result;
}
</script>

<style scoped>
.search-panel { height: 100%; min-height: 0; display: flex; flex-direction: column; background: var(--bg-panel); color: var(--text-main); }
.search-head { height: 42px; display: flex; align-items: center; padding: 0 14px; border-bottom: 1px solid var(--border-ghost); font-size: 12px; text-transform: uppercase; }
.search-form { padding: 10px; border-bottom: 1px solid var(--border-ghost); }
.search-input-row { height: 32px; display: flex; align-items: center; border: 1px solid var(--border-subtle); background: var(--bg-editor); }
.search-input-row:focus-within { border-color: var(--accent); }
.search-input-row .material-symbols-rounded { padding-left: 7px; font-size: 17px; color: var(--text-muted); }
.search-input-row input { min-width: 0; flex: 1; border: 0; outline: 0; background: transparent; color: inherit; padding: 0 7px; }
.search-input-row button { width: 28px; height: 28px; border: 0; background: transparent; cursor: pointer; }
.search-input-row button .material-symbols-rounded { padding: 0; }
.search-options { display: flex; align-items: center; gap: 14px; padding-top: 9px; color: var(--text-muted); font-size: 11px; }
.search-option { display: inline-flex; align-items: center; gap: 6px; min-height: 18px; cursor: pointer; }
.search-option input { width: 15px; height: 15px; margin: 0; accent-color: var(--accent); }
.search-results { overflow: auto; flex: 1; }
.search-results > button { width: 100%; border: 0; border-bottom: 1px solid var(--border-ghost); background: transparent; color: inherit; padding: 8px 12px; text-align: left; cursor: pointer; display: grid; gap: 3px; }
.search-results > button:hover { background: var(--bg-hover); }
.search-file { display: flex; align-items: center; gap: 5px; font-size: 13px; font-weight: 600; }
.search-file .material-symbols-rounded { font-size: 15px; }
.search-path, .search-snippet { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); font-size: 11px; }
.search-snippet { color: var(--text-secondary); }
.search-state { padding: 16px 12px; color: var(--text-muted); font-size: 12px; }
.search-state.is-error { color: var(--danger); }
</style>
