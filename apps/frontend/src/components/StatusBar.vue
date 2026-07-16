<template>
  <footer class="status-bar">
    <div class="status-left">
      <span>{{ readinessLabel }}</span>
      <span>Memory Usage: {{ memoryUsageLabel }}</span>
      <span>{{ projectLabel }}</span>
    </div>
  </footer>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from "vue";
import { useWorkspaceStore } from "@/stores/workspace";

const workspaceStore = useWorkspaceStore();
const HEALTH_REFRESH_INTERVAL_MS = 15000;
let healthRefreshTimer: number | null = null;

const readinessLabel = computed(() => {
  if (workspaceStore.isBootstrapping) return "Connecting";
  if (workspaceStore.health?.status === "ok") return "Ready";
  if (workspaceStore.workspaceError) return "Error";
  return "Waiting";
});

const memoryUsageLabel = computed(() => {
  const rawValue = workspaceStore.health?.memoryUsageMb;
  if (typeof rawValue !== "number" || !Number.isFinite(rawValue) || rawValue < 0) {
    return "-- MB";
  }
  return `${Math.round(rawValue)} MB`;
});

const projectLabel = computed(() => {
  if (workspaceStore.launchScreenVisible) return "No project";
  return workspaceStore.currentProject?.projectName || workspaceStore.health?.projectName || "No project";
});

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

<style scoped></style>
