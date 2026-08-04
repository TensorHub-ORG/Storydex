<template>
  <section class="wlm">
    <header class="wlm-header">
      <div class="wlm-heading">
        <h2 class="wlm-title">平行时空线</h2>
        <p class="wlm-sub">
          分支只分不合：每条世界线都是一个独立的平行时空，从某个节点分出去之后就再也不会汇合。
        </p>
      </div>
      <div class="wlm-status">
        <span class="wlm-chip" :class="{ 'is-observing': gitStore.isDetached }">
          <span class="material-symbols-rounded">{{ gitStore.isDetached ? "visibility" : "fork_right" }}</span>
          <span>{{ gitStore.isDetached ? "观测态（不在任何世界线上）" : currentWorldlineLabel }}</span>
        </span>
        <span class="wlm-chip" :class="hasChanges ? 'is-dirty' : 'is-clean'">
          <span class="material-symbols-rounded">{{ hasChanges ? "edit_note" : "check_circle" }}</span>
          <span>{{ hasChanges ? `${gitStore.changedCount} 个文件还没进版本` : "所有改动都已进版本" }}</span>
        </span>
        <button class="wlm-refresh" type="button" title="刷新平行时空线" @click="refresh">
          <span class="material-symbols-rounded" :class="{ spinning: gitStore.isTimelineLoading }">refresh</span>
        </button>
      </div>
    </header>

    <div v-if="gitStore.error" class="wlm-feedback is-error">
      <span class="material-symbols-rounded">error</span>
      <span>{{ gitStore.error }}</span>
    </div>
    <div v-else-if="gitStore.successMessage" class="wlm-feedback is-success">
      <span class="material-symbols-rounded">check_circle</span>
      <span>{{ gitStore.successMessage }}</span>
    </div>

    <div class="wlm-canvas">
      <TimelineGraph
        :timeline="gitStore.timeline"
        :loading="gitStore.isTimelineLoading"
        :detached-override="gitStore.isDetached"
        :dirty="hasChanges"
        :busy="actions.busy.value"
        density="full"
        @jump="actions.requestJump"
        @fork="actions.requestFork"
        @inspect="actions.requestInspect"
        @rename-worldline="actions.requestRename"
        @delete-worldline="actions.requestDelete"
      />
    </div>

    <WorldlineDialog
      :state="actions.dialog.value"
      :submitting="actions.isSubmitting.value"
      @confirm="actions.confirm"
      @cancel="actions.close"
      @update:input="onDialogInput"
    />
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted } from "vue";
import { useGitStore } from "@/stores/git";
import { useWorldlineActions } from "@/composables/useWorldlineActions";
import TimelineGraph from "@/components/TimelineGraph.vue";
import WorldlineDialog from "@/components/WorldlineDialog.vue";

const gitStore = useGitStore();
const actions = useWorldlineActions();

const hasChanges = computed(() => gitStore.changedCount > 0);
const currentWorldlineLabel = computed(() => gitStore.currentWorldline || "尚未确定世界线");

function refresh(): void {
  void gitStore.refreshSummary({ force: true });
  void gitStore.refreshBranches();
  void gitStore.refreshTimeline({ force: true });
}

function onDialogInput(value: string): void {
  actions.dialog.value = { ...actions.dialog.value, input: value, error: "" };
}

onMounted(() => {
  refresh();
});

defineExpose({
  __testUtils: import.meta.env.MODE === "test" ? {
    hasChanges, currentWorldlineLabel, refresh, onDialogInput
  } : null
});
</script>

<style scoped>
.wlm {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--bg-editor);
  color: var(--text-main);
}

.wlm-header {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding: 16px 22px 12px;
  border-bottom: 1px solid var(--border-ghost);
}

.wlm-heading {
  min-width: 0;
}

.wlm-title {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
}

.wlm-sub {
  margin: 4px 0 0;
  max-width: 62ch;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.7;
}

.wlm-status {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 8px;
}

.wlm-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  max-width: 260px;
  height: 24px;
  padding: 0 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  background: var(--bg-card);
  color: var(--text-soft);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
}

.wlm-chip .material-symbols-rounded {
  flex: 0 0 auto;
  font-size: 14px;
}

.wlm-chip.is-observing {
  border-color: color-mix(in srgb, var(--warning) 45%, transparent);
  color: var(--warning);
}

.wlm-chip.is-dirty {
  color: var(--warning);
}

.wlm-chip.is-clean {
  color: var(--success);
}

.wlm-refresh {
  width: 26px;
  height: 26px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--radius-sm, 4px);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.wlm-refresh:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}

.wlm-refresh .material-symbols-rounded {
  font-size: 17px;
}

.spinning {
  animation: wlm-spin 0.9s linear infinite;
}

@keyframes wlm-spin {
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .spinning { animation: none; }
}

.wlm-feedback {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 10px 22px 0;
  padding: 8px 10px;
  border-radius: var(--radius-sm, 4px);
  font-size: 12px;
  line-height: 1.6;
}

.wlm-feedback .material-symbols-rounded {
  flex: 0 0 auto;
  font-size: 16px;
}

.wlm-feedback.is-error {
  background: color-mix(in srgb, var(--danger) 10%, transparent);
  color: var(--danger);
}

.wlm-feedback.is-success {
  background: color-mix(in srgb, var(--success) 10%, transparent);
  color: var(--success);
}

.wlm-canvas {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
</style>
