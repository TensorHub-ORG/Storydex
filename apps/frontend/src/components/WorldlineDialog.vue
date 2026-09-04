<template>
  <div v-if="view.mode" class="wld-backdrop" role="presentation" @click.self="emit('cancel')">
    <div class="wld" role="dialog" aria-modal="true" :aria-label="title">
      <h3 class="wld-title">
        <span class="material-symbols-rounded" :class="{ 'is-danger': view.mode === 'delete' }">{{ icon }}</span>
        <span>{{ title }}</span>
      </h3>

      <p class="wld-body">{{ body }}</p>

      <template v-if="needsInput">
        <label class="wld-label" :for="inputId">{{ inputLabel }}</label>
        <input
          :id="inputId"
          ref="inputRef"
          class="wld-input"
          :value="view.input"
          :placeholder="placeholder"
          :disabled="submitting"
          @input="emit('update:input', ($event.target as HTMLInputElement).value)"
          @keydown.enter.prevent="emit('confirm')"
          @keydown.esc.prevent="emit('cancel')"
        />
        <p v-if="hint" class="wld-hint">{{ hint }}</p>
      </template>

      <p v-if="view.mode === 'delete'" class="wld-danger">
        这条世界线独有的
        <strong>{{ view.exclusiveCommits }}</strong>
        个版本会被永久丢弃，无法恢复。Storydex 的世界线不会合并，所以这些内容不存在于任何其它线上。
      </p>

      <p v-if="view.error" class="wld-error">{{ view.error }}</p>

      <div class="wld-actions">
        <button class="wld-btn" type="button" :disabled="submitting" @click="emit('cancel')">取消</button>
        <button
          class="wld-btn is-primary"
          :class="{ 'is-danger': view.mode === 'delete' }"
          type="button"
          :disabled="submitting"
          @click="emit('confirm')"
        >
          {{ submitting ? "处理中…" : confirmLabel }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import type { WorldlineDialogState } from "@/composables/useWorldlineActions";

const props = defineProps<{
  state?: WorldlineDialogState;
  submitting?: boolean;
}>();

const emit = defineEmits<{
  (e: "confirm"): void;
  (e: "cancel"): void;
  (e: "update:input", value: string): void;
}>();

const inputRef = ref<HTMLInputElement | null>(null);
const inputId = "worldline-dialog-input";

/**
 * `state` 由调用方的 composable 提供，但组件不能因为它缺失就崩：对话框在没有
 * state 的时候本来就该什么都不渲染。
 */
const view = computed<WorldlineDialogState>(
  () =>
    props.state || {
      mode: "",
      commitId: "",
      worldlineName: "",
      input: "",
      exclusiveCommits: 0,
      nodeLabel: "",
      error: ""
    }
);

const needsInput = computed(() =>
  view.value.mode === "fork" || view.value.mode === "rename" || view.value.mode === "jump-dirty"
);

const title = computed(() => {
  switch (view.value.mode) {
    case "fork":
      return "开辟一条新世界线";
    case "rename":
      return "给世界线改名";
    case "delete":
      return "删除世界线";
    case "jump-dirty":
      return "还有没提交的改动";
    default:
      return "";
  }
});

const icon = computed(() => {
  switch (view.value.mode) {
    case "fork":
      return "alt_route";
    case "rename":
      return "edit";
    case "delete":
      return "warning";
    case "jump-dirty":
      return "save";
    default:
      return "help";
  }
});

const body = computed(() => {
  switch (view.value.mode) {
    case "fork":
      return `从「${view.value.nodeLabel}」分出一条新的世界线。此后写的内容只会留在这条线上，原来那条线保持原样。`;
    case "rename":
      return `重命名世界线「${view.value.worldlineName}」。改名只影响显示，节点内容不受影响。`;
    case "delete":
      return `即将删除世界线「${view.value.worldlineName}」。`;
    case "jump-dirty":
      return `工作区里还有没提交的改动。先把它们提交成当前世界线上的一个节点，再跳转到「${view.value.nodeLabel}」，这样改动不会丢。`;
    default:
      return "";
  }
});

const inputLabel = computed(() =>
  view.value.mode === "jump-dirty" ? "这次改动的说明" : "世界线名称"
);

const placeholder = computed(() => {
  if (view.value.mode === "jump-dirty") return "留空会自动生成一条带时间的说明";
  if (view.value.mode === "rename") return view.value.worldlineName;
  return "例如 alt/dark-ending";
});

const hint = computed(() =>
  view.value.mode === "jump-dirty"
    ? ""
    : "目前只支持英文字母、数字和 . _ - / ；用 / 可以分组，例如 alt/dark-ending。"
);

const confirmLabel = computed(() => {
  switch (view.value.mode) {
    case "fork":
      return "开辟世界线";
    case "rename":
      return "保存新名字";
    case "delete":
      return "确认永久删除";
    case "jump-dirty":
      return "提交并跳转";
    default:
      return "确定";
  }
});

watch(
  () => view.value.mode,
  (mode) => {
    if (!mode || !needsInput.value) return;
    void nextTick(() => {
      inputRef.value?.focus();
      inputRef.value?.select();
    });
  },
  { immediate: true }
);

defineExpose({
  __testUtils: import.meta.env.MODE === "test" ? {
    needsInput, title, icon, body, inputLabel, placeholder, hint, confirmLabel
  } : null
});
</script>

<style scoped>
.wld-backdrop {
  position: fixed;
  inset: 0;
  z-index: 900;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(0, 0, 0, 0.42);
}

.wld {
  width: min(420px, 100%);
  padding: 18px 20px 16px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--bg-elevated);
  color: var(--text-main);
  box-shadow: var(--shadow-modal);
}

.wld-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 700;
}

.wld-title .material-symbols-rounded {
  font-size: 19px;
  color: var(--accent);
}

.wld-title .material-symbols-rounded.is-danger {
  color: var(--danger);
}

.wld-body {
  margin: 0 0 12px;
  color: var(--text-soft);
  font-size: 12px;
  line-height: 1.75;
}

.wld-label {
  display: block;
  margin-bottom: 5px;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 600;
}

.wld-input {
  width: 100%;
  height: 32px;
  padding: 0 9px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
}

.wld-input:focus {
  outline: none;
  border-color: var(--accent);
}

.wld-hint {
  margin: 6px 0 0;
  color: var(--text-faint);
  font-size: 11px;
  line-height: 1.6;
}

.wld-danger {
  margin: 10px 0 0;
  padding: 9px 11px;
  border-radius: var(--radius-sm);
  background: color-mix(in srgb, var(--danger) 10%, transparent);
  color: var(--danger);
  font-size: 12px;
  line-height: 1.7;
}

.wld-error {
  margin: 10px 0 0;
  color: var(--danger);
  font-size: 12px;
  line-height: 1.6;
}

.wld-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}

.wld-btn {
  height: 30px;
  padding: 0 14px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.wld-btn:hover:not(:disabled) {
  background: var(--bg-hover);
}

.wld-btn.is-primary {
  border-color: transparent;
  background: var(--accent);
  color: var(--accent-contrast);
  font-weight: 600;
}

.wld-btn.is-primary:hover:not(:disabled) {
  background: var(--accent-strong);
}

.wld-btn.is-primary.is-danger {
  background: var(--danger);
}

.wld-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
</style>
