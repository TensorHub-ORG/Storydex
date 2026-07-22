const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const preloadPath = path.resolve(__dirname, "..", "electron", "preload.cjs");

function loadDesktopBridge() {
  const ipcRenderer = new EventEmitter();
  ipcRenderer.invoke = async () => undefined;
  let bridge = null;
  const contextBridge = {
    exposeInMainWorld(name, value) {
      if (name === "storydexDesktop") bridge = value;
    }
  };
  const source = fs.readFileSync(preloadPath, "utf8");
  vm.runInNewContext(source, {
    require(id) {
      if (id === "electron") return { contextBridge, ipcRenderer };
      throw new Error(`Unexpected preload dependency: ${id}`);
    },
    process: {
      platform: "linux",
      env: {},
      versions: { electron: "test", chrome: "test", node: process.versions.node }
    }
  }, { filename: preloadPath });
  return { bridge, ipcRenderer };
}

test("desktop updater broadcasts state to multiple renderer subscribers", () => {
  const { bridge, ipcRenderer } = loadDesktopBridge();
  const notificationStates = [];
  const settingsStates = [];
  const detachNotification = bridge.updater.onState((state) => notificationStates.push(state));
  bridge.updater.onState((state) => settingsStates.push(state));

  const downloading = { status: "downloading", progress: { percent: 42 } };
  ipcRenderer.emit("storydex:updater-state", {}, downloading);
  assert.deepEqual(notificationStates, [downloading]);
  assert.deepEqual(settingsStates, [downloading]);

  detachNotification();
  const downloaded = { status: "downloaded", progress: null };
  ipcRenderer.emit("storydex:updater-state", {}, downloaded);
  assert.deepEqual(notificationStates, [downloading]);
  assert.deepEqual(settingsStates, [downloading, downloaded]);
});
