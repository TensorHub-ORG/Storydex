<template>
  <aside class="preset-panel">
    <header class="preset-header">
      <div class="preset-header-copy">
        <h2 class="preset-title">预设管理</h2>
        <p class="preset-subtitle">{{ projectLabel }}</p>
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
          title="导入"
          :disabled="workspaceStore.launchScreenVisible || importing"
          @click="openImportPicker"
        >
          <span class="material-symbols-rounded">upload_file</span>
        </button>
        <button
          class="preset-icon-btn"
          type="button"
          title="刷新"
          :disabled="workspaceStore.isTreeLoading || workspaceStore.launchScreenVisible"
          @click="refreshTree"
        >
          <span class="material-symbols-rounded">refresh</span>
        </button>
      </div>
    </header>

    <div class="preset-body">
      <template v-if="workspaceStore.launchScreenVisible">
        <div class="preset-empty">先打开一个 Storydex 项目，再管理项目级预设。</div>
      </template>

      <template v-else>
        <section class="preset-summary">
          <div class="preset-summary-line">
            <span class="preset-summary-label">目录</span>
            <span class="preset-summary-value">.storydex/presets</span>
          </div>
          <div class="preset-workbench-entry-row">
            <button
              class="preset-workbench-entry"
              type="button"
              title="导入"
              :disabled="importing"
              @click="openImportPicker"
            >
              <span class="material-symbols-rounded">upload_file</span>
              <span>导入</span>
              <b v-if="importing">处理中</b>
            </button>
          </div>
          <p v-if="importSummary" class="preset-import-summary">{{ importSummary }}</p>
        </section>

        <section class="preset-section">
          <header class="preset-section-header">
            <div class="preset-section-title">
              <span class="material-symbols-rounded">check_circle</span>
              <span>已启用</span>
            </div>
            <span class="preset-section-count">{{ enabledItems.length }}</span>
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
                <span class="preset-row-path">
                  <span>{{ item.displayPath }}</span>
                  <span v-if="item.isActiveMain" class="preset-status-badge">主预设</span>
                  <span v-if="item.hasSidecar" class="preset-status-badge">参数</span>
                </span>
              </span>
              <span class="preset-row-actions">
                <span
                  v-if="item.extension === '.md'"
                  class="preset-row-action material-symbols-rounded"
                  title="编辑参数"
                  @click.stop="openEditor(item.relativePath)"
                >tune</span>
                <span
                  class="preset-row-action material-symbols-rounded"
                  title="停用（移回 library）"
                  @click.stop="handleDeactivate(item.relativePath)"
                >toggle_on</span>
                <span class="preset-row-meta">{{ extensionLabel(item.extension) }}</span>
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
                <span class="preset-row-path">
                  <span>{{ item.displayPath }}</span>
                  <span v-if="item.hasSidecar" class="preset-status-badge">参数</span>
                </span>
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
                  class="preset-row-action material-symbols-rounded"
                  title="激活（移到 active）"
                  @click.stop="handleActivate(item.relativePath)"
                >toggle_off</span>
                <span class="preset-row-meta">{{ extensionLabel(item.extension) }}</span>
              </span>
            </button>
          </div>
        </section>
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
  background: var(--bg-sidebar);
  color: var(--text-main);
  border-right: 1px solid var(--border-subtle);
}

.preset-header {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 18px 12px;
  border-bottom: 1px solid var(--border-ghost);
}

.preset-header-copy {
  min-width: 0;
}

.preset-header-actions {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.preset-import-input {
  display: none;
}

.preset-title {
  margin: 0;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.02em;
}

.preset-subtitle {
  margin: 4px 0 0;
  color: var(--text-muted);
  font-size: 12px;
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
  border-radius: 0;
  background: transparent;
  color: var(--text-main);
  cursor: pointer;
}

.preset-icon-btn:hover:not(:disabled),
.preset-icon-btn:focus-visible {
  background: var(--bg-hover);
  outline: none;
}

.preset-icon-btn:disabled,
.preset-row:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.preset-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.preset-summary {
  flex: 0 0 auto;
  padding: 12px 18px;
  border-bottom: 1px solid var(--border-ghost);
}

.preset-summary-line {
  min-height: 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  color: var(--text-muted);
  font-size: 12px;
}

.preset-summary-label {
  flex: 0 0 auto;
  color: var(--text-soft);
  font-weight: 700;
}

.preset-summary-value {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: right;
}

.preset-workbench-entry-row {
  display: grid;
  grid-template-columns: 1fr;
  gap: 6px;
  margin-top: 8px;
}

.preset-workbench-entry {
  min-width: 0;
  display: grid;
  grid-template-columns: 16px minmax(0, 1fr) auto;
  align-items: center;
  gap: 5px;
  padding: 6px 7px;
  border: 1px solid var(--border-ghost);
  border-radius: 4px;
  background: var(--bg-elevated, transparent);
  color: var(--text-muted);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}

.preset-workbench-entry:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.preset-workbench-entry .material-symbols-rounded {
  font-size: 16px;
}

.preset-workbench-entry span:nth-child(2) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.preset-workbench-entry b {
  padding: 1px 4px;
  border-radius: 999px;
  background: var(--bg-hover);
  color: var(--text-faint);
  font-size: 9px;
  font-weight: 800;
}

.preset-import-summary {
  margin: 8px 0 0;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.5;
}

.preset-section {
  flex: 1 1 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-bottom: 1px solid var(--border-ghost);
}

.preset-section-header {
  flex: 0 0 auto;
  min-height: 42px;
  padding: 0 18px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  border-bottom: 1px solid var(--border-ghost);
}

.preset-section-title {
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--text-soft);
  font-size: 12px;
  font-weight: 700;
}

.preset-section-title .material-symbols-rounded {
  color: var(--text-muted);
  font-size: 17px;
}

.preset-section-count {
  flex: 0 0 auto;
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 700;
}

.preset-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  padding: 4px 10px 8px;
}

.preset-row {
  width: 100%;
  min-height: 38px;
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  padding: 7px 8px;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  text-align: left;
  font: inherit;
}

.preset-row:hover,
.preset-row:focus-visible {
  background: var(--bg-hover);
  outline: none;
}

.preset-row.active {
  background: var(--bg-selected);
  color: var(--accent-strong);
}

.preset-row-icon {
  color: var(--text-muted);
  font-size: 16px;
}

.preset-row-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.preset-row-name {
  min-width: 0;
  color: var(--text-main);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.preset-row-path {
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--text-muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.preset-row-path > span:first-child {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.preset-status-badge {
  flex: 0 0 auto;
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--bg-hover);
  color: var(--text-faint);
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.04em;
}

.preset-row-meta {
  flex: 0 0 auto;
  color: var(--text-faint);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.preset-row-actions {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.preset-row-action {
  font-size: 18px;
  color: var(--text-muted);
  cursor: pointer;
  border-radius: 4px;
  padding: 2px;
  transition: background-color 0.15s ease, color 0.15s ease;
}

.preset-row-action:hover {
  background: var(--bg-hover);
  color: var(--accent-strong);
}

.preset-context-menu {
  position: fixed;
  z-index: 10000;
  min-width: 168px;
  padding: 5px;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--bg-panel, var(--bg-sidebar));
  box-shadow: var(--shadow-popover);
}

.preset-context-menu-item {
  width: 100%;
  min-height: 28px;
  display: flex;
  align-items: center;
  padding: 0 10px;
  border: 0;
  border-radius: 4px;
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
  background: var(--bg-sidebar);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  position: relative;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

/* 导入预览是双栏布局，需要更宽的窗口 */
.preset-import-preview-modal {
  width: min(960px, 96vw);
  height: min(86vh, 720px);
  border-radius: 4px;
}

.preset-editor-close {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 1;
  background: transparent;
  border: 0;
  color: var(--text-muted);
  cursor: pointer;
  border-radius: 4px;
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
  padding: 18px 16px;
}

.preset-empty-inline {
  padding: 12px 8px;
}
</style>
