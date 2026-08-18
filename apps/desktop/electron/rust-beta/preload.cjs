const { contextBridge, ipcRenderer } = require("electron");

function readRuntimeConnection() {
  const connection = ipcRenderer.sendSync("storydex:rust-beta-runtime-connection");
  const port = Number(connection?.port);
  const token = String(connection?.token || "").trim();
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("Storydex Rust Beta did not receive a valid storydex-agentd port");
  }
  if (!/^[0-9a-f]{32}$/i.test(token)) {
    throw new Error("Storydex Rust Beta did not receive a valid storydex-agentd token");
  }
  return { port, token };
}

const runtime = readRuntimeConnection();

contextBridge.exposeInMainWorld("storydexDesktop", {
  platform: process.platform,
  backendBaseUrl: `http://127.0.0.1:${runtime.port}/api/v1`,
  backendAuthToken: runtime.token,
  isTitleBarOverlaySupported: false,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node
  }
});
