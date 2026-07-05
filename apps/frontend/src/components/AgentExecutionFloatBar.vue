<template>
  <div v-if="visible" class="agent-execution-float" @click="emit('collapse')">
    <button
      class="float-segment float-changes"
      type="button"
      :disabled="changedCount === 0"
      title="查看本轮修改 Diff"
      @click.stop="openRunDiff"
    >
      <span class="material-symbols-rounded">edit_note</span>
      <span class="float-label">{{ changedLabel }}</span>
      <span v-if="hasDiffTotals" class="float-stats">
        <span class="is-added">+{{ diffTotals.added }}</span>
        <span class="is-removed">-{{ diffTotals.removed }}</span>
      </span>
    </button>
    <button
      class="float-collapse-toggle"
      type="button"
      title="收起文件变更摘要"
      aria-label="收起文件变更摘要"
      @click.stop="emit('collapse')"
    >
      <span class="material-symbols-rounded">keyboard_arrow_down</span>
    </button>

    <div
      v-if="showTaskSegment"
      class="float-segment float-tasks"
      @mouseenter="taskPopoverOpen = tasks.length > 0"
      @mouseleave="taskPopoverOpen = false"
    >
      <button class="float-task-trigger" type="button" :title="taskTriggerTitle" @click.stop>
        <span class="float-label">{{ progressLabel }}</span>
      </button>

      <div v-if="taskPopoverOpen && tasks.length > 0" class="float-task-popover" @click.stop>
        <div class="float-task-head">
          <span>任务清单</span>
          <span>{{ completedCount }}/{{ tasks.length }}</span>
        </div>
        <div class="float-task-list">
          <div v-for="task in tasks" :key="task.taskId" class="float-task-row" :class="`is-${task.status}`">
            <span class="float-task-dot"></span>
            <span class="float-task-title" :title="task.detail || task.title">{{ task.title }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { useAgentStore } from "@/stores/agent";
import { useWorkspaceStore } from "@/stores/workspace";
import type { AgentTaskItem } from "@/types/agent";

const agentStore = useAgentStore();
const workspaceStore = useWorkspaceStore();
const emit = defineEmits<{
  (event: "collapse"): void;
}>();
const taskPopoverOpen = ref(false);

const activeRun = computed(() => agentStore.activeTraceRun);
const ledger = computed(() => activeRun.value?.changeLedger || null);
const changedCount = computed(() => ledger.value?.changedFileCount || ledger.value?.changedFiles?.length || 0);
const diffTotals = computed(() => ({
  files: changedCount.value,
  added: ledger.value?.added || 0,
  removed: ledger.value?.removed || 0
}));
const tasks = computed<AgentTaskItem[]>(() => activeRun.value?.tasks || []);
const completedCount = computed(() => tasks.value.filter((task) => task.status === "completed").length);
const hasDiffTotals = computed(() => diffTotals.value.files > 0);
const visible = computed(() => {
  if (workspaceStore.launchScreenVisible) {
    return false;
  }
  return changedCount.value > 0;
});
const showTaskSegment = computed(() => agentStore.isRunning && tasks.value.length > 0);
const changedLabel = computed(() => {
  if (changedCount.value <= 0) {
    return "暂无本轮修改";
  }
  return `${changedCount.value}个文件已修改`;
});
const progressLabel = computed(() => {
  if (tasks.value.length === 0) {
    return "暂未规划";
  }
  const total = tasks.value.length;
  const runningIndex = tasks.value.findIndex((task) => task.status === "running");
  const current = runningIndex >= 0 ? runningIndex + 1 : Math.min(total, Math.max(1, completedCount.value));
  return `第 ${current}/${total} 步`;
});
const taskTriggerTitle = computed(() => (tasks.value.length > 0 ? "查看本轮任务清单" : "暂未规划"));

function openRunDiff(): void {
  const run = activeRun.value;
  if (!run || changedCount.value <= 0) {
    return;
  }
  void workspaceStore.openAgentRunDiff({
    traceId: run.traceId,
    sessionId: run.sessionId,
    changedFiles: ledger.value?.changedFiles || [],
    commitHash: ledger.value?.commitHash || ""
  });
}
</script>

<style scoped>
.agent-execution-float {
  position: relative;
  z-index: 4;
  display: flex;
  align-items: stretch;
  align-self: flex-end;
  width: fit-content;
  max-width: 100%;
  margin: 0 4px 0 auto;
  border: 1px solid color-mix(in srgb, var(--text-muted) 18%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg-card) 94%, transparent);
  color: var(--text-main);
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.16);
  overflow: visible;
}

.float-segment {
  min-width: 0;
  min-height: 28px;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.float-changes,
.float-task-trigger {
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  font-size: 12px;
}

.float-changes {
  flex: 1 1 auto;
  max-width: min(260px, 54vw);
  padding: 0 9px;
  cursor: pointer;
}

.float-changes:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent-soft) 14%, transparent);
}

.float-changes:disabled {
  cursor: default;
  opacity: 0.62;
}

.float-changes .material-symbols-rounded {
  color: var(--text-secondary);
  font-size: 16px;
}

.float-collapse-toggle {
  flex: 0 0 auto;
  width: 28px;
  min-height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-left: 1px solid color-mix(in srgb, var(--text-muted) 14%, transparent);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.float-collapse-toggle:hover {
  background: color-mix(in srgb, var(--accent-soft) 14%, transparent);
  color: var(--text-main);
}

.float-collapse-toggle .material-symbols-rounded {
  font-size: 17px;
}

.float-tasks {
  position: relative;
  flex: 0 0 auto;
}

.float-tasks::before {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: 100%;
  height: 8px;
}

.float-task-trigger {
  height: 100%;
  padding: 0 10px 0 9px;
  cursor: default;
}

.float-label {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.float-stats {
  flex: 0 0 auto;
  display: inline-flex;
  gap: 6px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.is-added {
  color: var(--success, #2f8b57);
}

.is-removed {
  color: var(--danger);
}

.float-task-popover {
  position: absolute;
  right: 0;
  bottom: calc(100% + 6px);
  width: min(340px, calc(100vw - 48px));
  max-height: min(380px, 58vh);
  display: flex;
  flex-direction: column;
  border: 1px solid color-mix(in srgb, var(--text-muted) 18%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg-card) 98%, transparent);
  box-shadow: 0 16px 42px rgba(0, 0, 0, 0.3);
  overflow: hidden;
}

.float-task-head {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 11px;
  border-bottom: 1px solid var(--border-ghost);
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 700;
}

.float-task-list {
  min-height: 0;
  overflow: auto;
  padding: 5px 0;
}

.float-task-row {
  min-height: 30px;
  display: grid;
  grid-template-columns: 16px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  padding: 0 11px;
  color: var(--text-secondary);
  font-size: 12px;
}

.float-task-dot {
  width: 11px;
  height: 11px;
  border-radius: 999px;
  border: 2px solid color-mix(in srgb, var(--text-muted) 60%, transparent);
}

.float-task-row.is-running .float-task-dot {
  border-color: color-mix(in srgb, var(--accent-strong) 72%, transparent);
  border-top-color: transparent;
  animation: float-ring-spin 0.8s linear infinite;
}

.float-task-row.is-completed .float-task-dot {
  border-color: var(--success, #2f8b57);
  background: var(--success, #2f8b57);
}

.float-task-row.is-failed .float-task-dot {
  border-color: var(--danger);
  background: var(--danger);
}

.float-task-row.is-skipped .float-task-dot {
  border-color: var(--warning, #b7791f);
  background: var(--warning, #b7791f);
}

.float-task-title {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

@keyframes float-ring-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
