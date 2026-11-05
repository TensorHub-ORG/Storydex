<template>
  <section v-if="visible" class="system-settings-overlay" @click.self="emit('close')">
    <div class="system-settings-window" role="dialog" aria-modal="true" aria-label="系统设置">
      <header class="system-settings-titlebar">
        <div class="system-settings-titlebar-main">
          <span class="material-symbols-rounded system-settings-titlebar-icon" aria-hidden="true">settings</span>
          <span class="system-settings-titlebar-text">系统设置</span>
        </div>
        <button class="system-settings-titlebar-close" type="button" title="关闭" @click="emit('close')">
          <span class="material-symbols-rounded">close</span>
        </button>
      </header>
      <div class="system-settings-body">
        <aside class="system-settings-sidebar">
          <label class="system-settings-search" for="system-settings-search-input">
            <span class="material-symbols-rounded" aria-hidden="true">search</span>
            <input
              id="system-settings-search-input"
              v-model.trim="searchKeyword"
              type="text"
              placeholder="搜索设置，如主题、字体"
            />
          </label>

          <div class="system-settings-nav-label">分类</div>
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
                <div v-if="section.id !== 'feedback' && section.id !== 'about'" class="system-settings-card-actions">
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
                      :aria-pressed="uiStore.theme === item.code"
                      @click="handleThemeSelect(item.code)"
                    >
                      <span class="theme-option-preview" :style="{ background: item.preview }"></span>
                      <span class="theme-option-copy">
                        <span class="theme-option-label">{{ item.label }}</span>
                        <span class="theme-option-description">{{ item.description }}</span>
                      </span>
                      <span v-if="uiStore.theme === item.code" class="material-symbols-rounded theme-option-check" aria-hidden="true">check_circle</span>
                    </button>
                  </div>
                </section>


                <section class="system-settings-block">
                  <div class="system-settings-block-title">编辑器字体样式</div>
                  <div class="system-settings-font-grid">
                    <button
                      v-for="font in editorFontOptions"
                      :key="font.id"
                      type="button"
                      class="system-settings-font-option"
                      :class="{ active: uiStore.fontFamily === font.id }"
                      :style="{ fontFamily: font.css }"
                      @click="uiStore.setFontFamily(font.id)"
                    >
                      <span>{{ font.label }}</span>
                      <small>Storydex 小说编辑预览</small>
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

              <div v-else-if="section.id === 'feedback'" class="system-settings-card-body">
                <section class="system-settings-block">
                  <div class="system-settings-block-title">问题与建议</div>
                  <div class="system-settings-inline-note">可主动提交功能异常、稳定性问题或建议，并附加最多 4 张截图。</div>
                  <div class="system-settings-update-actions">
                    <button class="system-settings-update-btn primary" type="button" @click="feedbackOpen = true">
                      <span class="material-symbols-rounded">send</span>
                      提交反馈
                    </button>
                  </div>
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
                        · 正在下载并校验签名安装包
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
                        下载更新
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
                  <div class="system-settings-chips">
                    <span class="system-settings-chip">来源 <b>{{ sourceLabel }}</b></span>
                    <span class="system-settings-chip">更新 <b>{{ updatedAtLabel }}</b></span>
                    <span class="system-settings-chip system-settings-chip-path" :title="settingsPathLabel">
                      路径 <b>{{ settingsPathLabel }}</b>
                    </span>
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
      <footer class="system-settings-footer">
        <span class="system-settings-footer-hint">界面、布局与字体设置即时生效并自动保存；Agent 与项目设置修改后需点击保存。</span>
        <span class="system-settings-footer-key">Esc 关闭</span>
      </footer>
    </div>
    <FeedbackDialog
      :open="feedbackOpen"
      source="settings"
      :diagnostics="settingsFeedbackDiagnostics"
      @close="feedbackOpen = false"
      @submitted="feedbackOpen = false"
    />
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { themeOptions } from "@/constants/themes";
import { fetchAgentSettings, updateAgentSettings } from "@/api/system";
import FeedbackDialog from "@/components/FeedbackDialog.vue";
import type { ThemeCode } from "@/constants/themes";
import { editorFontOptions, useUiStore } from "@/stores/ui";
import { useWorkspaceStore } from "@/stores/workspace";
import type { StorySegmentExtension } from "@/types/workspace";
import { MAX_PANE_FONT_SCALE, MIN_PANE_FONT_SCALE, PANE_FONT_SCALE_STEP } from "@/utils/paneFontScale";

type SettingsSectionId = "appearance" | "layout" | "agent" | "project" | "feedback" | "about";

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
    id: "feedback",
    label: "反馈与支持",
    icon: "support_agent",
    description: "",
    keywords: "反馈 报错 截图 建议 支持 bug feedback support"
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
const feedbackOpen = ref(false);

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
  feedUrl: "",
  diagnosticLog: ""
});
const updaterBridgeAvailable = computed(() => Boolean(window.storydexDesktop?.updater));
const settingsFeedbackDiagnostics = computed<Record<string, unknown>>(() => ({
  storydexVersion: updaterState.value.currentVersion,
  platform: window.storydexDesktop?.platform || navigator.platform,
  updateState: updaterState.value.status,
  runtime: "storydex-desktop"
}));
const updaterBusy = computed(() => updaterState.value.status === "checking" || updaterState.value.status === "downloading");
const updaterStatusText = computed(() => {
  switch (updaterState.value.status) {
    case "checking":
      return "正在检查更新…";
    case "available":
      return `发现新版本 v${updaterState.value.availableVersion}，可下载签名安装包。`;
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
      return "点击“检查更新”获取最新版本，下载完成后会先校验签名再安装。";
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
  const installed = await bridge.install();
  if (!installed) {
    updaterState.value = {
      ...updaterState.value,
      status: "error",
      error: updaterState.value.error || "未能启动更新安装，请重试。"
    };
  }
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
/* 系统设置窗口：与工作台同一套层级——画布作底，设置分组是白色卡片 */
.system-settings-overlay {
  position: fixed;
  inset: 0;
  z-index: 160;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 14px;
  background: var(--overlay-scrim);
  backdrop-filter: blur(2px);
}

.system-settings-window {
  width: min(1120px, calc(100vw - 28px));
  height: min(780px, calc(100vh - 28px));
  border-radius: var(--radius-xl);
  border: 1px solid var(--card-border);
  background: var(--canvas);
  box-shadow: var(--shadow-modal);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* ---- 标题栏 ---- */
.system-settings-titlebar {
  flex: 0 0 auto;
  height: 46px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 10px 0 18px;
  border-bottom: 1px solid var(--border-ghost);
  background: transparent;
}

.system-settings-titlebar-main {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.system-settings-titlebar-icon {
  font-size: 18px;
  color: var(--text-muted);
}

.system-settings-titlebar-text {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-main);
}

.system-settings-titlebar-close {
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.system-settings-titlebar-close:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}

.system-settings-titlebar-close:focus-visible {
  outline: none;
  box-shadow: 0 0 0 1px var(--accent), 0 0 0 3px var(--accent-soft);
}

.system-settings-titlebar-close .material-symbols-rounded {
  font-size: 18px;
}

.system-settings-body {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 216px minmax(0, 1fr);
  overflow: hidden;
}

/* ---- 左侧分类 ---- */
.system-settings-sidebar {
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 12px 0 10px;
  border-right: 1px solid var(--border-ghost);
  background: transparent;
}

.system-settings-search {
  flex: 0 0 auto;
  margin: 0 10px;
  height: 32px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-subtle);
  background: var(--surface-1);
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 0 9px;
  color: var(--text-muted);
  transition: border-color 140ms ease, box-shadow 140ms ease;
}

.system-settings-search:hover {
  border-color: var(--border-strong);
}

.system-settings-search:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}

.system-settings-search .material-symbols-rounded {
  font-size: 17px;
  color: var(--text-faint);
}

.system-settings-search input {
  width: 100%;
  min-width: 0;
  border: 0;
  background: transparent;
  color: var(--text-main);
  outline: none;
  font: inherit;
  font-size: 13px;
}

.system-settings-search input::placeholder {
  color: var(--text-faint);
}

.system-settings-nav-label {
  padding: 16px 18px 6px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-faint);
}

.system-settings-nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 0 8px;
  overflow-y: auto;
}

.system-settings-nav-item {
  min-height: 34px;
  padding: 0 10px;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-soft);
  display: inline-flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  text-align: left;
  font: inherit;
  font-size: 13px;
}

.system-settings-nav-item .material-symbols-rounded {
  font-size: 18px;
  color: var(--text-muted);
}

.system-settings-nav-item:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}

.system-settings-nav-item:focus-visible {
  outline: none;
  box-shadow: 0 0 0 1px var(--accent), 0 0 0 3px var(--accent-soft);
}

/* 选中项与资源浏览器一致：抬到卡片层 */
.system-settings-nav-item.active {
  background: var(--nav-selected-bg);
  box-shadow: var(--nav-selected-shadow);
  color: var(--text-main);
  font-weight: 600;
}

.system-settings-nav-item.active .material-symbols-rounded {
  color: var(--accent);
}

.system-settings-search-meta {
  margin: auto 14px 4px;
  padding-top: 8px;
  color: var(--text-muted);
  font-size: 12px;
}

/* ---- 右侧内容 ---- */
.system-settings-content {
  min-height: 0;
  overflow: hidden;
}

.system-settings-content-scroll {
  height: 100%;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 20px 28px 32px;
  display: flex;
  flex-direction: column;
  gap: 28px;
  overscroll-behavior: contain;
}

.system-settings-empty {
  min-height: 220px;
  display: grid;
  place-items: center;
  padding: 0 18px;
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.7;
  text-align: center;
}

.system-settings-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.system-settings-card-header {
  min-height: 32px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.system-settings-card-title-wrap {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.system-settings-card-title-wrap .material-symbols-rounded {
  font-size: 18px;
  color: var(--accent);
}

.system-settings-card-title {
  margin: 0;
  color: var(--text-main);
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 0;
}

.system-settings-card-actions {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

.system-settings-icon-action {
  width: 28px;
  height: 28px;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-muted);
  display: inline-grid;
  place-items: center;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}

.system-settings-icon-action:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-main);
}

.system-settings-icon-action:focus-visible {
  outline: none;
  box-shadow: 0 0 0 1px var(--accent), 0 0 0 3px var(--accent-soft);
}

.system-settings-icon-action:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.system-settings-icon-action .material-symbols-rounded {
  font-size: 18px;
}

.system-settings-card-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* 设置分组：一张卡片承载一组设置，行与行之间用细线分隔 */
.system-settings-block {
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  border: 1px solid var(--border-ghost);
  border-radius: var(--radius-lg);
  background: var(--surface-1);
  box-shadow: var(--shadow-resting);
}

.system-settings-block-title {
  color: var(--text-main);
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0;
}

/* ---- 界面主题 ---- */
.theme-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.theme-option {
  min-width: 0;
  display: grid;
  grid-template-columns: 36px minmax(0, 1fr) 18px;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--surface-1);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color 140ms ease, background 140ms ease;
}

.theme-option:hover {
  border-color: var(--border-strong);
  background: var(--bg-hover);
}

.theme-option:focus-visible {
  outline: none;
  box-shadow: 0 0 0 1px var(--accent), 0 0 0 3px var(--accent-soft);
}

.theme-option.active {
  border-color: var(--accent);
  background: var(--bg-selected);
}

.theme-option-preview {
  width: 36px;
  height: 36px;
  border-radius: var(--radius-md);
  box-shadow: inset 0 0 0 1px var(--border-subtle);
}

.theme-option-copy {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.theme-option-label {
  color: var(--text-main);
  font-size: 13px;
  font-weight: 600;
}

.theme-option-description {
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.45;
}

.theme-option-check {
  color: var(--accent);
  font-size: 18px;
}

/* ---- 编辑器字体 ---- */
.system-settings-font-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(196px, 1fr));
  gap: 8px;
}

.system-settings-font-option {
  min-width: 0;
  display: grid;
  gap: 2px;
  padding: 9px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--surface-1);
  color: var(--text-main);
  font-size: 14px;
  text-align: left;
  cursor: pointer;
  transition: border-color 140ms ease, background 140ms ease;
}

.system-settings-font-option:hover {
  border-color: var(--border-strong);
  background: var(--bg-hover);
}

.system-settings-font-option:focus-visible {
  outline: none;
  box-shadow: 0 0 0 1px var(--accent), 0 0 0 3px var(--accent-soft);
}

.system-settings-font-option.active {
  border-color: var(--accent);
  background: var(--bg-selected);
}

.system-settings-font-option small {
  overflow: hidden;
  color: var(--text-muted);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---- 设置行：左侧名称，右侧控件 ---- */
.system-settings-field {
  display: grid;
  grid-template-columns: minmax(120px, 168px) minmax(0, 1fr);
  align-items: center;
  gap: 16px;
  padding: 4px 0;
  color: var(--text-main);
  font-size: 13px;
  font-weight: 500;
}

.system-settings-field > span {
  color: var(--text-main);
}

.system-settings-field ~ .system-settings-field,
.system-settings-field ~ .system-settings-switch-card,
.system-settings-switch-card ~ .system-settings-switch-card {
  padding-top: 16px;
  border-top: 1px solid var(--border-ghost);
}

.system-settings-range-row {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 12px;
}

.system-settings-range {
  flex: 1;
  min-width: 0;
  accent-color: var(--accent);
  cursor: pointer;
}

.system-settings-range-value {
  min-width: 64px;
  color: var(--text-soft);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.system-settings-inline-note {
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.6;
}

.system-settings-input {
  width: 100%;
  max-width: 240px;
  min-height: 32px;
  padding: 0 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--surface-1);
  color: var(--text-main);
  font: inherit;
  font-size: 13px;
  font-weight: 400;
  outline: none;
  transition: border-color 140ms ease, box-shadow 140ms ease;
}

.system-settings-input:hover:not(:disabled) {
  border-color: var(--border-strong);
}

.system-settings-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}

/* ---- 元信息 chips ---- */
.system-settings-chips {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.system-settings-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 24px;
  padding: 0 10px;
  border: 1px solid var(--border-ghost);
  border-radius: var(--radius-full);
  background: var(--surface-2);
  color: var(--text-muted);
  font-size: 12px;
  white-space: nowrap;
}

.system-settings-chip b {
  color: var(--text-main);
  font-weight: 600;
}

.system-settings-chip-path {
  min-width: 0;
  max-width: 100%;
}

.system-settings-chip-path b {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 400;
}

.system-settings-empty-inline {
  min-height: 52px;
  display: grid;
  place-items: center;
  padding: 10px 12px;
  border: 1px dashed var(--border-subtle);
  border-radius: var(--radius-md);
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.7;
  text-align: center;
}

/* ---- 开关行 ---- */
.system-settings-switch-card {
  min-height: 44px;
  padding: 4px 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  cursor: pointer;
}

.system-settings-switch-copy {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.system-settings-switch-title {
  color: var(--text-main);
  font-size: 13px;
  font-weight: 500;
}

.system-settings-switch-description {
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.55;
}

.system-settings-switch {
  position: relative;
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
}

.system-settings-switch input {
  position: absolute;
  opacity: 0;
  inset: 0;
  margin: 0;
  cursor: pointer;
}

.system-settings-switch-track {
  width: 36px;
  height: 20px;
  border-radius: var(--radius-full);
  background: var(--border-strong);
  position: relative;
  transition: background-color 0.18s ease, box-shadow 0.18s ease;
}

.system-settings-switch-track::after {
  content: "";
  position: absolute;
  top: 2px;
  left: 2px;
  width: 16px;
  height: 16px;
  border-radius: var(--radius-full);
  background: #fff;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
  transition: transform 0.18s ease;
}

.system-settings-switch input:checked + .system-settings-switch-track {
  background: var(--accent);
}

.system-settings-switch input:checked + .system-settings-switch-track::after {
  transform: translateX(16px);
}

.system-settings-switch input:focus-visible + .system-settings-switch-track {
  box-shadow: 0 0 0 3px var(--accent-soft);
}

/* ---- 按钮 ---- */
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
  border-radius: var(--radius-full);
  background: var(--bg-hover);
  overflow: hidden;
}

.system-settings-update-progress-bar span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
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
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--surface-1);
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease;
}

.system-settings-update-btn .material-symbols-rounded {
  font-size: 16px;
}

.system-settings-update-btn:hover:not(:disabled) {
  border-color: var(--border-strong);
  background: var(--bg-hover);
}

.system-settings-update-btn:focus-visible {
  outline: none;
  box-shadow: 0 0 0 1px var(--accent), 0 0 0 3px var(--accent-soft);
}

.system-settings-update-btn.primary {
  border-color: transparent;
  background: var(--accent);
  color: var(--accent-contrast);
  font-weight: 600;
}

.system-settings-update-btn.primary:hover:not(:disabled) {
  border-color: transparent;
  background: var(--accent-strong);
  color: var(--accent-contrast);
}

.system-settings-update-btn:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.system-settings-input:disabled,
.system-settings-switch input:disabled + .system-settings-switch-track {
  opacity: 0.56;
  cursor: not-allowed;
}

.system-settings-feedback {
  padding: 8px 10px;
  border: 1px solid var(--success-border);
  border-radius: var(--radius-md);
  background: var(--success-bg);
  color: var(--success-fg);
  font-size: 12px;
  line-height: 1.6;
}

.system-settings-feedback.is-error {
  border-color: var(--danger-border);
  background: var(--danger-bg);
  color: var(--danger-fg);
}

.system-settings-feedback.is-success {
  border-color: var(--success-border);
  background: var(--success-bg);
  color: var(--success-fg);
}

/* ---- 底栏 ---- */
.system-settings-footer {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 36px;
  padding: 0 18px;
  border-top: 1px solid var(--border-ghost);
  background: transparent;
}

.system-settings-footer-hint {
  min-width: 0;
  color: var(--text-faint);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.system-settings-footer-key {
  flex: 0 0 auto;
  padding: 1px 7px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--surface-1);
  color: var(--text-muted);
  font-size: 11px;
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
  }

  .theme-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 700px) {
  .system-settings-overlay {
    padding: 8px;
  }

  .system-settings-content-scroll {
    padding: 14px;
  }

  .system-settings-nav {
    grid-template-columns: minmax(0, 1fr);
  }

  .system-settings-field {
    grid-template-columns: minmax(0, 1fr);
    gap: 8px;
  }

  .system-settings-input {
    max-width: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .system-settings-search,
  .system-settings-input,
  .theme-option,
  .system-settings-font-option,
  .system-settings-switch-track,
  .system-settings-switch-track::after,
  .system-settings-update-progress-bar span {
    transition: none;
  }
}
</style>
