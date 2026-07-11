<template>
  <section class="large-file-shell">
    <header class="large-file-banner">
      <div>
        <strong>{{ isReadOnly ? "大文件快速预览" : "渐进加载" }}</strong>
        <span>已加载第 {{ startLine + 1 }}～{{ endLine }} 行，共约 {{ formatInteger(totalLines) }} 行<template v-if="!windowData.lineCountExact">（后台索引中）</template></span>
      </div>
      <button type="button" @click="loadFully">完整加载并编辑</button>
    </header>
    <div ref="scroller" class="large-file-scroll" @scroll.passive="handleScroll">
      <div class="large-file-spacer" :style="{ height: `${virtualHeight}px` }">
        <pre class="large-file-window" :style="{ transform: `translateY(${windowTop}px)` }">{{ windowData.content }}</pre>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import { useWorkspaceStore } from "@/stores/workspace";

const workspaceStore = useWorkspaceStore();
const scroller = ref<HTMLElement | null>(null);
let scrollTimer: ReturnType<typeof setTimeout> | null = null;
const windowData = computed(() => workspaceStore.activeLargeFileWindow!);
const totalLines = computed(() => Math.max(1, Number(windowData.value?.lineCount || 1)));
const startLine = computed(() => Math.max(0, Number(windowData.value?.startLine || 0)));
const endLine = computed(() => Math.min(totalLines.value, startLine.value + Number(windowData.value?.loadedLines || 0)));
const isReadOnly = computed(() => Boolean(windowData.value?.readOnly));
const virtualHeight = computed(() => Math.min(12_000_000, Math.max(600, totalLines.value * 22)));
const windowTop = computed(() => (startLine.value / totalLines.value) * virtualHeight.value);

function handleScroll(): void {
  if (scrollTimer) clearTimeout(scrollTimer);
  scrollTimer = setTimeout(() => {
    const element = scroller.value;
    if (!element) return;
    const ratio = Math.min(1, Math.max(0, element.scrollTop / Math.max(1, element.scrollHeight - element.clientHeight)));
    const target = Math.max(0, Math.floor(ratio * totalLines.value) - 120);
    if (target < startLine.value + 80 || target > endLine.value - 160) void workspaceStore.loadLargeFileWindow(target);
  }, 80);
}

async function loadFully(): Promise<void> {
  if (isReadOnly.value && !window.confirm("该文件超过20MB，完整加载和编辑可能占用大量内存。是否继续？")) return;
  await workspaceStore.loadActiveFileFully();
  await workspaceStore.setEditorMode("edit");
}

function formatInteger(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(Math.max(0, Math.floor(value)));
}

onBeforeUnmount(() => { if (scrollTimer) clearTimeout(scrollTimer); });
</script>

<style scoped>
.large-file-shell { display: flex; flex: 1; min-height: 0; flex-direction: column; }
.large-file-banner { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 8px 14px; border-bottom: 1px solid var(--border-ghost); color: var(--text-secondary); font-size: 12px; }
.large-file-banner div { display: flex; align-items: center; gap: 10px; min-width: 0; }
.large-file-banner strong { color: var(--text-primary); }
.large-file-banner button { border: 1px solid var(--border-ghost); background: transparent; color: var(--text-secondary); padding: 5px 10px; cursor: pointer; font-size: 12px; }
.large-file-scroll { flex: 1; min-height: 0; overflow: auto; contain: strict; }
.large-file-spacer { position: relative; width: 100%; }
.large-file-window { position: absolute; inset: 0 0 auto; margin: 0; padding: 10px 18px 24px; white-space: pre; font-family: var(--font-editor, "Cascadia Mono", "Consolas", monospace); font-size: 13px; line-height: 22px; color: var(--text-primary); }
</style>
