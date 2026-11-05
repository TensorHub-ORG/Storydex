<template>
  <aside class="preset-panel">
    <header class="preset-header">
      <div class="preset-header-copy">
        <h2 class="preset-title">预设管理</h2>
        <p class="preset-subtitle" :title="projectLabel">{{ projectLabel }}</p>
      </div>

      <div class="preset-header-actions">
        <input
          ref="importInputRef"
          class="preset-import-input"
          type="file"
          multiple
          accept=".md,.json,.txt"
          @change="handleImportInputChange"
        />
        <button
          class="preset-icon-btn"
          type="button"
          :title="importing ? '正在处理导入…' : '导入预设（.md / .json / .txt）'"
          aria-label="导入预设"
          :disabled="workspaceStore.launchScreenVisible || importing"
          @click="openImportPicker"
        >
          <span class="material-symbols-rounded" :class="{ 'is-spinning': importing }">{{ importing ? "progress_activity" : "upload_file" }}</span>
        </button>
        <button
          class="preset-icon-btn"
          type="button"
          title="刷新"
          aria-label="刷新预设列表"
          :disabled="workspaceStore.isTreeLoading || workspaceStore.launchScreenVisible"
          @click="refreshTree"
        >
          <span class="material-symbols-rounded">refresh</span>
        </button>
      </div>
    </header>

    <div class="preset-body">
      <template v-if="workspaceStore.launchScreenVisible">
        <div class="preset-empty">
          <span class="material-symbols-rounded preset-empty-icon">folder_open</span>
          <p class="preset-empty-title">尚未打开项目</p>
          <p>先打开一个 Storydex 项目，再管理项目级预设。</p>
        </div>
      </template>

      <template v-else>
        <section class="preset-summary">
          <button
            class="preset-guidance-card"
            type="button"
            :disabled="presetStore.saving"
            title="打开小说写作指导的参数"
            @click="openWritingGuidance"
          >
            <span class="preset-guidance-icon material-symbols-rounded">edit_note</span>
            <span class="preset-guidance-copy">
              <strong>小说写作指导</strong>
              <span>分别调整语言、情节节奏与连续性建议</span>
            </span>
            <span class="material-symbols-rounded preset-guidance-arrow">chevron_right</span>
          </button>
          <p v-if="importSummary" class="preset-import-summary">{{ importSummary }}</p>
          <p v-if="presetStore.errorMessage" class="preset-import-summary is-error" role="alert">{{ presetStore.errorMessage }}</p>
        </section>

        <div class="preset-scroll">
          <section class="preset-section">
            <header class="preset-section-header">
              <div class="preset-section-title">
                <span class="material-symbols-rounded">check_circle</span>
                <span>已启用</span>
              </div>
              <span class="preset-section-count" :class="{ 'is-active': enabledItems.length > 0 }">{{ enabledItems.length }}</span>
            </header>

            <div class="preset-list">
              <div v-if="enabledItems.length === 0" class="preset-empty-inline">
                active 目录暂无启用预设。
              </div>
              <button
                v-for="item in enabledItems"
                :key="item.relativePath"
                class="preset-row"
                type="button"
                :class="{ active: workspaceStore.activeFileBindingOrPath === item.relativePath }"
                :title="item.relativePath"
                @contextmenu.prevent.stop="openPresetContextMenu($event, item)"
                @click="openPreset(item.relativePath)"
              >
                <span class="preset-row-icon material-symbols-rounded">{{ iconFor(item) }}</span>
                <span class="preset-row-main">
                  <span class="preset-row-name">{{ item.name }}</span>
                  <span v-if="presetFolder(item)" class="preset-row-folder">{{ presetFolder(item) }}</span>
                  <span v-if="item.isActiveMain" class="preset-status-badge is-main">主预设</span>
                  <span v-if="item.hasSidecar" class="preset-status-badge">参数</span>
                  <span v-else-if="item.extension === '.md'" class="preset-status-badge is-warning" title="使用 Markdown 临时预设；编辑并保存参数可生成参数文件">缺参数</span>
                </span>
                <span class="preset-row-actions">
                  <span
                    v-if="item.extension === '.md'"
                    class="preset-row-action material-symbols-rounded"
                    title="编辑参数"
                    @click.stop="openEditor(item.relativePath)"
                  >tune</span>
                  <span
                    class="preset-switch is-on"
                    title="停用（移回 library）"
                    @click.stop="handleDeactivate(item.relativePath)"
                  ><span class="preset-switch-knob"></span></span>
                </span>
              </button>
            </div>
          </section>

          <section class="preset-section">
            <header class="preset-section-header">
              <div class="preset-section-title">
                <span class="material-symbols-rounded">inventory_2</span>
                <span>未启用</span>
              </div>
              <span class="preset-section-count">{{ disabledItems.length }}</span>
            </header>

            <div class="preset-list">
              <div v-if="disabledItems.length === 0" class="preset-empty-inline">
                library 目录暂无未启用预设。
              </div>
              <button
                v-for="item in disabledItems"
                :key="item.relativePath"
                class="preset-row"
                type="button"
                :class="{ active: workspaceStore.activeFileBindingOrPath === item.relativePath }"
                :title="item.relativePath"
                @contextmenu.prevent.stop="openPresetContextMenu($event, item)"
                @click="openPreset(item.relativePath)"
              >
                <span class="preset-row-icon material-symbols-rounded">{{ iconFor(item) }}</span>
                <span class="preset-row-main">
                  <span class="preset-row-name">{{ item.name }}</span>
                  <span v-if="presetFolder(item)" class="preset-row-folder">{{ presetFolder(item) }}</span>
                  <span v-if="item.hasSidecar" class="preset-status-badge">参数</span>
                  <span v-else-if="item.extension === '.md'" class="preset-status-badge is-warning" title="编辑并保存参数可生成参数文件">缺参数</span>
                </span>
                <span class="preset-row-actions">
                  <span
                    v-if="item.extension === '.md'"
                    class="preset-row-action material-symbols-rounded"
                    title="编辑参数"
                    @click.stop="openEditor(item.relativePath)"
                  >tune</span>
                  <span
                    v-if="item.extension === '.md'"
                    class="preset-switch"
                    title="激活（移到 active）"
                    @click.stop="handleActivate(item.relativePath)"
                  ><span class="preset-switch-knob"></span></span>
                </span>
              </button>
            </div>
          </section>

          <p class="preset-location-note">
            预设保存在 <code>.storydex/presets</code>：启用的在 <code>active</code>，未启用的在 <code>library</code>。
          </p>
        </div>
      </template>
    </div>

    <Teleport to="body">
      <div v-if="editorOpen" class="preset-editor-overlay" @click.self="closeEditor">
        <div class="preset-editor-modal">
          <button class="preset-editor-close" type="button" title="关闭" @click="closeEditor">
            <span class="material-symbols-rounded">close</span>
          </button>
          <PresetEditor />
        </div>
      </div>
    </Teleport>

    <Teleport to="body">
      <div v-if="previewOpen" class="preset-editor-overlay" @click.self="cancelImport">
        <div class="preset-editor-modal preset-import-preview-modal">
          <PresetImportPreview
            :items="previewItems"
            :loading="previewLoading"
            :error-message="previewErrorMessage"
            @confirm="confirmImport"
            @cancel="cancelImport"
          />
        </div>
      </div>
    </Teleport>

    <Teleport to="body">
      <div
        v-if="contextMenu.visible"
        ref="contextMenuRef"
        class="preset-context-menu"
        :style="{ left: `${contextMenu.x}px`, top: `${contextMenu.y}px` }"
        @click.stop
      >
        <button class="preset-context-menu-item" type="button" @click="handleOpenContextPreset">打开</button>
        <button
          v-if="contextMenu.item?.extension === '.md'"
          class="preset-context-menu-item"
          type="button"
          @click="handleEditContextPreset"
        >
          编辑参数
        </button>
        <button
          v-if="contextMenu.item && isEnabledPreset(contextMenu.item)"
          class="preset-context-menu-item"
          type="button"
          @click="handleContextDeactivate"
        >
          停用
        </button>
        <button
          v-else-if="contextMenu.item?.extension === '.md'"
          class="preset-context-menu-item"
          type="button"
          @click="handleContextActivate"
        >
          启用
        </button>
        <div class="preset-context-menu-separator"></div>
        <button class="preset-context-menu-item" type="button" @click="handleCopyPresetRelativePath">
          复制相对路径
        </button>
        <button class="preset-context-menu-item is-danger" type="button" @click="handleDeletePreset">删除</button>
      </div>
    </Teleport>
  </aside>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import PresetEditor from "@/components/PresetEditor.vue";
import PresetImportPreview from "@/components/PresetImportPreview.vue";
import {
  importSillyTavernPresets,
  previewSillyTavernImport,
  type SillyTavernPresetImportItem,
  type SillyTavernPresetImportFilePayload
} from "@/api/presets";
import { usePresetStore } from "@/stores/preset";
import { useWorkspaceStore } from "@/stores/workspace";
import type { WorkspaceTreeNode } from "@/types/workspace";

interface PresetItem {
  name: string;
  relativePath: string;
  displayPath: string;
  extension: string;
  hasSidecar: boolean;
  isActiveMain: boolean;
}

interface PresetContextMenuState {
  visible: boolean;
  x: number;
  y: number;
  item: PresetItem | null;
}

const workspaceStore = useWorkspaceStore();
const presetStore = usePresetStore();
const importInputRef = ref<HTMLInputElement | null>(null);
const importing = ref(false);
const editorOpen = ref(false);
const importSummary = ref("");
// 导入预览状态
const previewOpen = ref(false);
const previewLoading = ref(false);
const previewItems = ref<SillyTavernPresetImportItem[]>([]);
const previewErrorMessage = ref("");
const pendingImportPayload = ref<SillyTavernPresetImportFilePayload[]>([]);
const contextMenuRef = ref<HTMLDivElement | null>(null);
const contextMenu = ref<PresetContextMenuState>({
  visible: false,
  x: 0,
  y: 0,
  item: null
});

const projectLabel = computed(() => workspaceStore.projectLabel || "未打开项目");
const enabledItems = computed(() => collectPresetItems(".storydex/presets/active"));
const disabledItems = computed(() => collectPresetItems(".storydex/presets/library"));

onMounted(() => {
  window.addEventListener("click", closePresetContextMenu);
  window.addEventListener("blur", closePresetContextMenu);
  window.addEventListener("keydown", handlePresetContextKeydown);
});

onBeforeUnmount(() => {
  window.removeEventListener("click", closePresetContextMenu);
  window.removeEventListener("blur", closePresetContextMenu);
  window.removeEventListener("keydown", handlePresetContextKeydown);
});

async function handleActivate(relativePath: string): Promise<void> {
  await presetStore.activate(relativePath);
}

async function openWritingGuidance(): Promise<void> {
  const path = await presetStore.configureWritingGuidance();
  if (path) await openEditor(path);
}

async function handleDeactivate(relativePath: string): Promise<void> {
  await presetStore.deactivate(relativePath);
}

async function openPresetContextMenu(event: MouseEvent, item: PresetItem): Promise<void> {
  contextMenu.value = {
    visible: true,
    x: event.clientX,
    y: event.clientY,
    item
  };
  await nextTick();
  repositionPresetContextMenu(event.clientX, event.clientY);
}

function closePresetContextMenu(): void {
  contextMenu.value.visible = false;
  contextMenu.value.item = null;
}

function handlePresetContextKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    if (contextMenu.value.visible) {
      closePresetContextMenu();
      return;
    }
    if (editorOpen.value || previewOpen.value) {
      event.preventDefault();
      if (editorOpen.value) closeEditor();
      else cancelImport();
    }
    closePresetContextMenu();
  }
}

function repositionPresetContextMenu(anchorX: number, anchorY: number): void {
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

function handleOpenContextPreset(): void {
  const item = contextMenu.value.item;
  closePresetContextMenu();
  if (item) {
    openPreset(item.relativePath);
  }
}

async function handleEditContextPreset(): Promise<void> {
  const item = contextMenu.value.item;
  closePresetContextMenu();
  if (item?.extension === ".md") {
    await openEditor(item.relativePath);
  }
}

async function handleContextActivate(): Promise<void> {
  const item = contextMenu.value.item;
  closePresetContextMenu();
  if (item?.extension === ".md") {
    await handleActivate(item.relativePath);
  }
}

async function handleContextDeactivate(): Promise<void> {
  const item = contextMenu.value.item;
  closePresetContextMenu();
  if (item) {
    await handleDeactivate(item.relativePath);
  }
}

async function handleCopyPresetRelativePath(): Promise<void> {
  const relativePath = contextMenu.value.item?.relativePath || "";
  closePresetContextMenu();
  if (!relativePath || !navigator.clipboard?.writeText) {
    return;
  }
  try {
    await navigator.clipboard.writeText(relativePath);
  } catch {
    // Clipboard permissions are browser-controlled; failing to copy should not affect file operations.
  }
}

async function handleDeletePreset(): Promise<void> {
  const item = contextMenu.value.item;
  closePresetContextMenu();
  if (!item?.relativePath) {
    return;
  }
  const confirmed = window.confirm(`确定要删除“${item.name}”吗？`);
  if (!confirmed) {
    return;
  }
  try {
    await workspaceStore.deletePath(item.relativePath);
    if (item.hasSidecar) {
      await workspaceStore.deletePath(sidecarPathFor(item.relativePath));
    }
    await Promise.all([workspaceStore.refreshTree(), presetStore.refreshList()]);
  } catch {
    // handled by stores
  }
}

async function openEditor(relativePath: string): Promise<void> {
  await presetStore.loadDocument(relativePath);
  editorOpen.value = true;
}

function closeEditor(): void {
  editorOpen.value = false;
}

function refreshTree(): void {
  void workspaceStore.refreshTree();
}

function openImportPicker(): void {
  importInputRef.value?.click();
}

async function handleImportInputChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement | null;
  const files = Array.from(input?.files || []).filter((file) => file.name && file.size >= 0);
  if (input) {
    input.value = "";
  }
  if (!files.length) {
    return;
  }

  previewLoading.value = true;
  previewErrorMessage.value = "";
  previewItems.value = [];
  pendingImportPayload.value = [];

  try {
    const payload = await Promise.all(
      files.map(async (file) => ({
        name: file.name,
        contentBase64: await fileToBase64(file)
      }))
    );
    pendingImportPayload.value = payload;
    const { data } = await previewSillyTavernImport({ files: payload });
    previewItems.value = data.items;
    previewOpen.value = true;
  } catch (error) {
    importSummary.value = error instanceof Error ? error.message : "导入预览失败。";
  } finally {
    previewLoading.value = false;
  }
}

async function confirmImport(): Promise<void> {
  if (!pendingImportPayload.value.length) {
    previewOpen.value = false;
    return;
  }

  importing.value = true;
  importSummary.value = "";
  previewOpen.value = false;

  try {
    const { data } = await importSillyTavernPresets({ files: pendingImportPayload.value });
    await Promise.all([workspaceStore.refreshTree(), presetStore.refreshList()]);
    const firstImported = data.items.find((item) => item.relativePath);
    const moduleCount = data.items.reduce((sum, item) => sum + (item.moduleCount || 0), 0);
    const warningCount = data.items.reduce((sum, item) => sum + (item.importWarnings?.length || 0), 0);
    importSummary.value = `已导入 ${data.items.length} 个预设，${moduleCount} 个模块，${warningCount} 条宏提示。`;
    if (firstImported) {
      await openEditor(firstImported.relativePath);
    }
  } catch (error) {
    importSummary.value = error instanceof Error ? error.message : "导入失败。";
  } finally {
    importing.value = false;
    pendingImportPayload.value = [];
    previewItems.value = [];
  }
}

function cancelImport(): void {
  previewOpen.value = false;
  pendingImportPayload.value = [];
  previewItems.value = [];
  previewErrorMessage.value = "";
}

function openPreset(relativePath: string): void {
  closePresetContextMenu();
  void workspaceStore.openFile(relativePath, { forceReload: true });
}

function collectPresetItems(sectionPath: string): PresetItem[] {
  const root = findNode(workspaceStore.tree, sectionPath);
  if (!root || root.kind !== "directory") {
    return [];
  }

  const items: PresetItem[] = [];
  walkPresetFiles(root.children || [], sectionPath, items);
  return items.sort((left, right) => left.relativePath.localeCompare(right.relativePath, "zh-CN"));
}

function walkPresetFiles(nodes: WorkspaceTreeNode[], sectionPath: string, items: PresetItem[]): void {
  for (const node of nodes) {
    const relativePath = normalizePath(node.relativePath || "");
    if (!relativePath) {
      continue;
    }
    if (node.kind === "directory") {
      walkPresetFiles(node.children || [], sectionPath, items);
      continue;
    }
    if (!isPresetFile(node)) {
      continue;
    }
    items.push({
      name: node.name,
      relativePath,
      displayPath: relativePath.startsWith(`${sectionPath}/`)
        ? relativePath.slice(sectionPath.length + 1)
        : relativePath,
      extension: String(node.extension || extensionFromName(node.name)).toLowerCase(),
      hasSidecar: hasSidecarFor(relativePath),
      isActiveMain: normalizePath(presetStore.activeMainPreset) === relativePath
    });
  }
}

function hasSidecarFor(relativePath: string): boolean {
  if (!relativePath.toLowerCase().endsWith(".md")) {
    return false;
  }
  return Boolean(findNode(workspaceStore.tree, sidecarPathFor(relativePath)));
}

function sidecarPathFor(relativePath: string): string {
  return normalizePath(relativePath).replace(/\.md$/iu, ".preset.json");
}

function isEnabledPreset(item: PresetItem): boolean {
  return normalizePath(item.relativePath).startsWith(".storydex/presets/active/");
}

function findNode(nodes: WorkspaceTreeNode[], targetPath: string): WorkspaceTreeNode | null {
  const normalizedTarget = normalizePath(targetPath);
  for (const node of nodes) {
    const relativePath = normalizePath(node.relativePath || "");
    if (relativePath === normalizedTarget) {
      return node;
    }
    const nested = findNode(node.children || [], normalizedTarget);
    if (nested) {
      return nested;
    }
  }
  return null;
}

function isPresetFile(node: WorkspaceTreeNode): boolean {
  const extension = String(node.extension || extensionFromName(node.name)).toLowerCase();
  return extension === ".md" || extension === ".json" || extension === ".txt";
}

function iconFor(item: PresetItem): string {
  if (item.extension === ".json") {
    return "data_object";
  }
  if (item.extension === ".md") {
    return "article";
  }
  return "description";
}

function extensionLabel(extension: string): string {
  return extension ? extension.replace(/^\./u, "").toUpperCase() : "FILE";
}

/** 预设放在 active / library 的子目录里时，在名字后标出目录，避免同名文件分不清。 */
function presetFolder(item: PresetItem): string {
  const index = item.displayPath.lastIndexOf("/");
  return index > 0 ? item.displayPath.slice(0, index) : "";
}

function extensionFromName(name: string): string {
  const match = String(name || "").match(/\.[^.]+$/u);
  return match ? match[0] : "";
}

function normalizePath(value: string): string {
  return String(value || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
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
defineExpose({
  __testUtils: import.meta.env.MODE === "test" ? {
    contextMenu, editorOpen, previewOpen, previewItems, pendingImportPayload, enabledItems, disabledItems,
    handleActivate, handleDeactivate, openPresetContextMenu, closePresetContextMenu, handlePresetContextKeydown,
    repositionPresetContextMenu, handleOpenContextPreset, handleEditContextPreset, handleContextActivate,
    handleContextDeactivate, handleCopyPresetRelativePath, handleDeletePreset, openEditor, closeEditor,
    refreshTree, openImportPicker, handleImportInputChange, confirmImport, cancelImport, openPreset,
    collectPresetItems, walkPresetFiles, hasSidecarFor, sidecarPathFor, isEnabledPreset, findNode,
    isPresetFile, iconFor, extensionLabel, extensionFromName, normalizePath, fileToBase64
  } : null
});
</script>

<style scoped>
.preset-panel,
.preset-panel * {
  box-sizing: border-box;
}

.preset-panel {
  width: 100%;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--canvas);
  color: var(--text-main);
}

.preset-header {
  flex: 0 0 auto;
  min-height: 52px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 10px 8px 14px;
  border-bottom: 0;
  background: transparent;
}

.preset-header-copy {
  min-width: 0;
}

.preset-header-actions {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

.preset-import-input {
  display: none;
}

/* 侧栏统一页头：页面名为主标题，项目名为副标题 */
.preset-title {
  margin: 0;
  color: var(--text-main);
  font-size: 13px;
  font-weight: 600;
  line-height: 1.35;
  letter-spacing: 0;
}

.preset-subtitle {
  margin: 2px 0 0;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.preset-icon-btn {
  flex: 0 0 auto;
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.preset-icon-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-main);
}

.preset-icon-btn:focus-visible {
  outline: none;
  box-shadow: 0 0 0 1px var(--accent), 0 0 0 3px var(--accent-soft);
}

.preset-icon-btn:disabled,
.preset-row:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.is-spinning {
  animation: preset-spin 1s linear infinite;
}

@keyframes preset-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .is-spinning {
    animation: none;
  }
}

.preset-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ---------- 写作指导入口：一张小卡片 ---------- */

.preset-summary {
  flex: 0 0 auto;
  padding: 0 10px 2px;
}

.preset-guidance-card {
  width: 100%;
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr) 18px;
  align-items: center;
  gap: 10px;
  padding: 9px 8px 9px 10px;
  border: 1px solid var(--border-ghost);
  border-radius: var(--radius-lg);
  background: var(--surface-1);
  box-shadow: var(--shadow-resting);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color 140ms ease, box-shadow 140ms ease;
}

.preset-guidance-card:hover:not(:disabled) {
  border-color: var(--border-subtle);
  box-shadow: var(--shadow-raised);
}

.preset-guidance-card:focus-visible {
  outline: none;
  box-shadow: 0 0 0 1px var(--accent), 0 0 0 3px var(--accent-soft);
}

.preset-guidance-card:disabled {
  cursor: progress;
  opacity: 0.7;
}

.preset-guidance-icon {
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-md);
  background: var(--accent-soft);
  color: var(--accent-strong);
  font-size: 18px;
}

.preset-guidance-copy {
  min-width: 0;
  display: grid;
  gap: 1px;
}

.preset-guidance-copy strong {
  color: var(--text-main);
  font-size: 13px;
  font-weight: 600;
}

.preset-guidance-copy span {
  overflow: hidden;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.45;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.preset-guidance-arrow {
  color: var(--text-faint);
  font-size: 18px;
}

.preset-import-summary {
  margin: 8px 2px 0;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.5;
}

.preset-import-summary.is-error {
  color: var(--danger-fg);
}

/* ---------- 列表：两段按内容高度依次排列，整体一起滚动 ---------- */

.preset-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  padding-bottom: 14px;
}

.preset-section {
  display: flex;
  flex-direction: column;
}

.preset-section-header {
  min-height: 28px;
  margin: 12px 14px 4px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.preset-section-title {
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.preset-section-title .material-symbols-rounded {
  color: var(--text-faint);
  font-size: 15px;
}

.preset-section-count {
  flex: 0 0 auto;
  min-width: 20px;
  height: 18px;
  padding: 0 6px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-full);
  background: var(--bg-hover);
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.preset-section-count.is-active {
  background: var(--accent-soft);
  color: var(--accent-strong);
}

.preset-list {
  padding: 0 6px;
}

.preset-row {
  width: 100%;
  min-height: 32px;
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  margin-bottom: 1px;
  padding: 4px 6px 4px 8px;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-align: left;
  font: inherit;
  transition: background 120ms ease, box-shadow 120ms ease;
}

.preset-row:hover {
  background: var(--bg-hover);
}

.preset-row:focus-visible {
  outline: none;
  box-shadow: 0 0 0 1px var(--accent), 0 0 0 3px var(--accent-soft);
}

/* 选中项抬到卡片层，与资源浏览器一致 */
.preset-row.active {
  background: var(--nav-selected-bg);
  box-shadow: var(--nav-selected-shadow);
}

.preset-row-icon {
  color: var(--text-muted);
  font-size: 16px;
}

.preset-row.active .preset-row-icon {
  color: var(--accent);
}

.preset-row-main {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  overflow: hidden;
}

.preset-row-name {
  min-width: 0;
  flex: 0 1 auto;
  overflow: hidden;
  color: var(--text-main);
  font-size: 13px;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.preset-row-folder {
  min-width: 0;
  flex: 0 2 auto;
  overflow: hidden;
  color: var(--text-faint);
  font-size: 11px;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.preset-status-badge {
  flex: 0 0 auto;
  padding: 0 6px;
  border-radius: var(--radius-full);
  background: var(--bg-hover);
  color: var(--text-muted);
  font-size: 10px;
  font-weight: 600;
  line-height: 17px;
  white-space: nowrap;
}

.preset-status-badge.is-main {
  background: var(--accent-soft);
  color: var(--accent-strong);
}

.preset-status-badge.is-warning {
  background: var(--warning-bg);
  color: var(--warning-fg);
}

.preset-row-actions {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.preset-row-action {
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-sm);
  color: var(--text-faint);
  font-size: 16px;
  cursor: pointer;
  transition: background-color 0.15s ease, color 0.15s ease;
}

.preset-row:hover .preset-row-action {
  color: var(--text-muted);
}

.preset-row .preset-row-action:hover {
  background: var(--bg-active);
  color: var(--accent-strong);
}

/* 启用开关：开 = 强调色，点击即启用 / 停用 */
.preset-switch {
  position: relative;
  flex: 0 0 auto;
  width: 28px;
  height: 16px;
  margin: 0 2px 0 4px;
  border-radius: var(--radius-full);
  background: var(--border-strong);
  cursor: pointer;
  transition: background 160ms ease, box-shadow 160ms ease;
}

.preset-switch-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: var(--radius-full);
  background: #fff;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
  transition: transform 160ms ease;
}

.preset-switch.is-on {
  background: var(--accent);
}

.preset-switch.is-on .preset-switch-knob {
  transform: translateX(12px);
}

.preset-switch:hover {
  box-shadow: 0 0 0 3px var(--accent-soft);
}

@media (prefers-reduced-motion: reduce) {
  .preset-switch,
  .preset-switch-knob {
    transition: none;
  }
}

.preset-location-note {
  margin: 16px 14px 0;
  color: var(--text-faint);
  font-size: 11px;
  line-height: 1.6;
}

.preset-location-note code {
  padding: 0 4px;
  border-radius: var(--radius-sm);
  background: var(--bg-hover);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 10.5px;
}

.preset-context-menu {
  position: fixed;
  z-index: 10000;
  min-width: 168px;
  padding: 5px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--surface-2);
  box-shadow: var(--shadow-popover);
}

.preset-context-menu-item {
  width: 100%;
  min-height: 28px;
  display: flex;
  align-items: center;
  padding: 0 10px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}

.preset-context-menu-item:hover,
.preset-context-menu-item:focus-visible {
  background: var(--bg-hover);
  outline: none;
}

.preset-context-menu-item.is-danger {
  color: var(--danger);
}

.preset-context-menu-separator {
  height: 1px;
  margin: 5px 4px;
  background: var(--border-ghost);
}

.preset-editor-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9000;
}

.preset-editor-modal {
  width: min(720px, 95vw);
  height: min(90vh, 800px);
  background: var(--surface-1);
  border: 1px solid var(--card-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-modal);
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

/* 导入预览是双栏布局，需要更宽的窗口 */
.preset-import-preview-modal {
  width: min(960px, 96vw);
  height: min(86vh, 720px);
}

.preset-editor-close {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 4;
  background: transparent;
  border: 0;
  color: var(--text-muted);
  cursor: pointer;
  border-radius: var(--radius-sm);
  padding: 4px;
}

.preset-editor-close:hover {
  background: var(--bg-hover);
}

.preset-empty,
.preset-empty-inline {
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.7;
}

.preset-empty {
  display: grid;
  justify-items: center;
  gap: 6px;
  padding: 36px 20px;
  text-align: center;
}

.preset-empty p {
  margin: 0;
}

.preset-empty .preset-empty-title {
  color: var(--text-main);
  font-size: 13px;
  font-weight: 600;
}

.preset-empty-icon {
  width: 40px;
  height: 40px;
  display: grid;
  place-items: center;
  margin-bottom: 4px;
  border-radius: var(--radius-full);
  background: var(--surface-1);
  box-shadow: var(--shadow-resting);
  color: var(--text-faint);
  font-size: 22px;
}

.preset-empty-inline {
  margin: 0 2px;
  padding: 8px 8px 10px;
  color: var(--text-faint);
}
</style>
