<template>
  <div class="preview-window">
    <header class="top-header preview-window-header">
      <div class="preview-window-tabs no-drag">
        <button
          v-for="tab in tabs"
          :key="tab.relativePath"
          class="preview-window-tab"
          :class="{ active: tab.relativePath === activeRelativePath }"
          type="button"
          @click="activateTab(tab.relativePath)"
        >
          <span class="material-symbols-rounded preview-window-tab-icon">{{ iconFor(tab.extension) }}</span>
          <span class="preview-window-tab-title">{{ tab.title }}</span>
          <span v-if="isTabDirty(tab)" class="preview-window-tab-dirty"></span>
          <span
            class="material-symbols-rounded preview-window-tab-close"
            @click.stop="void closeTab(tab.relativePath)"
          >
            close
          </span>
        </button>
      </div>

      <div class="preview-window-caption">
        <div class="preview-window-path">{{ activeDisplayPath }}</div>
        <div v-if="activeFileStats" class="preview-window-meta">{{ activeFileStats }}</div>
      </div>

      <div v-if="activeTab" class="preview-window-actions no-drag">
        <div class="preview-window-mode-switch" role="tablist" aria-label="预览窗口模式">
          <button
            class="preview-window-mode-btn"
            :class="{ active: activeTab.editorMode === 'preview' }"
            type="button"
            @click="handleModeChange('preview')"
          >
            预览
          </button>
          <button
            v-if="!activeTab.readOnly"
            class="preview-window-mode-btn"
            :class="{ active: activeTab.editorMode === 'edit' }"
            type="button"
            @click="handleModeChange('edit')"
          >
            编辑
          </button>
        </div>
      </div>
    </header>

    <main class="preview-window-body">
      <div v-if="!activeTab" class="preview-window-empty">
        从 Storydex 中选择文件后，这里会以单窗口标签页的方式集中显示。
      </div>

      <div v-else-if="activeTab.isLoading" class="preview-window-empty">正在读取文件内容...</div>

      <div v-else-if="activeTab.loadErrorMessage" class="preview-window-empty is-error">
        {{ activeTab.loadErrorMessage }}
      </div>

      <template v-else>
        <div v-if="activeTab.saveErrorMessage" class="preview-window-feedback is-error">
          {{ activeTab.saveErrorMessage }}
        </div>

        <div
          v-if="activeTab.editorMode === 'edit' && !activeTab.readOnly"
          class="preview-window-editor-shell"
        >
          <textarea
            :value="activeTab.content"
            class="preview-window-editor"
            spellcheck="false"
            @input="handleEditorInput"
          />
        </div>

        <div v-else-if="activeTab.previewUnsupportedMessage" class="preview-window-empty">
          {{ activeTab.previewUnsupportedMessage }}
        </div>

        <div
          v-else-if="activeTab.extension === '.md'"
          class="preview-window-markdown doc-markdown"
          @click="handleMarkdownLinkClick"
          v-html="renderedMarkdown"
        ></div>

        <pre v-else class="preview-window-code"><code>{{ activeTab.content }}</code></pre>
      </template>
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";
import { fetchUiPreferences } from "@/api/system";
import { readWorkspaceFile, writeWorkspaceFile } from "@/api/workspace";
import { isThemeCode } from "@/constants/themes";
import { useTheme } from "@/composables/useTheme";
import { readCachedThemeCode, writeCachedThemeCode } from "@/utils/appearance";
import { createMarkdownRenderer } from "@/utils/markdown";
import { legacyFileFontSizeToPaneScale } from "@/utils/paneFontScale";
import {
  findMarkdownLinkAnchor,
  isExternalMarkdownHref,
  resolveMarkdownWorkspaceHref
} from "@/utils/workspaceLinks";

type PreviewEditorMode = "preview" | "edit";

interface PreviewTab {
  relativePath: string;
  title: string;
  extension: string;
  displayPath: string;
  content: string;
  savedContent: string;
  size: number;
  wordCount: number;
  lineCount: number;
  updatedAt: string;
  readOnly: boolean;
  editorMode: PreviewEditorMode;
  isLoading: boolean;
  isSaving: boolean;
  loadErrorMessage: string;
  saveErrorMessage: string;
  previewUnsupportedMessage: string;
  requestToken: number;
}

const markdown = createMarkdownRenderer();

const route = useRoute();
const { applyTheme, applyPaneFontScale } = useTheme();

const tabs = ref<PreviewTab[]>([]);
const activeRelativePath = ref("");
let detachPreviewListener: (() => void) | null = null;
let nextRequestToken = 0;
let autoSaveTimer: number | null = null;

const routeRelativePath = computed(() => normalizeRelativePath(String(route.query.relativePath || "")));
const activeTab = computed(
  () => tabs.value.find((tab) => tab.relativePath === activeRelativePath.value) ?? null
);
const activeDisplayPath = computed(() => activeTab.value?.displayPath || activeTab.value?.relativePath || "未选择文件");
const activeFileStats = computed(() => {
  const tab = activeTab.value;
  if (!tab || tab.isLoading || tab.loadErrorMessage) {
    return "";
  }
  const parts: string[] = [];
  if (tab.wordCount > 0) {
    parts.push(`约 ${formatInteger(tab.wordCount)} 字`);
  }
  if (tab.lineCount > 0) {
    parts.push(`${formatInteger(tab.lineCount)} 行`);
  }
  if (parts.length === 0 && tab.size > 0) {
    parts.push(formatBytes(tab.size));
  }
  return parts.join(" · ");
});
const renderedMarkdown = computed(() => markdown.render(activeTab.value?.content || ""));
const isActiveTabDirty = computed(() => Boolean(activeTab.value && isTabDirty(activeTab.value)));

watch(
  routeRelativePath,
  (relativePath) => {
    if (!relativePath) {
      return;
    }
    void openOrActivateTab(relativePath);
  },
  { immediate: true }
);

watch(
  activeTab,
  (tab) => {
    document.title = tab ? `${isTabDirty(tab) ? "* " : ""}${tab.title} · Storydex` : "Storydex Preview";
  },
  { immediate: true }
);

onMounted(() => {
  void applyPersistedAppearance();
  window.addEventListener("focus", handleWindowFocus);
  window.addEventListener("blur", handleWindowBlur);
  window.addEventListener("keydown", handleWindowKeydown);
  window.addEventListener("pagehide", handleWindowPageHide);
  detachPreviewListener = window.storydexDesktop?.onPreviewOpenFile?.((relativePath) => {
    if (!relativePath) {
      return;
    }
    void openOrActivateTab(relativePath);
  }) ?? null;
});

onBeforeUnmount(() => {
  window.removeEventListener("focus", handleWindowFocus);
  window.removeEventListener("blur", handleWindowBlur);
  window.removeEventListener("keydown", handleWindowKeydown);
  window.removeEventListener("pagehide", handleWindowPageHide);
  detachPreviewListener?.();
  clearAutoSaveTimer();
});

async function applyPersistedAppearance(): Promise<void> {
  const cachedTheme = readCachedThemeCode();
  if (cachedTheme) {
    applyTheme(cachedTheme);
  }

  try {
    const result = await fetchUiPreferences();
    const theme = result.data.theme;
    applyPaneFontScale(
      result.data.centerPaneFontScale ?? legacyFileFontSizeToPaneScale(result.data.fileFontSize)
    );
    if (isThemeCode(theme)) {
      applyTheme(theme);
      writeCachedThemeCode(theme);
      return;
    }
  } catch {
    // Ignore appearance bootstrap errors for preview windows.
  }
  applyTheme(cachedTheme || "default");
  applyPaneFontScale();
}

function handleWindowFocus(): void {
  void applyPersistedAppearance();
}

function handleWindowBlur(): void {
  void flushActiveTabAutoSave();
}

function handleWindowPageHide(): void {
  void saveAllTabs();
}

function handleWindowKeydown(event: KeyboardEvent): void {
  const isSaveShortcut = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s";
  if (!isSaveShortcut) {
    return;
  }
  event.preventDefault();
  void flushActiveTabAutoSave();
}

function handleMarkdownLinkClick(event: MouseEvent): void {
  const anchor = findMarkdownLinkAnchor(event.target);
  const href = anchor?.getAttribute("href") || "";
  const relativePath = resolveMarkdownWorkspaceHref(href, activeTab.value?.relativePath || "");
  if (relativePath) {
    event.preventDefault();
    event.stopPropagation();
    void openOrActivateTab(relativePath);
    return;
  }

  if (isExternalMarkdownHref(href)) {
    event.preventDefault();
    window.open(anchor?.href || href, "_blank", "noopener,noreferrer");
  }
}

async function openOrActivateTab(relativePath: string): Promise<void> {
  const normalizedRelativePath = normalizeRelativePath(relativePath);
  if (!normalizedRelativePath) {
    return;
  }

  await persistTabBeforeLeaving(activeRelativePath.value, normalizedRelativePath);
  const tab = ensureTab(normalizedRelativePath);
  activeRelativePath.value = normalizedRelativePath;
  await loadTabContent(tab);
}

function ensureTab(relativePath: string): PreviewTab {
  const existing = tabs.value.find((tab) => tab.relativePath === relativePath);
  if (existing) {
    return existing;
  }

  const tab = reactive<PreviewTab>({
    relativePath,
    title: fileNameFromPath(relativePath) || "文件预览",
    extension: extensionFromPath(relativePath),
    displayPath: relativePath,
    content: "",
    savedContent: "",
    size: 0,
    wordCount: 0,
    lineCount: 0,
    updatedAt: "",
    readOnly: false,
    editorMode: "preview",
    isLoading: false,
    isSaving: false,
    loadErrorMessage: "",
    saveErrorMessage: "",
    previewUnsupportedMessage: "",
    requestToken: 0
  });
  tabs.value.push(tab);
  return tabs.value[tabs.value.length - 1] ?? tab;
}

async function loadTabContent(tab: PreviewTab): Promise<void> {
  const requestToken = ++nextRequestToken;
  tab.requestToken = requestToken;
  tab.isLoading = true;
  tab.loadErrorMessage = "";
  tab.saveErrorMessage = "";
  tab.previewUnsupportedMessage = "";

  try {
    const result = await readWorkspaceFile({ relativePath: tab.relativePath });
    if (tab.requestToken !== requestToken) {
      return;
    }

    tab.content = String(result.data.content || "");
    tab.savedContent = tab.content;
    tab.size = Number(result.data.size || 0);
    tab.wordCount = Number(result.data.wordCount || countStoryTextWords(tab.content));
    tab.lineCount = Number(result.data.lineCount || countLines(tab.content));
    tab.extension = String(result.data.extension || extensionFromPath(tab.relativePath));
    tab.title = String(result.data.title || fileNameFromPath(tab.relativePath) || tab.title);
    tab.displayPath = String(result.data.displayPath || tab.relativePath);
    tab.updatedAt = String(result.data.updatedAt || "");
    tab.readOnly = Boolean(result.data.readOnly);
    tab.previewUnsupportedMessage = Boolean(result.data.media?.previewUnsupported)
      ? String(result.data.media?.message || "当前文件暂不支持直接预览。")
      : "";
    if (tab.readOnly && tab.editorMode === "edit") {
      tab.editorMode = "preview";
    }
  } catch (error: unknown) {
    if (tab.requestToken !== requestToken) {
      return;
    }

    tab.content = "";
    tab.savedContent = "";
    tab.size = 0;
    tab.wordCount = 0;
    tab.lineCount = 0;
    tab.loadErrorMessage = error instanceof Error && error.message ? error.message : "文件读取失败。";
    tab.extension = extensionFromPath(tab.relativePath);
    tab.displayPath = tab.relativePath;
    tab.previewUnsupportedMessage = "";
    tab.readOnly = false;
    tab.editorMode = "preview";
  } finally {
    if (tab.requestToken === requestToken) {
      tab.isLoading = false;
    }
  }
}

async function activateTab(relativePath: string): Promise<void> {
  const normalizedRelativePath = normalizeRelativePath(relativePath);
  if (!normalizedRelativePath) {
    return;
  }
  await persistTabBeforeLeaving(activeRelativePath.value, normalizedRelativePath);
  activeRelativePath.value = normalizedRelativePath;
}

async function closeTab(relativePath: string): Promise<void> {
  const normalizedRelativePath = normalizeRelativePath(relativePath);
  const closingIndex = tabs.value.findIndex((tab) => tab.relativePath === normalizedRelativePath);
  if (closingIndex < 0) {
    return;
  }

  const targetTab = tabs.value[closingIndex];
  if (targetTab && isTabDirty(targetTab) && !targetTab.readOnly) {
    const saved = await saveTab(targetTab);
    if (!saved) {
      activeRelativePath.value = targetTab.relativePath;
      return;
    }
  }

  const wasActive = activeRelativePath.value === normalizedRelativePath;
  tabs.value.splice(closingIndex, 1);

  if (!wasActive) {
    return;
  }

  const nextTab = tabs.value[closingIndex] ?? tabs.value[closingIndex - 1] ?? null;
  activeRelativePath.value = nextTab?.relativePath || "";
}

async function handleModeChange(mode: PreviewEditorMode): Promise<void> {
  if (!activeTab.value || activeTab.value.editorMode === mode) {
    return;
  }
  if (mode === "edit" && activeTab.value.readOnly) {
    return;
  }
  if (mode === "preview" && isTabDirty(activeTab.value)) {
    const saved = await saveTab(activeTab.value);
    if (!saved) {
      return;
    }
  }
  activeTab.value.editorMode = mode;
}

function handleEditorInput(event: Event): void {
  if (!activeTab.value) {
    return;
  }
  const target = event.target as HTMLTextAreaElement | null;
  activeTab.value.content = String(target?.value || "");
  activeTab.value.saveErrorMessage = "";
  scheduleActiveTabAutoSave();
}

async function saveTab(tab: PreviewTab): Promise<boolean> {
  if (tab.readOnly || !isTabDirty(tab) || tab.isSaving) {
    return true;
  }

  tab.isSaving = true;
  tab.saveErrorMessage = "";

  try {
    const result = await writeWorkspaceFile({
      relativePath: tab.relativePath,
      content: tab.content
    });
    tab.content = String(result.data.content || "");
    tab.savedContent = tab.content;
    tab.size = Number(result.data.size || 0);
    tab.wordCount = Number(result.data.wordCount || countStoryTextWords(tab.content));
    tab.lineCount = Number(result.data.lineCount || countLines(tab.content));
    tab.extension = String(result.data.extension || tab.extension);
    tab.title = String(result.data.title || fileNameFromPath(tab.relativePath) || tab.title);
    tab.displayPath = String(result.data.displayPath || tab.relativePath);
    tab.updatedAt = String(result.data.updatedAt || tab.updatedAt);
    tab.readOnly = Boolean(result.data.readOnly);
    tab.previewUnsupportedMessage = Boolean(result.data.media?.previewUnsupported)
      ? String(result.data.media?.message || "当前文件暂不支持直接预览。")
      : "";
    return true;
  } catch (error: unknown) {
    tab.saveErrorMessage = error instanceof Error && error.message ? error.message : "文件保存失败。";
    return false;
  } finally {
    tab.isSaving = false;
  }
}

function isTabDirty(tab: PreviewTab): boolean {
  return tab.content !== tab.savedContent;
}

function scheduleActiveTabAutoSave(): void {
  clearAutoSaveTimer();
  if (!activeTab.value || activeTab.value.readOnly) {
    return;
  }
  autoSaveTimer = window.setTimeout(() => {
    autoSaveTimer = null;
    void flushActiveTabAutoSave();
  }, 700);
}

function clearAutoSaveTimer(): void {
  if (autoSaveTimer !== null) {
    window.clearTimeout(autoSaveTimer);
    autoSaveTimer = null;
  }
}

async function flushActiveTabAutoSave(): Promise<void> {
  clearAutoSaveTimer();
  if (!activeTab.value) {
    return;
  }
  await saveTab(activeTab.value);
}

async function persistTabBeforeLeaving(currentRelativePath: string, nextRelativePath: string): Promise<void> {
  const currentPath = normalizeRelativePath(currentRelativePath);
  const nextPath = normalizeRelativePath(nextRelativePath);
  if (!currentPath || currentPath === nextPath) {
    return;
  }
  const currentTab = tabs.value.find((tab) => tab.relativePath === currentPath);
  if (!currentTab) {
    return;
  }
  clearAutoSaveTimer();
  await saveTab(currentTab);
}

async function saveAllTabs(): Promise<void> {
  clearAutoSaveTimer();
  for (const tab of tabs.value) {
    await saveTab(tab);
  }
}

function iconFor(extension: string): string {
  if (extension === ".md") return "markdown";
  if (extension === ".json") return "data_object";
  if (extension === ".py") return "code";
  if (extension === ".vue") return "web";
  return "description";
}

function extensionFromPath(value: string): string {
  const normalized = normalizeRelativePath(value);
  if (!normalized) {
    return ".txt";
  }
  const fileName = fileNameFromPath(normalized);
  const dotIndex = fileName.lastIndexOf(".");
  return dotIndex >= 0 ? fileName.slice(dotIndex).toLowerCase() : ".txt";
}

function countStoryTextWords(content: string): number {
  return Array.from(String(content || "")).filter((char) => !/\s/.test(char)).length;
}

function countLines(content: string): number {
  if (!content) {
    return 0;
  }
  return String(content).split(/\r\n|\r|\n/).length;
}

function formatInteger(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(Math.max(0, Math.round(value)));
}

function formatBytes(value: number): string {
  const size = Math.max(0, Number(value) || 0);
  if (size < 1024) {
    return `${formatInteger(size)} 字节`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function normalizeRelativePath(value: string): string {
  return String(value || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "").trim();
}

function fileNameFromPath(value: string): string {
  const normalized = normalizeRelativePath(value);
  if (!normalized) {
    return "";
  }
  const parts = normalized.split("/");
  return parts[parts.length - 1] || normalized;
}
defineExpose({
  __testUtils: import.meta.env.MODE === "test" ? {
    tabs, activeRelativePath, activeTab, activeFileStats, renderedMarkdown, isActiveTabDirty,
    applyPersistedAppearance, handleWindowFocus, handleWindowBlur, handleWindowPageHide, handleWindowKeydown,
    handleMarkdownLinkClick, openOrActivateTab, ensureTab, loadTabContent, activateTab, closeTab,
    handleModeChange, handleEditorInput, saveTab, isTabDirty, scheduleActiveTabAutoSave, clearAutoSaveTimer,
    flushActiveTabAutoSave, persistTabBeforeLeaving, saveAllTabs, iconFor, extensionFromPath,
    countStoryTextWords, countLines, formatInteger, formatBytes, normalizeRelativePath, fileNameFromPath
  } : null
});
</script>

<style scoped>
.preview-window {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--bg-editor);
}

.preview-window-header {
  display: flex;
  align-items: stretch;
  gap: 0;
  padding: 0 0 0 10px;
  border-bottom: 1px solid var(--border-subtle);
  background: color-mix(in srgb, var(--bg-header) 92%, transparent);
  -webkit-app-region: drag;
  app-region: drag;
}

.preview-window-tabs {
  min-width: 0;
  max-width: 46vw;
  display: flex;
  align-items: stretch;
  overflow-x: auto;
  overflow-y: hidden;
  flex-shrink: 1;
}

.preview-window-caption {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  justify-content: flex-end;
  padding: 0 10px 0 14px;
}

.preview-window-actions {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 0 10px 0 0;
}

.preview-window-tab {
  min-width: 0;
  max-width: min(320px, 34vw);
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 0 12px;
  border: 0;
  border-right: 1px solid var(--border-ghost);
  border-top: 2px solid transparent;
  background: transparent;
  color: var(--text-soft);
  cursor: pointer;
  font: inherit;
}

.preview-window-tab:hover {
  background: color-mix(in srgb, var(--bg-hover) 80%, transparent);
  color: var(--text-main);
}

.preview-window-tab.active {
  border-top-color: color-mix(in srgb, var(--accent) 56%, transparent);
  background: color-mix(in srgb, var(--bg-editor) 90%, transparent);
  color: var(--text-main);
}

.preview-window-tab-icon {
  color: inherit;
  font-size: 18px;
}

.preview-window-tab-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 600;
}

.preview-window-tab-dirty {
  width: 8px;
  height: 8px;
  border-radius: 6px;
  background: var(--accent);
  flex-shrink: 0;
}

.preview-window-tab-close {
  flex-shrink: 0;
  border-radius: 4px;
  color: var(--text-muted);
  font-size: 16px;
}

.preview-window-tab-close:hover {
  background: color-mix(in srgb, var(--bg-hover) 82%, transparent);
  color: var(--text-main);
}

.preview-window-path {
  max-width: min(34vw, 420px);
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  text-align: right;
}

.preview-window-meta {
  margin-top: 4px;
  max-width: min(34vw, 420px);
  color: color-mix(in srgb, var(--text-muted) 78%, transparent);
  font-size: 11px;
  line-height: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  text-align: right;
}

.preview-window-mode-btn {
  min-height: 26px;
  padding: 0 9px;
  border-radius: 6px;
  border: 1px solid var(--border-subtle);
  background: color-mix(in srgb, var(--bg-card) 92%, transparent);
  color: var(--text-soft);
  cursor: pointer;
  font: inherit;
  font-size: 11px;
  font-weight: 600;
}

.preview-window-mode-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-main);
}

.preview-window-mode-btn:disabled {
  opacity: 0.56;
  cursor: not-allowed;
}

.preview-window-mode-switch {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px;
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  background: color-mix(in srgb, var(--bg-card-muted) 88%, transparent);
}

.preview-window-mode-btn {
  border-color: transparent;
  background: transparent;
}

.preview-window-mode-btn.active {
  border-color: color-mix(in srgb, var(--accent) 30%, var(--border-subtle));
  background: color-mix(in srgb, var(--accent) 14%, transparent);
  color: var(--accent-strong);
}

.no-drag,
.no-drag * {
  -webkit-app-region: no-drag;
  app-region: no-drag;
}

.preview-window-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 22px 26px 28px;
}

.preview-window-empty,
.preview-window-feedback {
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.8;
}

.preview-window-empty.is-error,
.preview-window-feedback.is-error {
  color: var(--danger);
}

.preview-window-feedback {
  margin-bottom: 12px;
}

.preview-window-editor-shell {
  height: 100%;
  min-height: 0;
}

.preview-window-editor {
  width: 100%;
  min-height: 100%;
  border: 0;
  outline: none;
  resize: none;
  background: transparent;
  color: var(--text-main);
  font-family: var(--font-prose);
  font-size: 16px;
  line-height: 1.9;
}

.preview-window-markdown {
  max-width: 1120px;
  color: var(--text-main);
  font-size: 16px;
}

.preview-window-code {
  margin: 0;
  padding: 0;
  max-width: 1120px;
  color: var(--text-main);
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 16px;
  line-height: 1.75;
}

@media (max-width: 1080px) {
  .preview-window-tabs {
    max-width: 42vw;
  }

  .preview-window-path {
    max-width: 24vw;
  }
}

@media (max-width: 900px) {
  .preview-window-header {
    flex-wrap: wrap;
    align-items: stretch;
    padding-left: 0;
  }

  .preview-window-tabs {
    max-width: none;
    width: 100%;
    border-bottom: 1px solid var(--border-ghost);
  }

  .preview-window-caption {
    min-width: 0;
    flex: 1 1 auto;
    justify-content: flex-start;
    padding: 8px 14px;
  }

  .preview-window-actions {
    padding: 6px 14px 8px 0;
  }

  .preview-window-tab {
    max-width: 70vw;
    min-height: 34px;
  }

  .preview-window-path {
    max-width: none;
    text-align: left;
  }

  .preview-window-body {
    padding: 18px 18px 22px;
  }
}
</style>
