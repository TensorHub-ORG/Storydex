<template>
  <aside class="prompt-repository-panel">
    <header class="prompt-repository-header">
      <h2 class="prompt-repository-title">指令仓库</h2>
      <button class="prompt-icon-button" type="button" title="刷新指令仓库" @click="loadRepository">
        <span class="material-symbols-rounded">refresh</span>
      </button>
    </header>

    <template v-if="selectedItem">
      <div class="prompt-detail-toolbar">
        <button class="prompt-back-button" type="button" @click="selectedId = ''">
          <span class="material-symbols-rounded">arrow_back</span>
          返回列表
        </button>
        <span class="prompt-category-badge">{{ selectedItem.category }}</span>
      </div>

      <div class="prompt-detail-scroll">
        <h3>{{ selectedItem.title }}</h3>
        <p v-if="selectedItem.summary" class="prompt-summary">{{ selectedItem.summary }}</p>

        <div v-if="selectedItem.placeholders.length" class="prompt-placeholder-section">
          <div class="prompt-section-label">可替换参数</div>
          <div class="prompt-placeholder-list">
            <span v-for="placeholder in selectedItem.placeholders" :key="placeholder" class="prompt-placeholder">
              {{ placeholder }}
            </span>
          </div>
        </div>

        <div class="prompt-actions">
          <button class="prompt-primary-action" type="button" @click="copyPrompt(selectedItem.promptText)">
            <span class="material-symbols-rounded">content_copy</span>
            复制指令
          </button>
          <button
            class="prompt-secondary-action"
            type="button"
            :disabled="workspaceStore.launchScreenVisible"
            :title="workspaceStore.launchScreenVisible ? '请先打开小说项目' : '填入右侧 Agent 输入框'"
            @click="sendToAgent(selectedItem.promptText)"
          >
            <span class="material-symbols-rounded">send</span>
            填入 Agent
          </button>
        </div>

        <div v-if="feedback" class="prompt-feedback" role="status">{{ feedback }}</div>

        <div class="prompt-section-label">指令正文</div>
        <pre class="prompt-content">{{ selectedItem.promptText }}</pre>
        <div class="prompt-source-path">来源：docs/prompts/{{ selectedItem.relativePath }}</div>
      </div>
    </template>

    <template v-else>
      <div class="prompt-search-wrap">
        <span class="material-symbols-rounded">search</span>
        <input v-model="searchQuery" type="search" placeholder="搜索用途、主题或关键词" aria-label="搜索指令" />
      </div>

      <div class="prompt-category-tabs" aria-label="指令分类">
        <button
          type="button"
          :class="{ active: selectedCategory === '' }"
          @click="selectedCategory = ''"
        >
          全部 <span>{{ items.length }}</span>
        </button>
        <button
          v-for="category in categories"
          :key="category.id"
          type="button"
          :class="{ active: selectedCategory === category.id }"
          @click="selectedCategory = category.id"
        >
          {{ category.label }} <span>{{ category.count }}</span>
        </button>
      </div>

      <div v-if="loading" class="prompt-empty-state">
        <span class="material-symbols-rounded prompt-state-icon is-loading">progress_activity</span>
        <p>正在读取 docs/prompts…</p>
      </div>
      <div v-else-if="errorMessage" class="prompt-empty-state is-error">
        <span class="material-symbols-rounded prompt-state-icon">error</span>
        <p>{{ errorMessage }}</p>
        <button type="button" @click="loadRepository">重新加载</button>
      </div>
      <div v-else-if="!filteredItems.length" class="prompt-empty-state">
        <span class="material-symbols-rounded prompt-state-icon">inventory_2</span>
        <p>没有匹配的指令模板。</p>
      </div>
      <div v-else class="prompt-list">
        <button
          v-for="item in filteredItems"
          :key="item.id"
          class="prompt-list-item"
          type="button"
          @click="selectedId = item.id"
        >
          <span class="prompt-list-icon material-symbols-rounded">prompt_suggestion</span>
          <span class="prompt-list-copy">
            <strong>{{ item.title }}</strong>
            <span>{{ item.summary || "通用小说创作指令模板" }}</span>
            <span class="prompt-list-meta">{{ item.category }} · {{ item.placeholders.length }} 个参数</span>
          </span>
          <span class="material-symbols-rounded prompt-list-arrow">chevron_right</span>
        </button>
      </div>
    </template>
  </aside>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { describeTransportError } from "@/api/client";
import { fetchPromptRepository } from "@/api/help";
import type { PromptRepositoryCategory, PromptRepositoryItem } from "@/api/help";
import { useAgentStore } from "@/stores/agent";
import { useUiStore } from "@/stores/ui";
import { useWorkspaceStore } from "@/stores/workspace";

const agentStore = useAgentStore();
const uiStore = useUiStore();
const workspaceStore = useWorkspaceStore();

const items = ref<PromptRepositoryItem[]>([]);
const categories = ref<PromptRepositoryCategory[]>([]);
const loading = ref(false);
const errorMessage = ref("");
const searchQuery = ref("");
const selectedCategory = ref("");
const selectedId = ref("");
const feedback = ref("");
let feedbackTimer: number | null = null;

const selectedItem = computed(() => items.value.find((item) => item.id === selectedId.value) || null);

const filteredItems = computed(() => {
  const query = searchQuery.value.trim().toLowerCase();
  return items.value.filter((item) => {
    if (selectedCategory.value && item.category !== selectedCategory.value) {
      return false;
    }
    if (!query) {
      return true;
    }
    return [item.title, item.summary, item.category, item.promptText]
      .join("\n")
      .toLowerCase()
      .includes(query);
  });
});

onMounted(() => {
  void loadRepository();
});

onBeforeUnmount(() => {
  if (feedbackTimer !== null) {
    window.clearTimeout(feedbackTimer);
  }
});

async function loadRepository(): Promise<void> {
  loading.value = true;
  errorMessage.value = "";
  try {
    const result = await fetchPromptRepository();
    items.value = result.data.items || [];
    categories.value = result.data.categories || [];
    if (selectedId.value && !items.value.some((item) => item.id === selectedId.value)) {
      selectedId.value = "";
    }
  } catch (error: unknown) {
    errorMessage.value = describeTransportError(error, "无法读取指令仓库。");
  } finally {
    loading.value = false;
  }
}

async function copyPrompt(text: string): Promise<void> {
  try {
    await writeClipboardText(text);
    showFeedback("指令已复制到剪贴板。");
  } catch {
    showFeedback("复制失败，请手动选择指令正文。");
  }
}

async function writeClipboardText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) {
    throw new Error("Clipboard unavailable");
  }
}

function sendToAgent(text: string): void {
  if (workspaceStore.launchScreenVisible) {
    showFeedback("请先打开一个小说项目。");
    return;
  }
  agentStore.promptInput = text;
  uiStore.setAgentCollapsed(false);
  showFeedback("指令已填入 Agent，可替换参数后发送。");
}

function showFeedback(message: string): void {
  feedback.value = message;
  if (feedbackTimer !== null) {
    window.clearTimeout(feedbackTimer);
  }
  feedbackTimer = window.setTimeout(() => {
    feedback.value = "";
    feedbackTimer = null;
  }, 2600);
}
</script>

<style scoped>
.prompt-repository-panel {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  color: var(--text-primary);
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border-subtle);
}

.prompt-repository-header {
  flex: 0 0 auto;
  min-height: 52px;
  padding: 12px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px solid var(--border-ghost);
}

.prompt-repository-title {
  margin: 0;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: var(--text-main);
}

.prompt-icon-button {
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
  font: inherit;
}

.prompt-icon-button:hover {
  color: var(--text-main);
  background: var(--bg-hover);
}

.prompt-icon-button .material-symbols-rounded {
  font-size: 17px;
}

.prompt-search-wrap {
  margin: 10px 10px 8px;
  height: 28px;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 8px;
  border: 1px solid var(--border-subtle);
  border-radius: 3px;
  background: var(--bg-input);
}

.prompt-search-wrap:focus-within {
  border-color: var(--accent);
  box-shadow: none;
}

.prompt-search-wrap .material-symbols-rounded {
  color: var(--text-muted);
  font-size: 16px;
}

.prompt-search-wrap input {
  min-width: 0;
  width: 100%;
  border: 0;
  outline: 0;
  color: var(--text-main);
  background: transparent;
  font: inherit;
  font-size: 12px;
}

.prompt-category-tabs {
  flex: 0 0 auto;
  padding: 4px 10px 9px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  border-bottom: 1px solid var(--border-ghost);
}

.prompt-category-tabs button {
  flex: 0 0 auto;
  height: 20px;
  padding: 0 7px;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  border: 1px solid var(--border-subtle);
  border-radius: 3px;
  color: var(--text-muted);
  background: var(--bg-card);
  font: inherit;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
}

.prompt-category-tabs button:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.prompt-category-tabs button.active {
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border-subtle));
  color: var(--accent-strong);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}

.prompt-category-tabs button span {
  margin-left: 2px;
  font-size: 10px;
  opacity: 0.8;
}

.prompt-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 7px;
}

.prompt-list-item {
  width: 100%;
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr) 18px;
  align-items: start;
  gap: 7px;
  padding: 10px 7px;
  border: 0;
  border-radius: 3px;
  color: inherit;
  text-align: left;
  background: transparent;
  cursor: pointer;
  font: inherit;
}

.prompt-list-item:hover {
  background: var(--bg-hover);
}

.prompt-list-icon {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: 3px;
  color: var(--accent);
  background: var(--accent-soft);
  font-size: 17px;
}

.prompt-list-copy {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.prompt-list-copy strong {
  font-size: 12px;
  line-height: 1.35;
  font-weight: 600;
}

.prompt-list-copy > span {
  display: -webkit-box;
  overflow: hidden;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.45;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.prompt-list-copy .prompt-list-meta {
  color: var(--text-faint);
  font-size: 10px;
  -webkit-line-clamp: 1;
}

.prompt-list-arrow {
  align-self: center;
  color: var(--text-muted);
  font-size: 17px;
}

.prompt-detail-toolbar {
  flex: 0 0 auto;
  min-height: 44px;
  padding: 8px 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  border-bottom: 1px solid var(--border-ghost);
}

.prompt-back-button {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 5px 7px;
  border: 0;
  border-radius: 3px;
  color: var(--text-muted);
  background: transparent;
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}

.prompt-back-button:hover {
  color: var(--text-main);
  background: var(--bg-hover);
}

.prompt-back-button .material-symbols-rounded {
  font-size: 16px;
}

.prompt-category-badge {
  padding: 0 7px;
  height: 20px;
  display: inline-flex;
  align-items: center;
  border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--border-subtle));
  border-radius: 3px;
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  color: var(--accent-strong);
  font-size: 11px;
  font-weight: 700;
}

.prompt-detail-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 12px 18px;
}

.prompt-detail-scroll h3 {
  margin: 0;
  font-size: 15px;
  line-height: 1.45;
  font-weight: 600;
}

.prompt-summary {
  margin: 8px 0 14px;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.65;
}

.prompt-section-label {
  margin: 13px 0 7px;
  color: var(--text-muted);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.prompt-placeholder-list {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}

.prompt-placeholder {
  padding: 3px 6px;
  border: 1px solid var(--border-subtle);
  border-radius: 3px;
  color: var(--text-muted);
  background: var(--bg-card);
  font-family: var(--font-mono, monospace);
  font-size: 10px;
}

.prompt-actions {
  margin-top: 14px;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 7px;
}

.prompt-primary-action,
.prompt-secondary-action {
  min-height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  border: 0;
  border-radius: 3px;
  font: inherit;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
}

.prompt-primary-action {
  color: var(--accent-contrast);
  background: var(--accent);
}

.prompt-primary-action:hover {
  background: var(--accent-strong);
}

.prompt-secondary-action {
  color: var(--text-main);
  border: 1px solid var(--border-subtle);
  background: var(--bg-card);
}

.prompt-secondary-action:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
}

.prompt-secondary-action:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.prompt-actions .material-symbols-rounded {
  font-size: 15px;
}

.prompt-feedback {
  margin-top: 8px;
  padding: 7px 8px;
  border-radius: 3px;
  color: var(--accent-strong);
  background: var(--accent-soft);
  font-size: 11px;
  line-height: 1.45;
}

.prompt-content {
  margin: 0;
  padding: 10px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  border: 1px solid var(--border-subtle);
  border-radius: 3px;
  color: var(--text-main);
  background: var(--bg-card);
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  line-height: 1.65;
}

.prompt-source-path {
  margin-top: 8px;
  color: var(--text-faint);
  font-size: 10px;
  word-break: break-all;
}

.prompt-empty-state {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 9px;
  padding: 24px;
  color: var(--text-muted);
  text-align: center;
  font-size: 11px;
}

.prompt-empty-state p {
  margin: 0;
  line-height: 1.6;
}

.prompt-empty-state button {
  padding: 5px 9px;
  border: 1px solid var(--border-subtle);
  border-radius: 3px;
  color: var(--text-main);
  background: var(--bg-card);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}

.prompt-empty-state button:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.prompt-empty-state.is-error {
  color: var(--danger, #dc2626);
}

.prompt-state-icon {
  font-size: 24px;
}

.prompt-state-icon.is-loading {
  animation: prompt-spin 1s linear infinite;
}

@keyframes prompt-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
