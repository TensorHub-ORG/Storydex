<template>
  <Transition name="update-notification">
    <aside
      v-if="isVisible"
      class="update-notification"
      :class="`tone-${statusTone}`"
      data-testid="update-notification"
      aria-live="polite"
      :aria-busy="actionBusy"
    >
      <span class="update-notification-icon" aria-hidden="true">
        <span
          class="material-symbols-rounded"
          :class="{ bounce: updaterState.status === 'downloading' }"
          :style="{ fontSize: `${iconFontSize}px` }"
        >{{ iconName }}</span>
      </span>

      <div class="update-notification-copy">
        <strong>{{ title }}</strong>
        <span class="update-notification-detail">{{ detail }}</span>

        <button
          v-if="isError && hasErrorDetail"
          class="update-notification-error-toggle"
          type="button"
          :aria-expanded="errorExpanded"
          @click="errorExpanded = !errorExpanded"
        >
          <span class="material-symbols-rounded" aria-hidden="true">{{ errorExpanded ? "expand_less" : "expand_more" }}</span>
          {{ errorExpanded ? "收起详情" : "展开详情" }}
        </button>
        <pre v-if="isError && hasErrorDetail && errorExpanded" class="update-notification-error-detail">{{ updaterState.error }}</pre>
      </div>

      <div
        v-if="showProgress"
        class="update-notification-progress"
        role="progressbar"
        :aria-valuenow="progressPercent"
        aria-valuemin="0"
        aria-valuemax="100"
      >
        <span class="update-notification-progress-track">
          <span class="update-notification-progress-fill" :style="{ width: `${progressPercent}%` }"></span>
        </span>
        <small>{{ progressPercent.toFixed(0) }}%</small>
      </div>

      <button
        v-else-if="showAction"
        class="update-notification-action"
        type="button"
        :disabled="actionDisabled"
        :aria-label="actionLabel"
        @click="handleUpdateClick"
      >
        {{ actionText }}
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
const errorExpanded = ref(false);
const autoCheckStarted = ref(false);
const checkInFlight = ref(false);
const installStarted = ref(false);
let detachUpdaterListener: (() => void) | null = null;
let autoCheckTimer: number | null = null;

const updaterBridge = computed(() => window.storydexDesktop?.updater);
const updateVersionKey = computed(() => updaterState.value.availableVersion.trim() || "unknown");
const hasUpdateState = computed(() => {
  return UPDATE_STATUSES.has(updaterState.value.status)
    || (updaterState.value.status === "error" && Boolean(updaterState.value.availableVersion));
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
function formatBytes(bytes: number): string {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) {
    return "0 MB";
  }
  const mb = value / (1024 * 1024);
  if (mb < 1) {
    return `${mb.toFixed(2)} MB`;
  }
  if (mb < 100) {
    return `${mb.toFixed(1)} MB`;
  }
  return `${Math.round(mb)} MB`;
}
// 下载中显示已传输/总大小，如 “12.3 MB / 48.5 MB”。缺少 total 时只显示已下载量。
const downloadedLabel = computed(() => {
  const progress = updaterState.value.progress;
  if (!progress) {
    return "";
  }
  const transferred = formatBytes(progress.transferred);
  if (Number(progress.total) > 0) {
    return `${transferred} / ${formatBytes(progress.total)}`;
  }
  return transferred;
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
      return "arrow_downward";
    case "downloaded":
      return "system_update_alt";
    case "error":
      return "refresh";
    default:
      return "system_update_alt";
  }
});
// 不同图标在同一 font-size 下的实际墨迹占比差异很大（system_update_alt 几乎填满 em 盒，
// arrow_downward 只占约 2/3），导致视觉上大小明显不一致。按各字形的墨迹占比反算 font-size，
// 使它们的可见尺寸都落在约 16px，保证四个状态图标观感一致。
const iconFontSize = computed(() => {
  switch (updaterState.value.status) {
    case "downloading":
      return 24; // arrow_downward，墨迹占比约 0.667
    case "error":
      return 23; // refresh，墨迹占比约 0.683
    case "downloaded":
    default:
      return 19; // system_update_alt，墨迹占比约 0.85
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
      return downloadedLabel.value || "正在下载更新…";
    case "downloaded":
      return "点击安装并重启 Storydex。";
    case "error":
      return errorSummary.value;
    default:
      return "点击立即下载并安装。";
  }
});
const isError = computed(() => updaterState.value.status === "error");
// 把原始报错归类成一句用户能看懂的话，详细堆栈藏在“展开详情”里。
const errorSummary = computed(() => {
  const raw = updaterState.value.error.trim();
  if (!raw) {
    return "发生未知错误，请重试。";
  }
  const lower = raw.toLowerCase();
  if (/network|net::|econn|etimedout|timed out|enotfound|dns|socket|offline|超时|网络|连接/.test(lower)) {
    return "网络连接异常，请检查网络后重试。";
  }
  if (/signature|sha512|checksum|校验|签名/.test(lower)) {
    return "更新包校验失败，请重试。";
  }
  if (/permission|eacces|eperm|access denied|权限/.test(lower)) {
    return "权限不足，无法完成更新。";
  }
  if (/enospc|disk|space|磁盘|空间/.test(lower)) {
    return "磁盘空间不足，请清理后重试。";
  }
  const firstLine = raw.split(/\r?\n/)[0].trim();
  return firstLine.length > 48 ? `${firstLine.slice(0, 48)}…` : firstLine;
});
// 只有当原始报错比归类摘要更详细时才提供“展开详情”。
const hasErrorDetail = computed(() => {
  const raw = updaterState.value.error.trim();
  return Boolean(raw) && raw !== errorSummary.value;
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
const actionText = computed(() => {
  switch (updaterState.value.status) {
    case "downloaded":
      return "安装并重启";
    case "error":
      return "重试";
    case "downloading":
      return "下载中";
    default:
      return "下载更新";
  }
});
const statusTone = computed(() => {
  switch (updaterState.value.status) {
    case "error":
      return "danger";
    case "downloaded":
      return "success";
    case "downloading":
      return "progress";
    default:
      return "accent";
  }
});
const showAction = computed(() => updaterState.value.status !== "downloading");

function applyUpdaterState(nextState: StorydexDesktopUpdaterState | null | undefined): void {
  if (!nextState || typeof nextState !== "object") {
    return;
  }
  updaterState.value = { ...updaterState.value, ...nextState };
  if (updaterState.value.status !== "error") {
    errorExpanded.value = false;
  }
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
  --tone: var(--accent);
  --tone-soft: var(--accent-soft);
  position: fixed;
  left: 60px;
  bottom: calc(var(--footer-height) + 12px);
  z-index: 90;
  width: min(348px, calc(100vw - 72px));
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  grid-template-rows: auto auto;
  column-gap: 13px;
  row-gap: 12px;
  padding: 16px 18px 16px 18px;
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
  background: var(--bg-card);
  color: var(--text-main);
  box-shadow: var(--shadow-md);
  overflow: hidden;
}

/* 左侧状态色竖条 */
.update-notification::before {
  content: "";
  position: absolute;
  inset: 0 auto 0 0;
  width: 4px;
  background: var(--tone);
}

.update-notification.tone-info {
  --tone: var(--info);
  --tone-soft: rgba(43, 109, 225, 0.12);
}

.update-notification.tone-success {
  --tone: var(--success);
  --tone-soft: rgba(31, 138, 83, 0.14);
}

.update-notification.tone-danger {
  --tone: var(--danger);
  --tone-soft: rgba(192, 63, 54, 0.14);
}

/* 图标徽章：第一列，跨到文案行 */
.update-notification-icon {
  grid-column: 1;
  grid-row: 1;
  width: 38px;
  height: 38px;
  display: grid;
  place-items: center;
  border-radius: 11px;
  background: var(--tone-soft);
  color: var(--tone);
}

.update-notification-icon .material-symbols-rounded {
  /* 字号按各图标墨迹占比在模板里逐状态归一，opsz 自动跟随字号 */
  font-variation-settings: "wght" 500, "GRAD" 0, "FILL" 0;
  font-optical-sizing: auto;
}

.update-notification-icon .bounce {
  animation: update-notification-bounce 1.1s ease-in-out infinite;
}

@keyframes update-notification-bounce {
  0%,
  100% {
    transform: translateY(-3px);
  }
  50% {
    transform: translateY(3px);
  }
}

/* 文案区：第二列第一行，与图标垂直居中对齐 */
.update-notification-copy {
  grid-column: 2;
  grid-row: 1;
  align-self: center;
  min-width: 0;
  display: grid;
  gap: 3px;
  padding-right: 24px;
}

.update-notification-copy strong {
  min-width: 0;
  font-size: 14px;
  font-weight: 700;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.update-notification-detail {
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.45;
}

/* 报错“展开详情”开关 */
.update-notification-error-toggle {
  justify-self: start;
  display: inline-flex;
  align-items: center;
  gap: 2px;
  margin-top: 2px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--tone);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}

.update-notification-error-toggle:hover {
  text-decoration: underline;
}

.update-notification-error-toggle .material-symbols-rounded {
  font-size: 16px;
}

/* 展开后的原始报错，等宽字体、可滚动、不撑破卡片 */
.update-notification-error-detail {
  margin: 4px 0 0;
  max-height: 96px;
  overflow: auto;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--tone-soft);
  color: var(--text-soft);
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace);
  font-size: 11px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}

/* 操作行：跨两列，靠右 */
.update-notification-action {
  grid-column: 1 / -1;
  grid-row: 2;
  justify-self: end;
  min-height: 32px;
  padding: 0 20px;
  border: 0;
  border-radius: 8px;
  background: var(--tone);
  color: var(--accent-contrast);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: filter 140ms ease, opacity 140ms ease;
}

.update-notification-action:hover:not(:disabled) {
  filter: brightness(1.06);
}

.update-notification-action:disabled {
  cursor: default;
  opacity: 0.6;
}

.update-notification-close {
  position: absolute;
  top: 10px;
  right: 10px;
  width: 22px;
  height: 22px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
  transition: background 140ms ease, color 140ms ease;
}

.update-notification-close:hover {
  background: var(--bg-hover);
  color: var(--text-main);
}

.update-notification-close .material-symbols-rounded {
  font-size: 16px;
}

/* 进度：跨两列 */
.update-notification-progress {
  grid-column: 1 / -1;
  grid-row: 2;
  min-height: 32px;
  display: flex;
  align-items: center;
  gap: 10px;
}

.update-notification-progress-track {
  height: 6px;
  flex: 1 1 auto;
  overflow: hidden;
  border-radius: 999px;
  background: var(--bg-card-muted);
}

.update-notification-progress-fill {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--tone);
  transition: width 200ms ease;
}

.update-notification-progress small {
  min-width: 32px;
  color: var(--text-soft);
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.update-notification-enter-active,
.update-notification-leave-active {
  transition: opacity 180ms ease, transform 180ms ease;
}

.update-notification-enter-from,
.update-notification-leave-to {
  opacity: 0;
  transform: translateY(10px) scale(0.98);
}

@media (max-width: 560px) {
  .update-notification {
    left: 12px;
    width: calc(100vw - 24px);
  }
}
</style>
