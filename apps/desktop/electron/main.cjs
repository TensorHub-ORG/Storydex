const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");
const { spawn } = require("child_process");
let resolveUpdateFeedUrl = (desktopPackage, overrideUrl) =>
  String(overrideUrl || desktopPackage?.storydexUpdateFeedUrl || desktopPackage?.build?.extraMetadata?.storydexUpdateFeedUrl || "").trim();
try {
  ({ resolveUpdateFeedUrl } = require("./update-feed.cjs"));
} catch (error) {
  console.warn("[Storydex Desktop] update feed helper is temporarily unavailable:", error.message || String(error));
}

function readDesktopPackageMetadata() {
  try {
    return JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
  } catch {
    return {};
  }
}

const DESKTOP_PACKAGE = readDesktopPackageMetadata();
const DESKTOP_APP_ID = String(DESKTOP_PACKAGE.build?.appId || "cn.tensorhub.storydex").trim();
const DESKTOP_PRODUCT_NAME = String(DESKTOP_PACKAGE.build?.productName || DESKTOP_PACKAGE.productName || "Storydex").trim();
const FRONTEND_DEV_URL = process.env.STORYDEX_DESKTOP_URL || "http://127.0.0.1:5173";
const BACKEND_HOST = process.env.STORYDEX_BACKEND_HOST || "127.0.0.1";
const BACKEND_PORT = Number(process.env.STORYDEX_BACKEND_PORT || 18081);
const BACKEND_HEALTH_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}/api/v1/sys/health`;
const DESKTOP_TITLEBAR_HEIGHT = 42;
const DEFAULT_TITLEBAR_THEME = {
  color: "#f5f7fb",
  symbolColor: "#162030",
  height: DESKTOP_TITLEBAR_HEIGHT
};
let lastAppliedTitlebarTheme = { ...DEFAULT_TITLEBAR_THEME };

let mainWindow = null;
let backendProcess = null;
let quitting = false;
let pendingOpenTarget = null;
let nextOpenTargetId = 0;
let previewWindow = null;
const backendProcessLogs = new WeakMap();

const PYTHON_PREFLIGHT_CODE = [
  "import sys",
  "modules = ('fastapi', 'uvicorn', 'anthropic', 'pydantic_settings', 'dotenv')",
  "for name in modules: __import__(name)",
  "import main",
  "print('storydex-preflight-ok')",
  "print(sys.executable)",
  "print(sys.prefix)"
].join("\n");

// Defer app.setAppUserModelId until app is ready to avoid undefined app errors
function initializeAppMetadata() {
  try {
    if (app && typeof app.setAppUserModelId === 'function') {
      app.setAppUserModelId(DESKTOP_APP_ID);
    }
  } catch (e) {
    console.warn('[Storydex Desktop] Could not set app user model id:', e.message);
  }
}

function stripWrappingQuotes(value) {
  const normalized = String(value || "").trim();
  if (normalized.length >= 2) {
    const first = normalized[0];
    const last = normalized[normalized.length - 1];
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
      return normalized.slice(1, -1).trim();
    }
  }
  return normalized;
}

function resolvePackagedContentRoot() {
  const packagedRoot = path.join(process.resourcesPath, "app");
  const nestedAppRoot = path.join(packagedRoot, "app");
  return fs.existsSync(nestedAppRoot) ? nestedAppRoot : packagedRoot;
}

function pathEquals(left, right) {
  return String(left || "").trim().toLowerCase() === String(right || "").trim().toLowerCase();
}

function pathStartsWithPath(candidatePath, rootPath) {
  const normalizedCandidate = path.resolve(String(candidatePath || "")).toLowerCase();
  const normalizedRoot = path.resolve(String(rootPath || "")).toLowerCase();
  return normalizedCandidate === normalizedRoot || normalizedCandidate.startsWith(`${normalizedRoot}${path.sep}`);
}

function isInternalAppPath(candidatePath) {
  const normalized = path.resolve(candidatePath);
  if (pathEquals(normalized, process.execPath)) {
    return true;
  }

  const appPath = String(app.getAppPath?.() || "").trim();
  if (appPath && pathEquals(normalized, appPath)) {
    return true;
  }

  if (pathStartsWithPath(normalized, __dirname)) {
    return true;
  }

  if (app.isPackaged) {
    if (pathStartsWithPath(normalized, resolvePackagedContentRoot())) {
      return true;
    }
    if (pathStartsWithPath(normalized, process.resourcesPath)) {
      return true;
    }
  }

  return false;
}

function buildOpenTargetFromPath(candidatePath) {
  const normalizedInput = stripWrappingQuotes(candidatePath);
  if (!normalizedInput || normalizedInput === "." || normalizedInput.startsWith("--")) {
    return null;
  }

  let absolutePath = normalizedInput;
  if (!path.isAbsolute(absolutePath)) {
    absolutePath = path.resolve(absolutePath);
  }

  if (!app.isPackaged && pathEquals(absolutePath, process.cwd())) {
    return null;
  }

  if (!fs.existsSync(absolutePath) || isInternalAppPath(absolutePath)) {
    return null;
  }

  let stats = null;
  try {
    stats = fs.statSync(absolutePath);
  } catch {
    return null;
  }

  return {
    id: ++nextOpenTargetId,
    path: path.resolve(absolutePath),
    isFile: stats.isFile()
  };
}

function extractOpenTargetFromArgv(argv = []) {
  const candidates = Array.isArray(argv) ? argv.slice(1) : [];
  for (let index = candidates.length - 1; index >= 0; index -= 1) {
    const target = buildOpenTargetFromPath(candidates[index]);
    if (target) {
      return target;
    }
  }
  return null;
}

function focusWindow(windowRef) {
  if (!windowRef || windowRef.isDestroyed()) {
    return;
  }
  if (windowRef.isMinimized()) {
    windowRef.restore();
  }
  windowRef.focus();
}

function focusMainWindow() {
  focusWindow(mainWindow);
}

function queueOpenTarget(target) {
  if (!target || !String(target.path || "").trim()) {
    return;
  }

  pendingOpenTarget = target;
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send("storydex:open-target", target);
    focusMainWindow();
  }
}

function getPendingOpenTarget() {
  return pendingOpenTarget;
}

function acknowledgeOpenTarget(targetId) {
  if (!pendingOpenTarget) {
    return false;
  }
  if (Number(targetId) !== Number(pendingOpenTarget.id)) {
    return false;
  }
  pendingOpenTarget = null;
  return true;
}

function normalizeRelativePath(value) {
  return String(value || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "").trim();
}

function fileNameFromRelativePath(relativePath) {
  const normalized = normalizeRelativePath(relativePath);
  if (!normalized) {
    return "File Preview";
  }
  const parts = normalized.split("/");
  return parts[parts.length - 1] || normalized;
}

const singleInstanceLock = app.requestSingleInstanceLock();
if (!singleInstanceLock) {
  app.quit();
} else {
  const initialOpenTarget = extractOpenTargetFromArgv(process.argv);
  if (initialOpenTarget) {
    pendingOpenTarget = initialOpenTarget;
  }

  app.on("second-instance", (_event, argv) => {
    const target = extractOpenTargetFromArgv(argv);
    if (target) {
      queueOpenTarget(target);
      return;
    }
    focusMainWindow();
  });

  app.on("open-file", (event, openPath) => {
    event.preventDefault();
    const target = buildOpenTargetFromPath(openPath);
    if (target) {
      queueOpenTarget(target);
      return;
    }
    focusMainWindow();
  });
}

function resolveDesktopIconPath() {
  const devIconPath = path.resolve(
    __dirname,
    "..",
    "..",
    "..",
    "assets",
    "Storydex_icon",
    "storydex_icon_01.png"
  );
  const packagedIconPath = path.join(resolvePackagedContentRoot(), "assets", "Storydex_icon", "storydex_icon_01.png");
  const iconPath = app.isPackaged ? packagedIconPath : devIconPath;
  return fs.existsSync(iconPath) ? iconPath : undefined;
}

function resolveBackendDirectory() {
  if (app.isPackaged) {
    return path.join(resolvePackagedContentRoot(), "backend");
  }
  return path.resolve(__dirname, "..", "..", "backend");
}

function resolveHelpGuideRoot() {
  if (app.isPackaged) {
    return path.join(resolvePackagedContentRoot(), "docs", "使用指南");
  }
  return path.resolve(__dirname, "..", "..", "..", "docs", "使用指南");
}

function resolveMinGitDirectory() {
  if (app.isPackaged) {
    return path.join(resolvePackagedContentRoot(), "mingit");
  }
  return path.resolve(__dirname, "..", "vendor", "mingit");
}

function resolveMinGitExecutable() {
  const mingitRoot = resolveMinGitDirectory();
  const candidates =
    process.platform === "win32"
      ? [
          path.join(mingitRoot, "cmd", "git.exe"),
          path.join(mingitRoot, "bin", "git.exe"),
          path.join(mingitRoot, "mingw64", "bin", "git.exe")
        ]
      : [path.join(mingitRoot, "bin", "git")];
  return candidates.find((candidate) => fs.existsSync(candidate)) || "";
}

function buildMinGitPathEntries() {
  const mingitRoot = resolveMinGitDirectory();
  if (!fs.existsSync(mingitRoot)) {
    return [];
  }
  if (process.platform === "win32") {
    return [
      path.join(mingitRoot, "cmd"),
      path.join(mingitRoot, "bin"),
      path.join(mingitRoot, "mingw64", "bin"),
      path.join(mingitRoot, "usr", "bin")
    ];
  }
  return [path.join(mingitRoot, "bin")];
}

function resolveRendererEntry() {
  if (app.isPackaged) {
    return {
      kind: "file",
      value: path.join(resolvePackagedContentRoot(), "frontend-dist", "index.html")
    };
  }
  return {
    kind: "url",
    value: FRONTEND_DEV_URL
  };
}

function appendProcessLog(chunks, chunk) {
  const text = Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk || "");
  if (!text) {
    return;
  }

  chunks.push(text);
  let totalLength = chunks.reduce((sum, item) => sum + item.length, 0);
  while (totalLength > 12000 && chunks.length > 1) {
    totalLength -= chunks.shift().length;
  }
}

function readProcessLogTail(processRef) {
  const state = backendProcessLogs.get(processRef);
  if (!state) {
    return "";
  }

  return [state.stdout.join("").trim(), state.stderr.join("").trim()]
    .filter((item) => !!item)
    .join("\n")
    .slice(-4000);
}

function buildPythonPathEntries(pythonRoot) {
  if (!pythonRoot) {
    return [];
  }

  if (process.platform === "win32") {
    return [
      pythonRoot,
      path.join(pythonRoot, "Scripts"),
      path.join(pythonRoot, "Library", "bin"),
      path.join(pythonRoot, "Library", "usr", "bin"),
      path.join(pythonRoot, "DLLs")
    ];
  }

  return [path.join(pythonRoot, "bin")];
}

function normalizePythonRootFromCommand(command) {
  if (!command || !path.isAbsolute(command)) {
    return "";
  }

  const executableDirectory = path.dirname(command);
  if (process.platform === "win32" && path.basename(executableDirectory).toLowerCase() === "scripts") {
    return path.dirname(executableDirectory);
  }
  if (path.basename(executableDirectory).toLowerCase() === "bin") {
    return path.dirname(executableDirectory);
  }
  return executableDirectory;
}

function shouldSetPythonHome(pythonRoot) {
  if (!pythonRoot) {
    return false;
  }

  // venv/virtualenv already knows its base interpreter through pyvenv.cfg.
  // Forcing PYTHONHOME on a venv breaks stdlib resolution on Windows.
  if (fs.existsSync(path.join(pythonRoot, "pyvenv.cfg"))) {
    return false;
  }

  return true;
}

function createPythonCandidate({ command, label, pythonRoot = "", args = [] }) {
  return {
    command,
    args,
    label,
    pathEntries: buildPythonPathEntries(pythonRoot),
    pythonHome: shouldSetPythonHome(pythonRoot) ? pythonRoot : ""
  };
}

function resolveDesktopRuntimeEnvironment() {
  const userDataRoot = app.getPath("userData");
  const logsDir = path.join(userDataRoot, "logs");
  const workspacesRoot = path.join(userDataRoot, "workspaces");
  const workspaceRoot = path.join(workspacesRoot, "default");
  const globalRoot = path.join(app.getPath("home"), ".storydex");

  for (const target of [logsDir, workspacesRoot, workspaceRoot, globalRoot]) {
    fs.mkdirSync(target, { recursive: true });
  }

  return {
    userDataRoot,
    logsDir,
    workspaceRoot,
    globalRoot
  };
}

function resolvePythonCandidates() {
  const configured = String(process.env.STORYDEX_PYTHON || "").trim();
  const candidates = [];
  const seen = new Set();

  const pushCandidate = (candidate) => {
    const key = `${candidate.command} ${candidate.args.join(" ")}`;
    if (seen.has(key)) {
      return;
    }
    seen.add(key);
    candidates.push(candidate);
  };

  if (configured) {
    const configuredRoot = normalizePythonRootFromCommand(configured);
    pushCandidate(
      createPythonCandidate({
        command: configured,
        label: "configured STORYDEX_PYTHON",
        pythonRoot: configuredRoot
      })
    );
  }

  if (app.isPackaged) {
    const packagedPythonRoot = path.join(resolvePackagedContentRoot(), "python-env");
    const packagedPythonCommands =
      process.platform === "win32"
        ? [path.join(packagedPythonRoot, "python.exe"), path.join(packagedPythonRoot, "Scripts", "python.exe")]
        : [path.join(packagedPythonRoot, "bin", "python")];

    for (const packagedPythonCommand of packagedPythonCommands) {
      if (!fs.existsSync(packagedPythonCommand)) {
        continue;
      }
      pushCandidate(
        createPythonCandidate({
          command: packagedPythonCommand,
          label: "embedded python",
          pythonRoot: packagedPythonRoot
        })
      );
      break;
    }
  } else {
    const projectLocalPythonRoot = path.resolve(__dirname, "..", "..", "..", ".python39");
    const projectLocalPythonCommands =
      process.platform === "win32"
        ? [path.join(projectLocalPythonRoot, "python.exe"), path.join(projectLocalPythonRoot, "Scripts", "python.exe")]
        : [path.join(projectLocalPythonRoot, "bin", "python")];

    for (const projectLocalPythonCommand of projectLocalPythonCommands) {
      if (!fs.existsSync(projectLocalPythonCommand)) {
        continue;
      }
      pushCandidate(
        createPythonCandidate({
          command: projectLocalPythonCommand,
          label: "project-local python",
          pythonRoot: projectLocalPythonRoot
        })
      );
      break;
    }
  }

  const allowSystemFallback =
    !app.isPackaged || String(process.env.STORYDEX_ALLOW_SYSTEM_PYTHON_FALLBACK || "").trim() === "1";

  if (allowSystemFallback) {
    pushCandidate(createPythonCandidate({ command: "python", label: "python" }));
    if (process.platform === "win32") {
      pushCandidate(createPythonCandidate({ command: "py", args: ["-3"], label: "py -3" }));
    }
  }

  return candidates;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function isBackendHealthy() {
  try {
    const response = await fetch(BACKEND_HEALTH_URL, { method: "GET" });
    return response.ok;
  } catch {
    return false;
  }
}

async function waitUntilBackendHealthy(maxAttempts = 80, intervalMs = 500, processRef = null) {
  for (let index = 0; index < maxAttempts; index += 1) {
    if (processRef && (processRef.killed || processRef.exitCode !== null)) {
      return false;
    }
    if (await isBackendHealthy()) {
      if (!processRef) {
        return true;
      }
      await sleep(intervalMs);
      if (!processRef.killed && processRef.exitCode === null && (await isBackendHealthy())) {
        return true;
      }
      return false;
    }
    await sleep(intervalMs);
  }
  return false;
}

function stopBackendKernel(targetProcess = backendProcess) {
  if (!targetProcess || targetProcess.killed) {
    return;
  }

  const pid = targetProcess.pid;
  if (!pid) {
    return;
  }

  if (process.platform === "win32") {
    const killer = spawn("taskkill", ["/pid", String(pid), "/t", "/f"], { windowsHide: true });
    killer.on("error", () => {
      try {
        targetProcess.kill();
      } catch {
        // no-op
      }
    });
    return;
  }

  try {
    targetProcess.kill("SIGTERM");
  } catch {
    // no-op
  }
}

async function pickDirectory(_event, options = {}) {
  const ownerWindow = BrowserWindow.getFocusedWindow() || mainWindow || undefined;
  const result = await dialog.showOpenDialog(ownerWindow, {
    title: typeof options.title === "string" && options.title.trim() ? options.title.trim() : "Select folder",
    defaultPath:
      typeof options.defaultPath === "string" && options.defaultPath.trim()
        ? options.defaultPath.trim()
        : undefined,
    properties: ["openDirectory", "dontAddToRecent"]
  });
  return result.canceled ? "" : result.filePaths[0] || "";
}

async function revealPath(_event, absolutePath) {
  const normalizedPath = String(absolutePath || "").trim();
  if (!normalizedPath || !fs.existsSync(normalizedPath)) {
    return false;
  }

  if (fs.statSync(normalizedPath).isDirectory()) {
    await shell.openPath(normalizedPath);
    return true;
  }

  shell.showItemInFolder(normalizedPath);
  return true;
}

async function openWithDialog(_event, absolutePath) {
  const normalizedPath = String(absolutePath || "").trim();
  if (!normalizedPath || !fs.existsSync(normalizedPath)) {
    return false;
  }

  if (process.platform === "win32") {
    const child = spawn("rundll32.exe", ["shell32.dll,OpenAs_RunDLL", normalizedPath], {
      detached: true,
      stdio: "ignore",
      windowsHide: true
    });
    child.unref();
    return true;
  }

  await shell.openPath(normalizedPath);
  return true;
}

// ---- 差分更新（electron-updater + NSIS blockmap）----
// NSIS 安装包发布时会生成 *.exe.blockmap；electron-updater 更新时对比新旧 blockmap，
// 只下载有变化的数据块，实现增量（差分）更新。
let updaterModule = null;
let downloadedInstallerPath = "";
let updaterConfigured = false;
let updaterRetryTimer = null;
let updaterRetryIndex = 0;
const UPDATER_RETRY_DELAYS_MS = [1_000, 3_000, 7_000];
let updaterState = {
  supported: false,
  status: "idle",
  currentVersion: "",
  availableVersion: "",
  releaseNotes: "",
  progress: null,
  error: "",
  feedUrl: ""
};

function resolveAutoUpdater() {
  if (updaterModule) {
    return updaterModule.autoUpdater;
  }
  try {
    updaterModule = require("electron-updater");
    return updaterModule.autoUpdater;
  } catch (error) {
    console.warn("[Storydex Desktop] electron-updater unavailable:", error.message || String(error));
    return null;
  }
}

function updaterRuntimeRoot() {
  const base = String(process.env.LOCALAPPDATA || app.getPath("temp") || "").trim();
  return path.join(base, "storydex-updater");
}

function updaterInstallLockPath() {
  return path.join(updaterRuntimeRoot(), "installing.json");
}

function readUpdaterInstallLock() {
  try {
    const lockPath = updaterInstallLockPath();
    if (!fs.existsSync(lockPath)) return null;
    const payload = JSON.parse(fs.readFileSync(lockPath, "utf8"));
    const updatedAt = Date.parse(String(payload?.updatedAt || ""));
    if (Number.isFinite(updatedAt) && Date.now() - updatedAt > 30 * 60 * 1000) {
      fs.rmSync(lockPath, { force: true });
      return null;
    }
    return payload && typeof payload === "object" ? payload : null;
  } catch {
    return null;
  }
}

async function showUpdateInstallInProgress() {
  await dialog.showMessageBox({
    type: "info",
    title: "Storydex 正在安装更新",
    message: "Storydex 正在安装更新",
    detail: "应用文件正在安全替换。安装完成前请勿重复启动，完成后安装窗口会询问是否启动 Storydex。",
    buttons: ["知道了"],
    defaultId: 0
  });
  app.quit();
}

function setUpdaterState(patch) {
  updaterState = { ...updaterState, ...patch };
  for (const windowRef of BrowserWindow.getAllWindows()) {
    if (!windowRef.isDestroyed()) {
      windowRef.webContents.send("storydex:updater-state", { ...updaterState });
    }
  }
}

function scheduleAutoUpdaterRetry() {
  if (updaterRetryTimer || quitting) {
    return;
  }
  if (updaterRetryIndex >= UPDATER_RETRY_DELAYS_MS.length) {
    setUpdaterState({
      supported: false,
      status: "unsupported",
      error: "自动更新组件加载失败，请完全退出 Storydex 后重新打开。"
    });
    return;
  }

  const delay = UPDATER_RETRY_DELAYS_MS[updaterRetryIndex];
  updaterRetryIndex += 1;
  setUpdaterState({ supported: false, status: "initializing", error: "" });
  updaterRetryTimer = setTimeout(() => {
    updaterRetryTimer = null;
    initializeAutoUpdater();
  }, delay);
  updaterRetryTimer.unref?.();
}

function initializeAutoUpdater() {
  updaterState.currentVersion = app.getVersion();
  if (!app.isPackaged) {
    setUpdaterState({ supported: false, status: "unsupported", error: "开发模式不支持自动更新，请使用打包后的桌面版。" });
    return;
  }
  const autoUpdater = resolveAutoUpdater();
  if (!autoUpdater) {
    scheduleAutoUpdaterRetry();
    return;
  }

  if (updaterRetryTimer) {
    clearTimeout(updaterRetryTimer);
    updaterRetryTimer = null;
  }
  updaterRetryIndex = 0;

  if (updaterConfigured) {
    setUpdaterState({ supported: true, status: "idle", error: "" });
    return;
  }

  autoUpdater.autoDownload = false;
  // The persistent helper owns installation after the main process exits.
  autoUpdater.autoInstallOnAppQuit = false;
  autoUpdater.disableDifferentialDownload = false;

  const configuredFeed = resolveUpdateFeedUrl(DESKTOP_PACKAGE, process.env.STORYDEX_UPDATE_URL);
  if (configuredFeed) {
    autoUpdater.setFeedURL({ provider: "generic", url: configuredFeed });
  } else {
    setUpdaterState({ supported: false, status: "unsupported", error: "缺少桌面版更新源配置，无法自动更新。" });
    return;
  }

  autoUpdater.on("checking-for-update", () => {
    setUpdaterState({ status: "checking", error: "" });
  });
  autoUpdater.on("update-available", (info) => {
    setUpdaterState({
      status: "available",
      availableVersion: String(info?.version || ""),
      releaseNotes: typeof info?.releaseNotes === "string" ? info.releaseNotes : "",
      error: ""
    });
  });
  autoUpdater.on("update-not-available", () => {
    setUpdaterState({ status: "not-available", availableVersion: "", progress: null, error: "" });
  });
  autoUpdater.on("download-progress", (progress) => {
    setUpdaterState({
      status: "downloading",
      progress: {
        percent: Number(progress?.percent || 0),
        transferred: Number(progress?.transferred || 0),
        total: Number(progress?.total || 0),
        bytesPerSecond: Number(progress?.bytesPerSecond || 0)
      },
      error: ""
    });
  });
  autoUpdater.on("update-downloaded", (info) => {
    downloadedInstallerPath = String(info?.downloadedFile || downloadedInstallerPath || "").trim();
    setUpdaterState({
      status: "downloaded",
      availableVersion: String(info?.version || updaterState.availableVersion),
      progress: null,
      error: ""
    });
  });
  autoUpdater.on("error", (error) => {
    setUpdaterState({ status: "error", error: error?.message || String(error) });
  });

  updaterConfigured = true;
  setUpdaterState({ supported: true, status: "idle", feedUrl: configuredFeed, error: "" });
}

async function checkForDesktopUpdates() {
  const autoUpdater = updaterState.supported ? resolveAutoUpdater() : null;
  if (autoUpdater) {
    try {
      await autoUpdater.checkForUpdates();
    } catch (error) {
      setUpdaterState({ status: "error", error: error?.message || String(error) });
    }
  }
  return { ...updaterState };
}

async function downloadDesktopUpdate() {
  const autoUpdater = updaterState.supported ? resolveAutoUpdater() : null;
  if (autoUpdater) {
    try {
      setUpdaterState({ status: "downloading", error: "" });
      const downloadedFiles = await autoUpdater.downloadUpdate();
      const installer = Array.isArray(downloadedFiles)
        ? downloadedFiles.find((candidate) => String(candidate || "").toLowerCase().endsWith(".exe")) || downloadedFiles[0]
        : "";
      downloadedInstallerPath = String(installer || downloadedInstallerPath || "").trim();
    } catch (error) {
      setUpdaterState({ status: "error", error: error?.message || String(error) });
    }
  }
  return { ...updaterState };
}

function installDesktopUpdate() {
  const autoUpdater = updaterState.supported ? resolveAutoUpdater() : null;
  if (!autoUpdater || updaterState.status !== "downloaded") {
    return false;
  }
  if (process.platform !== "win32" || !downloadedInstallerPath || !fs.existsSync(downloadedInstallerPath)) {
    setUpdaterState({ status: "error", error: "未找到已下载的安装程序，请重新下载更新。" });
    return false;
  }
  try {
    const runtimeRoot = updaterRuntimeRoot();
    fs.mkdirSync(runtimeRoot, { recursive: true });
    const lockPath = updaterInstallLockPath();
    const logPath = path.join(runtimeRoot, "install.log");
    fs.writeFileSync(lockPath, JSON.stringify({ state: "preparing", updatedAt: new Date().toISOString() }), "utf8");
    const helperScript = path.join(__dirname, "update-helper.ps1");
    const helper = spawn(
      "powershell.exe",
      [
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", helperScript,
        "-InstallerPath", downloadedInstallerPath,
        "-AppPath", process.execPath,
        "-LockPath", lockPath,
        "-ParentPid", String(process.pid),
        "-LogPath", logPath
      ],
      { detached: true, stdio: "ignore", windowsHide: false }
    );
    helper.unref();
    quitting = true;
    stopBackendKernel();
    setTimeout(() => app.quit(), 150).unref?.();
    return true;
  } catch (error) {
    setUpdaterState({ status: "error", error: error?.message || String(error) });
    return false;
  }
}

function registerDesktopIpc() {
  ipcMain.handle("storydex:pick-directory", pickDirectory);
  ipcMain.handle("storydex:reveal-path", revealPath);
  ipcMain.handle("storydex:open-with-dialog", openWithDialog);
  ipcMain.handle("storydex:open-preview-window", (_event, relativePath) => openPreviewWindow(relativePath));
  ipcMain.handle("storydex:set-titlebar-theme", setTitlebarTheme);
  ipcMain.handle("storydex:get-pending-open-target", () => getPendingOpenTarget());
  ipcMain.handle("storydex:ack-open-target", (_event, targetId) => acknowledgeOpenTarget(targetId));
  ipcMain.handle("storydex:updater-get-state", () => ({ ...updaterState }));
  ipcMain.handle("storydex:updater-check", () => checkForDesktopUpdates());
  ipcMain.handle("storydex:updater-download", () => downloadDesktopUpdate());
  ipcMain.handle("storydex:updater-install", () => installDesktopUpdate());
}

function normalizeTitlebarColor(value, fallback) {
  const normalized = String(value || "").trim();
  if (/^#[0-9a-fA-F]{6}$/.test(normalized) || /^#[0-9a-fA-F]{8}$/.test(normalized)) {
    return normalized;
  }
  return fallback;
}

function canUseTitlebarOverlay(windowRef = mainWindow) {
  return (
    process.platform === "win32" &&
    !!windowRef &&
    typeof windowRef.setTitleBarOverlay === "function"
  );
}

async function setTitlebarTheme(_event, payload = {}) {
  const eventWindow = _event?.sender ? BrowserWindow.fromWebContents(_event.sender) : null;
  const targetWindow = eventWindow && !eventWindow.isDestroyed() ? eventWindow : mainWindow;
  if (!canUseTitlebarOverlay(targetWindow)) {
    return { applied: false };
  }

  const nextTheme = {
    color: normalizeTitlebarColor(payload.color, DEFAULT_TITLEBAR_THEME.color),
    symbolColor: normalizeTitlebarColor(payload.symbolColor, DEFAULT_TITLEBAR_THEME.symbolColor),
    height: DESKTOP_TITLEBAR_HEIGHT
  };

  targetWindow.setTitleBarOverlay(nextTheme);
  lastAppliedTitlebarTheme = { ...nextTheme };
  return { applied: true, ...nextTheme };
}

function buildBackendEnvironment(candidate, runtimeEnvironment) {
  const gitExecutable = resolveMinGitExecutable();
  const mingitRoot = resolveMinGitDirectory();
  const helpGuideRoot = resolveHelpGuideRoot();
  const pathEntries = [...buildMinGitPathEntries(), ...(candidate.pathEntries || [])].filter((entry) => !!entry);
  const currentPath = String(process.env.PATH || "");
  const nextPath = pathEntries.length
    ? `${pathEntries.join(path.delimiter)}${path.delimiter}${currentPath}`
    : currentPath;
  const nextEnv = {
    ...process.env,
    PATH: nextPath,
    PYTHONIOENCODING: "utf-8",
    PYTHONNOUSERSITE: "1",
    STORYDEX_WORKSPACE_ROOT: runtimeEnvironment.workspaceRoot,
    STORYDEX_GLOBAL_ROOT: runtimeEnvironment.globalRoot,
    STORYDEX_HELP_GUIDE_ROOT: helpGuideRoot,
    STORYDEX_MINGIT_ROOT: mingitRoot,
    STORYDEX_GIT_EXECUTABLE: gitExecutable,
    PYTHONUTF8: "1"
  };

  delete nextEnv.PYTHONHOME;

  if (candidate.pythonHome) {
    nextEnv.PYTHONHOME = candidate.pythonHome;
  }

  return nextEnv;
}

async function runPythonPreflight(candidate, backendDirectory, runtimeEnvironment) {
  return new Promise((resolve) => {
    let settled = false;
    const stdout = [];
    const stderr = [];
    const child = spawn(candidate.command, [...candidate.args, "-c", PYTHON_PREFLIGHT_CODE], {
      cwd: backendDirectory,
      env: buildBackendEnvironment(candidate, runtimeEnvironment),
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true
    });

    const finish = (result) => {
      if (settled) {
        return;
      }
      settled = true;
      resolve(result);
    };

    child.stdout.on("data", (chunk) => {
      appendProcessLog(stdout, chunk);
    });

    child.stderr.on("data", (chunk) => {
      appendProcessLog(stderr, chunk);
    });

    child.once("error", (error) => {
      finish({
        ok: false,
        detail: `${error.name || "Error"}: ${error.message || String(error)}`
      });
    });

    child.once("exit", (code) => {
      const stdoutText = stdout.join("").trim();
      const stderrText = stderr.join("").trim();
      if (code === 0 && stdoutText.includes("storydex-preflight-ok")) {
        finish({ ok: true, detail: stdoutText });
        return;
      }

      finish({
        ok: false,
        detail: [`exit=${code}`, stderrText || stdoutText || "preflight produced no output"]
          .filter((item) => !!item)
          .join("\n")
      });
    });
  });
}

async function trySpawnBackendCandidate(candidate, backendDirectory, uvicornArgs, runtimeEnvironment) {
  return new Promise((resolve) => {
    let settled = false;
    const child = spawn(candidate.command, [...candidate.args, ...uvicornArgs], {
      cwd: backendDirectory,
      env: buildBackendEnvironment(candidate, runtimeEnvironment),
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true
    });

    const handleSpawn = () => {
      if (settled) {
        return;
      }
      settled = true;
      child.off("error", handleError);
      resolve({ process: child, error: null });
    };

    const handleError = (error) => {
      if (settled) {
        return;
      }
      settled = true;
      child.off("spawn", handleSpawn);
      resolve({ process: null, error });
    };

    child.once("spawn", handleSpawn);
    child.once("error", handleError);
  });
}

function attachBackendProcessLogging(processRef, candidateLabel) {
  const logState = {
    stdout: [],
    stderr: []
  };
  backendProcessLogs.set(processRef, logState);

  processRef.stdout.on("data", (chunk) => {
    appendProcessLog(logState.stdout, chunk);
    process.stdout.write(`[Backend:${candidateLabel}] ${chunk}`);
  });

  processRef.stderr.on("data", (chunk) => {
    appendProcessLog(logState.stderr, chunk);
    process.stderr.write(`[Backend:${candidateLabel}] ${chunk}`);
  });

  processRef.on("error", async (error) => {
    if (processRef !== backendProcess || quitting) {
      return;
    }
    backendProcess = null;
    await dialog.showMessageBox({
      type: "error",
      title: "Storydex Desktop",
      message: [
        "Backend process failed to start.",
        `Launch command: ${candidateLabel}`,
        `Error: ${error.message || String(error)}`
      ].join("\n")
    });
  });

  processRef.on("exit", (code, signal) => {
    const message = quitting
      ? "Backend process stopped while quitting."
      : `Backend process exited unexpectedly. code=${code} signal=${signal}`;
    console.log(`[Storydex Desktop] ${message}`);
    if (processRef === backendProcess) {
      backendProcess = null;
    }
  });
}

async function showBackendFailureMessage({ backendDirectory, candidates, failures, runtimeEnvironment }) {
  const configuredPython = String(process.env.STORYDEX_PYTHON || "").trim();
  const pythonTip = configuredPython || candidates.map((item) => item.label).join(" / ");
  const detail = failures.length
    ? failures
        .map(
          (item, index) =>
            `[${index + 1}] ${item.label} (${item.phase})\n${String(item.detail || "").trim() || "No details."}`
        )
        .join("\n\n")
        .slice(-7000)
    : "No detailed failure logs were captured.";
  await dialog.showMessageBox({
    type: "error",
    title: "Storydex Desktop",
    message: [
      "Backend did not become ready.",
      app.isPackaged
        ? "Packaged builds require the bundled Python runtime. System Python fallback is disabled unless STORYDEX_ALLOW_SYSTEM_PYTHON_FALLBACK=1."
        : "Check the project-local Python 3.9 environment or STORYDEX_PYTHON.",
      `Python candidates: ${pythonTip}`,
      `Backend directory: ${backendDirectory}`,
      `Workspace root: ${runtimeEnvironment.workspaceRoot}`,
      `Global config root: ${runtimeEnvironment.globalRoot}`
    ].join("\n"),
    detail
  });
}

async function killExternalBackendOnPort() {
  if (process.platform !== "win32") {
    return;
  }
  return new Promise((resolve) => {
    const finder = spawn("cmd", ["/c", `netstat -ano | findstr :${BACKEND_PORT}`], {
      windowsHide: true,
      stdio: ["ignore", "pipe", "ignore"]
    });
    let buffer = "";
    finder.stdout.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
    });
    finder.on("close", () => {
      const pids = new Set();
      for (const line of buffer.split(/\r?\n/)) {
        if (!/LISTENING/i.test(line)) {
          continue;
        }
        const parts = line.trim().split(/\s+/);
        const pid = parts[parts.length - 1];
        if (pid && /^\d+$/.test(pid) && Number(pid) !== process.pid) {
          pids.add(pid);
        }
      }
      if (pids.size === 0) {
        resolve();
        return;
      }
      console.log(`[Storydex Desktop] Killing stale backend(s) on :${BACKEND_PORT}: ${Array.from(pids).join(", ")}`);
      const killers = Array.from(pids).map(
        (pid) =>
          new Promise((resolveKill) => {
            const killer = spawn("taskkill", ["/F", "/T", "/PID", pid], { windowsHide: true });
            killer.on("close", () => resolveKill());
            killer.on("error", () => resolveKill());
          })
      );
      Promise.all(killers).then(() => {
        setTimeout(resolve, 500);
      });
    });
    finder.on("error", () => resolve());
  });
}

async function startBackendKernel() {
  if (!app.isPackaged) {
    await killExternalBackendOnPort();
  } else if (await isBackendHealthy()) {
    console.log("[Storydex Desktop] Backend already active.");
    return true;
  }

  const backendDirectory = resolveBackendDirectory();
  const runtimeEnvironment = resolveDesktopRuntimeEnvironment();
  const uvicornArgs = [
    "-m",
    "uvicorn",
    "main:app",
    "--host",
    BACKEND_HOST,
    "--port",
    String(BACKEND_PORT)
  ];
  if (!app.isPackaged) {
    uvicornArgs.push("--reload", "--reload-dir", backendDirectory);
  }
  const candidates = resolvePythonCandidates();
  const failures = [];

  for (const candidate of candidates) {
    console.log(`[Storydex Desktop] Starting backend via ${candidate.label} ...`);
    console.log(`[Storydex Desktop] Backend directory: ${backendDirectory}`);
    console.log(`[Storydex Desktop] Workspace root: ${runtimeEnvironment.workspaceRoot}`);

    const preflight = await runPythonPreflight(candidate, backendDirectory, runtimeEnvironment);
    if (!preflight.ok) {
      failures.push({ label: candidate.label, phase: "preflight", detail: preflight.detail });
      continue;
    }

    const attempt = await trySpawnBackendCandidate(candidate, backendDirectory, uvicornArgs, runtimeEnvironment);
    if (!attempt.process) {
      failures.push({
        label: candidate.label,
        phase: "spawn",
        detail: attempt.error ? `${attempt.error.name || "Error"}: ${attempt.error.message || String(attempt.error)}` : "Unknown spawn failure."
      });
      continue;
    }

    backendProcess = attempt.process;
    attachBackendProcessLogging(backendProcess, candidate.label);

    const ready = await waitUntilBackendHealthy(40, 500, backendProcess);
    if (ready) {
      return true;
    }

    failures.push({
      label: candidate.label,
      phase: "health",
      detail: readProcessLogTail(backendProcess) || `Backend started with ${candidate.label} but never became healthy.`
    });
    stopBackendKernel(backendProcess);
    backendProcess = null;
    await sleep(250);
  }

  await showBackendFailureMessage({ backendDirectory, candidates, failures, runtimeEnvironment });
  return false;
}

function buildFileRouteHash(routePath = "/", query = {}) {
  const normalizedRoute = String(routePath || "/").startsWith("/") ? String(routePath || "/") : `/${String(routePath || "")}`;
  const searchParams = new URLSearchParams();
  Object.entries(query || {}).forEach(([key, value]) => {
    const normalizedValue = String(value || "").trim();
    if (normalizedValue) {
      searchParams.set(key, normalizedValue);
    }
  });
  const queryString = searchParams.toString();
  return queryString ? `${normalizedRoute}?${queryString}` : normalizedRoute;
}

function buildRendererUrl(routePath = "/", query = {}) {
  const url = new URL(FRONTEND_DEV_URL);
  url.pathname = String(routePath || "/");
  url.search = "";
  Object.entries(query || {}).forEach(([key, value]) => {
    const normalizedValue = String(value || "").trim();
    if (normalizedValue) {
      url.searchParams.set(key, normalizedValue);
    }
  });
  return url.toString();
}

async function loadRenderer(windowRef, routePath = "/", query = {}) {
  const entry = resolveRendererEntry();

  if (entry.kind === "file") {
    const hash = buildFileRouteHash(routePath, query);
    const rendererUrl = pathToFileURL(entry.value);
    if (hash !== "/") rendererUrl.hash = hash;
    for (let index = 0; index < 20; index += 1) {
      try {
        await windowRef.loadURL(rendererUrl.toString());
        return;
      } catch (error) {
        if (windowRef.isDestroyed() || index === 19) throw error;
        await sleep(250);
      }
    }
  }

  for (let index = 0; index < 80; index += 1) {
    try {
      await windowRef.loadURL(buildRendererUrl(routePath, query));
      return;
    } catch {
      await sleep(500);
    }
  }

  await dialog.showMessageBox({
    type: "error",
    title: "Storydex Desktop",
    message: "Frontend dev server is not ready. Please start apps/frontend first."
  });
}

function openExternalUrl(url) {
  const value = String(url || "").trim();
  if (/^https?:\/\//i.test(value)) {
    shell.openExternal(value);
  }
}

function attachWindowOpenHandler(windowRef) {
  const { webContents } = windowRef;

  webContents.setWindowOpenHandler(({ url }) => {
    openExternalUrl(url);
    return { action: "deny" };
  });

  // SPA 使用客户端路由（pushState / hash），正常情况下不会触发整页导航。
  // 因此任何 will-navigate 都视为“点击了链接想离开应用”，一律阻止：
  //   - 真正的外部 http/https 链接 → 交给系统浏览器打开；
  //   - 相对链接（如 001.md）被解析到应用自身来源或 file://，直接留在原界面，
  //     避免整页跳走导致白屏、跳到奇怪地址且无法返回。
  webContents.on("will-navigate", (event, targetUrl) => {
    event.preventDefault();

    const value = String(targetUrl || "").trim();
    if (!/^https?:\/\//i.test(value)) {
      return;
    }

    let sameOrigin = false;
    try {
      sameOrigin = new URL(value).origin === new URL(webContents.getURL()).origin;
    } catch {
      sameOrigin = false;
    }
    if (sameOrigin) {
      return;
    }

    shell.openExternal(value);
  });
}

function dispatchPreviewOpenFile(relativePath) {
  const normalizedRelativePath = normalizeRelativePath(relativePath);
  if (!normalizedRelativePath || !previewWindow || previewWindow.isDestroyed()) {
    return;
  }

  const payload = { relativePath: normalizedRelativePath };
  const deliver = () => {
    if (!previewWindow || previewWindow.isDestroyed()) {
      return;
    }
    previewWindow.webContents.send("storydex:preview-open-file", payload);
  };

  if (previewWindow.webContents.isLoading()) {
    previewWindow.webContents.once("did-finish-load", deliver);
    return;
  }

  deliver();
}

async function openPreviewWindow(relativePath) {
  const normalizedRelativePath = normalizeRelativePath(relativePath);
  if (!normalizedRelativePath) {
    return false;
  }

  if (previewWindow && !previewWindow.isDestroyed()) {
    dispatchPreviewOpenFile(normalizedRelativePath);
    focusWindow(previewWindow);
    return true;
  }

  const windowIcon = resolveDesktopIconPath();
  previewWindow = new BrowserWindow({
    title: `${fileNameFromRelativePath(normalizedRelativePath)} · ${DESKTOP_PRODUCT_NAME}`,
    icon: windowIcon,
    width: 1180,
    height: 860,
    minWidth: 760,
    minHeight: 560,
    autoHideMenuBar: true,
    parent: mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined,
    titleBarStyle: process.platform === "win32" ? "hidden" : "default",
    titleBarOverlay: process.platform === "win32" ? lastAppliedTitlebarTheme : false,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  attachWindowOpenHandler(previewWindow);

  previewWindow.on("closed", () => {
    previewWindow = null;
  });

  await loadRenderer(previewWindow, "/preview", { relativePath: normalizedRelativePath });
  focusWindow(previewWindow);
  return true;
}

async function createMainWindow() {
  const windowIcon = resolveDesktopIconPath();
  mainWindow = new BrowserWindow({
    title: DESKTOP_PRODUCT_NAME,
    icon: windowIcon,
    width: 1680,
    height: 980,
    minWidth: 1240,
    minHeight: 760,
    autoHideMenuBar: true,
    titleBarStyle: process.platform === "win32" ? "hidden" : "default",
    titleBarOverlay: process.platform === "win32" ? lastAppliedTitlebarTheme : false,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  attachWindowOpenHandler(mainWindow);

  await loadRenderer(mainWindow);

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

app.on("before-quit", () => {
  quitting = true;
  stopBackendKernel();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.whenReady().then(async () => {
  initializeAppMetadata();
  if (readUpdaterInstallLock()) {
    await showUpdateInstallInProgress();
    return;
  }
  registerDesktopIpc();
  initializeAutoUpdater();
  const backendReady = await startBackendKernel();
  if (!backendReady) {
    app.quit();
    return;
  }
  await createMainWindow();

  app.on("activate", async () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      await createMainWindow();
    }
  });
});
