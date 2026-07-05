<template>
  <WorkbenchLayout />
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted } from "vue";
import WorkbenchLayout from "@/layouts/WorkbenchLayout.vue";
import { useAuthStore } from "@/stores/auth";
import { useWorkspaceStore } from "@/stores/workspace";

const authStore = useAuthStore();
const workspaceStore = useWorkspaceStore();
let reconnectTimer: number | null = null;

async function bootstrapWorkbench(force = false): Promise<void> {
  await authStore.bootstrap();
  await workspaceStore.bootstrapGlobalState();
  await workspaceStore.bootstrap(force);
}

onMounted(() => {
  void bootstrapWorkbench();

  reconnectTimer = window.setInterval(() => {
    if (workspaceStore.health?.status === "ok" || workspaceStore.isBootstrapping) {
      return;
    }
    void bootstrapWorkbench(true);
  }, 3000);
});

onBeforeUnmount(() => {
  if (reconnectTimer !== null) {
    window.clearInterval(reconnectTimer);
    reconnectTimer = null;
  }
});
</script>
