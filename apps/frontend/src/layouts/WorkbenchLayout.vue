<template>
  <div class="app-shell">
    <TopHeader />

    <div ref="workspaceRef" class="workspace" :style="workspaceStyle">
      <ActivityBar />

      <template v-if="showStorydexSidebar">
        <div class="storydex-sidebar-shell workspace-font-pane" :style="leftPaneFontStyle">
          <component :is="sidebarComponent" />
        </div>
        <div
          class="workspace-splitter"
          title="拖动调整侧边栏宽度"
          @pointerdown="startResize('sidebar', $event)"
        ></div>
      </template>

      <EditorPane v-if="!relationshipGraphMode" class="workspace-font-pane" :style="centerPaneFontStyle" />
      <div
        v-else-if="workspaceStore.launchScreenVisible"
        class="storydex-relationship-empty workspace-font-pane"
        :style="centerPaneFontStyle"
      >
        先打开一个 Storydex 项目，再查看知识图谱和WIKI。
      </div>
      <StoryStatePanel
        v-else
        class="storydex-relationship-workspace workspace-font-pane"
        :style="centerPaneFontStyle"
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
      <AgentPanel v-if="showAgentPanel" class="workspace-font-pane" :style="rightPaneFontStyle" />
    </div>

    <StatusBar />
    <UpdateNotification />

    <SystemSettingsWindow :visible="uiStore.systemSettingsOpen" @close="uiStore.setSystemSettingsOpen(false)" />

    <div v-if="closeDialogVisible" class="close-guard-backdrop" role="presentation" @click.self="cancelClose">
      <section class="close-guard-dialog" role="dialog" aria-modal="true" aria-labelledby="close-guard-title">
        <div class="close-guard-icon" aria-hidden="true">
          <span class="material-symbols-rounded">pending_actions</span>
        </div>
        <div class="close-guard-copy">
          <h2 id="close-guard-title">任务仍在进行</h2>
          <p>Coomi 仍在执行或有待处理消息。现在退出可能中断尚未保存的结果。</p>
        </div>
        <div class="close-guard-actions">
          <button type="button" class="close-guard-button" @click="cancelClose">取消</button>
          <button type="button" class="close-guard-button" @click="waitAndClose">等待完成后退出</button>
          <button type="button" class="close-guard-button danger" :disabled="stoppingForClose" @click="stopAndClose">
            {{ stoppingForClose ? "正在停止" : "停止任务并退出" }}
          </button>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import ActivityBar from "@/components/ActivityBar.vue";
import AgentPanel from "@/components/AgentPanel.vue";
import EditorPane from "@/components/EditorPane.vue";
import ExplorerSidebar from "@/components/ExplorerSidebar.vue";
import PresetManagementSidebar from "@/components/PresetManagementSidebar.vue";
import PromptRepositorySidebar from "@/components/PromptRepositorySidebar.vue";
import SearchSidebar from "@/components/SearchSidebar.vue";
import SourceControlSidebar from "@/components/SourceControlSidebar.vue";
import StatusBar from "@/components/StatusBar.vue";
import StoryStatePanel from "@/components/StoryStatePanel.vue";
import SystemSettingsWindow from "@/components/SystemSettingsWindow.vue";
import TopHeader from "@/components/TopHeader.vue";
import UpdateNotification from "@/components/UpdateNotification.vue";
import { useTheme } from "@/composables/useTheme";
import { useUiStore } from "@/stores/ui";
import { useWorkspaceStore } from "@/stores/workspace";
import { useAgentStore } from "@/stores/agent";
import { paneFontScaleStyle } from "@/utils/paneFontScale";

const uiStore = useUiStore();
const workspaceStore = useWorkspaceStore();
const agentStore = useAgentStore();
const { applyTheme, applyPaneFontScale, applyEditorFont } = useTheme();
applyPaneFontScale();
applyEditorFont(uiStore.fontFamily);

const workspaceRef = ref<HTMLElement | null>(null);
const closeDialogVisible = ref(false);
const exitWhenIdle = ref(false);
const stoppingForClose = ref(false);
let detachCloseRequested: (() => void) | undefined;

const hasActiveAgentWork = computed(() => agentStore.isRunning || agentStore.followups.some(
  (message) => !["sent", "cancelled", "failed"].includes(message.status)
));

function confirmClose(): void {
  void window.storydexDesktop?.confirmMainWindowClose?.();
}

function requestClose(): void {
  if (!hasActiveAgentWork.value) {
    confirmClose();
    return;
  }
  closeDialogVisible.value = true;
}

function cancelClose(): void {
  closeDialogVisible.value = false;
  exitWhenIdle.value = false;
}

function waitAndClose(): void {
  closeDialogVisible.value = false;
  exitWhenIdle.value = true;
}

async function stopAndClose(): Promise<void> {
  if (stoppingForClose.value) return;
  stoppingForClose.value = true;
  closeDialogVisible.value = false;
  try {
    await agentStore.stopActiveRun();
    confirmClose();
  } catch {
    closeDialogVisible.value = true;
  } finally {
    stoppingForClose.value = false;
  }
}

const ACTIVITY_BAR_WIDTH = 48;
const SPLITTER_WIDTH = 8;
const AGENT_SPLITTER_WIDTH = 8;
const MIN_EDITOR_WIDTH = 480;
const MIN_SIDEBAR_WIDTH = 220;
const MIN_AGENT_WIDTH = 320;

const viewportWidth = ref(Math.max(0, window.innerWidth));
const compactViewport = computed(
  () => viewportWidth.value
    < ACTIVITY_BAR_WIDTH
      + uiStore.sidebarWidth
      + SPLITTER_WIDTH
      + MIN_EDITOR_WIDTH
      + AGENT_SPLITTER_WIDTH
      + MIN_AGENT_WIDTH
);

const relationshipGraphMode = computed(() => uiStore.activeActivity === "relationships");
const showStorydexSidebar = computed(
  () => !compactViewport.value && !uiStore.sidebarCollapsed && !relationshipGraphMode.value
);
const showAgentPanel = computed(() => !uiStore.agentCollapsed && !workspaceStore.launchScreenVisible);
const sidebarComponent = computed(() => {
  if (uiStore.activeActivity === "source-control") {
    return SourceControlSidebar;
  }
  if (uiStore.activeActivity === "search") {
    return SearchSidebar;
  }
  if (uiStore.activeActivity === "presets") {
    return PresetManagementSidebar;
  }
  if (uiStore.activeActivity === "prompts") {
    return PromptRepositorySidebar;
  }
  return ExplorerSidebar;
});
const leftPaneFontStyle = computed(() => paneFontScaleStyle(uiStore.leftPaneFontScale));
const centerPaneFontStyle = computed(() => paneFontScaleStyle(uiStore.centerPaneFontScale));
const rightPaneFontStyle = computed(() => paneFontScaleStyle(uiStore.rightPaneFontScale));

const workspaceStyle = computed(() => {
  const sidebarWidth = workspaceStore.launchScreenVisible ? Math.min(uiStore.sidebarWidth, 320) : uiStore.sidebarWidth;
  const agentWidth = uiStore.agentWidth;
  const leadColumns = showStorydexSidebar.value
    ? [`${ACTIVITY_BAR_WIDTH}px`, `${sidebarWidth}px`, `${SPLITTER_WIDTH}px`]
    : [`${ACTIVITY_BAR_WIDTH}px`];
  const editorMinWidth = compactViewport.value || relationshipGraphMode.value ? 0 : MIN_EDITOR_WIDTH;
  const editorColumn = `minmax(${editorMinWidth}px, 1fr)`;

  if (!showAgentPanel.value) {
    return {
      gridTemplateColumns: [...leadColumns, editorColumn].join(" ")
    };
  }

  const leadWidth = ACTIVITY_BAR_WIDTH
    + (showStorydexSidebar.value ? sidebarWidth + SPLITTER_WIDTH : 0);
  const availableAgentWidth = Math.max(
    0,
    viewportWidth.value - leadWidth - AGENT_SPLITTER_WIDTH - editorMinWidth
  );
  const renderedAgentWidth = Math.min(agentWidth, availableAgentWidth);

  return {
    gridTemplateColumns: [
      ...leadColumns,
      editorColumn,
      `${AGENT_SPLITTER_WIDTH}px`,
      `${renderedAgentWidth}px`
    ].join(" ")
  };
});

function updateViewportWidth(): void {
  viewportWidth.value = Math.max(0, window.innerWidth);
}

onMounted(() => {
  updateViewportWidth();
  window.addEventListener("resize", updateViewportWidth, { passive: true });
  detachCloseRequested = window.storydexDesktop?.onCloseRequested?.(requestClose);
});

watch(hasActiveAgentWork, (active) => {
  if (!active && exitWhenIdle.value) {
    exitWhenIdle.value = false;
    confirmClose();
  }
});

watch(
  () => uiStore.theme,
  (nextTheme) => applyTheme(nextTheme),
  { immediate: true }
);

watch(() => uiStore.fontFamily, (fontFamily) => applyEditorFont(fontFamily), { immediate: true });

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
  window.removeEventListener("resize", updateViewportWidth);
  detachCloseRequested?.();
});

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}
</script>

<style scoped>
.workspace {
  position: relative;
}

.close-guard-backdrop {
  position: fixed;
  inset: 0;
  z-index: 10000;
  display: grid;
  place-items: center;
  padding: 24px;
  background: var(--overlay-scrim);
}

.close-guard-dialog {
  width: min(460px, 100%);
  display: grid;
  grid-template-columns: 40px minmax(0, 1fr);
  gap: 14px;
  padding: 20px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--surface-overlay);
  color: var(--text-main);
  box-shadow: var(--shadow-modal);
}

.close-guard-icon {
  width: 40px;
  height: 40px;
  display: grid;
  place-items: center;
  color: var(--warning-fg);
}

.close-guard-copy h2 {
  margin: 0 0 6px;
  font-size: 16px;
  letter-spacing: 0;
}

.close-guard-copy p {
  margin: 0;
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.6;
}

.close-guard-actions {
  grid-column: 1 / -1;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 6px;
}

.close-guard-button {
  min-height: 28px;
  padding: 0 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--surface-1);
  color: var(--text-main);
  font-weight: 500;
  cursor: pointer;
}

.close-guard-button:hover {
  background: var(--bg-hover);
}

.close-guard-button.danger {
  border-color: transparent;
  background: var(--danger-fg);
  color: #fff;
}

.workspace-font-pane {
  font-size: var(--ui-pane-scaled-px-14, 14px);
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
.storydex-sidebar-shell :deep(.preset-panel),
.storydex-sidebar-shell :deep(.prompt-repository-panel) {
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

.workspace-splitter-agent:hover::before {
  width: 2px;
  background: var(--accent);
}
</style>
