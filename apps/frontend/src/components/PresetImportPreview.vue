<template>
  <section class="preset-import-preview">
    <header class="preset-import-preview-header">
      <div class="preset-import-preview-title-block">
        <h2 class="preset-import-preview-title">导入预览</h2>
        <p class="preset-import-preview-subtitle">
          将导入 {{ items.length }} 个预设，请确认解析结果
        </p>
      </div>
      <button
        class="preset-import-preview-close"
        type="button"
        title="关闭"
        @click="$emit('cancel')"
      >
        <span class="material-symbols-rounded">close</span>
      </button>
    </header>

    <div class="preset-import-preview-body">
      <p v-if="loading" class="preset-import-preview-loading">正在解析文件…</p>
      <p v-if="errorMessage" class="preset-import-preview-error">{{ errorMessage }}</p>

      <section
        v-for="(item, index) in items"
        :key="index"
        class="preset-import-preview-card"
      >
        <div class="preset-import-preview-card-header">
          <h3 class="preset-import-preview-card-title">{{ item.title || item.name }}</h3>
          <span class="preset-import-preview-card-meta">
            模块 {{ item.moduleCount }} · 宏警告 {{ item.importWarnings?.length || 0 }}
          </span>
        </div>

        <div v-if="item.sampling && hasSamplingValues(item.sampling)" class="preset-import-preview-sampling">
          <span class="preset-import-preview-sampling-label">采样参数:</span>
          <span
            v-for="(value, key) in item.sampling"
            :key="key"
            v-show="value !== null && value !== undefined"
            class="preset-import-preview-sampling-item"
          >
            {{ samplingKeyLabel(String(key)) }}={{ value }}
          </span>
        </div>

        <details class="preset-import-preview-details">
          <summary class="preset-import-preview-summary">
            模块列表 ({{ item.modules?.length || 0 }})
          </summary>
          <ul class="preset-import-preview-module-list">
            <li
              v-for="mod in item.modules || []"
              :key="mod.id"
              class="preset-import-preview-module-item"
            >
              <span class="preset-import-preview-module-id">{{ mod.id }}</span>
              <span class="preset-import-preview-module-slot">{{ slotLabel(mod.slot) }}</span>
              <span class="preset-import-preview-module-priority">priority {{ mod.priority }}</span>
              <span
                v-if="!mod.enabledByDefault"
                class="preset-import-preview-module-off"
              >默认关</span>
            </li>
          </ul>
        </details>

        <details
          v-if="item.importWarnings && item.importWarnings.length"
          class="preset-import-preview-details preset-import-preview-details-warning"
        >
          <summary class="preset-import-preview-summary">
            宏警告 ({{ item.importWarnings.length }})
          </summary>
          <ul class="preset-import-preview-warning-list">
            <li
              v-for="(warning, wIndex) in item.importWarnings.slice(0, 20)"
              :key="wIndex"
              class="preset-import-preview-warning-item"
            >
              ⚠ {{ warning }}
            </li>
            <li v-if="item.importWarnings.length > 20" class="preset-import-preview-warning-more">
              …还有 {{ item.importWarnings.length - 20 }} 条
            </li>
          </ul>
        </details>

        <details
          v-if="item.displayRegexes && item.displayRegexes.length"
          class="preset-import-preview-details"
        >
          <summary class="preset-import-preview-summary">
            展示正则 ({{ item.displayRegexes.length }})
          </summary>
          <ul class="preset-import-preview-regex-list">
            <li
              v-for="(regex, rIndex) in item.displayRegexes"
              :key="rIndex"
              class="preset-import-preview-regex-item"
            >
              <span class="preset-import-preview-regex-name">{{ regex.scriptName }}</span>
              <code class="preset-import-preview-regex-find">{{ regex.findRegex }}</code>
            </li>
          </ul>
        </details>

        <details
          v-if="item.chatSquashMeta && Object.keys(item.chatSquashMeta).length"
          class="preset-import-preview-details"
        >
          <summary class="preset-import-preview-summary">SPreset ChatSquash 元数据</summary>
          <div class="preset-import-preview-chatsquash">
            <p
              v-for="(value, key) in chatsquashDisplayFields(item.chatSquashMeta)"
              :key="key"
              class="preset-import-preview-chatsquash-row"
            >
              <span class="preset-import-preview-chatsquash-key">{{ key }}:</span>
              <span class="preset-import-preview-chatsquash-value">{{ truncate(String(value), 80) }}</span>
            </p>
          </div>
        </details>
      </section>
    </div>

    <footer class="preset-import-preview-footer">
      <button
        class="preset-import-preview-btn preset-import-preview-btn-ghost"
        type="button"
        :disabled="loading"
        @click="$emit('cancel')"
      >
        取消
      </button>
      <button
        class="preset-import-preview-btn preset-import-preview-btn-primary"
        type="button"
        :disabled="loading || !!errorMessage || !items.length"
        @click="$emit('confirm')"
      >
        确认导入
      </button>
    </footer>
  </section>
</template>

<script setup lang="ts">
import type { SillyTavernPresetImportItem, SamplingParams } from "@/api/presets";

defineProps<{
  items: SillyTavernPresetImportItem[];
  loading: boolean;
  errorMessage: string;
}>();

defineEmits<{
  confirm: [];
  cancel: [];
}>();

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

function samplingKeyLabel(key: string): string {
  return SAMPLING_LABELS[key] || key;
}

function hasSamplingValues(sampling: SamplingParams): boolean {
  return Object.values(sampling).some((v) => v !== null && v !== undefined);
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
.preset-import-preview {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-sidebar);
}

.preset-import-preview-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 20px 12px;
  border-bottom: 1px solid var(--border-subtle);
}

.preset-import-preview-title-block {
  flex: 1;
  min-width: 0;
}

.preset-import-preview-title {
  margin: 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--text-main);
}

.preset-import-preview-subtitle {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--text-muted);
}

.preset-import-preview-close {
  flex: 0 0 auto;
  background: transparent;
  border: 0;
  color: var(--text-muted);
  cursor: pointer;
  border-radius: 4px;
  padding: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.preset-import-preview-close:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}

.preset-import-preview-body {
  flex: 1 1 auto;
  overflow-y: auto;
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.preset-import-preview-loading {
  color: var(--text-muted);
  font-size: 13px;
  text-align: center;
  padding: 24px 0;
}

.preset-import-preview-error {
  color: var(--state-danger);
  font-size: 13px;
  padding: 8px 12px;
  background: rgba(192, 63, 54, 0.08);
  border-radius: 6px;
  margin: 0;
}

.preset-import-preview-card {
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 12px 14px;
  background: var(--bg-card);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.preset-import-preview-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.preset-import-preview-card-title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-main);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.preset-import-preview-card-meta {
  flex: 0 0 auto;
  font-size: 11px;
  color: var(--text-muted);
}

.preset-import-preview-sampling {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 11px;
  color: var(--text-soft);
}

.preset-import-preview-sampling-label {
  color: var(--text-muted);
}

.preset-import-preview-sampling-item {
  font-family: var(--font-mono);
}

.preset-import-preview-details {
  border-top: 1px solid var(--border-ghost);
  padding-top: 8px;
}

.preset-import-preview-details-warning summary {
  color: var(--state-warning);
}

.preset-import-preview-summary {
  cursor: pointer;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-soft);
  user-select: none;
}

.preset-import-preview-summary:hover {
  color: var(--text-main);
}

.preset-import-preview-module-list,
.preset-import-preview-warning-list,
.preset-import-preview-regex-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.preset-import-preview-module-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-soft);
}

.preset-import-preview-module-id {
  color: var(--text-main);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.preset-import-preview-module-slot {
  flex: 0 0 auto;
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--bg-hover);
  color: var(--text-soft);
  font-family: var(--font-ui);
}

.preset-import-preview-module-priority {
  flex: 0 0 auto;
  color: var(--text-faint);
}

.preset-import-preview-module-off {
  flex: 0 0 auto;
  padding: 1px 4px;
  border-radius: 3px;
  background: rgba(var(--state-warning-rgb, 186, 107, 29), 0.12);
  color: var(--state-warning);
  font-family: var(--font-ui);
}

.preset-import-preview-warning-item {
  font-size: 11px;
  color: var(--state-warning);
  font-family: var(--font-mono);
  word-break: break-all;
}

.preset-import-preview-warning-more {
  font-size: 11px;
  color: var(--text-muted);
  font-style: italic;
}

.preset-import-preview-regex-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
}

.preset-import-preview-regex-name {
  flex: 0 0 auto;
  color: var(--text-main);
}

.preset-import-preview-regex-find {
  flex: 1;
  min-width: 0;
  font-family: var(--font-mono);
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.preset-import-preview-chatsquash {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 8px;
}

.preset-import-preview-chatsquash-row {
  display: flex;
  gap: 8px;
  font-size: 11px;
  margin: 0;
}

.preset-import-preview-chatsquash-key {
  flex: 0 0 auto;
  color: var(--text-muted);
  font-family: var(--font-mono);
}

.preset-import-preview-chatsquash-value {
  color: var(--text-soft);
  word-break: break-all;
}

.preset-import-preview-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid var(--border-subtle);
}

.preset-import-preview-btn {
  padding: 6px 16px;
  border-radius: 6px;
  font-size: 13px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: background-color 0.2s, border-color 0.2s, color 0.2s;
}

.preset-import-preview-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.preset-import-preview-btn-ghost {
  background: transparent;
  border-color: var(--border-subtle);
  color: var(--text-soft);
}

.preset-import-preview-btn-ghost:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-main);
}

.preset-import-preview-btn-primary {
  background: var(--accent);
  color: var(--accent-contrast);
}

.preset-import-preview-btn-primary:hover:not(:disabled) {
  background: var(--accent-strong);
}
</style>
