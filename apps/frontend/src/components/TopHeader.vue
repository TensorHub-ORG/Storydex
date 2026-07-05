<template>
  <header class="top-header">
    <div class="top-header-left">
      <div class="topbar-brand no-drag" title="Storydex">
        <img :src="storydexIcon" alt="Storydex" class="topbar-brand-icon" />
        <span class="topbar-brand-name">Storydex</span>
      </div>

      <div ref="projectMenuRef" class="file-menu-wrap no-drag">
        <button
          class="file-menu-trigger menu-bar-item"
          type="button"
          :aria-expanded="openMenu === 'project'"
          @click="toggleMenu('project')"
        >
          项目
        </button>

        <div v-if="openMenu === 'project'" class="file-menu-card">
          <div class="file-menu-section">
            <div class="file-menu-title">当前工作区</div>
            <div class="file-menu-project">{{ projectLabel }}</div>
            <div class="file-menu-path">
              {{ workspaceStore.projectRootLabel || "尚未打开项目文件夹" }}
            </div>
          </div>

          <button class="file-menu-item" type="button" @click="openCreateProjectDialog">
            <span class="material-symbols-rounded">create_new_folder</span>
            <span>新建项目</span>
          </button>
          <button class="file-menu-item" type="button" @click="handleOpenProjectRequest">
            <span class="material-symbols-rounded">folder_open</span>
            <span>打开文件夹</span>
          </button>
          <button
            class="file-menu-item"
            type="button"
            :disabled="!workspaceStore.projectRootLabel || workspaceStore.isProjectInitializing"
            @click="handleInitializeCurrentProject"
          >
            <span class="material-symbols-rounded">build</span>
            <span>{{ workspaceStore.requiresInitialization ? "初始化当前项目" : "重建项目配置" }}</span>
          </button>
          <button class="file-menu-item" type="button" @click="handleRefresh">
            <span class="material-symbols-rounded">refresh</span>
            <span>刷新工作区</span>
          </button>
          <button class="file-menu-item" type="button" @click="handleReturnWelcomePage">
            <span class="material-symbols-rounded">home</span>
            <span>返回欢迎页</span>
          </button>
        </div>
      </div>

      <div ref="helpMenuRef" class="file-menu-wrap no-drag">
        <button
          class="file-menu-trigger menu-bar-item"
          type="button"
          :aria-expanded="openMenu === 'help'"
          @click="toggleMenu('help')"
        >
          帮助
        </button>

        <div v-if="openMenu === 'help'" class="file-menu-card file-menu-card-help">
          <button class="file-menu-item" type="button" @click="handleOpenHelpGuide">
            <span class="material-symbols-rounded">menu_book</span>
            <span>使用指南</span>
          </button>
          <button class="file-menu-item" type="button" @click="handleOpenAbout">
            <span class="material-symbols-rounded">open_in_new</span>
            <span>关于我们</span>
          </button>
        </div>
      </div>
    </div>

    <div class="top-header-center">
      <label class="command-bar no-drag">
        <span class="material-symbols-rounded">search</span>
        <input class="command-bar-input" type="text" readonly :placeholder="commandPlaceholder" />
      </label>
    </div>

    <div class="top-header-right">

      <button
        class="header-utility-btn header-agent-toggle no-drag"
        :class="{ active: !uiStore.agentCollapsed }"
        type="button"
        :title="agentHeaderToggleTitle"
        :aria-label="agentHeaderToggleTitle"
        :aria-expanded="!uiStore.agentCollapsed"
        :aria-pressed="!uiStore.agentCollapsed"
        @click="uiStore.toggleAgentCollapsed()"
      >
        <span class="material-symbols-rounded">
          {{ uiStore.agentCollapsed ? "right_panel_open" : "right_panel_close" }}
        </span>
      </button>

      <div class="titlebar-spacer" aria-hidden="true"></div>
    </div>
  </header>

  <div v-if="dialogMode === 'create'" class="modal-mask" @click.self="closeDialog">
    <div class="modal-card">
      <div class="modal-title">新建项目</div>
      <div class="modal-desc">先选择项目存放目录，再填写项目名称，Storydex 会自动拼接最终项目路径。</div>

      <label class="modal-label">
        项目存放目录
        <div class="modal-input-row">
          <input
            v-model.trim="createBaseDirectory"
            class="modal-input modal-input-flex"
            placeholder="例如：F:\\写作项目"
            :disabled="workspaceStore.isProjectCreating"
            @keydown.enter.prevent="browseCreateBaseDirectory"
          />
          <button
            class="btn btn-outline"
            type="button"
            :disabled="workspaceStore.isProjectCreating"
            @click="browseCreateBaseDirectory"
          >
            浏览
          </button>
        </div>
      </label>

      <label class="modal-label">
        项目名称
        <input
          v-model.trim="createProjectName"
          class="modal-input"
          placeholder="例如：我的故事项目"
          :disabled="workspaceStore.isProjectCreating"
          @keydown.enter.prevent="handleCreateProjectSubmit"
        />
      </label>

      <label class="modal-label">
        最终项目路径
        <div class="modal-inline-readonly">{{ newProjectTargetPath || "请选择目录并填写项目名称" }}</div>
      </label>

      <div v-if="createValidationMessage" class="modal-error">{{ createValidationMessage }}</div>
      <div v-else-if="workspaceStore.workspaceError" class="modal-error">{{ workspaceStore.workspaceError }}</div>

      <div class="modal-actions">
        <button class="btn btn-outline" type="button" @click="closeDialog">取消</button>
        <button
          class="btn btn-primary"
          type="button"
          :disabled="!canCreateProject || workspaceStore.isProjectCreating"
          @click="handleCreateProjectSubmit"
        >
          {{ workspaceStore.isProjectCreating ? "创建中..." : "创建并打开" }}
        </button>
      </div>
    </div>
  </div>

  <div v-if="dialogMode === 'open'" class="modal-mask" @click.self="closeDialog">
    <div class="modal-card">
      <div class="modal-title">打开项目文件夹</div>
      <div class="modal-desc">如果当前环境不支持系统目录选择器，也可以直接输入项目路径。</div>

      <label class="modal-label">
        项目路径
        <div class="modal-input-row">
          <input
            v-model.trim="openProjectPathInput"
            class="modal-input modal-input-flex"
            placeholder="例如：F:\\_WorkSpace\\MyProject"
            :disabled="workspaceStore.isProjectSwitching"
            @keydown.enter.prevent="handleOpenProjectSubmit"
          />
          <button
            class="btn btn-outline"
            type="button"
            :disabled="workspaceStore.isProjectSwitching"
            @click="browseOpenProjectDirectory"
          >
            浏览
          </button>
        </div>
      </label>

      <div v-if="workspaceStore.workspaceError" class="modal-error">{{ workspaceStore.workspaceError }}</div>

      <div class="modal-actions">
        <button class="btn btn-outline" type="button" @click="closeDialog">取消</button>
        <button
          class="btn btn-primary"
          type="button"
          :disabled="!openProjectPathInput || workspaceStore.isProjectSwitching"
          @click="handleOpenProjectSubmit"
        >
          {{ workspaceStore.isProjectSwitching ? "打开中..." : "打开项目" }}
        </button>
      </div>
    </div>
  </div>

  <div v-if="pendingInitializationProject" class="modal-mask" @click.self="dismissInitializationPrompt">
    <div class="modal-card">
      <div class="modal-title">检测到项目缺少默认目录</div>
      <div class="modal-desc">
        当前项目已经打开，但缺少 Storydex 默认目录结构。是否立即创建 <code>.storydex</code> 及相关目录？
      </div>
      <div class="modal-path">{{ pendingInitializationProject.workspaceRoot }}</div>
      <div class="modal-missing">
        缺失目录：{{ pendingInitializationProject.missingDirectories.join(" / ") || "无" }}
      </div>

      <div class="modal-actions">
        <button class="btn btn-outline" type="button" @click="dismissInitializationPrompt">先不创建</button>
        <button
          class="btn btn-primary"
          type="button"
          :disabled="workspaceStore.isProjectInitializing"
          @click="confirmInitializationPrompt"
        >
          {{ workspaceStore.isProjectInitializing ? "初始化中..." : "创建默认目录" }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import storydexIcon from "@/assets/storydex_icon_01.png";
import { useProjectLauncher } from "@/composables/useProjectLauncher";
import { useUiStore } from "@/stores/ui";
import { useWorkspaceStore } from "@/stores/workspace";
import type { WorkspaceProjectInfo } from "@/types/workspace";

const uiStore = useUiStore();
const workspaceStore = useWorkspaceStore();
const launcher = useProjectLauncher();

type OpenMenu = "project" | "help" | null;

const projectMenuRef = ref<HTMLElement | null>(null);
const helpMenuRef = ref<HTMLElement | null>(null);
const openMenu = ref<OpenMenu>(null);
const pendingInitializationProject = ref<WorkspaceProjectInfo | null>(null);
const promptedProjectRoot = ref("");

const dialogMode = launcher.dialogMode;
const createBaseDirectory = launcher.createBaseDirectory;
const createProjectName = launcher.createProjectName;
const openProjectPathInput = launcher.openProjectPathInput;
const newProjectTargetPath = launcher.newProjectTargetPath;
const createValidationMessage = launcher.createValidationMessage;
const canCreateProject = launcher.canCreateProject;

const projectLabel = computed(() => {
  if (workspaceStore.launchScreenVisible) {
    return "尚未打开项目";
  }
  return workspaceStore.currentProject?.projectName || workspaceStore.health?.projectName || "未打开项目";
});

const commandPlaceholder = computed(() => {
  if (workspaceStore.launchScreenVisible) {
    return "搜索 Storydex 命令";
  }
  return "搜索命令、文件或项目内容";
});

const agentHeaderToggleTitle = computed(() => (uiStore.agentCollapsed ? "展开 Agent 侧栏" : "收起 Agent 侧栏"));

watch(
  () => workspaceStore.currentProject,
  (project) => {
    if (!project?.requiresInitialization) {
      if (project?.workspaceRoot) {
        promptedProjectRoot.value = project.workspaceRoot;
      }
      return;
    }

    if (promptedProjectRoot.value === project.workspaceRoot) {
      return;
    }

    pendingInitializationProject.value = project;
    promptedProjectRoot.value = project.workspaceRoot;
  },
  { immediate: true }
);

onMounted(() => {
  document.addEventListener("pointerdown", handleDocumentPointerDown, true);
});

onBeforeUnmount(() => {
  document.removeEventListener("pointerdown", handleDocumentPointerDown, true);
});

function handleDocumentPointerDown(event: PointerEvent): void {
  const target = event.target as Node | null;
  if (!openMenu.value) {
    return;
  }
  if (
    target &&
    (projectMenuRef.value?.contains(target) || helpMenuRef.value?.contains(target))
  ) {
    return;
  }
  openMenu.value = null;
}

function toggleMenu(menu: Exclude<OpenMenu, null>): void {
  openMenu.value = openMenu.value === menu ? null : menu;
}

function closeMenus(): void {
  openMenu.value = null;
}

function openCreateProjectDialog(): void {
  closeMenus();
  launcher.openCreateProjectDialog();
}

function closeDialog(): void {
  launcher.closeDialog();
}

async function browseCreateBaseDirectory(): Promise<void> {
  await launcher.browseCreateBaseDirectory();
}

async function browseOpenProjectDirectory(): Promise<void> {
  await launcher.browseOpenProjectDirectory();
}

async function handleOpenProjectRequest(): Promise<void> {
  closeMenus();
  await launcher.handleOpenProjectRequest();
}

async function handleCreateProjectSubmit(): Promise<void> {
  await launcher.handleCreateProjectSubmit();
}

async function handleOpenProjectSubmit(): Promise<void> {
  await launcher.handleOpenProjectSubmit();
}

async function handleInitializeCurrentProject(): Promise<void> {
  closeMenus();
  try {
    const project = await workspaceStore.initializeCurrentProject();
    pendingInitializationProject.value = project.requiresInitialization ? project : null;
  } catch {
    // handled by store
  }
}

function dismissInitializationPrompt(): void {
  pendingInitializationProject.value = null;
}

async function confirmInitializationPrompt(): Promise<void> {
  if (!pendingInitializationProject.value) {
    return;
  }
  try {
    await workspaceStore.initializeCurrentProject(pendingInitializationProject.value.workspaceRoot);
    pendingInitializationProject.value = null;
  } catch {
    // handled by store
  }
}

function handleRefresh(): void {
  closeMenus();
  void workspaceStore.bootstrap(true);
}

async function handleReturnWelcomePage(): Promise<void> {
  closeMenus();
  const saved = await workspaceStore.saveDirtyActiveFileIfNeeded();
  if (saved) {
    workspaceStore.enterLaunchScreen();
  }
}

async function handleOpenHelpGuide(): Promise<void> {
  closeMenus();
  uiStore.setActivity("resources");
  await workspaceStore.openHelpGuideDocument();
}

function handleOpenAbout(): void {
  closeMenus();
  window.open("https://tensorhub.cn/", "_blank", "noopener,noreferrer");
}
</script>

<style scoped>
.top-header {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.top-header-left,
.top-header-right {
  flex: 1 1 0;
  min-width: 0;
}

.top-header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.top-header-left .file-menu-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  line-height: 1;
}

.file-menu-card-help {
  width: 180px;
}

.top-header-right {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

.top-header-center {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}

.command-bar {
  width: min(680px, calc(100% - 520px));
  pointer-events: auto;
}

.topbar-brand-switch {
  border: 0;
  background: transparent;
  padding: 0 4px 0 0;
  border-radius: 10px;
  cursor: pointer;
  transition: background 180ms ease, box-shadow 180ms ease;
}

.topbar-brand-switch:hover {
  background: color-mix(in srgb, var(--bg-hover) 65%, transparent);
}

.topbar-brand-switch:hover .topbar-brand-name,
.topbar-brand-switch:focus-visible .topbar-brand-name {
  text-shadow: 0 0 8px color-mix(in srgb, var(--accent) 38%, transparent);
}

.topbar-brand-switch:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--accent-soft);
}

.header-utility-btn {
  min-width: 30px;
  height: 30px;
  border: 1px solid var(--border-subtle);
  border-radius: 999px;
  background: color-mix(in srgb, var(--bg-card) 82%, transparent);
  color: var(--text-soft);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}

.header-utility-btn:hover,
.header-utility-btn.active {
  background: var(--bg-hover);
  color: var(--text-main);
}

.header-story-settings-btn {
  padding: 0 12px;
  gap: 6px;
}

.header-story-settings-label {
  font-size: 12px;
  white-space: nowrap;
}

.header-agent-toggle {
  min-width: 28px;
  width: 28px;
  height: 28px;
  padding: 0;
  border-radius: 4px;
  box-shadow: none;
}

.header-agent-toggle.active {
  color: var(--accent);
  background: var(--accent-soft);
}

.header-agent-toggle .material-symbols-rounded {
  font-size: 18px;
}

.activity-account-badge {
  width: 22px;
  height: 22px;
  border-radius: 999px;
  display: grid;
  place-items: center;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
  font-weight: 700;
}

.top-header-account-wrap {
  position: relative;
  display: inline-flex;
  align-items: center;
}

.top-header-account-wrap :deep(.activity-account-menu) {
  left: auto;
  right: 0;
  bottom: auto;
  top: calc(100% + 8px);
}

@media (max-width: 1180px) {
  .command-bar {
    width: min(420px, calc(100% - 440px));
  }
}

@media (max-width: 960px) {
  .top-header {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 10px;
  }

  .top-header-center {
    position: static;
    inset: auto;
    grid-column: 1 / -1;
    pointer-events: auto;
  }

  .command-bar {
    width: 100%;
  }
}

@media (max-width: 760px) {
  .top-header-left {
    flex-wrap: wrap;
  }

  .top-header-right {
    justify-content: flex-end;
  }

  .header-story-settings-label {
    display: none;
  }

  .header-story-settings-btn {
    padding: 0;
  }
}
</style>
