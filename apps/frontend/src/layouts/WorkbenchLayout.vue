<template>
  <div class="app-shell">
    <TopHeader />

    <div ref="workspaceRef" class="workspace" :style="workspaceStyle">
      <ActivityBar />

      <template v-if="showStorydexSidebar">
        <div class="storydex-sidebar-shell">
          <component :is="sidebarComponent" />
        </div>
        <div
          class="workspace-splitter"
          title="拖动调整侧边栏宽度"
          @pointerdown="startResize('sidebar', $event)"
        ></div>
      </template>

      <EditorPane v-if="!relationshipGraphMode" />
      <div v-else-if="workspaceStore.launchScreenVisible" class="storydex-relationship-empty">
        先打开一个 Storydex 项目，再查看知识图谱和WIKI。
      </div>
      <StoryStatePanel
        v-else
        class="storydex-relationship-workspace"
        initial-tab="relations"
        relationship-only
        expanded
      />

      <div
        v-if="showAgentPanel"
        class="workspace-splitter workspace-splitter-agent"
        title="拖动调整 Agent 栏宽度"
        @pointerdown="startResize('agent', $event)"
      ></div>
      <AgentPanel v-if="showAgentPanel" />
    </div>

    <StatusBar />

    <SystemSettingsWindow :visible="uiStore.systemSettingsOpen" @close="uiStore.setSystemSettingsOpen(false)" />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import ActivityBar from "@/components/ActivityBar.vue";
import AgentPanel from "@/components/AgentPanel.vue";
import EditorPane from "@/components/EditorPane.vue";
import ExplorerSidebar from "@/components/ExplorerSidebar.vue";
import PresetManagementSidebar from "@/components/PresetManagementSidebar.vue";
import SourceControlSidebar from "@/components/SourceControlSidebar.vue";
import StatusBar from "@/components/StatusBar.vue";
import StoryStatePanel from "@/components/StoryStatePanel.vue";
import SystemSettingsWindow from "@/components/SystemSettingsWindow.vue";
import TopHeader from "@/components/TopHeader.vue";
import { useTheme } from "@/composables/useTheme";
import { useUiStore } from "@/stores/ui";
import { useWorkspaceStore } from "@/stores/workspace";

const uiStore = useUiStore();
const workspaceStore = useWorkspaceStore();
const { applyTheme, applyTypography } = useTheme();

const workspaceRef = ref<HTMLElement | null>(null);

const ACTIVITY_BAR_WIDTH = 48;
const SPLITTER_WIDTH = 8;
const AGENT_SPLITTER_WIDTH = 8;
const MIN_EDITOR_WIDTH = 480;
const MIN_SIDEBAR_WIDTH = 220;
const MIN_AGENT_WIDTH = 320;

const relationshipGraphMode = computed(() => uiStore.activeActivity === "relationships");
const showStorydexSidebar = computed(() => !uiStore.sidebarCollapsed && !relationshipGraphMode.value);
const showAgentPanel = computed(() => !uiStore.agentCollapsed && !workspaceStore.launchScreenVisible);
const sidebarComponent = computed(() => {
  if (uiStore.activeActivity === "source-control") {
    return SourceControlSidebar;
  }
  if (uiStore.activeActivity === "presets") {
    return PresetManagementSidebar;
  }
  return ExplorerSidebar;
});

const workspaceStyle = computed(() => {
  const sidebarWidth = workspaceStore.launchScreenVisible ? Math.min(uiStore.sidebarWidth, 320) : uiStore.sidebarWidth;
  const agentWidth = uiStore.agentWidth;
  const leadColumns = showStorydexSidebar.value
    ? [`${ACTIVITY_BAR_WIDTH}px`, `${sidebarWidth}px`, `${SPLITTER_WIDTH}px`]
    : [`${ACTIVITY_BAR_WIDTH}px`];
  const editorMinWidth = relationshipGraphMode.value ? 0 : MIN_EDITOR_WIDTH;
  const editorColumn = `minmax(${editorMinWidth}px, 1fr)`;

  if (!showAgentPanel.value) {
    return {
      gridTemplateColumns: [...leadColumns, editorColumn].join(" ")
    };
  }

  return {
    gridTemplateColumns: [...leadColumns, editorColumn, `${AGENT_SPLITTER_WIDTH}px`, `${agentWidth}px`].join(" ")
  };
});

watch(
  () => uiStore.theme,
  (nextTheme) => applyTheme(nextTheme),
  { immediate: true }
);

watch(
  () => [uiStore.fileFontSize, uiStore.playerFontSize] as const,
  ([fileFontSize, playerFontSize]) => applyTypography({ fileFontSize, playerFontSize }),
  { immediate: true }
);

function startResize(target: "sidebar" | "agent", event: PointerEvent): void {
  const workspace = workspaceRef.value;
  if (!workspace) {
    return;
  }

  event.preventDefault();
  event.currentTarget instanceof HTMLElement && event.currentTarget.setPointerCapture(event.pointerId);

  const rect = workspace.getBoundingClientRect();
  const startX = event.clientX;
  const startSidebar = uiStore.sidebarWidth;
  const startAgent = uiStore.agentWidth;

  const onPointerMove = (moveEvent: PointerEvent): void => {
    const deltaX = moveEvent.clientX - startX;
    const totalWidth = rect.width;
    const layoutLeadWidth = ACTIVITY_BAR_WIDTH;
    const minMainWidth = MIN_EDITOR_WIDTH;
    const minSidebarWidth = MIN_SIDEBAR_WIDTH;
    const minAgentWidth = MIN_AGENT_WIDTH;
    const sidebarTrackWidth = showStorydexSidebar.value ? uiStore.sidebarWidth : 0;
    const agentTrackWidth = showAgentPanel.value ? startAgent : 0;
    const splitterWidthTotal = (showStorydexSidebar.value ? SPLITTER_WIDTH : 0)
      + (showAgentPanel.value ? AGENT_SPLITTER_WIDTH : 0);

    if (target === "sidebar") {
      const maxSidebar = Math.max(
        minSidebarWidth,
        totalWidth - layoutLeadWidth - splitterWidthTotal - agentTrackWidth - minMainWidth
      );
      uiStore.setSidebarWidth(clamp(startSidebar + deltaX, minSidebarWidth, maxSidebar));
      return;
    }

    const maxAgent = Math.max(
      minAgentWidth,
      totalWidth - layoutLeadWidth - splitterWidthTotal - sidebarTrackWidth - minMainWidth
    );
    uiStore.setAgentWidth(clamp(startAgent - deltaX, minAgentWidth, maxAgent));
  };

  const onPointerUp = (): void => {
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", onPointerUp);
    window.removeEventListener("pointercancel", onPointerUp);
    document.body.classList.remove("is-resizing-panels");
  };

  document.body.classList.add("is-resizing-panels");
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp, { once: true });
  window.addEventListener("pointercancel", onPointerUp, { once: true });
}

onBeforeUnmount(() => {
  document.body.classList.remove("is-resizing-panels");
});

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}
</script>

<style scoped>
.workspace {
  position: relative;
}

.storydex-sidebar-shell {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.storydex-sidebar-shell :deep(.explorer-panel),
.storydex-sidebar-shell :deep(.source-control-panel),
.storydex-sidebar-shell :deep(.preset-panel) {
  flex: 1 1 auto;
  height: auto;
  min-height: 0;
}

.storydex-relationship-workspace {
  min-width: 0;
  height: 100%;
}

.storydex-relationship-empty {
  min-width: 0;
  height: 100%;
  display: grid;
  place-items: center;
  padding: 24px;
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.6;
  text-align: center;
  background: var(--bg-main);
}

.workspace-splitter-agent {
  width: 100%;
  margin: 0;
  background: transparent;
}

.workspace-splitter-agent::before {
  pointer-events: none;
  inset: 0 auto;
  left: 50%;
  width: 1px;
  transform: translateX(-50%);
  background: var(--border-subtle);
}

.workspace-splitter-agent:hover::before,
:global(body.is-resizing-panels) .workspace-splitter-agent::before {
  width: 2px;
  background: var(--accent);
}
</style>
