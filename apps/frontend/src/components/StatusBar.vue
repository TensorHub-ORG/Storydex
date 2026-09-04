<template>
  <footer class="status-bar">
    <div class="status-left">
      <span>{{ readinessLabel }}</span>
      <span>内存：{{ memoryUsageLabel }}</span>
      <span>{{ projectLabel }}</span>
    </div>
    <div class="status-right">
      <span :class="agentStatusClass">{{ agentStatusLabel }}</span>
      <span>{{ themeLabel }}</span>
    </div>
  </footer>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from "vue";
import { themeOptions } from "@/constants/themes";
import { useAgentStore } from "@/stores/agent";
import { useUiStore } from "@/stores/ui";
import { useWorkspaceStore } from "@/stores/workspace";

const workspaceStore = useWorkspaceStore();
const agentStore = useAgentStore();
const uiStore = useUiStore();
const HEALTH_REFRESH_INTERVAL_MS = 15000;
let healthRefreshTimer: number | null = null;

const readinessLabel = computed(() => {
  if (workspaceStore.isBootstrapping) return "正在连接";
  if (workspaceStore.health?.status === "ok") return "就绪";
  if (workspaceStore.workspaceError) return "错误";
  return "等待连接";
});

const memoryUsageLabel = computed(() => {
  const rawValue = workspaceStore.health?.memoryUsageMb;
  if (typeof rawValue !== "number" || !Number.isFinite(rawValue) || rawValue < 0) {
    return "-- MB";
  }
  return `${Math.round(rawValue)} MB`;
});

const projectLabel = computed(() => {
  if (workspaceStore.launchScreenVisible) return "未打开项目";
  return workspaceStore.currentProject?.projectName || workspaceStore.health?.projectName || "未打开项目";
});

const agentStatusLabel = computed(() => (agentStore.isRunning ? "Coomi 运行中" : "Coomi 空闲"));
const agentStatusClass = computed(() => (agentStore.isRunning ? "is-running" : undefined));
const themeLabel = computed(
  () => themeOptions.find((option) => option.code === uiStore.theme)?.label || uiStore.theme
);

onMounted(() => {
  healthRefreshTimer = window.setInterval(() => {
    if (!workspaceStore.isBootstrapping) {
      void workspaceStore.refreshHealth();
    }
  }, HEALTH_REFRESH_INTERVAL_MS);
});

onBeforeUnmount(() => {
  if (healthRefreshTimer !== null) {
    window.clearInterval(healthRefreshTimer);
    healthRefreshTimer = null;
  }
});
</script>

<style scoped>
.status-right span.is-running {
  color: var(--accent);
}
</style>
