import { defineStore } from "pinia";
import { updateUiPreferences } from "@/api/system";
import { isThemeCode } from "@/constants/themes";
import type { ThemeCode } from "@/constants/themes";
import type { UIPreferencesResponse, WorkbenchMode } from "@/types/system";
import { readCachedThemeCode, writeCachedThemeCode } from "@/utils/appearance";

interface UiState {
  bootstrapped: boolean;
  theme: ThemeCode;
  activeActivity: string;
  workbenchMode: WorkbenchMode;
  sidebarWidth: number;
  sidebarCollapsed: boolean;
  agentCollapsed: boolean;
  agentWidth: number;
  fileFontSize: number;
  playerFontSize: number;
  systemSettingsOpen: boolean;
}

const DEFAULT_WORKBENCH_MODE: WorkbenchMode = "storydex";
const DEFAULT_SIDEBAR_WIDTH = 320;
const DEFAULT_AGENT_WIDTH = 560;
const DEFAULT_FILE_FONT_SIZE = 16;
const DEFAULT_PLAYER_FONT_SIZE = 14;
const VALID_ACTIVITY_IDS = new Set(["resources", "search", "source-control", "presets", "relationships", "prompts", "export"]);
let persistTimer: number | null = null;

export const useUiStore = defineStore("ui", {
  state: (): UiState => ({
    bootstrapped: false,
    theme: readCachedThemeCode() || "white",
    activeActivity: "resources",
    workbenchMode: DEFAULT_WORKBENCH_MODE,
    sidebarWidth: DEFAULT_SIDEBAR_WIDTH,
    sidebarCollapsed: false,
    agentCollapsed: false,
    agentWidth: DEFAULT_AGENT_WIDTH,
    fileFontSize: DEFAULT_FILE_FONT_SIZE,
    playerFontSize: DEFAULT_PLAYER_FONT_SIZE,
    systemSettingsOpen: false
  }),
  actions: {
    applyPersistedState(payload?: Partial<UIPreferencesResponse> | null): void {
      const theme = payload?.theme;
      this.theme = isThemeCode(theme) ? theme : "white";
      writeCachedThemeCode(this.theme);
      this.activeActivity = normalizeActivityId(payload?.activeActivity);
      this.workbenchMode = DEFAULT_WORKBENCH_MODE;
      this.sidebarWidth = clamp(Number(payload?.sidebarWidth ?? DEFAULT_SIDEBAR_WIDTH), 220, 520);
      this.sidebarCollapsed = Boolean(payload?.sidebarCollapsed ?? false);
      this.agentCollapsed = Boolean(payload?.agentCollapsed ?? false);
      this.agentWidth = clamp(Number(payload?.agentWidth ?? DEFAULT_AGENT_WIDTH), 320, 760);
      this.fileFontSize = clamp(Number(payload?.fileFontSize ?? DEFAULT_FILE_FONT_SIZE), 12, 24);
      this.playerFontSize = clamp(Number(payload?.playerFontSize ?? DEFAULT_PLAYER_FONT_SIZE), 12, 28);
      this.bootstrapped = true;
    },

    setTheme(theme: ThemeCode): void {
      this.theme = theme;
      writeCachedThemeCode(theme);
      this.schedulePersist();
    },

    setActivity(activityId: string): void {
      this.activeActivity = normalizeActivityId(activityId);
      this.schedulePersist();
    },

    setWorkbenchMode(mode: WorkbenchMode): void {
      this.workbenchMode = mode === "storydex" ? mode : DEFAULT_WORKBENCH_MODE;
      this.schedulePersist();
    },

    setSidebarWidth(width: number): void {
      this.sidebarWidth = clamp(Math.round(width), 220, 520);
      this.schedulePersist();
    },

    setSidebarCollapsed(collapsed: boolean): void {
      this.sidebarCollapsed = Boolean(collapsed);
      this.schedulePersist();
    },

    toggleSidebarCollapsed(): void {
      this.sidebarCollapsed = !this.sidebarCollapsed;
      this.schedulePersist();
    },

    setAgentCollapsed(collapsed: boolean): void {
      this.agentCollapsed = Boolean(collapsed);
      this.schedulePersist();
    },

    toggleAgentCollapsed(): void {
      this.agentCollapsed = !this.agentCollapsed;
      this.schedulePersist();
    },

    setAgentWidth(width: number): void {
      this.agentWidth = clamp(Math.round(width), 320, 760);
      this.schedulePersist();
    },

    setFileFontSize(size: number): void {
      this.fileFontSize = clamp(Math.round(size), 12, 24);
      this.schedulePersist();
    },

    setPlayerFontSize(size: number): void {
      this.playerFontSize = clamp(Math.round(size), 12, 28);
      this.schedulePersist();
    },

    setSystemSettingsOpen(open: boolean): void {
      this.systemSettingsOpen = open;
    },

    schedulePersist(): void {
      if (typeof window === "undefined") {
        return;
      }
      if (persistTimer !== null) {
        window.clearTimeout(persistTimer);
      }
      persistTimer = window.setTimeout(() => {
        persistTimer = null;
        void this.flushPersistedState();
      }, 180);
    },

    async flushPersistedState(): Promise<void> {
      await updateUiPreferences({
        theme: this.theme,
        activeActivity: this.activeActivity,
        workbenchMode: this.workbenchMode,
        sidebarWidth: this.sidebarWidth,
        sidebarCollapsed: this.sidebarCollapsed,
        agentCollapsed: this.agentCollapsed,
        agentWidth: this.agentWidth,
        fileFontSize: this.fileFontSize,
        playerFontSize: this.playerFontSize
      });
    }
  }
});

function clamp(value: number, min: number, max: number): number {
  if (Number.isNaN(value)) {
    return min;
  }
  return Math.min(Math.max(value, min), max);
}

function normalizeActivityId(value: unknown): string {
  const normalized = String(value || "").trim();
  return VALID_ACTIVITY_IDS.has(normalized) ? normalized : "resources";
}
