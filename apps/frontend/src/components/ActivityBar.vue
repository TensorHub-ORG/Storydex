<template>
  <aside class="activity-bar">
    <div class="activity-group">
      <button
        v-for="item in topItems"
        :key="item.id"
        class="activity-icon"
        :class="{ active: uiStore.activeActivity === item.id, 'is-collapsed': uiStore.activeActivity === item.id && uiStore.sidebarCollapsed }"
        :title="item.label"
        type="button"
        @click="handleActivitySelect(item.id)"
      >
        <span :class="['material-symbols-rounded', item.iconClass]">{{ item.icon }}</span>
        <span v-if="activityBadge(item.id) > 0" class="activity-badge">{{ activityBadge(item.id) }}</span>
      </button>
    </div>

    <div class="activity-spacer"></div>

    <div class="activity-group">
      <button
        v-for="item in bottomItems"
        :key="item.id"
        class="activity-icon"
        :class="{ active: uiStore.activeActivity === item.id, 'is-collapsed': uiStore.activeActivity === item.id && uiStore.sidebarCollapsed }"
        :title="item.label"
        type="button"
        @click="handleActivitySelect(item.id)"
      >
        <span :class="['material-symbols-rounded', item.iconClass]">{{ item.icon }}</span>
      </button>

      <div ref="accountMenuRef" class="activity-settings-wrap">
        <button
          class="activity-icon"
          :class="{ active: openMenu === 'account' }"
          :title="accountButtonTitle"
          type="button"
          aria-haspopup="dialog"
          :aria-expanded="openMenu === 'account'"
          @click="toggleMenu('account')"
        >
          <span v-if="authStore.isAuthenticated" class="activity-account-badge">{{ authStore.initials }}</span>
          <span v-else class="material-symbols-rounded">account_circle</span>
        </button>

        <transition name="settings-menu">
          <ActivityAccountMenu v-if="openMenu === 'account'" />
        </transition>
      </div>

      <div ref="settingsMenuRef" class="activity-settings-wrap">
        <button
          class="activity-icon"
          :class="{ active: openMenu === 'settings' }"
          title="设置"
          type="button"
          aria-haspopup="menu"
          :aria-expanded="openMenu === 'settings'"
          @click="toggleMenu('settings')"
        >
          <span class="material-symbols-rounded">settings</span>
        </button>

        <transition name="settings-menu">
          <div
            v-if="openMenu === 'settings'"
            class="activity-settings-menu"
            role="menu"
            aria-label="设置菜单"
          >
            <div class="activity-settings-section">
              <div class="activity-settings-heading">界面主题</div>
              <button
                v-for="item in themeOptions"
                :key="item.code"
                class="activity-theme-option"
                :class="{ active: uiStore.theme === item.code }"
                type="button"
                role="menuitemradio"
                :aria-checked="uiStore.theme === item.code"
                @click="handleThemeSelect(item.code)"
              >
                <span class="activity-theme-preview" :style="{ background: item.preview }"></span>
                <span class="activity-theme-copy">
                  <span class="activity-theme-label">{{ item.label }}</span>
                  <span class="activity-theme-description">{{ item.description }}</span>
                </span>
                <span v-if="uiStore.theme === item.code" class="material-symbols-rounded activity-theme-check">
                  check
                </span>
              </button>
            </div>

            <div class="activity-settings-section">
              <div class="activity-settings-heading">系统设置</div>
              <button class="activity-settings-link" type="button" @click="openSystemSettings">
                <span class="material-symbols-rounded">tune</span>
                <span>打开系统设置</span>
              </button>
            </div>
          </div>
        </transition>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import ActivityAccountMenu from "@/components/ActivityAccountMenu.vue";
import { themeOptions } from "@/constants/themes";
import type { ThemeCode } from "@/constants/themes";
import { useAuthStore } from "@/stores/auth";
import { useGitStore } from "@/stores/git";
import { useUiStore } from "@/stores/ui";

type OpenMenu = "account" | "settings" | null;
interface ActivityItem {
  id: string;
  label: string;
  icon: string;
  iconClass?: string;
}

const uiStore = useUiStore();
const authStore = useAuthStore();
const gitStore = useGitStore();

const accountMenuRef = ref<HTMLElement | null>(null);
const settingsMenuRef = ref<HTMLElement | null>(null);
const openMenu = ref<OpenMenu>(null);

const topItems: ActivityItem[] = [
  { id: "resources", label: "文件", icon: "description" },
  { id: "search", label: "搜索", icon: "search" },
  { id: "source-control", label: "版本控制", icon: "account_tree" },
  { id: "presets", label: "预设管理", icon: "tune" },
  { id: "relationships", label: "知识图谱", icon: "hub", iconClass: "activity-icon-symbol-compact" }
];

const bottomItems: ActivityItem[] = [
  { id: "export", label: "导出", icon: "upload" }
];

const accountButtonTitle = computed(() =>
  authStore.isAuthenticated ? `${authStore.displayName} · 账号系统` : "账号系统"
);

onMounted(() => {
  document.addEventListener("pointerdown", handleDocumentPointerDown, true);
  document.addEventListener("keydown", handleDocumentKeydown);
});

onBeforeUnmount(() => {
  document.removeEventListener("pointerdown", handleDocumentPointerDown, true);
  document.removeEventListener("keydown", handleDocumentKeydown);
});

function handleActivitySelect(activityId: string): void {
  if (uiStore.activeActivity === activityId) {
    uiStore.toggleSidebarCollapsed();
    return;
  }
  uiStore.setActivity(activityId);
  uiStore.setSidebarCollapsed(false);
}

function toggleMenu(menu: Exclude<OpenMenu, null>): void {
  openMenu.value = openMenu.value === menu ? null : menu;
  if (openMenu.value === "account" && authStore.isAuthenticated) {
    void authStore.refreshSummary({ silentAuthFailure: false });
  }
}

function closeMenus(): void {
  openMenu.value = null;
}

function handleThemeSelect(theme: ThemeCode): void {
  uiStore.setTheme(theme);
  closeMenus();
}

function openSystemSettings(): void {
  closeMenus();
  uiStore.setSystemSettingsOpen(true);
}

function activityBadge(activityId: string): number {
  if (activityId !== "source-control") {
    return 0;
  }
  return gitStore.changedCount;
}

function handleDocumentPointerDown(event: PointerEvent): void {
  if (!openMenu.value) {
    return;
  }

  const target = event.target as Node | null;
  if (!target) {
    closeMenus();
    return;
  }

  if (accountMenuRef.value?.contains(target) || settingsMenuRef.value?.contains(target)) {
    return;
  }

  closeMenus();
}

function handleDocumentKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") {
    closeMenus();
  }
}
</script>

<style scoped>
.activity-icon {
  position: relative;
}

.activity-icon-symbol-compact {
  font-size: 16px;
}

.activity-icon.is-collapsed {
  opacity: 0.6;
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

.activity-badge {
  position: absolute;
  right: 4px;
  bottom: 4px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 999px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--accent);
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  line-height: 1;
}

.activity-settings-link {
  width: 100%;
  min-height: 34px;
  border: 0;
  background: transparent;
  color: var(--text-main);
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding: 0 2px;
  cursor: pointer;
  font-size: 12px;
}

.activity-settings-link:hover:not(:disabled) {
  color: var(--text-main);
}
</style>
