<template>
  <footer class="status-bar">
    <div class="status-left">
      <span>{{ workspaceStore.health?.service ?? "Storydex Core" }}</span>
      <span>{{ connectionLabel }}</span>
      <span>{{ projectLabel }}</span>
      <span v-if="workspaceStore.activeFile">File: {{ workspaceStore.activeFile }}</span>
    </div>
    <div class="status-right">
      <span>Chars: {{ workspaceStore.wordCount }}</span>
      <span>Lines: {{ workspaceStore.lineCount }}</span>
      <span v-if="agentStore.lastTrace">Trace: {{ shortTrace(agentStore.lastTrace.traceId) }}</span>
    </div>
  </footer>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useAgentStore } from "@/stores/agent";
import { useWorkspaceStore } from "@/stores/workspace";

const agentStore = useAgentStore();
const workspaceStore = useWorkspaceStore();

const connectionLabel = computed(() => {
  if (workspaceStore.isBootstrapping) return "Backend connecting";
  if (workspaceStore.health?.status === "ok") return "Backend connected";
  if (workspaceStore.workspaceError) return "Backend error";
  return "Waiting";
});

const projectLabel = computed(() => {
  if (workspaceStore.launchScreenVisible) {
    return "No project";
  }
  return workspaceStore.currentProject?.projectName || workspaceStore.health?.projectName || "No project";
});

function shortTrace(traceId: string): string {
  if (!traceId) {
    return "unknown";
  }
  return `${traceId.slice(0, 8)}...${traceId.slice(-4)}`;
}
</script>

<style scoped></style>
