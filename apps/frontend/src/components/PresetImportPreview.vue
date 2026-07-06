<template>
  <section class="stip">
    <header class="stip-titlebar">
      <div class="stip-titlebar-main">
        <span class="stip-titlebar-icon material-symbols-rounded">upload_file</span>
        <span class="stip-titlebar-text">导入预设</span>
        <span class="stip-titlebar-badge">{{ items.length }}</span>
      </div>
      <button class="stip-titlebar-close" type="button" title="关闭" @click="$emit('cancel')">
        <span class="material-symbols-rounded">close</span>
      </button>
    </header>

    <div v-if="loading" class="stip-state">
      <span class="stip-spinner"></span>
      正在解析文件…
    </div>
    <div v-else-if="errorMessage" class="stip-state stip-state-error">
      <span class="material-symbols-rounded">error</span>
      {{ errorMessage }}
    </div>

    <div v-else class="stip-body">
      <!-- 左侧：文件列表 -->
      <nav class="stip-nav">
        <div class="stip-nav-label">文件</div>
        <button
          v-for="(item, index) in items"
          :key="index"
          class="stip-nav-item"
          :class="{ selected: index === selectedIndex }"
          type="button"
          :title="item.name"
          @click="selectedIndex = index"
        >
          <span class="stip-nav-item-icon material-symbols-rounded">data_object</span>
          <span class="stip-nav-item-main">
            <span class="stip-nav-item-title">{{ item.title || item.name }}</span>
            <span class="stip-nav-item-meta">{{ item.moduleCount }} 模块</span>
          </span>
        </button>
      </nav>

      <!-- 右侧：详情 -->
      <div v-if="selected" class="stip-detail">
        <div class="stip-detail-head">
          <h3 class="stip-detail-title">{{ selected.title || selected.name }}</h3>
          <p class="stip-detail-source">{{ selected.name }}</p>
          <div class="stip-chips">
            <span class="stip-chip">模块 <b>{{ selected.moduleCount }}</b></span>
            <span class="stip-chip stip-chip-ok">默认启用 <b>{{ enabledCount }}</b></span>
            <span v-if="selected.importWarnings?.length" class="stip-chip stip-chip-info">
              提示 <b>{{ selected.importWarnings.length }}</b>
            </span>
            <span v-if="selected.displayRegexes?.length" class="stip-chip">
              正则 <b>{{ selected.displayRegexes.length }}</b>
            </span>
          </div>
          <div v-if="samplingEntries.length" class="stip-sampling">
            <code v-for="entry in samplingEntries" :key="entry.key" class="stip-sampling-item">
              {{ entry.key }}={{ entry.value }}
            </code>
          </div>
        </div>

        <!-- 模块列表 -->
        <div class="stip-modules">
          <div class="stip-modules-toolbar">
            <span class="stip-section-label">模块</span>
            <div class="stip-filter">
              <span class="material-symbols-rounded">search</span>
              <input
                v-model="moduleFilter"
                class="stip-filter-input"
                type="text"
                placeholder="筛选模块…"
                spellcheck="false"
              />
            </div>
            <span class="stip-modules-count">{{ filteredModules.length }}/{{ selected.modules?.length || 0 }}</span>
          </div>
          <div class="stip-module-list">
            <div
              v-for="mod in filteredModules"
              :key="mod.id"
              class="stip-module-row"
              :class="{ disabled: !mod.enabledByDefault }"
              :title="mod.id"
            >
              <span class="stip-module-dot" :class="mod.enabledByDefault ? 'on' : 'off'"></span>
              <span class="stip-module-title">{{ mod.title || mod.id }}</span>
              <span class="stip-module-slot">{{ slotLabel(mod.slot) }}</span>
              <span v-if="!mod.enabledByDefault" class="stip-module-off">默认关</span>
            </div>
            <div v-if="!filteredModules.length" class="stip-module-empty">没有匹配的模块。</div>
          </div>
        </div>

        <!-- 折叠信息区 -->
        <div class="stip-extras">
          <details v-if="selected.importWarnings?.length" class="stip-fold">
            <summary class="stip-fold-summary">
              <span class="stip-fold-chevron material-symbols-rounded">chevron_right</span>
              宏提示
              <span class="stip-fold-count">{{ selected.importWarnings.length }}</span>
            </summary>
            <ul class="stip-fold-list">
              <li v-for="(warning, wi) in selected.importWarnings" :key="wi" class="stip-fold-item">
                {{ warning }}
              </li>
            </ul>
          </details>

          <details v-if="selected.displayRegexes?.length" class="stip-fold">
            <summary class="stip-fold-summary">
              <span class="stip-fold-chevron material-symbols-rounded">chevron_right</span>
              展示正则（仅保存为元数据）
              <span class="stip-fold-count">{{ selected.displayRegexes.length }}</span>
            </summary>
            <ul class="stip-fold-list">
              <li v-for="(regex, ri) in selected.displayRegexes" :key="ri" class="stip-fold-item stip-fold-item-row">
                <span class="stip-regex-name">{{ regex.scriptName }}</span>
                <code class="stip-regex-find">{{ regex.findRegex }}</code>
              </li>
            </ul>
          </details>

          <details v-if="selected.chatSquashMeta && Object.keys(selected.chatSquashMeta).length" class="stip-fold">
            <summary class="stip-fold-summary">
              <span class="stip-fold-chevron material-symbols-rounded">chevron_right</span>
              SPreset ChatSquash 元数据
            </summary>
            <ul class="stip-fold-list">
              <li
                v-for="(value, key) in chatsquashDisplayFields(selected.chatSquashMeta)"
                :key="key"
                class="stip-fold-item stip-fold-item-row"
              >
                <span class="stip-regex-name">{{ key }}</span>
                <code class="stip-regex-find">{{ truncate(String(value), 100) }}</code>
              </li>
            </ul>
          </details>
        </div>
      </div>
    </div>

    <footer class="stip-footer">
      <span class="stip-footer-hint">导入到 .storydex/presets/library/，之后可在列表中激活。</span>
      <div class="stip-footer-actions">
        <button class="stip-btn" type="button" :disabled="loading" @click="$emit('cancel')">取消</button>
        <button
          class="stip-btn stip-btn-primary"
          type="button"
          :disabled="loading || !!errorMessage || !items.length"
          @click="$emit('confirm')"
        >
          导入 {{ items.length }} 个预设
        </button>
      </div>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { SillyTavernPresetImportItem } from "@/api/presets";

const props = defineProps<{
  items: SillyTavernPresetImportItem[];
  loading: boolean;
  errorMessage: string;
}>();

defineEmits<{
  confirm: [];
  cancel: [];
}>();

const selectedIndex = ref(0);
const moduleFilter = ref("");

watch(
  () => props.items,
  () => {
    selectedIndex.value = 0;
    moduleFilter.value = "";
  }
);

const selected = computed(() => props.items[selectedIndex.value] ?? null);

const enabledCount = computed(
  () => (selected.value?.modules || []).filter((mod) => mod.enabledByDefault !== false).length
);

const samplingEntries = computed(() => {
  const sampling = selected.value?.sampling || {};
  return Object.entries(sampling)
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => ({ key: SAMPLING_LABELS[key] || key, value }));
});

const filteredModules = computed(() => {
  const modules = selected.value?.modules || [];
  const keyword = moduleFilter.value.trim().toLowerCase();
  if (!keyword) {
    return modules;
  }
  return modules.filter(
    (mod) =>
      (mod.title || "").toLowerCase().includes(keyword) ||
      (mod.id || "").toLowerCase().includes(keyword) ||
      (mod.slot || "").toLowerCase().includes(keyword)
  );
});

const SLOT_LABELS: Record<string, string> = {
  boundary: "硬边界",
  author_reference: "参考作家",
  language_mechanics: "语言机制",
  scene_module: "场景模块",
  negative_rules: "负面规则",
  self_check: "自检",
  advanced: "进阶",
};

const SAMPLING_LABELS: Record<string, string> = {
  temperature: "temp",
  topP: "top_p",
  topK: "top_k",
  frequencyPenalty: "freq_pen",
  presencePenalty: "pres_pen",
  seed: "seed",
};

function slotLabel(slot: string): string {
  return SLOT_LABELS[slot] || slot;
}

function truncate(text: string, max: number): string {
  return text.length > max ? text.slice(0, max) + "…" : text;
}

function chatsquashDisplayFields(meta: Record<string, unknown>): Record<string, unknown> {
  // 只展示非 squashed_post_script 的短字段，post_script 太长不默认展示
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(meta)) {
    if (key === "squashed_post_script") {
      result[key] = `[JavaScript ${String(value).length} 字符，未执行]`;
    } else if (value !== null && value !== undefined && value !== "") {
      result[key] = value;
    }
  }
  return result;
}
</script>

<style scoped>
.stip,
.stip * {
  box-sizing: border-box;
}

.stip {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--bg-sidebar);
  color: var(--text-main);
  font-size: 13px;
}

/* ---- 标题栏（VSCode 面板头） ---- */
.stip-titlebar {
  flex: 0 0 auto;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 8px 0 14px;
  border-bottom: 1px solid var(--border-subtle);
  background: var(--bg-card-muted);
}

.stip-titlebar-main {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}

.stip-titlebar-icon {
  font-size: 16px;
  color: var(--text-muted);
}

.stip-titlebar-text {
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--text-soft);
}

.stip-titlebar-badge {
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 9px;
  background: var(--accent);
  color: var(--accent-contrast);
  font-size: 10px;
  font-weight: 700;
}

.stip-titlebar-close {
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
}

.stip-titlebar-close:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}

.stip-titlebar-close .material-symbols-rounded {
  font-size: 16px;
}

/* ---- 加载 / 错误 ---- */
.stip-state {
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--text-muted);
  font-size: 13px;
}

.stip-state-error {
  color: var(--state-danger);
}

.stip-state-error .material-symbols-rounded {
  font-size: 18px;
}

.stip-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid var(--border-strong);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: stip-spin 0.8s linear infinite;
}

@keyframes stip-spin {
  to {
    transform: rotate(360deg);
  }
}

/* ---- 主体：左列表 + 右详情 ---- */
.stip-body {
  flex: 1 1 auto;
  min-height: 0;
  display: grid;
  grid-template-columns: 200px minmax(0, 1fr);
}

.stip-nav {
  min-height: 0;
  overflow-y: auto;
  border-right: 1px solid var(--border-subtle);
  background: var(--bg-card-muted);
  padding: 6px 0;
}

.stip-nav-label {
  padding: 4px 14px 6px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-faint);
}

.stip-nav-item {
  width: 100%;
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr);
  align-items: center;
  gap: 7px;
  padding: 5px 10px 5px 14px;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-align: left;
  font: inherit;
}

.stip-nav-item:hover {
  background: var(--bg-hover);
}

.stip-nav-item.selected {
  background: var(--bg-selected);
  box-shadow: inset 2px 0 0 var(--accent);
}

.stip-nav-item-icon {
  font-size: 15px;
  color: var(--text-muted);
}

.stip-nav-item.selected .stip-nav-item-icon {
  color: var(--accent-strong);
}

.stip-nav-item-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.stip-nav-item-title {
  font-size: 12px;
  color: var(--text-main);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.stip-nav-item-meta {
  font-size: 10px;
  color: var(--text-faint);
}

/* ---- 详情 ---- */
.stip-detail {
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.stip-detail-head {
  flex: 0 0 auto;
  padding: 12px 16px 10px;
  border-bottom: 1px solid var(--border-ghost);
}

.stip-detail-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-main);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.stip-detail-source {
  margin: 2px 0 0;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.stip-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.stip-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 20px;
  padding: 0 8px;
  border: 1px solid var(--border-subtle);
  border-radius: 3px;
  background: var(--bg-card);
  color: var(--text-muted);
  font-size: 11px;
}

.stip-chip b {
  color: var(--text-main);
  font-weight: 700;
}

.stip-chip-ok b {
  color: var(--state-success);
}

.stip-chip-info b {
  color: var(--state-info);
}

.stip-sampling {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.stip-sampling-item {
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--bg-code);
  color: var(--text-soft);
  font-family: var(--font-mono);
  font-size: 11px;
}

/* ---- 模块列表 ---- */
.stip-modules {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 8px 16px 0;
}

.stip-modules-toolbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
  padding-bottom: 6px;
}

.stip-section-label {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-faint);
}

.stip-filter {
  flex: 1 1 auto;
  min-width: 0;
  height: 24px;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0 6px;
  border: 1px solid var(--border-subtle);
  border-radius: 3px;
  background: var(--bg-input);
}

.stip-filter:focus-within {
  border-color: var(--accent);
}

.stip-filter .material-symbols-rounded {
  font-size: 14px;
  color: var(--text-faint);
}

.stip-filter-input {
  flex: 1 1 auto;
  min-width: 0;
  border: 0;
  background: transparent;
  color: var(--text-main);
  font-size: 12px;
  outline: none;
}

.stip-filter-input::placeholder {
  color: var(--text-faint);
}

.stip-modules-count {
  flex: 0 0 auto;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-faint);
}

.stip-module-list {
  flex: 1 1 auto;
  min-height: 80px;
  overflow-y: auto;
  border: 1px solid var(--border-ghost);
  border-radius: 3px;
  background: var(--bg-card);
}

.stip-module-row {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 24px;
  padding: 0 8px;
  font-size: 12px;
}

.stip-module-row:hover {
  background: var(--bg-hover);
}

.stip-module-row.disabled .stip-module-title {
  color: var(--text-faint);
}

.stip-module-dot {
  flex: 0 0 auto;
  width: 7px;
  height: 7px;
  border-radius: 50%;
}

.stip-module-dot.on {
  background: var(--state-success);
}

.stip-module-dot.off {
  background: var(--border-strong);
}

.stip-module-title {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--text-main);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.stip-module-slot {
  flex: 0 0 auto;
  padding: 0 6px;
  border-radius: 3px;
  background: var(--bg-code);
  color: var(--text-muted);
  font-size: 10px;
  line-height: 16px;
}

.stip-module-off {
  flex: 0 0 auto;
  color: var(--text-faint);
  font-size: 10px;
}

.stip-module-empty {
  padding: 16px;
  color: var(--text-faint);
  font-size: 12px;
  text-align: center;
}

/* ---- 折叠区 ---- */
.stip-extras {
  flex: 0 0 auto;
  max-height: 32%;
  overflow-y: auto;
  padding: 6px 16px 10px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.stip-fold-summary {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 24px;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-soft);
  cursor: pointer;
  user-select: none;
  list-style: none;
}

.stip-fold-summary::-webkit-details-marker {
  display: none;
}

.stip-fold-summary:hover {
  color: var(--text-main);
}

.stip-fold-chevron {
  font-size: 16px;
  color: var(--text-faint);
  transition: transform 0.12s ease;
}

.stip-fold[open] .stip-fold-chevron {
  transform: rotate(90deg);
}

.stip-fold-count {
  padding: 0 5px;
  border-radius: 8px;
  background: var(--bg-hover);
  color: var(--text-muted);
  font-size: 10px;
  font-weight: 700;
}

.stip-fold-list {
  list-style: none;
  margin: 2px 0 6px;
  padding: 0 0 0 20px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.stip-fold-item {
  font-size: 11px;
  color: var(--text-muted);
  font-family: var(--font-mono);
  word-break: break-all;
}

.stip-fold-item-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.stip-regex-name {
  flex: 0 0 auto;
  color: var(--text-soft);
  font-family: var(--font-ui);
}

.stip-regex-find {
  flex: 1;
  min-width: 0;
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- 底栏 ---- */
.stip-footer {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 16px;
  border-top: 1px solid var(--border-subtle);
  background: var(--bg-card-muted);
}

.stip-footer-hint {
  min-width: 0;
  font-size: 11px;
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.stip-footer-actions {
  flex: 0 0 auto;
  display: inline-flex;
  gap: 8px;
}

.stip-btn {
  height: 26px;
  padding: 0 14px;
  border: 1px solid var(--border-strong);
  border-radius: 3px;
  background: var(--bg-card);
  color: var(--text-soft);
  font-size: 12px;
  cursor: pointer;
}

.stip-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-main);
}

.stip-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.stip-btn-primary {
  border-color: transparent;
  background: var(--accent);
  color: var(--accent-contrast);
  font-weight: 600;
}

.stip-btn-primary:hover:not(:disabled) {
  background: var(--accent-strong);
  color: var(--accent-contrast);
}
</style>
