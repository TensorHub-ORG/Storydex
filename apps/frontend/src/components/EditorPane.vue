<template>
  <main class="editor-workbench">
    <template v-if="showLaunchScreen">
      <div class="editor-tabs editor-tabs-launch">
        <div class="editor-tab active is-launch">
          <span class="material-symbols-rounded">home</span>
          <span class="editor-tab-label">欢迎</span>
        </div>
      </div>

      <div class="editor-surface-wrap editor-surface-wrap-welcome">
        <WelcomeStartPage />
      </div>
    </template>

    <template v-else>
      <div class="editor-tabs" @contextmenu.prevent="handleTabStripContextMenu">
        <button
          v-for="tab in workspaceStore.openTabs"
          :key="tab.relativePath"
          class="editor-tab"
          :class="{ active: tab.relativePath === workspaceStore.activeFile }"
          type="button"
          @click="handleActivateTab(tab.relativePath)"
          @contextmenu.prevent.stop="openTabContextMenu($event, tab)"
        >
          <span class="material-symbols-rounded">{{ iconFor(tab.extension) }}</span>
          <span class="editor-tab-label">{{ tab.title }}</span>
          <span v-if="tab.dirty" class="editor-dirty-dot"></span>
          <span class="material-symbols-rounded editor-tab-close" @click.stop="handleCloseTab(tab.relativePath)">
            close
          </span>
        </button>
      </div>

      <div class="editor-surface-wrap">
        <div v-if="workspaceStore.isBootstrapping || workspaceStore.isFileLoading" class="editor-empty">
          正在读取文件内容...
        </div>
        <div v-else-if="workspaceStore.workspaceError && !workspaceStore.activeFile" class="editor-empty editor-error">
          {{ workspaceStore.workspaceError }}
        </div>
        <div v-else-if="!workspaceStore.activeFile" class="editor-empty">
          从左侧资源管理器选择文件，或点击待审批文件查看临时预览。
        </div>
        <section v-else ref="editorPaneRef" class="editor-pane">
          <div class="editor-pane-head">
            <div>
              <div class="editor-pane-title">{{ workspaceStore.activeFileName }}</div>
              <div class="editor-pane-subtitle">
                <span>{{ workspaceStore.activeDisplayPath || "未命名文件" }}</span>
                <span>{{ activeFileStats }}</span>
                <span>{{ formatDate(workspaceStore.activeFileUpdatedAt) }}</span>
              </div>
            </div>

            <div class="editor-pane-actions">
              <button class="editor-mode-btn" type="button" title="在文件中查找" aria-label="在文件中查找" @click="toggleFind">
                <span class="material-symbols-rounded">find_in_page</span>
              </button>
              <div v-if="workspaceStore.isGitReviewActive" class="editor-readonly-badge">
                <span class="material-symbols-rounded">difference</span>
                <span>只读 Diff</span>
              </div>

              <div
                v-else-if="workspaceStore.isAgentPreviewActive"
                class="editor-mode-switch"
                role="tablist"
                aria-label="待审批预览模式"
              >
                <button
                  type="button"
                  class="editor-mode-btn"
                  :class="{ active: agentPreviewMode === 'preview' }"
                  title="预览"
                  aria-label="预览"
                  @click="handleAgentPreviewModeChange('preview')"
                >
                  <span class="material-symbols-rounded">visibility</span>
                </button>
                <button
                  type="button"
                  class="editor-mode-btn"
                  :class="{ active: agentPreviewMode === 'diff' }"
                  title="Diff"
                  aria-label="Diff"
                  @click="handleAgentPreviewModeChange('diff')"
                >
                  <span class="material-symbols-rounded">difference</span>
                </button>
              </div>

              <div v-else class="editor-mode-switch" role="tablist" aria-label="编辑器模式">
                <button
                  type="button"
                  class="editor-mode-btn"
                  :class="{ active: workspaceStore.editorMode === 'preview' }"
                  title="预览"
                  aria-label="预览"
                  @click="handleModeChange('preview')"
                >
                  <span class="material-symbols-rounded">visibility</span>
                </button>
                <button
                  v-if="!workspaceStore.activeDocumentReadOnly"
                  type="button"
                  class="editor-mode-btn"
                  :class="{ active: workspaceStore.editorMode === 'edit' }"
                  title="编辑"
                  aria-label="编辑"
                  @click="handleModeChange('edit')"
                >
                  <span class="material-symbols-rounded">edit</span>
                </button>
              </div>
            </div>
          </div>

          <div v-if="editorSearchOpen" class="editor-find-box" @keydown.esc.prevent="closeFind">
            <span class="material-symbols-rounded">search</span>
            <input ref="editorFindInputRef" v-model="editorFindQuery" placeholder="在文件中查找" @keydown.enter.prevent="findNext($event.shiftKey ? -1 : 1)" />
            <span class="editor-find-count">{{ editorFindCount ? `${editorFindIndex + 1}/${editorFindCount}` : "无结果" }}</span>
            <button type="button" title="上一个" @click="findNext(-1)"><span class="material-symbols-rounded">keyboard_arrow_up</span></button>
            <button type="button" title="下一个" @click="findNext(1)"><span class="material-symbols-rounded">keyboard_arrow_down</span></button>
            <button type="button" title="关闭" @click="closeFind"><span class="material-symbols-rounded">close</span></button>
          </div>

          <GitReviewPane
            v-if="workspaceStore.isGitReviewActive"
            :diff="workspaceStore.activeGitReviewDiff"
            :title="workspaceStore.activeFileName"
            :focus-path="workspaceStore.activeGitReviewFocusPath"
            :loading="workspaceStore.isGitReviewLoading"
            :error="workspaceStore.gitReviewError"
            @refresh="handleGitReviewRefresh"
          />

          <div v-else-if="workspaceStore.isAgentPreviewActive" class="agent-preview-shell">
            <div class="agent-preview-banner">
              <span class="material-symbols-rounded">preview</span>
              <span>待审批文件预览</span>
            </div>
            <div v-if="agentPreviewMode === 'diff'" class="agent-preview-content">
              <div
                v-for="line in workspaceStore.activePreviewLines"
                :key="line.id"
                class="agent-preview-line"
                :class="`is-${line.kind}`"
              >
                <span class="agent-preview-gutter">{{ line.lineNumber ?? "" }}</span>
                <pre class="agent-preview-text">{{ line.content || " " }}</pre>
              </div>
            </div>
            <div v-else class="doc-preview-shell agent-clean-preview-content">
              <div
                v-if="renderedCharacterJsonMarkdown"
                class="doc-markdown"
                @click="handleMarkdownLinkClick"
                v-html="renderedCharacterJsonMarkdown"
              ></div>
              <div
                v-else-if="workspaceStore.isMarkdownFile"
                class="doc-markdown"
                @click="handleMarkdownLinkClick"
                v-html="renderedMarkdown"
              ></div>
              <pre v-else class="doc-preview">{{ workspaceStore.editorContent }}</pre>
            </div>
          </div>

          <LargeFileViewer v-else-if="workspaceStore.activeLargeFileWindow" />

          <div v-else-if="workspaceStore.editorMode === 'preview'" class="doc-preview-shell">
            <div v-if="workspaceStore.isPreviewUnsupported" class="doc-unavailable">
              {{ workspaceStore.previewUnsupportedMessage }}
            </div>
            <div
              v-else-if="renderedCharacterJsonMarkdown"
              class="doc-markdown"
              @click="handleMarkdownLinkClick"
              v-html="renderedCharacterJsonMarkdown"
            ></div>
            <div
              v-else-if="workspaceStore.isMarkdownFile"
              class="doc-markdown"
              @click="handleMarkdownLinkClick"
              v-html="renderedMarkdown"
            ></div>
            <pre v-else class="doc-preview">{{ workspaceStore.editorContent }}</pre>
          </div>

          <textarea
            v-else-if="!workspaceStore.isPreviewUnsupported"
            ref="editorTextAreaRef"
            :value="workspaceStore.editorContent"
            class="doc-editor"
            spellcheck="false"
            @input="handleInput"
            @keydown="handleEditorKeydown"
          />
          <div v-else class="doc-unavailable">
            {{ workspaceStore.previewUnsupportedMessage }}
          </div>
        </section>
      </div>
    </template>
  </main>

  <Teleport to="body">
    <div
      v-if="tabContextMenu.visible && contextTab"
      ref="tabContextMenuRef"
      class="explorer-context-menu editor-tab-context-menu"
      :style="{ left: `${tabContextMenu.x}px`, top: `${tabContextMenu.y}px` }"
      @click.stop
    >
      <button class="context-menu-item" type="button" @click="handleContextClose">
        <span class="tab-context-label">关闭</span>
        <span class="tab-context-hint">Ctrl+F4</span>
      </button>
      <button class="context-menu-item" type="button" :disabled="!canCloseOthers" @click="handleContextCloseOthers">
        <span class="tab-context-label">关闭其他</span>
      </button>
      <button class="context-menu-item" type="button" :disabled="!canCloseRight" @click="handleContextCloseRight">
        <span class="tab-context-label">关闭右侧标签页</span>
      </button>
      <button class="context-menu-item" type="button" :disabled="!canCloseSaved" @click="handleContextCloseSaved">
        <span class="tab-context-label">关闭已保存</span>
        <span class="tab-context-hint">Ctrl+K U</span>
      </button>
      <button class="context-menu-item" type="button" :disabled="!canCloseAll" @click="handleContextCloseAll">
        <span class="tab-context-label">全部关闭</span>
        <span class="tab-context-hint">Ctrl+K W</span>
      </button>
      <div class="context-menu-separator"></div>
      <button class="context-menu-item" type="button" @click="handleCopyPath">
        <span class="tab-context-label">复制路径</span>
        <span class="tab-context-hint">Shift+Alt+C</span>
      </button>
      <button class="context-menu-item" type="button" @click="handleCopyRelativePath">
        <span class="tab-context-label">复制相对路径</span>
        <span class="tab-context-hint">Ctrl+K Ctrl+Shift+C</span>
      </button>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import GitReviewPane from "@/components/GitReviewPane.vue";
import LargeFileViewer from "@/components/LargeFileViewer.vue";
import WelcomeStartPage from "@/components/WelcomeStartPage.vue";
import { useWorkspaceStore } from "@/stores/workspace";
import type { WorkspaceEditorTab } from "@/types/workspace";
import { createMarkdownRenderer } from "@/utils/markdown";
import {
  findMarkdownLinkAnchor,
  isExternalMarkdownHref,
  resolveMarkdownWorkspaceHref
} from "@/utils/workspaceLinks";

const workspaceStore = useWorkspaceStore();
const markdown = createMarkdownRenderer();

const showLaunchScreen = computed(() => workspaceStore.launchScreenVisible && !workspaceStore.isHelpGuideActive);
const renderedMarkdown = computed(() => markdown.render(workspaceStore.editorContent || ""));
const renderedCharacterJsonMarkdown = computed(() => {
  if (!isCharacterJsonDocument.value) {
    return "";
  }
  const markdownText = renderCharacterJsonAsMarkdown(workspaceStore.editorContent || "");
  return markdownText ? markdown.render(markdownText) : "";
});
const isCharacterJsonDocument = computed(() => {
  if (workspaceStore.activeFileExtension !== ".json") {
    return false;
  }
  return normalizeRelativePath(workspaceStore.activeFileBindingOrPath).startsWith(".storydex/characters/");
});
const tabContextMenu = ref({
  visible: false,
  x: 0,
  y: 0,
  relativePath: ""
});
const tabContextMenuRef = ref<HTMLDivElement | null>(null);
const agentPreviewMode = ref<"preview" | "diff">("preview");
const editorSearchOpen = ref(false);
const editorFindQuery = ref("");
const editorFindIndex = ref(-1);
const editorFindInputRef = ref<HTMLInputElement | null>(null);
const editorTextAreaRef = ref<HTMLTextAreaElement | null>(null);
const editorPaneRef = ref<HTMLElement | null>(null);
const previewFindMatches = ref<HTMLElement[]>([]);
const previewFindCount = ref(0);
const editorFindMatches = computed(() => {
  const query = editorFindQuery.value;
  if (!query) return [] as number[];
  const text = workspaceStore.editorContent || "";
  const positions: number[] = [];
  let offset = 0;
  while ((offset = text.indexOf(query, offset)) >= 0) { positions.push(offset); offset += Math.max(1, query.length); }
  return positions;
});
const editorFindCount = computed(() => editorTextAreaRef.value ? editorFindMatches.value.length : previewFindCount.value);
watch(editorFindQuery, async () => {
  editorFindIndex.value = editorFindQuery.value ? 0 : -1;
  await nextTick();
  refreshPreviewHighlights();
});
const contextTab = computed(
  () => workspaceStore.openTabs.find((tab) => tab.relativePath === tabContextMenu.value.relativePath) ?? null
);
const contextTabIndex = computed(() => {
  if (!contextTab.value) {
    return -1;
  }
  return workspaceStore.openTabs.findIndex((tab) => tab.relativePath === contextTab.value?.relativePath);
});
const canCloseOthers = computed(() => Boolean(contextTab.value) && workspaceStore.openTabs.length > 1);
const canCloseRight = computed(
  () => contextTabIndex.value >= 0 && contextTabIndex.value < workspaceStore.openTabs.length - 1
);
const canCloseSaved = computed(() => workspaceStore.openTabs.some((tab) => !tab.dirty));
const canCloseAll = computed(() => workspaceStore.openTabs.length > 0);
const activeFileStats = computed(() => {
  const parts: string[] = [];
  if (workspaceStore.wordCount > 0) {
    parts.push(`约 ${formatInteger(workspaceStore.wordCount)} 字`);
  }
  if (workspaceStore.lineCount > 0) {
    parts.push(`${formatInteger(workspaceStore.lineCount)} 行`);
  }
  return parts.length ? parts.join(" · ") : formatBytes(workspaceStore.activeFileSize);
});
onMounted(() => {
  document.addEventListener("pointerdown", handleDocumentPointerDown, true);
  document.addEventListener("keydown", handleDocumentKeydown, true);
  window.addEventListener("blur", closeTabContextMenu);
});

onBeforeUnmount(() => {
  clearPreviewHighlights();
  document.removeEventListener("pointerdown", handleDocumentPointerDown, true);
  document.removeEventListener("keydown", handleDocumentKeydown, true);
  window.removeEventListener("blur", closeTabContextMenu);
});

watch(
  () => workspaceStore.activeFile,
  async () => {
    agentPreviewMode.value = "preview";
    clearPreviewHighlights();
    await nextTick();
    refreshPreviewHighlights();
  }
);

watch(
  [() => workspaceStore.editorMode, () => workspaceStore.isAgentPreviewActive, agentPreviewMode],
  async () => {
    clearPreviewHighlights();
    await nextTick();
    refreshPreviewHighlights();
  }
);

watch(
  () => workspaceStore.isAgentPreviewActive,
  (active) => {
    if (!active) {
      agentPreviewMode.value = "preview";
    }
  }
);

function handleInput(event: Event): void {
  const target = event.target as HTMLTextAreaElement;
  workspaceStore.setEditorContent(target.value);
}

function toggleFind(): void {
  editorSearchOpen.value = true;
  void nextTick(() => editorFindInputRef.value?.focus());
}
function closeFind(): void {
  editorSearchOpen.value = false;
  editorFindQuery.value = "";
  clearPreviewHighlights();
  window.getSelection()?.removeAllRanges();
}
function findNext(direction: number): void {
  const area = editorTextAreaRef.value;
  if (area) {
    const matches = editorFindMatches.value;
    if (!matches.length) return;
    editorFindIndex.value = (editorFindIndex.value + direction + matches.length) % matches.length;
    const start = matches[editorFindIndex.value];
    area.focus();
    area.setSelectionRange(start, start + editorFindQuery.value.length);
    const lineHeight = Number.parseFloat(window.getComputedStyle(area).lineHeight) || 22;
    const lineIndex = workspaceStore.editorContent.slice(0, start).split("\n").length - 1;
    area.scrollTop = Math.max(0, lineIndex * lineHeight - area.clientHeight / 2);
  } else {
    const count = previewFindMatches.value.length;
    if (!count) return;
    editorFindIndex.value = (editorFindIndex.value + direction + count) % count;
    activatePreviewMatch(editorFindIndex.value, true);
  }
}

function clearPreviewHighlights(): void {
  const marks = editorPaneRef.value?.querySelectorAll<HTMLElement>("mark.editor-find-highlight") || [];
  for (const mark of marks) mark.replaceWith(document.createTextNode(mark.textContent || ""));
  editorPaneRef.value?.normalize();
  previewFindMatches.value = [];
  previewFindCount.value = 0;
}

function refreshPreviewHighlights(): void {
  clearPreviewHighlights();
  const query = editorFindQuery.value;
  const root = editorPaneRef.value?.querySelector<HTMLElement>(".doc-preview-shell, .doc-markdown");
  if (!root || !query) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement;
      if (!parent || parent.closest("mark.editor-find-highlight, script, style")) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    }
  });
  const matchesByNode: Array<{ node: Text; offsets: number[] }> = [];
  let node: Node | null;
  while ((node = walker.nextNode())) {
    const text = node.textContent || "";
    const offsets: number[] = [];
    let offset = 0;
    let found = text.indexOf(query, offset);
    while (found >= 0) {
      offsets.push(found);
      offset = found + Math.max(1, query.length);
      found = text.indexOf(query, offset);
    }
    if (offsets.length) matchesByNode.push({ node: node as Text, offsets });
  }

  const marks: HTMLElement[] = [];
  for (const match of matchesByNode) {
    const text = match.node.data;
    const fragment = document.createDocumentFragment();
    let offset = 0;
    for (const start of match.offsets) {
      fragment.append(text.slice(offset, start));
      const mark = document.createElement("mark");
      mark.className = "editor-find-highlight";
      mark.textContent = text.slice(start, start + query.length);
      fragment.append(mark);
      marks.push(mark);
      offset = start + query.length;
    }
    fragment.append(text.slice(offset));
    match.node.replaceWith(fragment);
  }
  previewFindMatches.value = marks;
  previewFindCount.value = marks.length;
  if (marks.length) {
    editorFindIndex.value = Math.min(Math.max(editorFindIndex.value, 0), marks.length - 1);
    activatePreviewMatch(editorFindIndex.value, false);
  } else {
    editorFindIndex.value = -1;
  }
}

function activatePreviewMatch(matchIndex: number, shouldScroll: boolean): void {
  previewFindMatches.value.forEach((mark, index) => mark.classList.toggle("is-active", index === matchIndex));
  const active = previewFindMatches.value[matchIndex];
  if (shouldScroll && active) active.scrollIntoView({ block: "center", behavior: "smooth" });
}

function handleEditorKeydown(event: KeyboardEvent): void {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") {
    event.preventDefault();
    toggleFind();
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
    event.preventDefault();
    void workspaceStore.saveActiveFile();
    return;
  }
  if (event.key !== "Tab") return;
  const area = event.currentTarget as HTMLTextAreaElement;
  event.preventDefault();
  const start = area.selectionStart;
  const end = area.selectionEnd;
  const value = workspaceStore.editorContent;
  const indent = "  ";
  workspaceStore.editorContent = `${value.slice(0, start)}${indent}${value.slice(end)}`;
  workspaceStore.syncActiveDocument();
  void nextTick(() => area.setSelectionRange(start + indent.length, start + indent.length));
}

function handleModeChange(mode: "preview" | "edit"): void {
  void workspaceStore.setEditorMode(mode);
}

function handleAgentPreviewModeChange(mode: "preview" | "diff"): void {
  agentPreviewMode.value = mode;
}

function handleMarkdownLinkClick(event: MouseEvent): void {
  const anchor = findMarkdownLinkAnchor(event.target);
  const href = anchor?.getAttribute("href") || "";
  const relativePath = resolveMarkdownWorkspaceHref(href, workspaceStore.activeFileBindingOrPath);
  if (relativePath) {
    event.preventDefault();
    event.stopPropagation();
    void workspaceStore.openFile(relativePath);
    return;
  }

  if (isExternalMarkdownHref(href)) {
    event.preventDefault();
    window.open(anchor?.href || href, "_blank", "noopener,noreferrer");
  }
}

function handleGitReviewRefresh(): void {
  void workspaceStore.refreshGitReviewDiff({ focusPath: workspaceStore.activeGitReviewFocusPath });
}

function handleActivateTab(relativePath: string): void {
  void workspaceStore.activateTab(relativePath);
}

function handleCloseTab(relativePath: string): void {
  void workspaceStore.closeTab(relativePath);
}

function handleDocumentPointerDown(event: PointerEvent): void {
  if (!tabContextMenu.value.visible) {
    return;
  }
  const target = event.target as Node | null;
  if (!target || !tabContextMenuRef.value?.contains(target)) {
    closeTabContextMenu();
  }
}

function handleDocumentKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    closeTabContextMenu();
  }
}

function handleTabStripContextMenu(event: MouseEvent): void {
  const fallbackTab =
    workspaceStore.openTabs.find((tab) => tab.relativePath === workspaceStore.activeFile) ??
    workspaceStore.openTabs[0];
  if (!fallbackTab) {
    return;
  }
  void openTabContextMenu(event, fallbackTab);
}

async function openTabContextMenu(event: MouseEvent, tab: WorkspaceEditorTab): Promise<void> {
  const anchorX = event.clientX;
  const anchorY = event.clientY;
  tabContextMenu.value = {
    visible: true,
    x: anchorX,
    y: anchorY,
    relativePath: tab.relativePath
  };
  await nextTick();
  repositionTabContextMenu(anchorX, anchorY);
}

function closeTabContextMenu(): void {
  tabContextMenu.value.visible = false;
  tabContextMenu.value.relativePath = "";
}

function repositionTabContextMenu(anchorX: number, anchorY: number): void {
  const menuElement = tabContextMenuRef.value;
  if (!menuElement || !tabContextMenu.value.visible) {
    return;
  }

  const margin = 12;
  const menuWidth = menuElement.offsetWidth;
  const menuHeight = menuElement.offsetHeight;
  const maxX = Math.max(margin, window.innerWidth - menuWidth - margin);
  const maxY = Math.max(margin, window.innerHeight - menuHeight - margin);

  tabContextMenu.value = {
    ...tabContextMenu.value,
    x: Math.min(Math.max(anchorX, margin), maxX),
    y: Math.min(Math.max(anchorY, margin), maxY)
  };
}

async function handleContextClose(): Promise<void> {
  const relativePath = contextTab.value?.relativePath || "";
  closeTabContextMenu();
  if (!relativePath) {
    return;
  }
  await workspaceStore.closeTab(relativePath);
}

async function handleContextCloseOthers(): Promise<void> {
  const currentPath = contextTab.value?.relativePath || "";
  if (!currentPath) {
    return;
  }
  const paths = workspaceStore.openTabs
    .map((tab) => tab.relativePath)
    .filter((relativePath) => relativePath !== currentPath)
    .reverse();
  closeTabContextMenu();
  await closeTabBatch(paths);
}

async function handleContextCloseRight(): Promise<void> {
  if (contextTabIndex.value < 0) {
    return;
  }
  const paths = workspaceStore.openTabs
    .slice(contextTabIndex.value + 1)
    .map((tab) => tab.relativePath)
    .reverse();
  closeTabContextMenu();
  await closeTabBatch(paths);
}

async function handleContextCloseSaved(): Promise<void> {
  const paths = workspaceStore.openTabs
    .filter((tab) => !tab.dirty)
    .map((tab) => tab.relativePath)
    .reverse();
  closeTabContextMenu();
  await closeTabBatch(paths);
}

async function handleContextCloseAll(): Promise<void> {
  const paths = workspaceStore.openTabs.map((tab) => tab.relativePath).reverse();
  closeTabContextMenu();
  await closeTabBatch(paths);
}

async function closeTabBatch(paths: string[]): Promise<void> {
  for (const relativePath of paths) {
    await workspaceStore.closeTab(relativePath);
  }
}

async function handleCopyPath(): Promise<void> {
  const relativePath = contextTab.value?.relativePath || "";
  closeTabContextMenu();
  const absolutePath = absolutePathFor(relativePath);
  if (absolutePath) {
    await writeClipboard(absolutePath);
  }
}

async function handleCopyRelativePath(): Promise<void> {
  const relativePath = contextTab.value?.relativePath || "";
  closeTabContextMenu();
  if (relativePath) {
    await writeClipboard(relativePath);
  }
}

function formatDate(isoText: string): string {
  if (!isoText) {
    return "未知时间";
  }
  const date = new Date(isoText);
  if (Number.isNaN(date.getTime())) {
    return isoText;
  }
  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatInteger(value: number): string {
  return Math.max(0, Math.round(value)).toLocaleString("zh-CN");
}

function formatBytes(value: number): string {
  const size = Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0;
  return `${formatInteger(size)} 字节`;
}

function iconFor(extension: string): string {
  if (extension === ".json") return "data_object";
  if (extension === ".diff") return "difference";
  if (extension === ".md") return "markdown";
  if (extension === ".vue") return "web";
  if (extension === ".py") return "code";
  return "description";
}

function samePath(left: string, right: string): boolean {
  const normalize = (value: string) => String(value || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "").trim();
  return Boolean(normalize(left)) && normalize(left) === normalize(right);
}

function absolutePathFor(relativePath: string): string {
  const projectRoot = workspaceStore.projectRootLabel;
  if (!projectRoot) {
    return "";
  }
  const normalizedRoot = projectRoot.replace(/[\\/]+$/, "");
  const normalizedRelative = normalizeRelativePath(relativePath);
  if (!normalizedRelative) {
    return normalizedRoot;
  }
  const separator = normalizedRoot.includes("\\") ? "\\" : "/";
  return `${normalizedRoot}${separator}${normalizedRelative.split("/").join(separator)}`;
}

function normalizeRelativePath(value: string): string {
  return String(value || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "").trim();
}

function renderCharacterJsonAsMarkdown(content: string): string {
  const parsed = parseCharacterJson(content);
  if (!parsed) {
    return "";
  }

  const title = stringValue(parsed.name) || "未命名角色";
  const lines = [`# ${title}`];
  const fieldLabels: Array<[string, string]> = [
    ["role", "定位"],
    ["age", "年龄"],
    ["identity", "身份"],
    ["appearance", "外貌"],
    ["personality", "性格"],
    ["motivation", "动机"],
    ["secret", "秘密"],
    ["arc", "成长弧"],
    ["notes", "写作提示"],
  ];

  for (const [key, label] of fieldLabels) {
    const value = parsed[key];
    if (value === undefined || value === null || value === "") {
      continue;
    }
    lines.push("", `## ${label}`, renderMarkdownValue(value));
  }

  const relationships = parsed.relationships;
  if (relationships && typeof relationships === "object" && !Array.isArray(relationships)) {
    const entries = Object.entries(relationships as Record<string, unknown>)
      .filter(([, value]) => value !== undefined && value !== null && String(value).trim());
    if (entries.length > 0) {
      lines.push("", "## 人物关系");
      for (const [name, value] of entries) {
        lines.push(`- **${name}**：${renderInlineValue(value)}`);
      }
    }
  }

  return lines.join("\n").trim();
}

function parseCharacterJson(content: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(content);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

function renderMarkdownValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value.map((item) => `- ${renderInlineValue(item)}`).join("\n");
  }
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => `- **${key}**：${renderInlineValue(item)}`)
      .join("\n");
  }
  return renderInlineValue(value);
}

function renderInlineValue(value: unknown): string {
  if (value === undefined || value === null) {
    return "";
  }
  if (Array.isArray(value)) {
    return value.map((item) => renderInlineValue(item)).filter(Boolean).join("；");
  }
  if (typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => `${key}：${renderInlineValue(item)}`)
      .join("；");
  }
  return String(value).trim();
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
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
    tabContextMenu, agentPreviewMode, contextTab, contextTabIndex, activeFileStats, renderedMarkdown,
    renderedCharacterJsonMarkdown, isCharacterJsonDocument, handleInput, handleModeChange,
    handleAgentPreviewModeChange, handleMarkdownLinkClick, handleGitReviewRefresh, handleActivateTab,
    handleCloseTab, handleDocumentPointerDown, handleDocumentKeydown, handleTabStripContextMenu,
    openTabContextMenu, closeTabContextMenu, repositionTabContextMenu, handleContextClose,
    handleContextCloseOthers, handleContextCloseRight, handleContextCloseSaved, handleContextCloseAll,
    closeTabBatch, handleCopyPath, handleCopyRelativePath, formatDate, formatInteger, formatBytes, iconFor,
    samePath, absolutePathFor, normalizeRelativePath, renderCharacterJsonAsMarkdown, parseCharacterJson,
    renderMarkdownValue, renderInlineValue, stringValue, writeClipboard
  } : null
});
</script>

<style scoped>
.editor-tab-context-menu {
  width: 264px;
  z-index: 280;
  font-size: 12px;
}

.editor-tab-context-menu .context-menu-item {
  width: 100%;
  border: 0;
  background: transparent;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  text-align: left;
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  line-height: 1.4;
}

.tab-context-label {
  min-width: 0;
}

.tab-context-hint {
  flex-shrink: 0;
  color: var(--text-muted);
  font-size: 12px;
}

.editor-pane-subtitle {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}

.editor-pane { position: relative; min-height: 0; height: 100%; }
.doc-editor,
.doc-preview,
.doc-markdown {
  font-family: var(--font-editor-user, var(--font-prose));
  font-size: var(--ui-pane-scaled-px-16, 16px);
  line-height: 1.75;
}
.editor-pane-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.editor-find-box {
  position: absolute;
  z-index: 5;
  top: 64px;
  right: 14px;
  width: min(420px, calc(100% - 28px));
  min-height: 34px;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 3px 5px;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--bg-card);
  box-shadow: 0 10px 26px rgba(15, 23, 42, .18);
}
.editor-find-box > .material-symbols-rounded { color: var(--text-muted); font-size: 17px; }
.editor-find-box input { min-width: 0; flex: 1; border: 0; outline: 0; background: transparent; color: var(--text-main); padding: 4px; }
.editor-find-box button { width: 26px; height: 26px; border: 0; background: transparent; color: var(--text-muted); cursor: pointer; display: grid; place-items: center; }
.editor-find-box button:hover { background: var(--bg-hover); color: var(--text-main); }
.editor-find-count { color: var(--text-muted); font-size: 11px; white-space: nowrap; }
.editor-pane :deep(mark.editor-find-highlight) {
  padding: 0;
  border-radius: 2px;
  background: #ffe082;
  color: #202124;
  box-shadow: 0 0 0 1px rgba(120, 84, 0, .2);
}
.editor-pane :deep(mark.editor-find-highlight.is-active) {
  background: #ff9f43;
  box-shadow: 0 0 0 2px rgba(196, 90, 0, .42);
}

.editor-readonly-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 28px;
  color: var(--text-secondary);
  font-size: 12px;
  white-space: nowrap;
}

.editor-readonly-badge .material-symbols-rounded {
  font-size: 16px;
  color: var(--accent-strong);
}

.editor-inline-action {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 30px;
  padding: 0 10px;
  border: 1px solid var(--border-ghost);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 12px;
}

.editor-inline-action:hover:not(:disabled) {
  color: var(--text-primary);
  background: color-mix(in srgb, var(--text-secondary) 8%, transparent);
}

.editor-inline-action:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.editor-inline-feedback {
  padding: 0 18px 12px;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.6;
}

.editor-inline-feedback.is-error {
  color: var(--state-danger);
}

.agent-preview-shell {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.agent-preview-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 18px 0;
  color: var(--text-secondary);
  font-size: 12px;
  letter-spacing: 0.04em;
}

.agent-preview-content {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 14px 0 24px;
}

.agent-preview-line {
  display: grid;
  grid-template-columns: 72px minmax(0, 1fr);
  align-items: stretch;
  min-height: 24px;
}

.agent-preview-line.is-added {
  background: color-mix(in srgb, var(--accent-success, #4caf50) 18%, transparent);
}

.agent-preview-line.is-removed {
  background: color-mix(in srgb, var(--accent-danger, #d15d5d) 18%, transparent);
}

.agent-preview-gutter {
  padding: 4px 14px 4px 18px;
  text-align: right;
  color: var(--text-secondary);
  user-select: none;
  border-right: 1px solid color-mix(in srgb, var(--text-secondary) 18%, transparent);
}

.agent-preview-text {
  margin: 0;
  padding: 4px 18px;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-editor, "Cascadia Mono", "Consolas", monospace);
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-primary);
}
</style>
