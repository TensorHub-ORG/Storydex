<template>
  <aside class="explorer-panel" @click="closeContextMenu">
    <template v-if="workspaceStore.launchScreenVisible">
      <div class="explorer-header explorer-header-launch">
        <div class="explorer-header-main">
          <div class="explorer-title">资源浏览器</div>
          <div class="explorer-project">尚未打开文件夹</div>
        </div>
      </div>

      <div class="explorer-subtitle">
        先打开一个已有的写作文件夹，或者从这里创建一个全新的 Storydex 项目。
      </div>

      <div class="tree-view explorer-launch-shell">
        <div class="tree-empty explorer-launch-empty">
          <div class="explorer-launch-actions">
            <button
              class="explorer-launch-action"
              type="button"
              :disabled="workspaceStore.isProjectSwitching || workspaceStore.isProjectCreating"
              @click="handleOpenProjectRequest"
            >
              <span class="explorer-launch-action-title">打开文件夹</span>
              <span class="explorer-launch-action-desc">继续已有写作目录</span>
            </button>
            <button
              class="explorer-launch-action"
              type="button"
              :disabled="workspaceStore.isProjectSwitching || workspaceStore.isProjectCreating"
              @click="openCreateProjectDialog"
            >
              <span class="explorer-launch-action-title">新建项目</span>
              <span class="explorer-launch-action-desc">创建新的 Storydex 工作区</span>
            </button>
          </div>
        </div>
      </div>

    </template>

    <template v-else>
      <div class="explorer-header">
        <div class="explorer-header-main">
          <div class="explorer-header-row">
            <div class="explorer-title">资源浏览器</div>

            <div class="explorer-toolbar">
              <button
                class="explorer-toolbar-btn"
                type="button"
                title="新建文件"
                :disabled="toolbarDisabled"
                @click="startRootCreate('file')"
              >
                <span class="material-symbols-rounded">note_add</span>
              </button>
              <button
                class="explorer-toolbar-btn"
                type="button"
                title="新建文件夹"
                :disabled="toolbarDisabled"
                @click="startRootCreate('directory')"
              >
                <span class="material-symbols-rounded">create_new_folder</span>
              </button>
              <button
                class="explorer-toolbar-btn"
                type="button"
                title="刷新"
                :disabled="workspaceStore.isTreeLoading || workspaceStore.isBootstrapping"
                @click="handleRefresh"
              >
                <span class="material-symbols-rounded">refresh</span>
              </button>
              <button
                class="explorer-toolbar-btn"
                type="button"
                title="保存"
                :disabled="saveDisabled"
                @click="handleSave"
              >
                <span class="material-symbols-rounded">save</span>
              </button>
              <button class="explorer-toolbar-btn explorer-problems-trigger" type="button" title="问题" @click="problemsOpen = !problemsOpen">
                <span class="material-symbols-rounded">problem</span>
                <small v-if="workspaceStore.diagnostics.length" class="explorer-problem-badge">{{ formatBadgeCount(workspaceStore.diagnostics.length) }}</small>
              </button>
            </div>
          </div>
          <div class="explorer-project">{{ projectLabel }}</div>
          <div v-if="workspaceStore.projectRootLabel" class="explorer-project-path" :title="workspaceStore.projectRootLabel">
            {{ workspaceStore.projectRootLabel }}
          </div>
        </div>
      </div>

      <div
        class="tree-view"
        :class="{ 'is-import-drop-target': dragTargetPath === ROOT_PARENT_PATH }"
        @contextmenu.prevent="handleRootContextMenu"
        @dragenter.prevent="handleRootDragEnter"
        @dragover.prevent="handleRootDragOver"
        @dragleave="handleRootDragLeave"
        @drop.prevent="handleRootDrop"
      >
        <div
          v-if="pendingCreate && pendingCreate.parentPath === ROOT_PARENT_PATH"
          class="tree-inline-create tree-inline-create-root"
          @click.stop
        >
          <span class="material-symbols-rounded tree-icon">
            {{ pendingCreate.kind === "file" ? "note_add" : "create_new_folder" }}
          </span>
          <input
            ref="pendingCreateInputRef"
            v-model.trim="pendingCreate.value"
            class="tree-inline-create-input"
            :placeholder="pendingCreate.kind === 'file' ? '输入文件名，例如 chapter.md' : '输入文件夹名'"
            @keydown.enter.prevent="submitPendingCreate"
            @keydown.esc.prevent="cancelPendingCreate"
          />
        </div>

        <div v-if="workspaceStore.isBootstrapping || workspaceStore.isTreeLoading" class="tree-empty tree-loading">
          正在加载工作区资源...
        </div>
        <div v-else-if="workspaceStore.workspaceError" class="tree-empty tree-error">
          {{ workspaceStore.workspaceError }}
        </div>
        <div v-else-if="rows.length === 0 && !pendingCreate" class="tree-empty tree-empty-resources">
          当前目录下还没有可展示的资源。可通过顶部图标或右键空白区域创建内容。
        </div>
        <template v-else>
          <template v-for="row in rows" :key="row.key">
            <div class="tree-row-shell">
              <div
                v-if="isRenamingNode(row.node)"
                class="tree-inline-create tree-inline-rename"
                :style="{ paddingLeft: `${12 + row.depth * 16}px` }"
                @click.stop
              >
                <span class="material-symbols-rounded tree-caret hidden">chevron_right</span>
                <span class="material-symbols-rounded tree-icon">{{ iconFor(row.node) }}</span>
                <input
                  ref="pendingRenameInputRef"
                  v-model.trim="pendingRenameValue"
                  class="tree-inline-create-input"
                  :placeholder="row.node.kind === 'file' ? '输入新的文件名' : '输入新的文件夹名'"
                  @keydown.enter.prevent="submitPendingRename"
                  @keydown.esc.prevent="cancelPendingRename"
                />
              </div>

              <button
                v-else
                type="button"
                class="tree-row"
                :class="{
                  active: row.node.relativePath === workspaceStore.activeFile,
                  'is-folder': row.node.kind === 'directory',
                  'is-drop-target': isDirectoryDropTarget(row.node),
                  'has-diagnostics': hasDiagnostics(row.node),
                  'has-direct-diagnostics': hasDirectDiagnostics(row.node)
                }"
                :style="{
                  paddingLeft: `${12 + row.depth * 16}px`,
                  paddingRight: `${treeRowTrailingWidth(row.node)}px`
                }"
                :disabled="workspaceStore.isFileLoading && row.node.kind === 'file'"
                :title="rowTitle(row.node) || undefined"
                @click.stop="handleRowClick(row.node)"
                @contextmenu.prevent.stop="openNodeContextMenu($event, row.node)"
                @dragenter.prevent.stop="handleNodeDragEnter($event, row.node)"
                @dragover.prevent.stop="handleNodeDragOver($event, row.node)"
                @dragleave.stop="handleNodeDragLeave($event, row.node)"
                @drop.prevent.stop="handleNodeDrop($event, row.node)"
              >
                <span class="material-symbols-rounded tree-caret" :class="{ hidden: row.node.kind !== 'directory' }">
                  {{ row.node.kind === "directory" ? (isExpanded(row.node) ? "expand_more" : "chevron_right") : "chevron_right" }}
                </span>
                <span class="material-symbols-rounded tree-icon">{{ iconFor(row.node) }}</span>
                <span class="tree-label">{{ row.node.name }}</span>
                <span v-if="row.node.kind === 'file' && row.node.extension" class="tree-meta">{{ row.node.extension }}</span>
              </button>
              <div
                v-if="hasNodeDecorations(row.node) || isChapterDirectory(row.node)"
                class="tree-row-trailing"
                @click.stop
              >
                <span v-if="hasNodeDecorations(row.node)" class="tree-status-cluster">
                  <span
                    v-for="severity in diagnosticSeverities"
                    v-show="diagnosticCounts(row.node)[severity] > 0"
                    :key="severity"
                    class="tree-diagnostic-number"
                    :class="`is-${severity}`"
                    :title="diagnosticHint(row.node)"
                  >{{ diagnosticCounts(row.node)[severity] }}</span>
                  <span
                    v-if="gitDecoration(row.node)"
                    class="tree-git-decoration"
                    :class="`is-${gitDecoration(row.node)?.tone}`"
                    :title="gitDecoration(row.node)?.title"
                  >{{ gitDecoration(row.node)?.label }}</span>
                </span>
                <label
                  v-if="!isRenamingNode(row.node) && isChapterDirectory(row.node)"
                  class="tree-chapter-state"
                  :class="{ completed: workspaceStore.isChapterCompleted(nodePath(row.node)) }"
                  :title="chapterStateTitle(row.node)"
                  @click.stop
                >
                  <input
                    :checked="workspaceStore.isChapterCompleted(nodePath(row.node))"
                    type="checkbox"
                    :aria-label="chapterStateTitle(row.node)"
                    :disabled="workspaceStore.isStorySettingsLoading"
                    @change="handleChapterCompletionToggle(row.node, $event)"
                  />
                  <span class="material-symbols-rounded">{{ workspaceStore.isChapterCompleted(nodePath(row.node)) ? "check_circle" : "radio_button_unchecked" }}</span>
                </label>
              </div>
            </div>

            <div
              v-if="pendingCreate && shouldRenderCreateRowAfter(row.node)"
              class="tree-inline-create"
              :style="{ paddingLeft: `${28 + row.depth * 16}px` }"
              @click.stop
            >
              <span class="material-symbols-rounded tree-icon">
                {{ pendingCreate.kind === "file" ? "note_add" : "create_new_folder" }}
              </span>
              <input
                ref="pendingCreateInputRef"
                v-model.trim="pendingCreate.value"
                class="tree-inline-create-input"
                :placeholder="pendingCreate.kind === 'file' ? '输入文件名，例如 chapter.md' : '输入文件夹名'"
                @keydown.enter.prevent="submitPendingCreate"
                @keydown.esc.prevent="cancelPendingCreate"
              />
            </div>
          </template>
        </template>
      </div>
    </template>

    <section v-if="problemsOpen && !workspaceStore.launchScreenVisible" class="explorer-problems" @click.stop>
      <header class="explorer-problems-header">
        <strong>问题</strong>
        <label class="explorer-problem-filter">
          <span class="material-symbols-rounded">filter_alt</span>
          <select v-model="problemSeverity" aria-label="筛选问题级别"><option value="all">全部</option><option value="error">错误</option><option value="warning">警告</option><option value="info">提示</option></select>
          <span class="material-symbols-rounded filter-chevron">expand_more</span>
        </label>
        <button class="explorer-problems-close" type="button" title="关闭问题面板" aria-label="关闭问题面板" @click="problemsOpen = false"><span class="material-symbols-rounded">close</span></button>
      </header>
      <div class="explorer-problem-list">
        <div v-for="item in filteredProblems" :key="`${item.relativePath}:${item.code}:${item.line}:${item.message}`" class="explorer-problem-item">
          <button type="button" class="problem-open" @click="openProblem(item.relativePath)"><span class="problem-severity" :class="`is-${item.severity}`"></span><span><strong>{{ item.code || item.source }}</strong><small>{{ item.relativePath }}{{ item.line ? `:${item.line}:${item.column || 1}` : "" }}</small><em>{{ item.message }}</em></span></button>
          <button v-if="item.fixes?.[0]" type="button" class="problem-fix" @click="applyProblemFix(item.relativePath, item.fixes[0].id)">{{ item.fixes[0].label }}</button>
        </div>
        <div v-if="!filteredProblems.length" class="explorer-problem-empty">没有匹配的问题</div>
      </div>
    </section>

    <Teleport to="body">
      <div
        v-if="contextMenu.visible"
        ref="contextMenuRef"
        class="explorer-context-menu"
        :style="{ left: `${contextMenu.x}px`, top: `${contextMenu.y}px` }"
        @click.stop
      >
      <template v-if="contextMenu.target === 'root'">
        <button class="context-menu-item" type="button" @click="startRootCreate('file')">新建文件</button>
        <button class="context-menu-item" type="button" @click="startRootCreate('directory')">新建文件夹</button>
        <div class="context-menu-separator"></div>
        <button class="context-menu-item" type="button" @click="handleRefresh">刷新</button>
        <button class="context-menu-item" type="button" :disabled="saveDisabled" @click="handleSave">保存</button>
        <div class="context-menu-separator"></div>
        <button class="context-menu-item" type="button" @click="handleRevealRoot">在资源管理器中显示</button>
      </template>

      <template v-else-if="contextMenu.node?.kind === 'directory'">
        <button class="context-menu-item" type="button" @click="startCreate('file', contextMenu.node)">新建文件</button>
        <button class="context-menu-item" type="button" @click="startCreate('directory', contextMenu.node)">新建文件夹</button>
        <button class="context-menu-item" type="button" @click="handleReveal(contextMenu.node)">在资源管理器中显示</button>
        <button class="context-menu-item" type="button" disabled>在文件夹中查找...</button>
        <button class="context-menu-item" type="button" @click="handleAddToChat(contextMenu.node)">添加到聊天</button>
        <div class="context-menu-separator"></div>
        <button class="context-menu-item" type="button" @click="handleCut(contextMenu.node)">剪切</button>
        <button class="context-menu-item" type="button" @click="handleCopy(contextMenu.node)">复制</button>
        <button class="context-menu-item" type="button" :disabled="!clipboardState" @click="handlePaste(contextMenu.node)">
          粘贴
        </button>
        <div class="context-menu-separator"></div>
        <button class="context-menu-item" type="button" @click="handleCopyPath(contextMenu.node)">复制路径</button>
        <button class="context-menu-item" type="button" @click="handleCopyRelativePath(contextMenu.node)">
          复制相对路径
        </button>
        <div class="context-menu-separator"></div>
        <button class="context-menu-item" type="button" @click="startRename(contextMenu.node)">重命名...</button>
        <button class="context-menu-item is-danger" type="button" @click="handleDeleteNode(contextMenu.node)">删除</button>
      </template>

      <template v-else-if="contextMenu.node">
        <button class="context-menu-item" type="button" @click="handleOpenToSide(contextMenu.node)">在侧边打开</button>
        <button class="context-menu-item" type="button" @click="handleOpenWith(contextMenu.node)">打开方式...</button>
        <button class="context-menu-item" type="button" @click="handleReveal(contextMenu.node)">在资源管理器中显示</button>
        <div class="context-menu-separator"></div>
        <button class="context-menu-item" type="button" disabled>选择以进行比较</button>
        <button class="context-menu-item" type="button" disabled>打开时间线</button>
        <button class="context-menu-item" type="button" @click="handleAddToChat(contextMenu.node)">添加到聊天</button>
        <div class="context-menu-separator"></div>
        <button class="context-menu-item" type="button" @click="handleCut(contextMenu.node)">剪切</button>
        <button class="context-menu-item" type="button" @click="handleCopy(contextMenu.node)">复制</button>
        <button class="context-menu-item" type="button" @click="handleCopyPath(contextMenu.node)">复制路径</button>
        <button class="context-menu-item" type="button" @click="handleCopyRelativePath(contextMenu.node)">
          复制相对路径
        </button>
        <div class="context-menu-separator"></div>
        <button class="context-menu-item" type="button" @click="startRename(contextMenu.node)">重命名...</button>
        <button class="context-menu-item is-danger" type="button" @click="handleDeleteNode(contextMenu.node)">删除</button>
      </template>
      </div>
    </Teleport>
  </aside>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useProjectLauncher } from "@/composables/useProjectLauncher";
import { useAgentStore } from "@/stores/agent";
import { useGitStore } from "@/stores/git";
import { useWorkspaceStore } from "@/stores/workspace";
import type { WorkspaceDiagnosticItem, WorkspaceGitChangedFile, WorkspaceTreeNode } from "@/types/workspace";

interface TreeRow {
  key: string;
  depth: number;
  node: WorkspaceTreeNode;
}

interface ContextMenuState {
  visible: boolean;
  x: number;
  y: number;
  target: "root" | "node" | null;
  node: WorkspaceTreeNode | null;
}

interface ClipboardState {
  mode: "copy" | "cut";
  node: WorkspaceTreeNode;
}

interface PendingCreateState {
  kind: "file" | "directory";
  parentPath: string;
  value: string;
}

interface PendingRenameState {
  relativePath: string;
  parentPath: string;
  originalName: string;
  value: string;
}

const ROOT_PARENT_PATH = "";
const AUTO_REFRESH_INTERVAL_MS = 4000;

const workspaceStore = useWorkspaceStore();
const agentStore = useAgentStore();
const gitStore = useGitStore();
const { handleOpenProjectRequest, openCreateProjectDialog } = useProjectLauncher();
let autoRefreshTimer: number | null = null;
let lastGitRefreshAt = 0;
const GIT_REFRESH_INTERVAL_MS = 8000;
const diagnosticSeverities = ["error", "warning", "info"] as const;
type DiagnosticSeverity = (typeof diagnosticSeverities)[number];
type DiagnosticCounts = Record<DiagnosticSeverity, number>;
type GitDecoration = { label: string; tone: "uncommitted"; title: string };

const expandedPaths = ref<Record<string, boolean>>({});
const contextMenu = ref<ContextMenuState>({
  visible: false,
  x: 0,
  y: 0,
  target: null,
  node: null
});
const clipboardState = ref<ClipboardState | null>(null);
const pendingCreate = ref<PendingCreateState | null>(null);
const pendingCreateInputRef = ref<HTMLInputElement | null>(null);
const pendingRename = ref<PendingRenameState | null>(null);
const pendingRenameInputRef = ref<HTMLInputElement | null>(null);
const contextMenuRef = ref<HTMLDivElement | null>(null);
const dragTargetPath = ref<string | null>(null);
const dragDepth = ref(0);
const problemsOpen = ref(false);
const problemSeverity = ref<"all" | "error" | "warning" | "info">("all");
const filteredProblems = computed(() => workspaceStore.diagnostics.filter(
  (item) => problemSeverity.value === "all" || item.severity === problemSeverity.value
));
const diagnosticIndex = computed(() => buildPathIndex(workspaceStore.diagnostics));
const gitIndex = computed(() => buildPathIndex(gitStore.summary?.changedFiles || []));
const pendingRenameValue = computed({
  get: () => pendingRename.value?.value ?? "",
  set: (value: string) => {
    if (pendingRename.value) {
      pendingRename.value.value = value;
    }
  }
});

const rows = computed<TreeRow[]>(() => flattenTree(workspaceStore.tree, expandedPaths.value));
const projectLabel = computed(() => workspaceStore.currentProject?.projectName || workspaceStore.health?.projectName || "未打开项目");
const toolbarDisabled = computed(
  () =>
    !workspaceStore.projectRootLabel ||
    workspaceStore.isBootstrapping ||
    workspaceStore.isProjectSwitching
);
const saveDisabled = computed(() => !workspaceStore.isDirty || workspaceStore.isSaving);

function nodePath(node: WorkspaceTreeNode): string {
  return normalizeNodePath(node.relativePath);
}

function normalizeNodePath(relativePath: string | null | undefined): string {
  return String(relativePath || "")
    .replace(/\\/g, "/")
    .replace(/^\/+|\/+$/g, "")
    .trim();
}

function isChapterDirectory(node: WorkspaceTreeNode): boolean {
  const relativePath = nodePath(node);
  return node.kind === "directory" && /^chapters\/[^/]+$/i.test(relativePath);
}

function diagnosticCount(node: WorkspaceTreeNode): number {
  const relativePath = nodePath(node);
  if (!relativePath) {
    return 0;
  }
  return workspaceStore.diagnosticCountForPath(relativePath);
}

function diagnosticsForNode(node: WorkspaceTreeNode): WorkspaceDiagnosticItem[] {
  const relativePath = nodePath(node);
  if (!relativePath) return [];
  return node.kind === "directory"
    ? diagnosticIndex.value.aggregate.get(relativePath) || []
    : diagnosticIndex.value.direct.get(relativePath) || [];
}

function diagnosticCounts(node: WorkspaceTreeNode): DiagnosticCounts {
  const counts: DiagnosticCounts = { error: 0, warning: 0, info: 0 };
  for (const item of diagnosticsForNode(node)) {
    const severity = diagnosticSeverities.includes(item.severity as DiagnosticSeverity)
      ? item.severity as DiagnosticSeverity
      : "info";
    counts[severity] += 1;
  }
  return counts;
}

function gitFilesForNode(node: WorkspaceTreeNode): WorkspaceGitChangedFile[] {
  const relativePath = nodePath(node);
  if (!relativePath) return [];
  return node.kind === "directory"
    ? gitIndex.value.aggregate.get(relativePath) || []
    : gitIndex.value.direct.get(relativePath) || [];
}

function buildPathIndex<T extends { relativePath: string }>(items: T[]): {
  direct: Map<string, T[]>;
  aggregate: Map<string, T[]>;
} {
  const direct = new Map<string, T[]>();
  const aggregate = new Map<string, T[]>();
  for (const item of items) {
    const relativePath = normalizeNodePath(item.relativePath);
    if (!relativePath) continue;
    direct.set(relativePath, [...(direct.get(relativePath) || []), item]);
    const parts = relativePath.split("/");
    for (let index = 1; index <= parts.length; index += 1) {
      const ancestor = parts.slice(0, index).join("/");
      aggregate.set(ancestor, [...(aggregate.get(ancestor) || []), item]);
    }
  }
  return { direct, aggregate };
}

function gitDecoration(node: WorkspaceTreeNode): GitDecoration | null {
  const files = gitFilesForNode(node);
  if (!files.length) return null;
  if (node.kind === "directory") {
    return { label: String(files.length), tone: "uncommitted", title: `${files.length} 个未提交文件` };
  }
  return gitFileDecoration(files[0]);
}

function gitFileDecoration(file: WorkspaceGitChangedFile): GitDecoration {
  const status = String(file.status || "").padEnd(2, " ").slice(0, 2);
  const stageState = [file.staged ? "已暂存" : "", file.unstaged ? "未暂存" : ""].filter(Boolean).join("、");
  return {
    label: "U",
    tone: "uncommitted",
    title: `未提交${stageState ? ` · ${stageState}` : ""} · Git ${status}`
  };
}

function hasNodeDecorations(node: WorkspaceTreeNode): boolean {
  return diagnosticsForNode(node).length > 0 || gitFilesForNode(node).length > 0;
}

function treeRowTrailingWidth(node: WorkspaceTreeNode): number {
  const counts = diagnosticCounts(node);
  const diagnosticSlots = diagnosticSeverities.filter((severity) => counts[severity] > 0).length;
  const gitSlots = gitDecoration(node) ? 1 : 0;
  const chapterSlots = isChapterDirectory(node) ? 1 : 0;
  const totalSlots = diagnosticSlots + gitSlots + chapterSlots;
  return totalSlots > 0 ? 14 + totalSlots * 14 + Math.max(0, totalSlots - 1) * 3 : 10;
}

function formatBadgeCount(count: number): string {
  return count > 99 ? "99+" : String(Math.max(0, count));
}

function directDiagnosticCount(node: WorkspaceTreeNode): number {
  const relativePath = nodePath(node);
  if (!relativePath) {
    return 0;
  }
  return workspaceStore.diagnostics.filter((item) => normalizeNodePath(item.relativePath) === relativePath).length;
}

function hasDiagnostics(node: WorkspaceTreeNode): boolean {
  return diagnosticCount(node) > 0;
}

function hasDirectDiagnostics(node: WorkspaceTreeNode): boolean {
  return directDiagnosticCount(node) > 0;
}

function diagnosticSeverity(node: WorkspaceTreeNode): "error" | "warning" | "info" {
  const relativePath = nodePath(node);
  const items = relativePath ? workspaceStore.diagnosticsForPath(relativePath) : [];
  if (items.some((item) => item.severity === "error")) return "error";
  if (items.some((item) => item.severity === "warning")) return "warning";
  return "info";
}

function diagnosticHint(node: WorkspaceTreeNode): string {
  const relativePath = nodePath(node);
  if (!relativePath) {
    return "";
  }
  const items = workspaceStore.diagnosticsForPath(relativePath);
  if (!items.length) {
    return "";
  }
  const preview = items
    .slice(0, 3)
    .map((item) => `[${String(item.severity || "info").toUpperCase()}] ${item.code ? `${item.code}: ` : ""}${item.message}${item.line ? ` (行 ${item.line}${item.column ? `:${item.column}` : ""})` : ""}${item.evidence ? `\n证据：${item.evidence}` : ""}`)
    .join("\n");
  return items.length > 3 ? `${preview}\n...还有 ${items.length - 3} 条诊断` : preview;
}

async function openProblem(relativePath: string): Promise<void> {
  if (relativePath) await workspaceStore.openFile(relativePath);
}

async function applyProblemFix(relativePath: string, fixId: string): Promise<void> {
  await workspaceStore.applyDiagnosticFix(relativePath, fixId);
}

function shouldShowRowActions(node: WorkspaceTreeNode): boolean {
  return showDiagnosticDot(node) || isChapterDirectory(node);
}

function showDiagnosticDot(node: WorkspaceTreeNode): boolean {
  return node.kind === "file" && hasDirectDiagnostics(node);
}

function chapterCompletionLabel(node: WorkspaceTreeNode): string {
  return workspaceStore.isChapterCompleted(nodePath(node)) ? "章节已完成" : "章节未完成";
}

function isStorySegmentNode(node: WorkspaceTreeNode): boolean {
  const relativePath = nodePath(node);
  return node.kind === "file" && /^chapters\/[^/]+\/seg-[^/]+\.(md|txt)$/i.test(relativePath);
}

function storyDiagnosticCount(node: WorkspaceTreeNode): number {
  const relativePath = nodePath(node);
  if (!relativePath || (!isChapterDirectory(node) && !isStorySegmentNode(node))) {
    return 0;
  }
  return workspaceStore.diagnosticCountForPath(relativePath);
}

function storyDiagnosticHint(node: WorkspaceTreeNode): string {
  const relativePath = nodePath(node);
  if (!relativePath) {
    return "";
  }
  const items = workspaceStore.diagnosticsForPath(relativePath);
  if (!items.length) {
    return "";
  }
  const preview = items
    .slice(0, 3)
    .map((item) => `${item.message}${item.line ? ` (L${item.line}${item.column ? `:${item.column}` : ""})` : ""}`)
    .join("\n");
  return items.length > 3 ? `${preview}\n另有 ${items.length - 3} 条诊断` : preview;
}

function shouldShowStoryRowActions(node: WorkspaceTreeNode): boolean {
  return storyDiagnosticCount(node) > 0 || isChapterDirectory(node);
}

function chapterStateTitle(node: WorkspaceTreeNode): string {
  return workspaceStore.isChapterCompleted(nodePath(node)) ? "章节已完结" : "章节未完结";
}

const rowTitle = (node: WorkspaceTreeNode): string => {
  const parts: string[] = [];
  if (isChapterDirectory(node)) {
    parts.push(chapterStateTitle(node));
  }
  const hint = diagnosticHint(node);
  if (hint) {
    parts.push(hint);
  }
  return parts.join("\n");
};

function isRenamingNode(node: WorkspaceTreeNode): boolean {
  return normalizePath(node.relativePath || "") === normalizePath(pendingRename.value?.relativePath || "");
}

watch(
  () => workspaceStore.treeResetToken,
  () => {
    expandedPaths.value = {};
    pendingCreate.value = null;
    pendingRename.value = null;
    closeContextMenu();
  }
);

watch(
  () => workspaceStore.launchScreenVisible,
  (visible) => {
    if (!visible) {
      return;
    }
    expandedPaths.value = {};
    clipboardState.value = null;
    cancelPendingCreate();
    cancelPendingRename();
    closeContextMenu();
  }
);

onMounted(() => {
  window.addEventListener("pointerdown", handleWindowPointerDown, true);
  window.addEventListener("click", closeContextMenu);
  window.addEventListener("blur", closeContextMenu);
  autoRefreshTimer = window.setInterval(handleAutoRefresh, AUTO_REFRESH_INTERVAL_MS);
  if (!workspaceStore.launchScreenVisible) {
    lastGitRefreshAt = Date.now();
    void gitStore.refreshSummary({ silent: true });
  }
});

onBeforeUnmount(() => {
  window.removeEventListener("pointerdown", handleWindowPointerDown, true);
  window.removeEventListener("click", closeContextMenu);
  window.removeEventListener("blur", closeContextMenu);
  if (autoRefreshTimer !== null) {
    window.clearInterval(autoRefreshTimer);
    autoRefreshTimer = null;
  }
});

function handleRefresh(): void {
  closeContextMenu();
  cancelPendingCreate();
  cancelPendingRename();
  workspaceStore.collapseTree();
  lastGitRefreshAt = Date.now();
  void Promise.all([
    workspaceStore.refreshTree({ silent: false }),
    gitStore.refreshSummary({ silent: true })
  ]);
}

function handleAutoRefresh(): void {
  if (
    workspaceStore.launchScreenVisible ||
    workspaceStore.isBootstrapping ||
    workspaceStore.isTreeLoading ||
    workspaceStore.isProjectSwitching ||
    pendingCreate.value ||
    pendingRename.value
  ) {
    return;
  }
  void workspaceStore.refreshTree({ silent: true });
  if (Date.now() - lastGitRefreshAt >= GIT_REFRESH_INTERVAL_MS) {
    lastGitRefreshAt = Date.now();
    void gitStore.refreshSummary({ silent: true });
  }
}

function handleWindowPointerDown(event: PointerEvent): void {
  const target = event.target instanceof Element ? event.target : null;
  if (target?.closest(".tree-inline-create")) {
    return;
  }
  if (pendingCreate.value) {
    cancelPendingCreate();
  }
  if (pendingRename.value) {
    cancelPendingRename();
  }
}

function handleSave(): void {
  closeContextMenu();
  void workspaceStore.saveActiveFile();
}

function handleRowClick(node: WorkspaceTreeNode): void {
  closeContextMenu();
  cancelPendingRename();
  if (node.kind === "directory") {
    toggleDirectory(node);
    return;
  }
  cancelPendingCreate();
  if (node.relativePath) {
    void workspaceStore.openFile(node.relativePath);
  }
}

function handleChapterCompletionToggle(node: WorkspaceTreeNode, event: Event): void {
  const target = event.target as HTMLInputElement | null;
  const chapterPath = nodePath(node);
  if (!target || !chapterPath) {
    return;
  }
  void workspaceStore.setChapterCompletion(chapterPath, target.checked);
}

function openNodeContextMenu(event: MouseEvent, node: WorkspaceTreeNode): void {
  openContextMenuAt(event, "node", node);
}

function handleRootContextMenu(event: MouseEvent): void {
  if (workspaceStore.launchScreenVisible) {
    return;
  }
  const target = event.target as HTMLElement | null;
  if (target?.closest(".tree-row")) {
    return;
  }
  openContextMenuAt(event, "root", null);
}

async function openContextMenuAt(
  event: MouseEvent,
  target: "root" | "node",
  node: WorkspaceTreeNode | null
): Promise<void> {
  cancelPendingRename();
  const anchorX = event.clientX;
  const anchorY = event.clientY;
  contextMenu.value = {
    visible: true,
    x: anchorX,
    y: anchorY,
    target,
    node
  };
  await nextTick();
  repositionContextMenu(anchorX, anchorY);
}

function closeContextMenu(): void {
  contextMenu.value.visible = false;
  contextMenu.value.target = null;
  contextMenu.value.node = null;
}

function isDirectoryDropTarget(node: WorkspaceTreeNode): boolean {
  return node.kind === "directory" && dragTargetPath.value === getDirectoryPath(node);
}

function handleRootDragEnter(event: DragEvent): void {
  if (!hasExternalFiles(event)) {
    return;
  }
  dragDepth.value += 1;
  dragTargetPath.value = ROOT_PARENT_PATH;
}

function handleRootDragOver(event: DragEvent): void {
  if (!hasExternalFiles(event)) {
    return;
  }
  event.dataTransfer!.dropEffect = "copy";
  dragTargetPath.value = ROOT_PARENT_PATH;
}

function handleRootDragLeave(event: DragEvent): void {
  if (!hasExternalFiles(event)) {
    return;
  }
  dragDepth.value = Math.max(0, dragDepth.value - 1);
  if (dragDepth.value === 0) {
    dragTargetPath.value = null;
  }
}

async function handleRootDrop(event: DragEvent): Promise<void> {
  if (!hasExternalFiles(event)) {
    return;
  }
  dragDepth.value = 0;
  dragTargetPath.value = null;
  await importDroppedFiles(event, ROOT_PARENT_PATH);
}

function handleNodeDragEnter(event: DragEvent, node: WorkspaceTreeNode): void {
  if (node.kind !== "directory" || !hasExternalFiles(event)) {
    return;
  }
  dragTargetPath.value = getDirectoryPath(node);
  expandedPaths.value = {
    ...expandedPaths.value,
    [getNodeKey(node)]: true
  };
}

function handleNodeDragOver(event: DragEvent, node: WorkspaceTreeNode): void {
  if (node.kind !== "directory" || !hasExternalFiles(event)) {
    return;
  }
  event.dataTransfer!.dropEffect = "copy";
  dragTargetPath.value = getDirectoryPath(node);
}

function handleNodeDragLeave(event: DragEvent, node: WorkspaceTreeNode): void {
  if (node.kind !== "directory") {
    return;
  }
  const nextTarget = event.relatedTarget as Node | null;
  const currentTarget = event.currentTarget as HTMLElement | null;
  if (currentTarget && nextTarget && currentTarget.contains(nextTarget)) {
    return;
  }
  if (dragTargetPath.value === getDirectoryPath(node)) {
    dragTargetPath.value = null;
  }
}

async function handleNodeDrop(event: DragEvent, node: WorkspaceTreeNode): Promise<void> {
  if (node.kind !== "directory" || !hasExternalFiles(event)) {
    return;
  }
  dragDepth.value = 0;
  dragTargetPath.value = null;
  await importDroppedFiles(event, getDirectoryPath(node));
}

function repositionContextMenu(anchorX: number, anchorY: number): void {
  const menuElement = contextMenuRef.value;
  if (!menuElement || !contextMenu.value.visible) {
    return;
  }

  const margin = 12;
  const menuWidth = menuElement.offsetWidth;
  const menuHeight = menuElement.offsetHeight;
  const maxX = Math.max(margin, window.innerWidth - menuWidth - margin);
  const maxY = Math.max(margin, window.innerHeight - menuHeight - margin);

  contextMenu.value = {
    ...contextMenu.value,
    x: Math.min(Math.max(anchorX, margin), maxX),
    y: Math.min(Math.max(anchorY, margin), maxY)
  };
}

function toggleDirectory(node: WorkspaceTreeNode): void {
  const key = getNodeKey(node);
  expandedPaths.value = {
    ...expandedPaths.value,
    [key]: !isExpanded(node)
  };
}

function isExpanded(node: WorkspaceTreeNode): boolean {
  return expandedPaths.value[getNodeKey(node)] ?? false;
}

function iconFor(node: WorkspaceTreeNode): string {
  if (node.kind === "directory") {
    return isExpanded(node) ? "folder_open" : "folder";
  }
  if (node.extension === ".json") return "data_object";
  if (node.extension === ".md") return "markdown";
  if (node.extension === ".vue") return "web";
  if (node.extension === ".py") return "code";
  return "description";
}

function getNodeKey(node: WorkspaceTreeNode): string {
  return node.relativePath || node.name;
}

function flattenTree(nodes: WorkspaceTreeNode[], expanded: Record<string, boolean>, depth = 0): TreeRow[] {
  const nextRows: TreeRow[] = [];
  for (const node of nodes) {
    nextRows.push({
      key: `${getNodeKey(node)}-${depth}`,
      depth,
      node
    });
    if (node.kind === "directory" && Array.isArray(node.children) && node.children.length > 0) {
      const isOpen = expanded[getNodeKey(node)] ?? false;
      if (isOpen) {
        nextRows.push(...flattenTree(node.children, expanded, depth + 1));
      }
    }
  }
  return nextRows;
}

function shouldRenderCreateRowAfter(node: WorkspaceTreeNode): boolean {
  return Boolean(
    pendingCreate.value &&
      node.kind === "directory" &&
      getNodeKey(node) === pendingCreate.value.parentPath
  );
}

function startRootCreate(kind: "file" | "directory"): void {
  startCreate(kind, null);
}

function startCreate(kind: "file" | "directory", node: WorkspaceTreeNode | null = null): void {
  closeContextMenu();
  cancelPendingRename();
  const parentPath = node ? getDirectoryPath(node) : ROOT_PARENT_PATH;
  if (node?.kind === "directory") {
    expandedPaths.value = {
      ...expandedPaths.value,
      [getNodeKey(node)]: true
    };
  }
  pendingCreate.value = {
    kind,
    parentPath,
    value: ""
  };
  void nextTick(() => pendingCreateInputRef.value?.focus());
}

function cancelPendingCreate(): void {
  pendingCreate.value = null;
}

function cancelPendingRename(): void {
  pendingRename.value = null;
}

function startRename(node: WorkspaceTreeNode | null): void {
  if (!node?.relativePath) {
    closeContextMenu();
    return;
  }
  closeContextMenu();
  cancelPendingCreate();
  pendingRename.value = {
    relativePath: node.relativePath,
    parentPath: getParentPath(node.relativePath),
    originalName: node.name,
    value: node.name
  };
  void nextTick(() => {
    pendingRenameInputRef.value?.focus();
    pendingRenameInputRef.value?.select();
  });
}

async function submitPendingRename(): Promise<void> {
  const draft = pendingRename.value;
  if (!draft) {
    return;
  }
  const name = sanitizeLeafName(draft.value);
  if (!name) {
    pendingRenameInputRef.value?.focus();
    pendingRenameInputRef.value?.select();
    return;
  }
  if (name === draft.originalName) {
    cancelPendingRename();
    return;
  }
  const targetPath = joinRelativePath(draft.parentPath, name);
  if (!targetPath || normalizePath(targetPath) === normalizePath(draft.relativePath)) {
    cancelPendingRename();
    return;
  }
  try {
    await workspaceStore.renamePath(draft.relativePath, targetPath);
    cancelPendingRename();
  } catch {
    pendingRenameInputRef.value?.focus();
    pendingRenameInputRef.value?.select();
  }
}

async function submitPendingCreate(): Promise<void> {
  const draft = pendingCreate.value;
  if (!draft) {
    return;
  }
  const name = sanitizeLeafName(draft.value);
  if (!name) {
    pendingCreateInputRef.value?.focus();
    return;
  }
  const relativePath = joinRelativePath(draft.parentPath, name);
  if (!relativePath) {
    return;
  }
  try {
    if (draft.kind === "file") {
      await workspaceStore.createFile(relativePath, "");
      await workspaceStore.openFile(relativePath);
    } else {
      await workspaceStore.createDirectory(relativePath);
      expandedPaths.value = {
        ...expandedPaths.value,
        [relativePath]: true
      };
    }
    cancelPendingCreate();
  } catch {
    // handled by store
  }
}

function handleOpenToSide(node: WorkspaceTreeNode): void {
  closeContextMenu();
  cancelPendingCreate();
  if (node.relativePath) {
    void workspaceStore.openFile(node.relativePath);
  }
}

async function handleOpenWith(node: WorkspaceTreeNode): Promise<void> {
  closeContextMenu();
  const absolutePath = absolutePathFor(node.relativePath);
  if (!absolutePath) {
    return;
  }
  if (window.storydexDesktop?.openWithDialog) {
    await window.storydexDesktop.openWithDialog(absolutePath);
    return;
  }
  if (node.relativePath) {
    void workspaceStore.openFile(node.relativePath, { forceReload: true });
  }
}

async function handleReveal(node: WorkspaceTreeNode): Promise<void> {
  closeContextMenu();
  const absolutePath = absolutePathFor(node.relativePath);
  if (!absolutePath) {
    return;
  }
  if (window.storydexDesktop?.revealPath) {
    await window.storydexDesktop.revealPath(absolutePath);
    return;
  }
  await writeClipboard(absolutePath);
}

async function handleRevealRoot(): Promise<void> {
  closeContextMenu();
  const absolutePath = absolutePathFor("");
  if (!absolutePath) {
    return;
  }
  if (window.storydexDesktop?.revealPath) {
    await window.storydexDesktop.revealPath(absolutePath);
    return;
  }
  await writeClipboard(absolutePath);
}

function handleAddToChat(node: WorkspaceTreeNode): void {
  closeContextMenu();
  const relativePath = node.relativePath || node.name;
  const prefix = node.kind === "directory" ? "请处理这个目录：" : "请处理这个文件：";
  agentStore.promptInput = [agentStore.promptInput.trim(), `${prefix}${relativePath}`].filter(Boolean).join("\n");
}

function handleCut(node: WorkspaceTreeNode): void {
  clipboardState.value = { mode: "cut", node };
  closeContextMenu();
}

function handleCopy(node: WorkspaceTreeNode): void {
  clipboardState.value = { mode: "copy", node };
  closeContextMenu();
}

async function handlePaste(targetNode: WorkspaceTreeNode): Promise<void> {
  closeContextMenu();
  const clipboard = clipboardState.value;
  if (!clipboard || !clipboard.node.relativePath) {
    return;
  }
  const targetDirectory = getDirectoryPath(targetNode);
  const proposedTarget = buildPastedRelativePath(
    clipboard.node,
    targetDirectory,
    workspaceStore.tree,
    clipboard.mode
  );
  if (!proposedTarget) {
    if (clipboard.mode === "cut") {
      clipboardState.value = null;
    }
    return;
  }
  try {
    if (clipboard.mode === "cut") {
      await workspaceStore.movePath(clipboard.node.relativePath, proposedTarget);
      clipboardState.value = null;
    } else {
      await workspaceStore.copyPath(clipboard.node.relativePath, proposedTarget);
    }
  } catch {
    // handled by store
  }
}

async function handleRename(node: WorkspaceTreeNode): Promise<void> {
  closeContextMenu();
  if (!node.relativePath) {
    return;
  }
  const nextName = window.prompt("请输入新的名称", node.name);
  const sanitized = sanitizeLeafName(nextName || "");
  if (!sanitized || sanitized === node.name) {
    return;
  }
  const targetPath = joinRelativePath(getParentPath(node.relativePath), sanitized);
  if (!targetPath) {
    return;
  }
  try {
    await workspaceStore.renamePath(node.relativePath, targetPath);
  } catch {
    // handled by store
  }
}

async function handleDelete(node: WorkspaceTreeNode): Promise<void> {
  closeContextMenu();
  if (!node.relativePath) {
    return;
  }
  const confirmed = window.confirm(`确定要删除“${node.name}”吗？`);
  if (!confirmed) {
    return;
  }
  try {
    await workspaceStore.deletePath(node.relativePath);
  } catch {
    // handled by store
  }
}

async function handleRenameNode(node: WorkspaceTreeNode | null): Promise<void> {
  if (!node?.relativePath) {
    closeContextMenu();
    return;
  }
  closeContextMenu();
  const nextName = window.prompt("请输入新的名称", node.name);
  if (nextName === null) {
    return;
  }
  const sanitized = sanitizeLeafName(nextName);
  if (!sanitized || sanitized === node.name) {
    return;
  }
  const targetPath = joinRelativePath(getParentPath(node.relativePath), sanitized);
  if (!targetPath) {
    return;
  }
  try {
    await workspaceStore.renamePath(node.relativePath, targetPath);
  } catch {
    // handled by store
  }
}

async function handleDeleteNode(node: WorkspaceTreeNode | null): Promise<void> {
  if (!node?.relativePath) {
    closeContextMenu();
    return;
  }
  closeContextMenu();
  const confirmed = window.confirm(`确定要删除“${node.name}”吗？`);
  if (!confirmed) {
    return;
  }
  try {
    await workspaceStore.deletePath(node.relativePath);
  } catch {
    // handled by store
  }
}

async function handleCopyPath(node: WorkspaceTreeNode): Promise<void> {
  closeContextMenu();
  const absolutePath = absolutePathFor(node.relativePath);
  if (absolutePath) {
    await writeClipboard(absolutePath);
  }
}

async function handleCopyRelativePath(node: WorkspaceTreeNode): Promise<void> {
  closeContextMenu();
  const relativePath = node.relativePath || node.name;
  if (relativePath) {
    await writeClipboard(relativePath);
  }
}

function hasExternalFiles(event: DragEvent): boolean {
  const transfer = event.dataTransfer;
  if (!transfer) {
    return false;
  }
  return Array.from(transfer.types || []).includes("Files");
}

async function importDroppedFiles(event: DragEvent, targetDirectory: string): Promise<void> {
  const files = Array.from(event.dataTransfer?.files || []).filter((file) => file.name && file.size >= 0);
  if (!files.length) {
    return;
  }
  try {
    const payload = await Promise.all(
      files.map(async (file) => ({
        name: file.name,
        contentBase64: await fileToBase64(file)
      }))
    );
    await workspaceStore.importFiles(targetDirectory, payload);
    if (targetDirectory) {
      expandedPaths.value = {
        ...expandedPaths.value,
        [targetDirectory]: true
      };
    }
  } catch {
    // handled by store/API error display
  }
}

async function fileToBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const chunkSize = 0x8000;
  let binary = "";
  for (let index = 0; index < bytes.length; index += chunkSize) {
    const chunk = bytes.subarray(index, index + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  return window.btoa(binary);
}

function getDirectoryPath(node: WorkspaceTreeNode): string {
  if (node.kind === "directory") {
    return node.relativePath || ROOT_PARENT_PATH;
  }
  return getParentPath(node.relativePath || "");
}

function getParentPath(relativePath: string): string {
  const normalized = normalizePath(relativePath);
  if (!normalized || !normalized.includes("/")) {
    return ROOT_PARENT_PATH;
  }
  return normalized.slice(0, normalized.lastIndexOf("/"));
}

function joinRelativePath(parentPath: string, name: string): string {
  const normalizedName = sanitizeLeafName(name);
  if (!normalizedName) {
    return "";
  }
  const normalizedParent = normalizePath(parentPath);
  return normalizedParent ? `${normalizedParent}/${normalizedName}` : normalizedName;
}

function sanitizeLeafName(value: string): string {
  const raw = String(value || "").trim().replace(/\\/g, "/");
  const leaf = raw.split("/").filter(Boolean).pop() || "";
  return leaf.replace(/[<>:"|?*\u0000-\u001F]/g, "").trim();
}

function normalizePath(value: string): string {
  return String(value || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "").trim();
}

function absolutePathFor(relativePath: string | null): string {
  const projectRoot = workspaceStore.projectRootLabel;
  if (!projectRoot) {
    return "";
  }
  const normalizedRoot = projectRoot.replace(/[\\/]+$/, "");
  const normalizedRelative = normalizePath(relativePath || "");
  if (!normalizedRelative) {
    return normalizedRoot;
  }
  const separator = normalizedRoot.includes("\\") ? "\\" : "/";
  return `${normalizedRoot}${separator}${normalizedRelative.split("/").join(separator)}`;
}

function buildPastedRelativePath(
  sourceNode: WorkspaceTreeNode,
  targetDirectory: string,
  roots: WorkspaceTreeNode[],
  mode: "copy" | "cut"
): string {
  const sourcePath = normalizePath(sourceNode.relativePath || "");
  const candidatePath = joinRelativePath(targetDirectory, sourceNode.name);
  if (!candidatePath) {
    return "";
  }

  if (mode === "cut" && candidatePath === sourcePath) {
    return "";
  }

  if (candidatePath !== sourcePath && !treeContainsPath(roots, candidatePath)) {
    return candidatePath;
  }

  const { stem, extension, copyIndex } = splitCopyName(sourceNode.name, sourceNode.kind);
  let attempt = Math.max(copyIndex + 1, 1);
  while (attempt < 500) {
    const suffix = attempt === 1 ? " copy" : ` copy ${attempt}`;
    const candidateName = `${stem}${suffix}${extension}`;
    const nextCandidatePath = joinRelativePath(targetDirectory, candidateName);
    if (!nextCandidatePath) {
      return "";
    }
    if (nextCandidatePath !== sourcePath && !treeContainsPath(roots, nextCandidatePath)) {
      return nextCandidatePath;
    }
    attempt += 1;
  }
  return joinRelativePath(targetDirectory, `${stem}-${Date.now()}${extension}`);
}

function splitCopyName(
  name: string,
  kind: WorkspaceTreeNode["kind"]
): { stem: string; extension: string; copyIndex: number } {
  const extension = kind === "file" && name.includes(".") ? name.slice(name.lastIndexOf(".")) : "";
  const baseName = extension ? name.slice(0, -extension.length) : name;
  const match = baseName.match(/^(.*?)(?: copy(?: (\d+))?)?$/i);
  const stem = (match?.[1] || baseName).trim() || baseName;
  const copyIndex = match?.[2] ? Number(match[2]) : / copy$/i.test(baseName) ? 1 : 0;
  return { stem, extension, copyIndex };
}

function treeContainsPath(nodes: WorkspaceTreeNode[], relativePath: string): boolean {
  for (const node of nodes) {
    if (node.relativePath === relativePath) {
      return true;
    }
    if (node.kind === "directory" && Array.isArray(node.children) && treeContainsPath(node.children, relativePath)) {
      return true;
    }
  }
  return false;
}

async function writeClipboard(text: string): Promise<void> {
  if (!text) {
    return;
  }
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  window.prompt("复制以下文本", text);
}
defineExpose({
  __testUtils: import.meta.env.MODE === "test" ? {
    expandedPaths, contextMenu, clipboardState, pendingCreate, pendingRename, pendingRenameValue, dragTargetPath, rows,
    nodePath, normalizeNodePath, isChapterDirectory, diagnosticCount, directDiagnosticCount, hasDiagnostics,
    diagnosticsForNode, diagnosticCounts, gitFilesForNode, gitDecoration, gitFileDecoration, buildPathIndex, hasNodeDecorations, treeRowTrailingWidth, formatBadgeCount,
    hasDirectDiagnostics, diagnosticSeverity, diagnosticHint, shouldShowRowActions, showDiagnosticDot, chapterCompletionLabel,
    isStorySegmentNode, storyDiagnosticCount, storyDiagnosticHint, shouldShowStoryRowActions, chapterStateTitle,
    isRenamingNode, handleRefresh, handleAutoRefresh, handleWindowPointerDown, handleSave, handleRowClick,
    handleChapterCompletionToggle, openNodeContextMenu, handleRootContextMenu, openContextMenuAt, closeContextMenu,
    isDirectoryDropTarget, handleRootDragEnter, handleRootDragOver, handleRootDragLeave, handleRootDrop,
    handleNodeDragEnter, handleNodeDragOver, handleNodeDragLeave, handleNodeDrop, repositionContextMenu,
    toggleDirectory, isExpanded, iconFor, getNodeKey, flattenTree, shouldRenderCreateRowAfter, startRootCreate,
    startCreate, cancelPendingCreate, cancelPendingRename, startRename, submitPendingRename, submitPendingCreate,
    handleOpenToSide, handleOpenWith, handleReveal, handleRevealRoot, handleAddToChat, handleCut, handleCopy,
    handlePaste, handleRename, handleDelete, handleRenameNode, handleDeleteNode, handleCopyPath,
    handleCopyRelativePath, hasExternalFiles, importDroppedFiles, fileToBase64, getDirectoryPath, getParentPath,
    joinRelativePath, sanitizeLeafName, normalizePath, absolutePathFor, buildPastedRelativePath, splitCopyName,
    treeContainsPath, writeClipboard
  } : null
});
</script>

<style scoped>
.tree-empty-resources {
  padding: 10px 14px;
  font-size: 12px;
  line-height: 1.6;
  letter-spacing: 0;
}

.explorer-header,
.explorer-header-main,
.explorer-header-row,
.explorer-toolbar { overflow: visible; }
.explorer-header-row { min-width: 0; gap: 4px; }
.explorer-title { flex: 1 1 auto; min-width: 0; }
.explorer-toolbar { flex: 0 0 auto; gap: 0; padding-right: 2px; }
.explorer-toolbar-btn { flex: 0 0 26px; box-sizing: border-box; }
.explorer-problems-trigger { position: relative; display: inline-flex; align-items: center; justify-content: center; overflow: visible; padding: 0; }
.explorer-problems-trigger > .material-symbols-rounded { width: 18px; display: inline-flex; align-items: center; justify-content: center; overflow: visible; font-size: 18px; line-height: 1; }
.explorer-problem-badge { position: absolute; right: 0; bottom: 0; min-width: 13px; height: 13px; padding: 0 3px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--bg-panel); border-radius: 7px; background: var(--state-danger); color: var(--accent-contrast, #fff); font-size: 8px; font-weight: 700; line-height: 1; font-variant-numeric: tabular-nums; }
.explorer-problems { position: absolute; inset: auto 8px 8px; z-index: 12; max-height: 46%; display: flex; flex-direction: column; overflow: hidden; opacity: 1; border: 1px solid var(--border-ghost); border-radius: 5px; background: var(--bg-sidebar, #fff); box-shadow: var(--shadow-popover); backdrop-filter: none; }
.explorer-problems-header { display: flex; align-items: center; gap: 7px; padding: 7px 8px 7px 10px; border-bottom: 1px solid var(--border-ghost); font-size: 12px; }
.explorer-problems-header strong { flex: 1; }
.explorer-problem-filter { position: relative; height: 26px; display: inline-flex; align-items: center; gap: 3px; padding-left: 7px; border: 1px solid color-mix(in srgb, var(--text-muted) 22%, transparent); border-radius: 4px; background: color-mix(in srgb, var(--bg-card) 92%, transparent); color: var(--text-muted); }
.explorer-problem-filter:hover { border-color: color-mix(in srgb, var(--accent) 36%, var(--border-subtle)); color: var(--text-main); }
.explorer-problem-filter:focus-within { border-color: var(--accent); box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 25%, transparent); }
.explorer-problem-filter .material-symbols-rounded { flex: none; font-size: 15px; pointer-events: none; }
.explorer-problem-filter select { height: 100%; min-width: 52px; padding: 0 19px 0 1px; border: 0; outline: 0; appearance: none; background: transparent; color: var(--text-main); font: inherit; cursor: pointer; }
.explorer-problem-filter .filter-chevron { position: absolute; right: 3px; font-size: 16px; }
.explorer-problems-close { width: 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center; border: 0; border-radius: 4px; background: transparent; color: var(--text-muted); cursor: pointer; }
.explorer-problems-close:hover { background: var(--bg-hover); color: var(--text-main); }
.explorer-problems-close .material-symbols-rounded { font-size: 17px; }
.explorer-problem-list { overflow: auto; }.explorer-problem-item { display: flex; align-items: flex-start; border-bottom: 1px solid var(--border-ghost); }
.problem-open { flex: 1; min-width: 0; display: flex; align-items: flex-start; gap: 8px; padding: 8px 10px; border: 0; background: transparent; text-align: left; cursor: pointer; }
.problem-open > span:nth-child(2) { flex: 1; min-width: 0; }.problem-open strong,.problem-open small,.problem-open em { display: block; font-size: 11px; font-style: normal; }.problem-open small { color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; }.problem-open em { margin-top: 3px; color: var(--text-secondary); }
.problem-severity { width: 8px; height: 8px; margin-top: 4px; border-radius: 50%; background: var(--state-info); }.problem-severity.is-error { background: var(--state-danger); }.problem-severity.is-warning { background: var(--state-warning); }
.problem-fix { flex: none; align-self: center; margin-right: 8px; border: 1px solid var(--border-ghost); background: transparent; padding: 3px 6px; font-size: 10px; cursor: pointer; }.explorer-problem-empty { padding: 16px; color: var(--text-muted); font-size: 12px; text-align: center; }

.tree-row-shell {
  position: relative;
}

.tree-row {
  padding-right: 10px;
}

.tree-row-trailing {
  position: absolute;
  top: 50%;
  right: 10px;
  z-index: 2;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 5px;
  transform: translateY(-50%);
  pointer-events: none;
}

.tree-chapter-state {
  position: relative;
  flex: 0 0 16px;
  width: 16px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: color-mix(in srgb, var(--text-muted) 60%, transparent);
  cursor: pointer;
  pointer-events: auto;
}

.tree-chapter-state.completed { color: var(--state-success); }
.tree-chapter-state:hover { color: var(--text-main); }
.tree-chapter-state.completed:hover { color: color-mix(in srgb, var(--state-success) 82%, var(--text-main)); }
.tree-chapter-state input { position: absolute; width: 16px; height: 20px; margin: 0; opacity: 0; cursor: pointer; }
.tree-chapter-state input:focus-visible + .material-symbols-rounded { outline: 1px solid var(--accent); outline-offset: 1px; border-radius: 50%; }
.tree-chapter-state .material-symbols-rounded { width: 14px; height: 14px; display: inline-flex; align-items: center; justify-content: center; overflow: visible; font-size: 13px; line-height: 14px; pointer-events: none; }

.tree-status-cluster {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 5px;
  height: 22px;
  font-size: 10px;
  font-weight: 700;
  line-height: 22px;
  font-variant-numeric: tabular-nums;
  pointer-events: auto;
}

.tree-diagnostic-number { min-width: 7px; height: 22px; display: inline-flex; align-items: center; justify-content: center; background: transparent; text-align: center; }
.tree-diagnostic-number.is-error { color: var(--state-danger); }
.tree-diagnostic-number.is-warning { color: var(--state-warning); }
.tree-diagnostic-number.is-info { color: var(--state-info); }
.tree-git-decoration { min-width: 10px; background: transparent; text-align: right; letter-spacing: -.02em; }
.tree-git-decoration.is-uncommitted { color: var(--state-success); }

.tree-view.is-import-drop-target {
  outline: 1px solid rgba(196, 100, 48, 0.45);
  outline-offset: -3px;
  background: rgba(196, 100, 48, 0.05);
}

.tree-row.is-drop-target {
  background: rgba(196, 100, 48, 0.12);
  color: var(--text);
}

.tree-row.is-drop-target .tree-icon,
.tree-row.is-drop-target .tree-label {
  color: var(--accent);
}

.tree-row.has-diagnostics {
  color: inherit;
}

.tree-row-actions {
  position: absolute;
  top: 50%;
  right: 10px;
  transform: translateY(-50%);
  display: inline-flex;
  align-items: center;
  gap: 8px;
  z-index: 1;
}

.tree-diagnostic-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--danger);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger) 14%, transparent);
  cursor: default;
}

.tree-diagnostic-dot.is-warning { background: #d99524; box-shadow: 0 0 0 3px color-mix(in srgb, #d99524 14%, transparent); }
.tree-diagnostic-dot.is-info { background: #4b83d1; box-shadow: 0 0 0 3px color-mix(in srgb, #4b83d1 14%, transparent); }
.tree-diagnostic-count { min-width: 16px; height: 16px; padding: 0 4px; display: inline-flex; align-items: center; justify-content: center; border-radius: 8px; background: #4b83d1; color: #fff; font-size: 9px; line-height: 1; }
.tree-diagnostic-count.is-error { background: var(--danger); }.tree-diagnostic-count.is-warning { background: #d99524; }

.tree-complete-toggle {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
  user-select: none;
}

.tree-complete-toggle-label {
  position: relative;
  white-space: nowrap;
  font-size: 0;
}

.tree-complete-toggle-label::before {
  content: "已完结";
  font-size: 11px;
}

.tree-complete-toggle input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}

.tree-complete-toggle-track {
  position: relative;
  width: 28px;
  height: 16px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--text-secondary) 18%, transparent);
  transition: background 160ms ease;
}

.tree-complete-toggle-track::after {
  content: "";
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--bg-panel);
  transition: transform 160ms ease;
}

.tree-complete-toggle input:checked + .tree-complete-toggle-track {
  background: color-mix(in srgb, var(--state-success, #4caf50) 72%, transparent);
}

.tree-complete-toggle input:checked + .tree-complete-toggle-track::after {
  transform: translateX(12px);
}

.tree-complete-toggle {
  justify-content: center;
  width: 14px;
  height: 14px;
  gap: 0;
  color: transparent;
  font-size: 0;
}

.tree-complete-toggle-label,
.tree-complete-toggle-label::before {
  display: none;
  content: none;
}

.tree-complete-toggle-track {
  width: 8px;
  height: 8px;
  background: color-mix(in srgb, var(--text-muted) 26%, transparent);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--text-muted) 10%, transparent);
  transition: background 160ms ease, box-shadow 160ms ease, transform 160ms ease;
}

.tree-complete-toggle-track::after {
  display: none;
  content: none;
}

.tree-complete-toggle:hover .tree-complete-toggle-track {
  transform: scale(1.06);
}

.tree-complete-toggle input:checked + .tree-complete-toggle-track {
  background: var(--state-success, #4caf50);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--state-success, #4caf50) 18%, transparent);
}

.tree-complete-toggle input:checked + .tree-complete-toggle-track::after {
  transform: none;
}

.explorer-header {
  align-items: stretch;
  padding-bottom: 10px;
}

.explorer-header-main {
  display: flex;
  flex: 1 1 auto;
  min-width: 0;
  flex-direction: column;
  gap: 6px;
}

.explorer-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.explorer-title {
  line-height: 1;
}

.explorer-project {
  margin-top: 0;
}

.explorer-project-path {
  min-width: 0;
  overflow: hidden;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.explorer-toolbar {
  margin-top: -2px;
  align-self: flex-start;
}

.tree-loading,
.tree-error {
  padding: 10px 8px;
  font-size: 12px;
  line-height: 1.45;
}

.tree-inline-create {
  min-height: 28px;
  gap: 6px;
  padding-top: 2px;
  padding-bottom: 2px;
}

.tree-inline-create-input {
  height: 26px;
  border-radius: 4px;
  padding: 0 8px;
  font-size: 13px;
}
</style>
