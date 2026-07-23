<template>
  <section v-if="visible" class="system-settings-overlay" @click.self="emit('close')">
    <div class="system-settings-window" role="dialog" aria-modal="true" aria-label="系统设置">
      <div class="system-settings-body">
        <aside class="system-settings-sidebar">
          <div class="system-settings-sidebar-title">系统设置</div>
          <label class="system-settings-search" for="system-settings-search-input">
            <span class="material-symbols-rounded" aria-hidden="true">search</span>
            <input
              id="system-settings-search-input"
              v-model.trim="searchKeyword"
              type="text"
              placeholder="搜索设置（主题、章节、布局）"
            />
          </label>

          <div class="system-settings-nav" role="tablist" aria-label="设置分类">
            <button
              v-for="section in matchedSections"
              :key="section.id"
              class="system-settings-nav-item"
              :class="{ active: activeSection === section.id }"
              type="button"
              role="tab"
              :aria-selected="activeSection === section.id"
              @click="selectSection(section.id)"
            >
              <span class="material-symbols-rounded" aria-hidden="true">{{ section.icon }}</span>
              <span>{{ section.label }}</span>
            </button>
          </div>

          <div v-if="normalizedSearch" class="system-settings-search-meta">
            {{ matchedSections.length ? `找到 ${matchedSections.length} 个匹配分类` : "没有匹配分类" }}
          </div>
        </aside>

        <main class="system-settings-content">
          <div class="system-settings-content-scroll">
          <div v-if="loading" class="system-settings-empty">正在加载系统设置...</div>

          <div v-else-if="!visibleSections.length" class="system-settings-empty">
            没有找到与“{{ searchKeyword }}”匹配的设置项。
          </div>

          <template v-else>
            <article v-for="section in visibleSections" :key="section.id" class="system-settings-card">
              <header class="system-settings-card-header">
                <div class="system-settings-card-title-wrap">
                  <span class="material-symbols-rounded" aria-hidden="true">{{ section.icon }}</span>
                  <h2 class="system-settings-card-title">{{ section.label }}</h2>
                </div>
                <div class="system-settings-card-actions">
                  <button
                    class="system-settings-icon-action"
                    type="button"
                    title="重新加载"
                    :disabled="isSectionReloading(section.id)"
                    @click="reloadSection(section.id)"
                  >
                    <span class="material-symbols-rounded" aria-hidden="true">refresh</span>
                  </button>
                  <button
                    class="system-settings-icon-action"
                    type="button"
                    title="保存"
                    :disabled="isSectionSaving(section.id)"
                    @click="saveSection(section.id)"
                  >
                    <span class="material-symbols-rounded" aria-hidden="true">save</span>
                  </button>
                  <button class="system-settings-icon-action" type="button" title="关闭" @click="emit('close')">
                    <span class="material-symbols-rounded" aria-hidden="true">close</span>
                  </button>
                </div>
              </header>

              <div v-if="section.id === 'appearance'" class="system-settings-card-body">
                <section class="system-settings-block">
                  <div class="system-settings-block-title">界面主题</div>
                  <div class="theme-grid">
                    <button
                      v-for="item in themeOptions"
                      :key="item.code"
                      class="theme-option"
                      :class="{ active: uiStore.theme === item.code }"
                      type="button"
                      @click="handleThemeSelect(item.code)"
                    >
                      <span class="theme-option-preview" :style="{ background: item.preview }"></span>
                      <span class="theme-option-copy">
                        <span class="theme-option-label">{{ item.label }}</span>
                        <span class="theme-option-description">{{ item.description }}</span>
                      </span>
                    </button>
                  </div>
                </section>


                <section class="system-settings-block">
                  <div class="system-settings-block-title">字体大小</div>

                  <label class="system-settings-field">
                    <span>左侧栏字体倍率</span>
                    <div class="system-settings-range-row">
                      <input
                        v-model.number="leftPaneFontScaleModel"
                        class="system-settings-range"
                        type="range"
                        :min="MIN_PANE_FONT_SCALE"
                        :max="MAX_PANE_FONT_SCALE"
                        :step="PANE_FONT_SCALE_STEP"
                      />
                      <span class="system-settings-range-value">{{ leftPaneFontScaleModel }}%</span>
                    </div>
                  </label>

                  <label class="system-settings-field">
                    <span>中间栏字体倍率</span>
                    <div class="system-settings-range-row">
                      <input
                        v-model.number="centerPaneFontScaleModel"
                        class="system-settings-range"
                        type="range"
                        :min="MIN_PANE_FONT_SCALE"
                        :max="MAX_PANE_FONT_SCALE"
                        :step="PANE_FONT_SCALE_STEP"
                      />
                      <span class="system-settings-range-value">{{ centerPaneFontScaleModel }}%</span>
                    </div>
                  </label>

                  <label class="system-settings-field">
                    <span>右侧栏字体倍率</span>
                    <div class="system-settings-range-row">
                      <input
                        v-model.number="rightPaneFontScaleModel"
                        class="system-settings-range"
                        type="range"
                        :min="MIN_PANE_FONT_SCALE"
                        :max="MAX_PANE_FONT_SCALE"
                        :step="PANE_FONT_SCALE_STEP"
                      />
                      <span class="system-settings-range-value">{{ rightPaneFontScaleModel }}%</span>
                    </div>
                  </label>

                  <div class="system-settings-inline-note">保留各栏原有字号层级，仅按倍率缩放；修改后自动保存</div>
                </section>
              </div>

              <div v-else-if="section.id === 'layout'" class="system-settings-card-body">
                <section class="system-settings-block">
                  <div class="system-settings-block-title">工作台宽度</div>

                  <label class="system-settings-field">
                    <span>资源栏宽度</span>
                    <div class="system-settings-range-row">
                      <input
                        v-model.number="sidebarWidthModel"
                        class="system-settings-range"
                        type="range"
                        min="220"
                        max="520"
                        step="1"
                      />
                      <span class="system-settings-range-value">{{ sidebarWidthModel }} px</span>
                    </div>
                  </label>

                  <label class="system-settings-field">
                    <span>Agent 栏宽度</span>
                    <div class="system-settings-range-row">
                      <input
                        v-model.number="agentWidthModel"
                        class="system-settings-range"
                        type="range"
                        min="320"
                        max="760"
                        step="1"
                      />
                      <span class="system-settings-range-value">{{ agentWidthModel }} px</span>
                    </div>
                  </label>


                  <div class="system-settings-inline-note">修改后自动保存</div>
                </section>
              </div>

              <div v-else-if="section.id === 'agent'" class="system-settings-card-body">
                <section class="system-settings-block">
                  <div class="system-settings-block-title">Agent 上下文来源</div>

                  <label class="system-settings-switch-card">
                    <span class="system-settings-switch-copy">
                      <span class="system-settings-switch-title">Coomi 通用 Memory</span>
                      <span class="system-settings-switch-description">
                        在下一轮执行中启用 Coomi persistent memory 注入与 MemoryRecall
                      </span>
                    </span>
                    <span class="system-settings-switch">
                      <input v-model="coomiMemoryEnabled" type="checkbox" :disabled="saving" />
                      <span class="system-settings-switch-track" aria-hidden="true"></span>
                    </span>
                  </label>

                  <label class="system-settings-switch-card">
                    <span class="system-settings-switch-copy">
                      <span class="system-settings-switch-title">WIKI 参考上下文</span>
                      <span class="system-settings-switch-description">
                        在下一轮执行中注入匹配实体的 WIKI 参考块；不影响主动 WIKI 查询工具
                      </span>
                    </span>
                    <span class="system-settings-switch">
                      <input v-model="wikiContextEnabled" type="checkbox" :disabled="saving" />
                      <span class="system-settings-switch-track" aria-hidden="true"></span>
                    </span>
                  </label>

                  <div class="system-settings-inline-note">默认均开启；修改只影响保存后的新执行。</div>
                  <div v-if="errorMessage" class="system-settings-feedback is-error">{{ errorMessage }}</div>
                  <div v-else-if="successMessage" class="system-settings-feedback is-success">{{ successMessage }}</div>
                </section>
              </div>

              <div v-else-if="section.id === 'about'" class="system-settings-card-body">
                <section class="system-settings-block">
                  <div class="system-settings-block-title">软件更新</div>

                  <div v-if="!updaterBridgeAvailable" class="system-settings-empty-inline">
                    当前在浏览器中运行，软件更新仅在桌面版可用。
                  </div>

                  <template v-else>
                    <div class="system-settings-update-meta">
                      <span>当前版本</span>
                      <strong>v{{ updaterState.currentVersion || "未知" }}</strong>
                    </div>
                    <div
                      v-if="updaterState.availableVersion && updaterState.status !== 'not-available'"
                      class="system-settings-update-meta"
                    >
                      <span>可用新版本</span>
                      <strong>v{{ updaterState.availableVersion }}</strong>
                    </div>

                    <div v-if="updaterState.status === 'downloading' && updaterState.progress" class="system-settings-update-progress">
                      <div class="system-settings-update-progress-bar">
                        <span :style="{ width: `${Math.min(100, Math.max(0, updaterState.progress.percent))}%` }"></span>
                      </div>
                      <small>
                        {{ updaterState.progress.percent.toFixed(1) }}% ·
                        {{ formatUpdateBytes(updaterState.progress.transferred) }} / {{ formatUpdateBytes(updaterState.progress.total) }}
                        · 差分下载，仅传输有变化的部分
                      </small>
                    </div>

                    <div class="system-settings-update-actions">
                      <button
                        class="system-settings-update-btn"
                        type="button"
                        :disabled="!updaterState.supported || updaterBusy"
                        @click="checkDesktopUpdate"
                      >
                        {{ updaterState.status === "checking" ? "检查中…" : "检查更新" }}
                      </button>
                      <button
                        v-if="updaterState.status === 'available'"
                        class="system-settings-update-btn primary"
                        type="button"
                        :disabled="updaterBusy"
                        @click="downloadDesktopUpdate"
                      >
                        下载更新（增量）
                      </button>
                      <button
                        v-if="updaterState.status === 'downloaded'"
                        class="system-settings-update-btn primary"
                        type="button"
                        @click="installDesktopUpdate"
                      >
                        安装更新
                      </button>
                    </div>

                    <div class="system-settings-inline-note">{{ updaterStatusText }}</div>
                    <div v-if="updaterState.error && updaterState.status !== 'unsupported'" class="system-settings-feedback is-error">{{ updaterState.error }}</div>
                  </template>
                </section>
              </div>

              <div v-else class="system-settings-card-body">
                <section class="system-settings-block">
                  <div class="system-settings-project-meta-bar">
                    <span>来源：{{ sourceLabel }}</span>
                    <span>更新：{{ updatedAtLabel }}</span>
                    <span class="system-settings-project-path">路径：{{ settingsPathLabel }}</span>
                  </div>
                </section>

                <section v-if="!projectSettingsAvailable" class="system-settings-block">
                  <div class="system-settings-empty-inline">
                    请先打开项目
                  </div>
                </section>

                <section v-else class="system-settings-block">
                  <label class="system-settings-field">
                    <span>剧情片段扩展名</span>
                    <select v-model="segmentExtension" class="system-settings-input" :disabled="saving">
                      <option value=".md">Markdown .md</option>
                      <option value=".txt">Text .txt</option>
                    </select>
                  </label>

                  <label class="system-settings-field">
                    <span>每章剧情片段上限</span>
                    <input
                      v-model.number="maxSegmentsPerChapter"
                      class="system-settings-input"
                      type="number"
                      min="1"
                      max="99"
                      step="1"
                      inputmode="numeric"
                      :disabled="saving"
                    />
                  </label>

                  <label class="system-settings-switch-card">
                    <span class="system-settings-switch-copy">
                      <span class="system-settings-switch-title">自动命名章节目录</span>
                      <span class="system-settings-switch-description">
                        兼容历史项目
                      </span>
                    </span>
                    <span class="system-settings-switch">
                      <input v-model="autoNameChapterDirectories" type="checkbox" :disabled="saving" />
                      <span class="system-settings-switch-track" aria-hidden="true"></span>
                    </span>
                  </label>

                  <label class="system-settings-switch-card">
                    <span class="system-settings-switch-copy">
                      <span class="system-settings-switch-title">Agent 结束后询问提交</span>
                      <span class="system-settings-switch-description">
                        检测到小说项目有未提交修改时弹出提交确认
                      </span>
                    </span>
                    <span class="system-settings-switch">
                      <input v-model="agentCommitPromptEnabled" type="checkbox" :disabled="saving" />
                      <span class="system-settings-switch-track" aria-hidden="true"></span>
                    </span>
                  </label>

                  <div v-if="errorMessage" class="system-settings-feedback is-error">{{ errorMessage }}</div>
                  <div v-else-if="successMessage" class="system-settings-feedback is-success">{{ successMessage }}</div>
                </section>
              </div>
            </article>
          </template>
          </div>
        </main>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { themeOptions } from "@/constants/themes";
import { fetchAgentSettings, updateAgentSettings } from "@/api/system";
import type { ThemeCode } from "@/constants/themes";
import { useUiStore } from "@/stores/ui";
import { useWorkspaceStore } from "@/stores/workspace";
import type { StorySegmentExtension } from "@/types/workspace";
import { MAX_PANE_FONT_SCALE, MIN_PANE_FONT_SCALE, PANE_FONT_SCALE_STEP } from "@/utils/paneFontScale";

type SettingsSectionId = "appearance" | "layout" | "agent" | "project" | "about";

interface SettingsSection {
  id: SettingsSectionId;
  label: string;
  icon: string;
  description: string;
  keywords: string;
}

const sections: SettingsSection[] = [
  {
    id: "appearance",
    label: "界面与主题",
    icon: "palette",
    description: "",
    keywords: "主题 外观 颜色 模式 storydex 字体 字号 文件 玩家"
  },
  {
    id: "layout",
    label: "工作台布局",
    icon: "view_sidebar",
    description: "",
    keywords: "布局 宽度 侧栏 agent 栏"
  },
  {
    id: "agent",
    label: "Agent 上下文",
    icon: "memory",
    description: "",
    keywords: "agent 上下文 memory coomi wiki 记忆 开关 注入 recall"
  },
  {
    id: "project",
    label: "项目设置",
    icon: "auto_stories",
    description: "",
    keywords: "剧情 片段 章节 自动命名 扩展名 提交 commit git agent"
  },
  {
    id: "about",
    label: "更新与关于",
    icon: "system_update_alt",
    description: "",
    keywords: "更新 升级 版本 差分 增量 检查更新 安装 about update version"
  }
];

const extraSections: SettingsSection[] = [];

const props = defineProps<{
  visible: boolean;
}>();

const emit = defineEmits<{
  close: [];
}>();

const uiStore = useUiStore();
const workspaceStore = useWorkspaceStore();

const loading = ref(false);
const saving = ref(false);
const errorMessage = ref("");
const successMessage = ref("");
const searchKeyword = ref("");
const activeSection = ref<SettingsSectionId>("appearance");

const segmentExtension = ref<StorySegmentExtension>(".md");
const maxSegmentsPerChapter = ref(3);
const autoNameChapterDirectories = ref(false);
const agentCommitPromptEnabled = ref(true);
const coomiMemoryEnabled = ref(true);
const wikiContextEnabled = ref(true);
const savedCoomiMemoryEnabled = ref(true);
const savedWikiContextEnabled = ref(true);

const updaterState = ref<StorydexDesktopUpdaterState>({
  supported: false,
  status: "idle",
  currentVersion: "",
  availableVersion: "",
  releaseNotes: "",
  progress: null,
  error: "",
  feedUrl: ""
});
const updaterBridgeAvailable = computed(() => Boolean(window.storydexDesktop?.updater));
const updaterBusy = computed(() => updaterState.value.status === "checking" || updaterState.value.status === "downloading");
const updaterStatusText = computed(() => {
  switch (updaterState.value.status) {
    case "checking":
      return "正在检查更新…";
    case "available":
      return `发现新版本 v${updaterState.value.availableVersion}，可增量下载。`;
    case "not-available":
      return "当前已是最新版本。";
    case "downloading":
      return "正在下载更新…";
    case "downloaded":
      return "更新已下载完成；点击安装后将打开独立安装窗口。";
    case "initializing":
      return "正在初始化自动更新组件…";
    case "error":
      return "更新失败，可稍后重试。";
    case "unsupported":
      return updaterState.value.error || "当前环境不支持自动更新。";
    default:
      return "点击“检查更新”获取最新版本，下载时使用差分更新以节省流量。";
  }
});
let detachUpdaterListener: (() => void) | null = null;

const sidebarWidthModel = computed({
  get: () => uiStore.sidebarWidth,
  set: (value: number) => uiStore.setSidebarWidth(Number(value))
});

const agentWidthModel = computed({
  get: () => uiStore.agentWidth,
  set: (value: number) => uiStore.setAgentWidth(Number(value))
});

const leftPaneFontScaleModel = computed({
  get: () => uiStore.leftPaneFontScale,
  set: (value: number) => uiStore.setLeftPaneFontScale(Number(value))
});

const centerPaneFontScaleModel = computed({
  get: () => uiStore.centerPaneFontScale,
  set: (value: number) => uiStore.setCenterPaneFontScale(Number(value))
});

const rightPaneFontScaleModel = computed({
  get: () => uiStore.rightPaneFontScale,
  set: (value: number) => uiStore.setRightPaneFontScale(Number(value))
});


const normalizedSearch = computed(() => searchKeyword.value.trim().toLowerCase());
const allSections = computed(() => [...sections, ...extraSections]);

const matchedSections = computed(() => {
  if (!normalizedSearch.value) {
    return allSections.value;
  }
  return allSections.value.filter((section) => sectionMatches(section, normalizedSearch.value));
});

const visibleSections = computed(() => {
  if (!normalizedSearch.value) {
    return allSections.value.filter((section) => section.id === activeSection.value);
  }
  return matchedSections.value;
});

const projectSettingsAvailable = computed(
  () => !workspaceStore.launchScreenVisible && Boolean(workspaceStore.currentProject)
);

const sourceLabel = computed(() => {
  if (workspaceStore.storySettings.source === "api") {
    return "API 优先";
  }
  if (workspaceStore.storySettings.source === "project_file") {
    return "项目文件回退";
  }
  return "默认设置";
});

const settingsPathLabel = computed(() => {
  return workspaceStore.storySettingsPath || ".storydex/config/project-settings.json";
});

const updatedAtLabel = computed(() => {
  const value = String(workspaceStore.storySettings.updatedAt || "").trim();
  if (!value) {
    return "尚未记录";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
});

const storySettingsDirty = computed(() => {
  return (
    segmentExtension.value !== workspaceStore.storySegmentExtension
    || normalizeMaxSegments(maxSegmentsPerChapter.value) !== workspaceStore.storyMaxSegmentsPerChapter
    || Boolean(autoNameChapterDirectories.value) !== workspaceStore.autoNameChapterDirectories
    || Boolean(agentCommitPromptEnabled.value) !== workspaceStore.storySettings.agentCommitPromptEnabled
  );
});

const agentSettingsDirty = computed(
  () =>
    coomiMemoryEnabled.value !== savedCoomiMemoryEnabled.value
    || wikiContextEnabled.value !== savedWikiContextEnabled.value
);

watch(
  matchedSections,
  (nextSections) => {
    if (!nextSections.length) {
      return;
    }
    if (!nextSections.some((section) => section.id === activeSection.value)) {
      activeSection.value = nextSections[0].id;
    }
  },
  { immediate: true }
);

watch(
  () => props.visible,
  async (visible) => {
    if (!visible) {
      searchKeyword.value = "";
      errorMessage.value = "";
      successMessage.value = "";
      return;
    }

    await initializeWindow();
  },
  { immediate: true }
);

onMounted(() => {
  document.addEventListener("keydown", handleDocumentKeydown);
  void initializeUpdaterBridge();
});

onBeforeUnmount(() => {
  document.removeEventListener("keydown", handleDocumentKeydown);
  detachUpdaterListener?.();
  detachUpdaterListener = null;
});

async function initializeUpdaterBridge(): Promise<void> {
  const bridge = window.storydexDesktop?.updater;
  if (!bridge) {
    return;
  }
  detachUpdaterListener = bridge.onState((state) => {
    updaterState.value = { ...updaterState.value, ...state };
  });
  try {
    updaterState.value = { ...updaterState.value, ...(await bridge.getState()) };
  } catch {
    // 桌面桥不可用时保持默认状态
  }
}

async function checkDesktopUpdate(): Promise<void> {
  const bridge = window.storydexDesktop?.updater;
  if (!bridge) {
    return;
  }
  updaterState.value = { ...updaterState.value, ...(await bridge.check()) };
}

async function downloadDesktopUpdate(): Promise<void> {
  const bridge = window.storydexDesktop?.updater;
  if (!bridge) {
    return;
  }
  updaterState.value = { ...updaterState.value, ...(await bridge.download()) };
}

async function installDesktopUpdate(): Promise<void> {
  const bridge = window.storydexDesktop?.updater;
  if (!bridge) {
    return;
  }
  await bridge.install();
}

function formatUpdateBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) {
    return "0 MB";
  }
  if (value >= 1024 * 1024 * 1024) {
    return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }
  if (value >= 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${Math.max(1, Math.round(value / 1024))} KB`;
}

function selectSection(sectionId: SettingsSectionId): void {
  activeSection.value = sectionId;
}

function handleThemeSelect(theme: ThemeCode): void {
  uiStore.setTheme(theme);
}

async function initializeWindow(): Promise<void> {
  loading.value = true;
  errorMessage.value = "";
  successMessage.value = "";

  try {
    await loadAgentSettings();
    if (projectSettingsAvailable.value) {
      await workspaceStore.refreshStorySettings();
    }
    syncStorySettingsFromStore();
  } catch (error: unknown) {
    errorMessage.value = error instanceof Error ? error.message : "加载系统设置失败。";
  } finally {
    loading.value = false;
  }
}

async function reloadProjectSettings(): Promise<void> {
  await initializeWindow();
}

async function reloadSection(sectionId: SettingsSectionId): Promise<void> {
  if (sectionId === "agent") {
    loading.value = true;
    errorMessage.value = "";
    successMessage.value = "";
    try {
      await loadAgentSettings();
    } catch (error: unknown) {
      errorMessage.value = error instanceof Error ? error.message : "加载 Agent 设置失败。";
    } finally {
      loading.value = false;
    }
    return;
  }
  if (sectionId === "project") {
    await reloadProjectSettings();
    return;
  }

  await initializeWindow();
}

function isSectionReloading(sectionId: SettingsSectionId): boolean {
  if (sectionId === "project" || sectionId === "agent") {
    return loading.value || saving.value;
  }
  return loading.value;
}

async function saveSection(sectionId: SettingsSectionId): Promise<void> {
  if (sectionId === "agent") {
    await handleSaveAgentSettings();
    return;
  }
  if (sectionId === "project") {
    await handleSaveProjectSettings();
    return;
  }

  errorMessage.value = "";
  successMessage.value = "";
  try {
    await uiStore.flushPersistedState();
    successMessage.value = "系统设置已保存。";
  } catch (error: unknown) {
    errorMessage.value = error instanceof Error ? error.message : "保存系统设置失败。";
  }
}

function isSectionSaving(sectionId: SettingsSectionId): boolean {
  if (sectionId === "agent") {
    return saving.value || !agentSettingsDirty.value;
  }
  if (sectionId === "project") {
    return saving.value || !storySettingsDirty.value || !projectSettingsAvailable.value;
  }
  return loading.value;
}

async function loadAgentSettings(): Promise<void> {
  const result = await fetchAgentSettings();
  coomiMemoryEnabled.value = Boolean(result.data.coomiMemoryEnabled);
  wikiContextEnabled.value = Boolean(result.data.wikiContextEnabled);
  savedCoomiMemoryEnabled.value = coomiMemoryEnabled.value;
  savedWikiContextEnabled.value = wikiContextEnabled.value;
}

async function handleSaveAgentSettings(): Promise<void> {
  saving.value = true;
  errorMessage.value = "";
  successMessage.value = "";
  try {
    const result = await updateAgentSettings({
      coomiMemoryEnabled: coomiMemoryEnabled.value,
      wikiContextEnabled: wikiContextEnabled.value
    });
    coomiMemoryEnabled.value = Boolean(result.data.coomiMemoryEnabled);
    wikiContextEnabled.value = Boolean(result.data.wikiContextEnabled);
    savedCoomiMemoryEnabled.value = coomiMemoryEnabled.value;
    savedWikiContextEnabled.value = wikiContextEnabled.value;
    successMessage.value = "Agent 上下文设置已保存，将从下一轮执行生效。";
  } catch (error: unknown) {
    errorMessage.value = error instanceof Error ? error.message : "保存 Agent 设置失败。";
  } finally {
    saving.value = false;
  }
}

async function handleSaveProjectSettings(): Promise<void> {
  if (!projectSettingsAvailable.value) {
    errorMessage.value = "请先打开一个项目。";
    return;
  }

  saving.value = true;
  errorMessage.value = "";
  successMessage.value = "";

  try {
    await workspaceStore.updateStorySettings({
      segmentExtension: segmentExtension.value,
      maxSegmentsPerChapter: normalizeMaxSegments(maxSegmentsPerChapter.value),
      autoNameChapterDirectories: Boolean(autoNameChapterDirectories.value),
      agentCommitPromptEnabled: Boolean(agentCommitPromptEnabled.value)
    });
    successMessage.value = "项目设置已保存。";
  } catch (error: unknown) {
    errorMessage.value = error instanceof Error ? error.message : "保存项目设置失败。";
  } finally {
    saving.value = false;
  }
}

function syncStorySettingsFromStore(): void {
  segmentExtension.value = workspaceStore.storySegmentExtension;
  maxSegmentsPerChapter.value = workspaceStore.storyMaxSegmentsPerChapter;
  autoNameChapterDirectories.value = workspaceStore.autoNameChapterDirectories;
  agentCommitPromptEnabled.value = workspaceStore.storySettings.agentCommitPromptEnabled;
}

function normalizeMaxSegments(value: number): number {
  const parsed = Number.isFinite(value) ? Math.trunc(value) : 3;
  return Math.max(1, Math.min(99, parsed || 3));
}

function handleDocumentKeydown(event: KeyboardEvent): void {
  if (!props.visible || event.key !== "Escape") {
    return;
  }
  emit("close");
}

function sectionMatches(section: SettingsSection, query: string): boolean {
  const searchSource = `${section.label} ${section.description} ${section.keywords}`.toLowerCase();
  return searchSource.includes(query);
}
</script>

<style scoped>
.system-settings-overlay {
  position: fixed;
  inset: 0;
  z-index: 160;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 14px;
  background: color-mix(in srgb, var(--bg-app) 82%, transparent);
  backdrop-filter: blur(8px);
}

.system-settings-window {
  width: min(1240px, calc(100vw - 28px));
  height: min(820px, calc(100vh - 28px));
  border-radius: 8px;
  border: 1px solid var(--border-subtle);
  background: var(--bg-card);
  box-shadow: var(--shadow-md);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.system-settings-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 228px minmax(0, 1fr);
  overflow: hidden;
}

.system-settings-sidebar {
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px;
  border-right: 1px solid var(--border-ghost);
  background: var(--bg-sidebar);
}

.system-settings-sidebar-title {
  padding: 2px 2px 4px;
  color: var(--text-main);
  font-size: 15px;
  font-weight: 700;
}

.system-settings-card-actions {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.system-settings-icon-action {
  width: 32px;
  height: 32px;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: transparent;
  color: var(--text-soft);
  display: inline-grid;
  place-items: center;
  cursor: pointer;
  transition: background 0.18s ease, border-color 0.18s ease, color 0.18s ease;
}

.system-settings-icon-action:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-main);
}

.system-settings-icon-action:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.system-settings-icon-action .material-symbols-rounded {
  font-size: 19px;
}

.system-settings-search {
  min-height: 34px;
  border-radius: 6px;
  border: 1px solid var(--border-subtle);
  background: color-mix(in srgb, var(--bg-card) 92%, transparent);
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 0 10px;
  color: var(--text-muted);
}

.system-settings-search input {
  width: 100%;
  min-width: 0;
  border: 0;
  background: transparent;
  color: var(--text-main);
  outline: none;
  font-size: 13px;
}

.system-settings-update-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  color: var(--text-muted);
}

.system-settings-update-meta strong {
  color: var(--text-main);
  font-variant-numeric: tabular-nums;
}

.system-settings-update-progress {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.system-settings-update-progress small {
  color: var(--text-muted);
  font-size: 11px;
}

.system-settings-update-progress-bar {
  height: 6px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--text-main) 8%, transparent);
  overflow: hidden;
}

.system-settings-update-progress-bar span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--accent-strong);
  transition: width 0.25s ease;
}

.system-settings-update-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.system-settings-update-btn {
  min-height: 32px;
  padding: 0 14px;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: transparent;
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.18s ease, border-color 0.18s ease, color 0.18s ease;
}

.system-settings-update-btn:hover:not(:disabled) {
  background: var(--bg-hover);
}

.system-settings-update-btn.primary {
  border-color: transparent;
  background: var(--accent-strong);
  color: var(--text-on-accent, #fff);
}

.system-settings-update-btn.primary:hover:not(:disabled) {
  filter: brightness(1.06);
}

.system-settings-update-btn:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.system-settings-nav {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.system-settings-nav-item {
  min-height: 34px;
  padding: 0 9px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--text-soft);
  display: inline-flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  text-align: left;
}

.system-settings-nav-item:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}

.system-settings-nav-item.active {
  border-color: color-mix(in srgb, var(--accent) 28%, var(--border-subtle));
  background: color-mix(in srgb, var(--accent) 14%, transparent);
  color: var(--accent-strong);
}

.system-settings-search-meta {
  margin-top: auto;
  color: var(--text-muted);
  font-size: 12px;
}

.system-settings-content {
  min-height: 0;
  overflow: hidden;
}

.system-settings-content-scroll {
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 14px 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  overscroll-behavior: contain;
}

.system-settings-empty {
  min-height: 220px;
  border: 1px dashed var(--border-subtle);
  border-radius: 6px;
  display: grid;
  place-items: center;
  color: var(--text-muted);
  background: color-mix(in srgb, var(--bg-card) 88%, transparent);
  font-size: 13px;
  text-align: center;
  line-height: 1.7;
  padding: 0 18px;
}

.system-settings-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-bottom: 0;
  border-bottom: 0;
}

.system-settings-card-header {
  min-height: 38px;
  padding: 0 0 10px;
  border-bottom: 1px solid var(--border-ghost);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.system-settings-card-title-wrap {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.system-settings-card-title-wrap .material-symbols-rounded {
  color: var(--accent);
}

.system-settings-card-heading {
  min-width: 0;
}

.system-settings-card-title {
  color: var(--text-main);
  font-size: 18px;
  font-weight: 700;
  margin: 0;
}

.system-settings-card-description {
  margin-top: 4px;
  color: var(--text-soft);
  font-size: 12px;
}

.system-settings-card-body {
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.system-settings-block {
  padding: 0 0 10px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  border-bottom: 1px solid color-mix(in srgb, var(--border-ghost) 78%, transparent);
}

.system-settings-block:last-child {
  padding-bottom: 0;
  border-bottom: 0;
}

.system-settings-block-title {
  color: var(--text-main);
  font-size: 13px;
  font-weight: 700;
}

.theme-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.theme-option {
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--bg-input);
  padding: 8px;
  display: flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  text-align: left;
}

.theme-option:hover {
  background: var(--bg-hover);
}

.theme-option.active {
  border-color: color-mix(in srgb, var(--accent) 32%, var(--border-subtle));
  background: color-mix(in srgb, var(--accent) 14%, transparent);
}

.theme-option-preview {
  width: 30px;
  height: 30px;
  border-radius: 6px;
  border: 1px solid color-mix(in srgb, var(--border-subtle) 90%, white 10%);
  flex-shrink: 0;
}

.theme-option-copy {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.theme-option-label {
  color: var(--text-main);
  font-size: 13px;
  font-weight: 600;
}

.theme-option-description {
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.35;
}

.mode-switch-row {
  display: inline-flex;
  gap: 8px;
}

.mode-switch-btn {
  min-height: 30px;
  padding: 0 12px;
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  background: transparent;
  color: var(--text-soft);
  cursor: pointer;
}

.mode-switch-btn:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}

.mode-switch-btn.active {
  border-color: color-mix(in srgb, var(--accent) 32%, var(--border-subtle));
  background: color-mix(in srgb, var(--accent) 16%, transparent);
  color: var(--accent-strong);
}

.system-settings-field {
  display: flex;
  flex-direction: column;
  gap: 8px;
  color: var(--text-main);
  font-size: 13px;
  font-weight: 600;
}

.system-settings-range-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.system-settings-range {
  flex: 1;
  min-width: 0;
}

.system-settings-range-value {
  min-width: 74px;
  color: var(--text-soft);
  font-size: 12px;
  text-align: right;
}

.system-settings-inline-note {
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.7;
}

.system-settings-input {
  width: 100%;
  min-height: 34px;
  padding: 0 10px;
  border: 1px solid var(--border-subtle);
  border-radius: 4px;
  background: color-mix(in srgb, var(--bg-card) 90%, transparent);
  color: var(--text-main);
  outline: none;
}

.system-settings-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-soft);
}

.system-settings-project-meta-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 14px;
  padding: 8px 10px;
  border: 1px solid var(--border-ghost);
  border-radius: 6px;
  background: var(--bg-input);
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.5;
}

.system-settings-project-path {
  min-width: 180px;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.system-settings-empty-inline {
  min-height: 48px;
  border: 1px dashed var(--border-subtle);
  border-radius: 6px;
  display: grid;
  place-items: center;
  color: var(--text-muted);
  text-align: center;
  line-height: 1.7;
  padding: 0 12px;
}

.system-settings-switch-card {
  min-height: 52px;
  padding: 4px 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  cursor: pointer;
}

.system-settings-switch-copy {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.system-settings-switch-title {
  color: var(--text-main);
  font-size: 13px;
  font-weight: 600;
}

.system-settings-switch-description {
  color: var(--text-soft);
  font-size: 12px;
  line-height: 1.6;
}

.system-settings-switch {
  position: relative;
  display: inline-flex;
  align-items: center;
}

.system-settings-switch input {
  position: absolute;
  opacity: 0;
  inset: 0;
  cursor: pointer;
}

.system-settings-switch-track {
  width: 48px;
  height: 28px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--text-soft) 22%, transparent);
  position: relative;
  transition: background-color 0.18s ease;
}

.system-settings-switch-track::after {
  content: "";
  position: absolute;
  top: 3px;
  left: 3px;
  width: 22px;
  height: 22px;
  border-radius: 999px;
  background: #fff;
  box-shadow: 0 4px 10px color-mix(in srgb, black 16%, transparent);
  transition: transform 0.18s ease;
}

.system-settings-switch input:checked + .system-settings-switch-track {
  background: color-mix(in srgb, var(--accent) 66%, white 10%);
}

.system-settings-switch input:checked + .system-settings-switch-track::after {
  transform: translateX(20px);
}

.system-settings-primary-btn,
.system-settings-secondary-btn {
  min-height: 30px;
  padding: 0 12px;
  border-radius: 4px;
  border: 1px solid var(--border-subtle);
  cursor: pointer;
}

.system-settings-primary-btn {
  background: var(--accent);
  border-color: transparent;
  color: var(--accent-contrast);
}

.system-settings-secondary-btn {
  background: transparent;
  color: var(--text-soft);
}

.system-settings-primary-btn:hover:not(:disabled) {
  background: var(--accent-strong);
}

.system-settings-secondary-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-main);
}

.system-settings-primary-btn:disabled,
.system-settings-secondary-btn:disabled,
.system-settings-input:disabled,
.system-settings-switch input:disabled + .system-settings-switch-track {
  opacity: 0.56;
  cursor: not-allowed;
}

.system-settings-feedback {
  font-size: 12px;
  line-height: 1.7;
}

.system-settings-feedback.is-error {
  color: var(--danger);
}

.system-settings-feedback.is-success {
  color: var(--success);
}

@media (max-width: 980px) {
  .system-settings-window {
    width: calc(100vw - 20px);
    height: calc(100vh - 20px);
  }

  .system-settings-body {
    grid-template-columns: minmax(0, 1fr);
  }

  .system-settings-sidebar {
    border-right: 0;
    border-bottom: 1px solid var(--border-ghost);
  }

  .system-settings-nav {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 8px;
  }

  .theme-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 700px) {
  .system-settings-overlay {
    padding: 8px;
  }

  .system-settings-sidebar,
  .system-settings-content-scroll {
    padding: 12px;
  }

  .system-settings-nav {
    grid-template-columns: minmax(0, 1fr);
  }

  .system-settings-range-row {
    flex-direction: column;
    align-items: stretch;
  }

  .system-settings-range-value {
    text-align: left;
    min-width: 0;
  }
}
</style>
