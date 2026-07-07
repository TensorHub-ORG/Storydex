const { contextBridge, ipcRenderer } = require("electron");

const BACKEND_HOST = process.env.STORYDEX_BACKEND_HOST || "127.0.0.1";
const BACKEND_PORT = Number(process.env.STORYDEX_BACKEND_PORT || 18081);
const BACKEND_BASE_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}/api/v1`;
const DEFAULT_TITLEBAR_HEIGHT = 42;
const WINDOWS_CONTROL_BUTTON_WIDTH = 138;
const WINDOWS_SIDE_PADDING = 14;
const TITLEBAR_STYLE_ID = "storydex-desktop-titlebar-style";

function installTitlebarIntegration() {
  if (process.platform !== "win32") {
    return;
  }

  const applyTitlebarMetrics = () => {
    const root = document.documentElement;
    if (!root) {
      return;
    }

    const overlay = window.navigator.windowControlsOverlay;
    const overlayVisible = !!overlay?.visible;
    const titlebarRect = overlayVisible ? overlay.getTitlebarAreaRect() : null;
    const titlebarHeight = titlebarRect?.height ? Math.max(DEFAULT_TITLEBAR_HEIGHT, Math.round(titlebarRect.height)) : DEFAULT_TITLEBAR_HEIGHT;
    const safeRight = overlayVisible ? Math.max(WINDOWS_CONTROL_BUTTON_WIDTH, window.innerWidth - (titlebarRect.x + titlebarRect.width)) : WINDOWS_CONTROL_BUTTON_WIDTH;
    const safeLeft = overlayVisible ? Math.max(WINDOWS_SIDE_PADDING, Math.round(titlebarRect.x)) : WINDOWS_SIDE_PADDING;

    root.classList.add("storydex-desktop-titlebar");
    root.style.setProperty("--storydex-titlebar-height", `${titlebarHeight}px`);
    root.style.setProperty("--storydex-titlebar-safe-left", `${safeLeft}px`);
    root.style.setProperty("--storydex-titlebar-safe-right", `${safeRight}px`);
  };

  const installStyle = () => {
    if (document.getElementById(TITLEBAR_STYLE_ID)) {
      return;
    }

    const style = document.createElement("style");
    style.id = TITLEBAR_STYLE_ID;
    style.textContent = `
      :root.storydex-desktop-titlebar .top-header {
        -webkit-app-region: drag;
        app-region: drag;
        height: max(var(--header-height, ${DEFAULT_TITLEBAR_HEIGHT}px), var(--storydex-titlebar-height, ${DEFAULT_TITLEBAR_HEIGHT}px));
        min-height: var(--storydex-titlebar-height, ${DEFAULT_TITLEBAR_HEIGHT}px);
        padding-left: max(${WINDOWS_SIDE_PADDING}px, var(--storydex-titlebar-safe-left, ${WINDOWS_SIDE_PADDING}px));
        padding-right: max(${WINDOWS_SIDE_PADDING}px, var(--storydex-titlebar-safe-right, ${WINDOWS_CONTROL_BUTTON_WIDTH}px));
        user-select: none;
      }

      :root.storydex-desktop-titlebar .top-header button,
      :root.storydex-desktop-titlebar .top-header input,
      :root.storydex-desktop-titlebar .top-header select,
      :root.storydex-desktop-titlebar .top-header textarea,
      :root.storydex-desktop-titlebar .top-header a,
      :root.storydex-desktop-titlebar .top-header [role="button"],
      :root.storydex-desktop-titlebar .top-header .file-menu-wrap,
      :root.storydex-desktop-titlebar .top-header .file-menu-card,
      :root.storydex-desktop-titlebar .top-header .command-bar,
      :root.storydex-desktop-titlebar .top-header .command-bar * {
        -webkit-app-region: no-drag;
        app-region: no-drag;
        user-select: auto;
      }
    `;
    document.head.appendChild(style);
  };

  const onReady = () => {
    installStyle();
    applyTitlebarMetrics();
  };

  if (document.readyState === "loading") {
    window.addEventListener("DOMContentLoaded", onReady, { once: true });
  } else {
    onReady();
  }

  window.addEventListener("resize", applyTitlebarMetrics);
  window.navigator.windowControlsOverlay?.addEventListener("geometrychange", applyTitlebarMetrics);
}

installTitlebarIntegration();

contextBridge.exposeInMainWorld("storydexDesktop", {
  platform: process.platform,
  backendBaseUrl: BACKEND_BASE_URL,
  isTitleBarOverlaySupported: process.platform === "win32",
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node
  },
  pickDirectory: async (options = {}) => ipcRenderer.invoke("storydex:pick-directory", options),
  revealPath: async (absolutePath) => ipcRenderer.invoke("storydex:reveal-path", absolutePath),
  openWithDialog: async (absolutePath) => ipcRenderer.invoke("storydex:open-with-dialog", absolutePath),
  setTitleBarTheme: async (theme) => ipcRenderer.invoke("storydex:set-titlebar-theme", theme),
  openPreviewWindow: async (relativePath) => ipcRenderer.invoke("storydex:open-preview-window", relativePath),
  updater: {
    getState: async () => ipcRenderer.invoke("storydex:updater-get-state"),
    check: async () => ipcRenderer.invoke("storydex:updater-check"),
    download: async () => ipcRenderer.invoke("storydex:updater-download"),
    install: async () => ipcRenderer.invoke("storydex:updater-install"),
    onState: (listener) => {
      const wrapped = (_event, state) => listener(state);
      ipcRenderer.on("storydex:updater-state", wrapped);
      return () => ipcRenderer.removeListener("storydex:updater-state", wrapped);
    }
  }
});
