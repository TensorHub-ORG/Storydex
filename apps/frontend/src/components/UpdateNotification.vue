<template>
  <Transition name="update-notification">
    <aside
      v-if="isVisible"
      class="update-notification"
      data-testid="update-notification"
      aria-live="polite"
      :aria-busy="actionBusy"
    >
      <button
        class="update-notification-action"
        type="button"
        :disabled="actionDisabled"
        :aria-label="actionLabel"
        @click="handleUpdateClick"
      >
        <span class="update-notification-icon" aria-hidden="true">
          <span class="material-symbols-rounded">{{ iconName }}</span>
        </span>
        <span class="update-notification-copy">
          <strong>{{ title }}</strong>
          <span class="update-notification-detail">{{ detail }}</span>
          <span
            v-if="showProgress"
            class="update-notification-progress"
            role="progressbar"
            :aria-valuenow="progressPercent"
            aria-valuemin="0"
            aria-valuemax="100"
          >
            <span class="update-notification-progress-track">
              <span :style="{ width: `${progressPercent}%` }"></span>
            </span>
            <small>{{ progressPercent.toFixed(0) }}%</small>
          </span>
        </span>
        <span v-if="showActionIcon" class="material-symbols-rounded update-notification-arrow" aria-hidden="true">
          arrow_forward
        </span>
      </button>

      <button
        class="update-notification-close"
        type="button"
        title="关闭更新提醒"
        aria-label="关闭更新提醒"
        @click="dismiss"
      >
        <span class="material-symbols-rounded" aria-hidden="true">close</span>
      </button>
    </aside>
  </Transition>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

const AUTO_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000;
const UPDATE_STATUSES = new Set(["available", "downloading", "downloaded"]);

const defaultUpdaterState = (): StorydexDesktopUpdaterState => ({
  supported: false,
  status: "idle",
  currentVersion: "",
  availableVersion: "",
  releaseNotes: "",
  progress: null,
  error: "",
  feedUrl: ""
});

const updaterState = ref<StorydexDesktopUpdaterState>(defaultUpdaterState());
const dismissedVersion = ref("");
const updateRequested = ref(false);
const actionBusy = ref(false);
const autoCheckStarted = ref(false);
const checkInFlight = ref(false);
const installStarted = ref(false);
let detachUpdaterListener: (() => void) | null = null;
let autoCheckTimer: number | null = null;

const updaterBridge = computed(() => window.storydexDesktop?.updater);
const updateVersionKey = computed(() => updaterState.value.availableVersion.trim() || "unknown");
const hasUpdateState = computed(() => {
  return UPDATE_STATUSES.has(updaterState.value.status)
    || (updateRequested.value && updaterState.value.status === "error" && Boolean(updaterState.value.availableVersion));
});
const isVisible = computed(() => {
  return Boolean(updaterBridge.value) && hasUpdateState.value && dismissedVersion.value !== updateVersionKey.value;
});
const showProgress = computed(() => {
  return updaterState.value.status === "downloading" && Boolean(updaterState.value.progress);
});
const progressPercent = computed(() => {
  const percent = Number(updaterState.value.progress?.percent || 0);
  return Math.min(100, Math.max(0, Number.isFinite(percent) ? percent : 0));
});
const actionDisabled = computed(() => {
  return actionBusy.value || updaterState.value.status === "downloading";
});
const showActionIcon = computed(() => {
  return updaterState.value.status === "available"
    || updaterState.value.status === "downloaded"
    || updaterState.value.status === "error";
});
const iconName = computed(() => {
  switch (updaterState.value.status) {
    case "downloading":
      return "downloading";
    case "downloaded":
      return "system_update_alt";
    case "error":
      return "refresh";
    default:
      return "system_update_alt";
  }
});
const title = computed(() => {
  const version = updaterState.value.availableVersion.trim();
  switch (updaterState.value.status) {
    case "downloading":
      return `正在下载 v${version || "新版本"}`;
    case "downloaded":
      return `v${version || "新版本"} 已准备好`;
    case "error":
      return "更新失败";
    default:
      return `发现新版本 v${version || "新版本"}`;
  }
});
const detail = computed(() => {
  switch (updaterState.value.status) {
    case "downloading":
      return "下载完成后将自动进入安装。";
    case "downloaded":
      return "点击安装并重启 Storydex。";
    case "error":
      return "点击重试更新。";
    default:
      return "点击立即下载并安装。";
  }
});
const actionLabel = computed(() => {
  switch (updaterState.value.status) {
    case "downloaded":
      return "安装并重启 Storydex";
    case "error":
      return "重试更新 Storydex";
    case "downloading":
      return "正在下载 Storydex 更新";
    default:
      return "下载并安装 Storydex 更新";
  }
});

function applyUpdaterState(nextState: StorydexDesktopUpdaterState | null | undefined): void {
  if (!nextState || typeof nextState !== "object") {
    return;
  }
  updaterState.value = { ...updaterState.value, ...nextState };
  if (updaterState.value.status === "downloaded" && updateRequested.value) {
    void installDownloadedUpdate();
  }
  if (updaterState.value.supported && updaterState.value.status === "idle") {
    void checkForUpdate();
  }
}

async function checkForUpdate(force = false): Promise<void> {
  const bridge = updaterBridge.value;
  if (!bridge || !updaterState.value.supported || checkInFlight.value) {
    return;
  }
  if (["checking", "downloading", "downloaded"].includes(updaterState.value.status)) {
    return;
  }
  if (!force && autoCheckStarted.value) {
    return;
  }
  autoCheckStarted.value = true;
  checkInFlight.value = true;
  try {
    applyUpdaterState(await bridge.check());
  } catch {
    // Automatic checks are best-effort; manual update actions surface their errors.
  } finally {
    checkInFlight.value = false;
  }
}

async function installDownloadedUpdate(): Promise<void> {
  const bridge = updaterBridge.value;
  if (!bridge || updaterState.value.status !== "downloaded" || installStarted.value) {
    return;
  }
  installStarted.value = true;
  actionBusy.value = true;
  try {
    const installed = await bridge.install();
    if (!installed) {
      installStarted.value = false;
      updaterState.value = {
        ...updaterState.value,
        status: "error",
        error: "未能启动更新安装，请重试。"
      };
    }
  } catch (error) {
    installStarted.value = false;
    updaterState.value = {
      ...updaterState.value,
      status: "error",
      error: error instanceof Error ? error.message : String(error)
    };
  } finally {
    if (!installStarted.value) {
      actionBusy.value = false;
    }
  }
}

async function handleUpdateClick(): Promise<void> {
  const bridge = updaterBridge.value;
  if (!bridge || actionDisabled.value) {
    return;
  }
  updateRequested.value = true;
  if (updaterState.value.status === "downloaded") {
    await installDownloadedUpdate();
    return;
  }

  actionBusy.value = true;
  try {
    applyUpdaterState(await bridge.download());
    await installDownloadedUpdate();
  } catch (error) {
    updaterState.value = {
      ...updaterState.value,
      status: "error",
      error: error instanceof Error ? error.message : String(error)
    };
  } finally {
    if (!installStarted.value) {
      actionBusy.value = false;
    }
  }
}

function dismiss(): void {
  dismissedVersion.value = updateVersionKey.value;
}

async function initializeUpdater(): Promise<void> {
  const bridge = updaterBridge.value;
  if (!bridge) {
    return;
  }
  try {
    detachUpdaterListener = bridge.onState((nextState) => applyUpdaterState(nextState));
    applyUpdaterState(await bridge.getState());
  } catch {
    // Keep the notification hidden when the desktop bridge is unavailable.
  }
}

onMounted(() => {
  void initializeUpdater();
  if (updaterBridge.value) {
    autoCheckTimer = window.setInterval(() => {
      void checkForUpdate(true);
    }, AUTO_CHECK_INTERVAL_MS);
  }
});

onBeforeUnmount(() => {
  detachUpdaterListener?.();
  detachUpdaterListener = null;
  if (autoCheckTimer !== null) {
    window.clearInterval(autoCheckTimer);
    autoCheckTimer = null;
  }
});
</script>

<style scoped>
.update-notification {
  position: fixed;
  left: 60px;
  bottom: calc(var(--footer-height) + 12px);
  z-index: 90;
  width: min(360px, calc(100vw - 72px));
  min-height: 78px;
  display: flex;
  overflow: hidden;
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  color: var(--text-main);
  box-shadow: var(--shadow-md);
}

.update-notification-action {
  min-width: 0;
  flex: 1 1 auto;
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  padding: 12px 36px 12px 12px;
  border: 0;
  background: transparent;
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.update-notification-action:hover:not(:disabled) {
  background: var(--bg-hover);
}

.update-notification-action:disabled {
  cursor: default;
}

.update-notification-icon {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: var(--accent-soft);
  color: var(--accent);
}

.update-notification-icon .material-symbols-rounded {
  font-size: 18px;
}

.update-notification-copy {
  min-width: 0;
  display: grid;
  gap: 3px;
}

.update-notification-copy strong,
.update-notification-detail {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.update-notification-copy strong {
  font-size: 13px;
  font-weight: 700;
}

.update-notification-detail {
  color: var(--text-muted);
  font-size: 11px;
}

.update-notification-arrow {
  color: var(--accent);
  font-size: 18px;
}

.update-notification-close {
  position: absolute;
  top: 5px;
  right: 5px;
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.update-notification-close:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}

.update-notification-close .material-symbols-rounded {
  font-size: 17px;
}

.update-notification-progress {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 7px;
}

.update-notification-progress-track {
  height: 4px;
  flex: 1 1 auto;
  overflow: hidden;
  border-radius: 999px;
  background: var(--bg-card-muted);
}

.update-notification-progress-track span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
  transition: width 160ms ease;
}

.update-notification-progress small {
  min-width: 30px;
  color: var(--text-muted);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.update-notification-enter-active,
.update-notification-leave-active {
  transition: opacity 160ms ease, transform 160ms ease;
}

.update-notification-enter-from,
.update-notification-leave-to {
  opacity: 0;
  transform: translateY(8px);
}

@media (max-width: 560px) {
  .update-notification {
    left: 12px;
    width: calc(100vw - 24px);
  }
}
</style>
