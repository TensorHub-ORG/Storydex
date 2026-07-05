<template>
  <section class="welcome-shell" :class="welcomeShellClass">
    <div class="welcome-inner">
      <div class="welcome-columns">
        <div class="welcome-main-column">
          <header class="welcome-heading">
            <div class="welcome-heading-mark">
              <img :src="storydexIcon" alt="Storydex" class="welcome-heading-icon" />
              <span>{{ brandName }}</span>
            </div>
            <h1 class="welcome-title-shell">
              <span class="sr-only">{{ brandName }}</span>
              <span class="welcome-title" :style="wordmarkStyle" aria-hidden="true"></span>
            </h1>
            <p class="welcome-subtitle">{{ brandSubtitle }}</p>
          </header>

          <section class="welcome-section">
            <h2 class="welcome-section-title">启动</h2>
            <p class="welcome-section-note">{{ launchNote }}</p>

            <div class="welcome-command-list">
              <button
                class="welcome-command"
                type="button"
                :disabled="workspaceStore.isProjectSwitching || workspaceStore.isProjectCreating"
                @click="handleOpenFolder"
              >
                <span class="material-symbols-rounded">folder_open</span>
                <span class="welcome-command-copy">
                  <span class="welcome-command-label">打开文件夹</span>
                  <span class="welcome-command-desc">选择已有小说或写作项目目录。</span>
                </span>
              </button>

              <button
                class="welcome-command"
                type="button"
                :disabled="workspaceStore.isProjectSwitching || workspaceStore.isProjectCreating"
                @click="openCreateProjectDialog"
              >
                <span class="material-symbols-rounded">create_new_folder</span>
                <span class="welcome-command-copy">
                  <span class="welcome-command-label">新建项目</span>
                  <span class="welcome-command-desc">{{ createProjectDescription }}</span>
                </span>
              </button>
            </div>
          </section>

          <section class="welcome-section welcome-section-recent">
            <h2 class="welcome-section-title">最近</h2>
            <p class="welcome-section-note">继续上次的工作目录，快速回到熟悉的创作环境。</p>

            <div v-if="recentProjectsForWelcome.length === 0" class="welcome-empty">
              暂无最近项目。打开一个项目后，这里会显示最近的工作目录。
            </div>
            <div v-else class="welcome-recent-list">
              <button
                v-for="item in recentProjectsForWelcome"
                :key="item.workspaceRoot"
                class="welcome-recent-row"
                type="button"
                :disabled="workspaceStore.isProjectSwitching"
                @click="handleOpenRecent(item.workspaceRoot)"
              >
                <span class="welcome-recent-name">{{ item.projectName || pathLeaf(item.workspaceRoot) }}</span>
                <span class="welcome-recent-path" :title="item.workspaceRoot">{{ compactPath(item.workspaceRoot) }}</span>
              </button>
            </div>
          </section>
        </div>

        <aside class="welcome-practice-column">
          <section class="welcome-section">
            <h2 class="welcome-section-title">演练</h2>
            <p class="welcome-section-note">先保留为灵感展示区，后续可扩展为一键演练入口。</p>

            <div class="welcome-practice-list">
              <article
                v-for="idea in practiceIdeas"
                :key="idea.title"
                class="welcome-practice-item"
                aria-disabled="true"
              >
                <div class="welcome-practice-badge">暂不可点击</div>
                <div class="welcome-practice-title">{{ idea.title }}</div>
                <div class="welcome-practice-copy">{{ idea.copy }}</div>
                <div class="welcome-practice-meta">{{ idea.meta }}</div>
              </article>
            </div>
          </section>
        </aside>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import storydexIcon from "@/assets/storydex_icon_01.png";
import storydexFront from "../../../../assets/Storydex_front.png";
import { computed } from "vue";
import { useProjectLauncher } from "@/composables/useProjectLauncher";
import { useWorkspaceStore } from "@/stores/workspace";

interface PracticeIdea {
  title: string;
  copy: string;
  meta: string;
}

const workspaceStore = useWorkspaceStore();
const { handleOpenProjectRequest, openCreateProjectDialog, openProjectAt } = useProjectLauncher();
const RECENT_PROJECT_LIMIT = 4;
const RECENT_PROJECT_PATH_MAX = 58;

const brandName = "Storydex";
const brandSubtitle = "小说编译与创作工作台";
const launchNote = "从已有写作目录继续，或快速建立新的 Storydex 工作区。";
const createProjectDescription = "创建包含默认结构的全新 Storydex 项目。";
const welcomeShellClass = "welcome-shell-storydex";
const wordmarkStyle = {
  "--welcome-wordmark-image": `url(${storydexFront})`, 
  "--welcome-wordmark-aspect": "863 / 346",
  "--welcome-wordmark-width": "min(408px, 100%)"
};

const recentProjectsForWelcome = computed(() => workspaceStore.recentProjects.slice(0, RECENT_PROJECT_LIMIT));

const practiceIdeas: PracticeIdea[] = [
  {
    title: "旧港口的失语预言师",
    copy: "一位只能在涨潮时说真话的预言师，被迫帮助落魄继承人寻找失踪的航海日志。",
    meta: "悬疑奇幻 · 双主角"
  },
  {
    title: "雨夜电台的第三封来信",
    copy: "午夜情感电台主持人开始收到来自未来听众的来信，每一封都提前预告一次现实事故。",
    meta: "都市悬疑 · 中篇连载"
  },
  {
    title: "王朝最后一位地图匠",
    copy: "帝国边境不断在夜里移动，唯一能画出真实疆域的人却早被判定为叛徒。",
    meta: "史诗冒险 · 高架空"
  },
  {
    title: "出租屋里的神明实习生",
    copy: "一位失业编剧和一个只会实现烂愿望的神明合租后，被迫一起修补城市里破碎的心愿。",
    meta: "轻奇治愈 · 群像"
  }
];

function handleOpenFolder(): void {
  void handleOpenProjectRequest();
}

function handleOpenRecent(projectPath: string): void {
  void openProjectAt(projectPath);
}

function pathLeaf(path: string): string {
  const normalized = path.replace(/[\\/]+$/, "");
  const parts = normalized.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || path;
}

function compactPath(path: string): string {
  const normalized = path.replace(/[\\/]+$/, "");
  if (normalized.length <= RECENT_PROJECT_PATH_MAX) {
    return path;
  }

  const separator = normalized.includes("\\") ? "\\" : "/";
  const hasLeadingSeparator = /^[\\/]/.test(normalized);
  const parts = normalized.split(/[\\/]/).filter(Boolean);

  if (parts.length < 4) {
    return compactTextMiddle(normalized, RECENT_PROJECT_PATH_MAX);
  }

  const prefixLength = parts[0]?.endsWith(":") ? 2 : 2;
  const suffixLength = 2;
  const prefix = parts.slice(0, prefixLength);
  const suffix = parts.slice(-suffixLength);
  let compact = joinCompactPath(prefix, suffix, separator, hasLeadingSeparator);

  if (compact.length <= RECENT_PROJECT_PATH_MAX) {
    return compact;
  }

  compact = joinCompactPath(parts.slice(0, 1), suffix, separator, hasLeadingSeparator);
  if (compact.length <= RECENT_PROJECT_PATH_MAX) {
    return compact;
  }

  return compactTextMiddle(normalized, RECENT_PROJECT_PATH_MAX);
}

function joinCompactPath(prefix: string[], suffix: string[], separator: string, hasLeadingSeparator: boolean): string {
  const body = [...prefix, "...", ...suffix].filter(Boolean).join(separator);
  return hasLeadingSeparator ? `${separator}${body}` : body;
}

function compactTextMiddle(value: string, maxLength: number): string {
  if (value.length <= maxLength) {
    return value;
  }

  const marker = "...";
  const available = Math.max(0, maxLength - marker.length);
  const headLength = Math.ceil(available * 0.42);
  const tailLength = Math.floor(available * 0.58);
  return `${value.slice(0, headLength)}${marker}${value.slice(value.length - tailLength)}`;
}
</script>
