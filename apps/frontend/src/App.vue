<template>
  <RouterView />
</template>

<script setup lang="ts">
import { RouterView } from "vue-router";
import { onBeforeUnmount, onMounted } from "vue";
import { useWorkspaceStore } from "@/stores/workspace";
const workspace = useWorkspaceStore();
function protectUnbackedDrafts(event: BeforeUnloadEvent): void {
  if (workspace.draftWritesPending || workspace.draftPersistenceError) {
    event.preventDefault();
    event.returnValue = "";
  }
}
onMounted(() => window.addEventListener("beforeunload", protectUnbackedDrafts));
onBeforeUnmount(() => window.removeEventListener("beforeunload", protectUnbackedDrafts));
</script>
