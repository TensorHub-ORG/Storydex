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
  feedUrl: "",
  diagnosticLog: ""
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
// 使它们的可见尺寸都落在约 15px，保证四个状态图标观感一致。
const iconFontSize = computed(() => {
  switch (updaterState.value.status) {
    case "downloading":
      return 22; // arrow_downward，墨迹占比约 0.667
    case "error":
      return 22; // refresh，墨迹占比约 0.683
    case "downloaded":
    default:
      return 18; // system_update_alt，墨迹占比约 0.85
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
type UpdateErrorRule = {
  pattern: RegExp;
  summary: string;
};

// 顺序很重要：先匹配明确的更新错误，再处理宽泛的 HTTP、文件和网络错误。
const UPDATE_ERROR_RULES: UpdateErrorRule[] = [
  {
    pattern: /err_updater_invalid_signature|not signed by the application owner|signature verification|authenticode|签名.*(?:无效|失败)/i,
    summary: "更新包签名验证失败，为保护你的设备已停止安装。"
  },
  {
    pattern: /err_checksum_mismatch|checksum mismatch|sha(?:256|512).*mismatch|校验(?:值)?(?:不一致|失败)/i,
    summary: "更新包校验失败，文件可能不完整，请重新下载。"
  },
  {
    pattern: /err_updater_no_checksum|doesn't contain nor sha256 neither sha512|missing checksum|缺少.*校验/i,
    summary: "更新信息缺少安全校验值，已停止下载。"
  },
  {
    pattern: /err_updater_channel_file_not_found|cannot find channel|latest\.ya?ml.*(?:404|not found)|未找到.*更新(?:信息|元数据)/i,
    summary: "暂未获取到更新信息，请稍后再试。"
  },
  {
    pattern: /err_updater_(?:invalid_update_info|invalid_release_feed)|cannot parse update info|unable to parse channel data|invalid release feed|(?:latest\.ya?ml|更新信息).*(?:parse|format|格式|解析)/i,
    summary: "更新信息格式异常，请稍后重试。"
  },
  {
    pattern: /err_updater_invalid_version|not a valid semver|invalid semver|版本(?:号|信息).*(?:无效|错误)/i,
    summary: "更新版本信息无效，请稍后重试。"
  },
  {
    pattern: /err_updater_no_files_provided|no files provided|err_updater_asset_not_found|cannot find asset/i,
    summary: "更新安装包尚未发布完整，请稍后重试。"
  },
  {
    pattern: /no update filepath provided|未找到已下载的安装程序|(?:downloaded|update).*(?:installer|file).*(?:not found|missing)|enoent.*(?:installer|\.exe)/i,
    summary: "未找到已下载的安装包，请重新下载更新。"
  },
  {
    pattern: /maximum allowed size|file too large|err_file_too_large|更新包.*(?:过大|超过)/i,
    summary: "更新包大小超过自动更新限制，请使用完整安装包更新。"
  },
  {
    pattern: /enospc|no space left|not enough (?:disk )?space|disk.*full|磁盘空间不足/i,
    summary: "磁盘空间不足，请清理空间后重试。"
  },
  {
    pattern: /eacces|eperm|access denied|permission denied|operation not permitted|权限不足|拒绝访问/i,
    summary: "没有写入或安装权限，请确认安装目录可写后重试。"
  },
  {
    pattern: /ebusy|resource busy|file.*(?:locked|in use)|being used by another process|文件.*(?:占用|锁定)/i,
    summary: "更新文件正被其他程序占用，请完全退出 Storydex 后重试。"
  },
  {
    pattern: /powershell.*(?:unavailable|not found|cannot|无法)|cannot execute get-authenticodesignature|update helper.*(?:failed|unavailable)|更新助手.*(?:失败|无法)/i,
    summary: "系统更新助手无法启动，请确认 PowerShell 可用后重试。"
  },
  {
    pattern: /already in progress|install(?:ation)?.*(?:in progress|running)|已有.*安装.*(?:进行|运行)/i,
    summary: "已有更新安装任务正在进行，请稍候。"
  },
  {
    pattern: /cannot run installer|installer.*(?:failed|exited with code)|spawn.*(?:failed|error)|未能启动更新安装|安装程序.*(?:失败|无法启动)/i,
    summary: "安装程序启动失败，请重试或使用完整安装包更新。"
  },
  {
    pattern: /err_updater_invalid_provider_configuration|err_updater_unsupported_provider|missing hostname|缺少桌面版更新源配置|更新源.*配置/i,
    summary: "更新源配置异常，请联系 Storydex 支持。"
  },
  {
    pattern: /(?:status|status code|http)[\s:=]*(?:401|403)\b|unauthorized|forbidden/i,
    summary: "更新服务器拒绝了访问，请稍后重试。"
  },
  {
    pattern: /(?:status|status code|http)[\s:=]*404\b|cannot download.*404|not found/i,
    summary: "更新安装包不存在或尚未发布完成，请稍后重试。"
  },
  {
    pattern: /(?:status|status code|http)[\s:=]*429\b|too many requests|rate limit/i,
    summary: "检查更新过于频繁，请稍后再试。"
  },
  {
    pattern: /(?:status|status code|http)[\s:=]*5\d\d\b|bad gateway|service unavailable|gateway timeout/i,
    summary: "更新服务器暂时不可用，请稍后重试。"
  },
  {
    pattern: /certificate|cert_|ssl|tls|unable_to_verify|self signed|安全证书|证书.*(?:无效|失败|过期)/i,
    summary: "无法验证更新服务器的安全证书，请检查系统时间或网络环境。"
  },
  {
    pattern: /too many redirects|redirect.*(?:loop|exceeded)|重定向.*(?:过多|异常)/i,
    summary: "更新服务器重定向异常，请稍后重试。"
  },
  {
    pattern: /request timed out|etimedout|timeout|timed[_ -]?out|请求超时|连接超时/i,
    summary: "连接更新服务器超时，请稍后重试。"
  },
  {
    pattern: /enotfound|eai_again|err_name_not_resolved|dns|找不到主机|域名.*解析/i,
    summary: "无法解析更新服务器地址，请检查网络或 DNS 设置。"
  },
  {
    pattern: /proxy.*(?:auth|407|failed)|tunnel connection failed|代理.*(?:认证|失败)/i,
    summary: "代理服务器连接失败，请检查代理设置后重试。"
  },
  {
    pattern: /request has been aborted|response has been aborted|cancelled|canceled|下载.*取消/i,
    summary: "更新下载已中断，可点击重试继续。"
  },
  {
    pattern: /err_stream_not_finished|response ends without|received data length|size mismatch|unexpected end|下载.*(?:不完整|中断)/i,
    summary: "更新包下载不完整，请重新下载。"
  },
  {
    pattern: /network|net::|econn(?:refused|reset|aborted)?|socket|offline|互联网|网络|连接失败/i,
    summary: "网络连接异常，请检查网络后重试。"
  }
];

// 把原始报错归类成一句用户能看懂的话，详细堆栈藏在“展开详情”里。
function summarizeUpdateError(rawError: string): string {
  const raw = rawError.trim();
  if (!raw) {
    return "更新未完成，请重试；若问题持续，请联系 Storydex 支持。";
  }
  return UPDATE_ERROR_RULES.find((rule) => rule.pattern.test(raw))?.summary
    || "更新未完成，请重试；若仍失败，请展开详情并联系 Storydex 支持。";
}

const errorSummary = computed(() => summarizeUpdateError(updaterState.value.error));
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
  position: fixed;
  left: 60px;
  bottom: calc(var(--footer-height) + 12px);
  z-index: 90;
  width: min(310px, calc(100vw - 72px));
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  grid-template-rows: auto auto;
  column-gap: 9px;
  row-gap: 10px;
  padding: 12px 13px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  color: var(--text-main);
  box-shadow: var(--shadow-sm);
}

.update-notification.tone-info {
  --tone: var(--info);
}

.update-notification.tone-success {
  --tone: var(--success);
}

.update-notification.tone-danger {
  --tone: var(--danger);
}

/* 图标：不加徽章底色，只用状态色着色，与标题行对齐 */
.update-notification-icon {
  grid-column: 1;
  grid-row: 1;
  display: grid;
  place-items: center;
  width: 18px;
  height: 18px;
  color: var(--tone);
}

.update-notification-icon .material-symbols-rounded {
  /* 字号按各图标墨迹占比在模板里逐状态归一，opsz 自动跟随字号 */
  font-variation-settings: "wght" 400, "GRAD" 0, "FILL" 0;
  font-optical-sizing: auto;
}

.update-notification-icon .bounce {
  animation: update-notification-bounce 1.1s ease-in-out infinite;
}

@keyframes update-notification-bounce {
  0%,
  100% {
    transform: translateY(-2px);
  }
  50% {
    transform: translateY(2px);
  }
}

/* 文案区：第二列第一行 */
.update-notification-copy {
  grid-column: 2;
  grid-row: 1;
  min-width: 0;
  display: grid;
  gap: 2px;
  padding-right: 20px;
}

.update-notification-copy strong {
  min-width: 0;
  font-size: 12.5px;
  font-weight: 600;
  line-height: 18px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.update-notification-detail {
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.5;
}

/* 报错“展开详情”开关 */
.update-notification-error-toggle {
  justify-self: start;
  display: inline-flex;
  align-items: center;
  gap: 1px;
  margin-top: 2px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--tone);
  font-size: 11px;
  font-weight: 500;
  cursor: pointer;
}

.update-notification-error-toggle:hover {
  text-decoration: underline;
}

.update-notification-error-toggle .material-symbols-rounded {
  font-size: 14px;
}

/* 展开后的原始报错，等宽字体、可滚动、不撑破卡片 */
.update-notification-error-detail {
  margin: 4px 0 0;
  max-height: 88px;
  overflow: auto;
  padding: 7px 8px;
  border-radius: var(--radius-md);
  background: var(--bg-card-muted);
  color: var(--text-soft);
  font-family: var(--font-mono);
  font-size: 10.5px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}

/* 操作行：跨两列，靠右 */
.update-notification-action {
  grid-column: 1 / -1;
  grid-row: 2;
  justify-self: end;
  min-height: 26px;
  padding: 0 12px;
  border: 0;
  border-radius: var(--radius-md);
  background: var(--tone);
  color: var(--accent-contrast);
  font-size: 11px;
  font-weight: 500;
  cursor: pointer;
  transition: opacity 140ms ease;
}

.update-notification-action:hover:not(:disabled) {
  opacity: 0.86;
}

.update-notification-action:disabled {
  cursor: default;
  opacity: 0.5;
}

.update-notification-close {
  position: absolute;
  top: 9px;
  right: 9px;
  width: 18px;
  height: 18px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
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
  font-size: 14px;
}

/* 进度：跨两列 */
.update-notification-progress {
  grid-column: 1 / -1;
  grid-row: 2;
  min-height: 26px;
  display: flex;
  align-items: center;
  gap: 9px;
}

.update-notification-progress-track {
  height: 4px;
  flex: 1 1 auto;
  overflow: hidden;
  border-radius: var(--radius-full);
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
  min-width: 28px;
  color: var(--text-muted);
  font-size: 10.5px;
  font-weight: 500;
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
  transform: translateY(8px);
}

@media (max-width: 560px) {
  .update-notification {
    left: 12px;
    width: calc(100vw - 24px);
  }
}
</style>
