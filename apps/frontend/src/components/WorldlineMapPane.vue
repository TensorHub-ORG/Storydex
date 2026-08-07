<template>
  <section class="wlm">
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
    hasChanges, refresh, onDialogInput
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

.wlm-feedback {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 10px 12px 0;
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
