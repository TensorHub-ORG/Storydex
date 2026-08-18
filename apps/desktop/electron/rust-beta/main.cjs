const { app, BrowserWindow, dialog, ipcMain, session, shell } = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const { fileURLToPath, pathToFileURL } = require("node:url");
const {
  RustBetaRuntimeSupervisor,
  createIsolatedBetaPaths,
  isPathInside,
  resolveAgentdBinary
} = require("./runtime.cjs");

const desktopRoot = path.resolve(__dirname, "..", "..");
const repoRoot = path.resolve(desktopRoot, "..", "..");
const runtimePaths = createIsolatedBetaPaths({
  profileRoot: process.env.STORYDEX_RUST_BETA_PROFILE_ROOT,
  workspaceRoot: process.env.STORYDEX_RUST_BETA_WORKSPACE_ROOT
});

app.setName("Storydex Rust Beta");
app.setPath("userData", runtimePaths.userDataRoot);
app.commandLine.appendSwitch("disable-http-cache");

let mainWindow = null;
let supervisor = null;
let runtimeConnection = null;
let quitting = false;
let quitAllowed = false;
let quitPromise = null;

function resolveRealPath(targetPath) {
  return fs.realpathSync.native ? fs.realpathSync.native(targetPath) : fs.realpathSync(targetPath);
}

function resolveLocalDevelopmentUrl(configuredUrl) {
  let parsed;
  try {
    parsed = new URL(configuredUrl);
  } catch (error) {
    throw new Error(`Invalid STORYDEX_RUST_BETA_URL: ${error.message}`);
  }
  const port = Number(parsed.port);
  if (
    parsed.protocol !== "http:" ||
    parsed.hostname !== "127.0.0.1" ||
    !Number.isInteger(port) ||
    port < 1 ||
    port > 65535 ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error("STORYDEX_RUST_BETA_URL must be an explicit http://127.0.0.1:<port> origin");
  }
  return parsed.origin;
}

function resolveConfiguredFrontendIndex(configuredDist) {
  const indexPath = path.join(path.resolve(configuredDist), "index.html");
  if (!fs.existsSync(indexPath) || !fs.statSync(indexPath).isFile()) {
    throw new Error(`Rust Beta frontend index is missing: ${indexPath}`);
  }
  const realIndexPath = resolveRealPath(indexPath);
  const frontendDistRoot = path.join(repoRoot, "apps", "frontend", "dist");
  const allowedFrontendRoot = fs.existsSync(frontendDistRoot)
    ? resolveRealPath(frontendDistRoot)
    : path.resolve(frontendDistRoot);
  if (
    !isPathInside(realIndexPath, allowedFrontendRoot) &&
    !isPathInside(realIndexPath, runtimePaths.temporaryRoot)
  ) {
    throw new Error(
      `STORYDEX_RUST_BETA_FRONTEND_DIST must stay inside ${allowedFrontendRoot} or ${runtimePaths.temporaryRoot}`
    );
  }
  return realIndexPath;
}

function resolveRendererEntry() {
  const configuredUrl = String(process.env.STORYDEX_RUST_BETA_URL || "").trim();
  if (configuredUrl) return { kind: "url", value: resolveLocalDevelopmentUrl(configuredUrl) };
  const configuredDist = String(process.env.STORYDEX_RUST_BETA_FRONTEND_DIST || "").trim();
  if (configuredDist) {
    return { kind: "file", value: resolveConfiguredFrontendIndex(configuredDist) };
  }
  const builtIndex = path.join(repoRoot, "apps", "frontend", "dist", "index.html");
  if (app.isPackaged && fs.existsSync(builtIndex)) return { kind: "file", value: builtIndex };
  return { kind: "url", value: "http://127.0.0.1:5173" };
}

function createIsolatedRendererSession() {
  return session.fromPartition(`storydex-rust-beta-${process.pid}`, { cache: false });
}

function isAllowedRendererNavigation(targetUrl, entry) {
  try {
    const parsed = new URL(targetUrl);
    if (entry.kind === "url") return parsed.origin === new URL(entry.value).origin;
    if (parsed.protocol !== "file:") return false;
    return isPathInside(fileURLToPath(parsed), path.dirname(entry.value));
  } catch {
    return false;
  }
}

async function loadRenderer(windowRef, entry) {
  if (entry.kind === "file") {
    await windowRef.loadURL(pathToFileURL(entry.value).toString());
    return;
  }
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      await windowRef.loadURL(entry.value);
      return;
    } catch (error) {
      if (attempt === 79) throw error;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
}

async function createMainWindow(runtime) {
  const rendererEntry = resolveRendererEntry();
  const betaSession = createIsolatedRendererSession();
  mainWindow = new BrowserWindow({
    title: "Storydex Rust Beta",
    width: 1680,
    height: 980,
    minWidth: 1240,
    minHeight: 760,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      session: betaSession
    }
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(String(url || ""))) void shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (!isAllowedRendererNavigation(url, rendererEntry)) event.preventDefault();
  });
  mainWindow.webContents.on("render-process-gone", (_event, details) => {
    if (!quitting) {
      void dialog.showMessageBox({
        type: "error",
        title: "Storydex Rust Beta",
        message: `Renderer process stopped unexpectedly: ${details.reason}`,
        detail: `storydex-agentd log: ${runtime.logFilePath}`
      });
    }
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  await loadRenderer(mainWindow, rendererEntry);
}

async function showRuntimeCrash(info) {
  if (quitting) return;
  const result = await dialog.showMessageBox({
    type: "error",
    title: "Storydex Rust Beta",
    buttons: ["Open log", "Exit"],
    defaultId: 0,
    cancelId: 1,
    message: `storydex-agentd exited unexpectedly (code=${info.code}, signal=${info.signal || "none"}).`,
    detail: [info.detail || "No process output was captured.", info.logFilePath ? `Log: ${info.logFilePath}` : ""]
      .filter(Boolean)
      .join("\n\n")
  });
  if (result.response === 0 && info.logFilePath) await shell.openPath(info.logFilePath);
  app.quit();
}

app.on("before-quit", (event) => {
  quitting = true;
  if (quitAllowed) return;
  event.preventDefault();
  if (!quitPromise) {
    quitPromise = (async () => {
      try {
        if (supervisor) await supervisor.stop();
      } catch (error) {
        console.error(`[Storydex Rust Beta] Runtime cleanup failed: ${error.message || String(error)}`);
      } finally {
        quitAllowed = true;
        app.quit();
      }
    })();
  }
});

app.on("window-all-closed", () => app.quit());

app.whenReady().then(async () => {
  try {
    app.setAppUserModelId("cn.tensorhub.storydex.rust-beta");
    const binaryPath = resolveAgentdBinary(desktopRoot);
    supervisor = new RustBetaRuntimeSupervisor({
      binaryPath,
      runtimePaths,
      onUnexpectedExit: showRuntimeCrash
    });
    runtimeConnection = await supervisor.start();
    ipcMain.on("storydex:rust-beta-runtime-connection", (event) => {
      if (!mainWindow || mainWindow.isDestroyed() || event.sender.id !== mainWindow.webContents.id) {
        event.returnValue = null;
        return;
      }
      event.returnValue = {
        port: runtimeConnection.port,
        token: runtimeConnection.token
      };
    });
    await createMainWindow(runtimeConnection);
  } catch (error) {
    await dialog.showMessageBox({
      type: "error",
      title: "Storydex Rust Beta",
      message: "Rust Beta failed to start. Python fallback is disabled.",
      detail: [error.message || String(error), runtimeConnection?.logFilePath ? `Log: ${runtimeConnection.logFilePath}` : ""]
        .filter(Boolean)
        .join("\n\n")
    });
    app.quit();
  }
});
