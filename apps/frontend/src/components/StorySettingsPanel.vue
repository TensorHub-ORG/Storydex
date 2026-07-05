<template>
  <section v-if="visible" class="story-settings-overlay" @click.self="$emit('close')">
    <div class="story-settings-shell">
      <header class="story-settings-header">
        <div class="story-settings-heading">
          <div class="story-settings-title">剧情设置</div>
          <div class="story-settings-subtitle">项目级剧情识别与章节辅助状态。当前优先复用现有项目配置文件，后续可平滑切换到专用接口。</div>
        </div>
        <button class="story-settings-close" type="button" title="关闭设置" @click="$emit('close')">
          <span class="material-symbols-rounded">close</span>
        </button>
      </header>

      <div v-if="workspaceStore.launchScreenVisible" class="story-settings-empty">
        请先打开一个项目，再配置剧情片段扩展名。
      </div>

      <template v-else>
        <div class="story-settings-toolbar">
          <div class="story-settings-meta-row">
            <span class="story-settings-scope">项目配置</span>
            <code class="story-settings-path">{{ workspaceStore.storySettingsPath || "未生成" }}</code>
          </div>
          <div class="story-settings-meta-row">
            <span class="story-settings-meta-label">来源</span>
            <span class="story-settings-meta-value">{{ sourceLabel }}</span>
          </div>
        </div>

        <div class="story-settings-body">
          <label class="story-settings-field">
            <span>剧情片段扩展名</span>
            <select v-model="segmentExtension" class="story-settings-input" :disabled="saving || loading">
              <option value=".md">Markdown `.md`</option>
              <option value=".txt">Text `.txt`</option>
            </select>
          </label>

          <div class="story-settings-note">
            这个设置会影响资源树章节识别、剧情片段编辑器工具入口，以及后续新接口接入时的默认片段类型。
          </div>
        </div>

        <div v-if="errorMessage" class="story-settings-feedback is-error">{{ errorMessage }}</div>
        <div v-else-if="successMessage" class="story-settings-feedback is-success">{{ successMessage }}</div>

        <footer class="story-settings-footer">
          <button class="story-settings-save" type="button" :disabled="saving || loading" @click="handleSave">
            {{ saving ? "保存中..." : "保存剧情设置" }}
          </button>
        </footer>
      </template>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useWorkspaceStore } from "@/stores/workspace";
import type { StorySegmentExtension } from "@/types/workspace";

const props = defineProps<{
  visible: boolean;
}>();

const emit = defineEmits<{
  close: [];
  saved: [];
}>();

const workspaceStore = useWorkspaceStore();
const loading = ref(false);
const saving = ref(false);
const errorMessage = ref("");
const successMessage = ref("");
const segmentExtension = ref<StorySegmentExtension>(".md");

const sourceLabel = computed(() => {
  if (workspaceStore.storySettings.source === "api") {
    return "专用接口";
  }
  if (workspaceStore.storySettings.source === "project_file") {
    return "项目文件回退";
  }
  return "默认值";
});

watch(
  () => props.visible,
  async (visible) => {
    if (!visible) {
      return;
    }
    loading.value = true;
    errorMessage.value = "";
    successMessage.value = "";
    try {
      if (!workspaceStore.launchScreenVisible) {
        await workspaceStore.refreshStorySettings();
      }
      segmentExtension.value = workspaceStore.storySegmentExtension;
    } catch (error: unknown) {
      errorMessage.value = error instanceof Error ? error.message : "加载剧情设置失败。";
    } finally {
      loading.value = false;
    }
  },
  { immediate: true }
);

async function handleSave(): Promise<void> {
  if (workspaceStore.launchScreenVisible) {
    errorMessage.value = "请先打开一个项目。";
    return;
  }

  saving.value = true;
  errorMessage.value = "";
  successMessage.value = "";
  try {
    await workspaceStore.updateStorySettings({
      segmentExtension: segmentExtension.value
    });
    successMessage.value = "剧情设置已保存。";
    emit("saved");
  } catch (error: unknown) {
    errorMessage.value = error instanceof Error ? error.message : "保存剧情设置失败。";
  } finally {
    saving.value = false;
  }
}
</script>

<style scoped>
.story-settings-overlay {
  position: absolute;
  inset: 0;
  z-index: 40;
  display: flex;
  justify-content: flex-end;
  background: color-mix(in srgb, var(--bg-app) 68%, transparent);
  backdrop-filter: blur(8px);
}

.story-settings-shell {
  width: min(420px, 100%);
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--bg-panel);
  border-left: 1px solid var(--border-ghost);
}

.story-settings-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 14px 18px 12px;
  border-bottom: 1px solid var(--border-ghost);
}

.story-settings-heading {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.story-settings-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.story-settings-subtitle,
.story-settings-note,
.story-settings-empty,
.story-settings-meta-label,
.story-settings-meta-value,
.story-settings-feedback {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.story-settings-close {
  width: 30px;
  height: 30px;
  border: 0;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
}

.story-settings-close:hover,
.story-settings-save:hover:not(:disabled) {
  color: var(--text-primary);
  background: color-mix(in srgb, var(--text-secondary) 8%, transparent);
}

.story-settings-empty {
  padding: 24px 18px;
}

.story-settings-toolbar {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px 18px 0;
}

.story-settings-meta-row {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.story-settings-scope {
  display: inline-flex;
  align-items: center;
  height: 24px;
  padding: 0 10px;
  color: var(--accent-primary);
  background: color-mix(in srgb, var(--accent-primary) 14%, transparent);
  border-radius: 999px;
  font-size: 12px;
}

.story-settings-path {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}

.story-settings-body {
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 18px;
}

.story-settings-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: var(--text-secondary);
  font-size: 12px;
}

.story-settings-input {
  width: 100%;
  min-width: 0;
  padding: 8px 0;
  border: 0;
  border-bottom: 1px solid var(--border-ghost);
  background: transparent;
  color: var(--text-primary);
  outline: none;
}

.story-settings-input:focus {
  border-bottom-color: var(--accent-primary);
}

.story-settings-feedback {
  margin: 0 18px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border-ghost);
}

.story-settings-feedback.is-error {
  color: var(--state-danger);
}

.story-settings-feedback.is-success {
  color: var(--state-success);
}

.story-settings-footer {
  margin-top: auto;
  display: flex;
  justify-content: flex-end;
  padding: 14px 18px 18px;
  border-top: 1px solid var(--border-ghost);
}

.story-settings-save {
  border: 0;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  padding: 8px 10px;
}

.story-settings-save:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
