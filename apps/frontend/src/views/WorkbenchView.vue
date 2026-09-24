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
let detachOpenTargetListener: (() => void) | null = null;
const openingTargetIds = new Set<number>();

async function consumeOpenTarget(target: StorydexDesktopOpenTarget | null): Promise<void> {
  if (!target || openingTargetIds.has(target.id)) {
    return;
  }
  openingTargetIds.add(target.id);
  try {
    await workspaceStore.openProjectTarget(target.path, { isFile: target.isFile });
    await window.storydexDesktop?.ackOpenTarget?.(target.id);
  } finally {
    openingTargetIds.delete(target.id);
  }
}

async function bootstrapWorkbench(force = false): Promise<void> {
  // Probe the sidecar first.  A Tauri window must never authenticate or load
  // workspace state through a stale Python service on the same machine.
  await workspaceStore.refreshHealth();
  // The Rust sidecar identity is validated before any account, recent-project,
  // workspace, or Agent request is allowed to run.
  if (workspaceStore.runtimeMismatch || workspaceStore.workspaceError) {
    return;
  }
  await authStore.bootstrap();
  await workspaceStore.bootstrapGlobalState();
  await workspaceStore.bootstrap(force);
}

onMounted(() => {
  void bootstrapWorkbench();
  detachOpenTargetListener = window.storydexDesktop?.onOpenTarget?.((target) => {
    void consumeOpenTarget(target);
  }) ?? null;
  void window.storydexDesktop?.getPendingOpenTarget?.().then((target) => consumeOpenTarget(target));

  reconnectTimer = window.setInterval(() => {
    if (workspaceStore.health?.status === "ok" || workspaceStore.isBootstrapping) {
      return;
    }
    void bootstrapWorkbench(true);
  }, 3000);
});

onBeforeUnmount(() => {
  detachOpenTargetListener?.();
  detachOpenTargetListener = null;
  if (reconnectTimer !== null) {
    window.clearInterval(reconnectTimer);
    reconnectTimer = null;
  }
});
</script>
