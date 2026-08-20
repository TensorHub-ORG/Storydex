import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const eventModule = vi.hoisted(() => ({
  listen: vi.fn()
}));
const updaterModule = vi.hoisted(() => ({
  check: vi.fn()
}));

vi.mock("@tauri-apps/api/event", () => eventModule);
vi.mock("@tauri-apps/plugin-updater", () => updaterModule);

import { installTauriDesktopBridge, isTauriRuntime } from "@/desktop/tauriDesktop";
import { installTauriUpdaterBridge } from "@/desktop/tauriUpdater";

const originalUrl = window.location.href;

function installTauriWindow(): StorydexDesktopBridge {
  window.location.href = "http://tauri.localhost/";
  Object.defineProperty(window, "__TAURI_INTERNALS__", {
    configurable: true,
    value: { invoke: vi.fn() }
  });
  const bridge: StorydexDesktopBridge = {
    platform: "win32",
    versions: { tauri: "2.0.5" }
  };
  Object.defineProperty(window, "storydexDesktop", {
    configurable: true,
    value: bridge
  });
  return bridge;
}

describe("Tauri desktop bridges", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window, "storydexDesktop", { configurable: true, value: undefined });
    Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: undefined });
  });

  afterEach(() => {
    window.dispatchEvent(new Event("pagehide"));
    window.location.href = originalUrl;
    Object.defineProperty(window, "storydexDesktop", { configurable: true, value: undefined });
    Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: undefined });
  });

  it("subscribes to Tauri open-target events and detaches them on page hide", async () => {
    expect(isTauriRuntime()).toBe(false);
    await installTauriDesktopBridge();

    const bridge = installTauriWindow();
    const handlers = new Map<string, (event: { payload: unknown }) => void>();
    const detachOpen = vi.fn();
    const detachPreview = vi.fn();
    eventModule.listen.mockImplementation(async (name: string, handler: (event: { payload: unknown }) => void) => {
      handlers.set(name, handler);
      return name === "storydex:open-target" ? detachOpen : detachPreview;
    });

    expect(isTauriRuntime()).toBe(true);
    await installTauriDesktopBridge();
    const openListener = vi.fn();
    const previewListener = vi.fn();
    const unsubscribeOpen = bridge.onOpenTarget?.(openListener);
    bridge.onPreviewOpenFile?.(previewListener);
    const target = { id: 7, path: "C:\\stories\\demo", isFile: false };
    handlers.get("storydex:open-target")?.({ payload: target });
    handlers.get("storydex:preview-open-file")?.({ payload: "chapters/001.md" });

    expect(openListener).toHaveBeenCalledWith(target);
    expect(previewListener).toHaveBeenCalledWith("chapters/001.md");
    unsubscribeOpen?.();
    handlers.get("storydex:open-target")?.({ payload: target });
    expect(openListener).toHaveBeenCalledTimes(1);

    window.dispatchEvent(new Event("pagehide"));
    expect(detachOpen).toHaveBeenCalledTimes(1);
    expect(detachPreview).toHaveBeenCalledTimes(1);
  });

  it("checks, downloads, installs, and reports updater state through the Tauri plugin", async () => {
    const bridge = installTauriWindow();
    const install = vi.fn().mockResolvedValue(undefined);
    const close = vi.fn().mockResolvedValue(undefined);
    const download = vi.fn(async (onEvent?: (event: unknown) => void) => {
      onEvent?.({ event: "Started", data: { contentLength: 100 } });
      onEvent?.({ event: "Progress", data: { chunkLength: 40 } });
      onEvent?.({ event: "Progress", data: { chunkLength: 60 } });
      onEvent?.({ event: "Finished" });
    });
    updaterModule.check.mockResolvedValue({
      version: "2.1.0",
      body: "release notes",
      download,
      install,
      close
    });

    await installTauriUpdaterBridge();
    const updater = bridge.updater;
    expect(await updater?.getState()).toMatchObject({ status: "idle", currentVersion: "2.0.5" });
    const listener = vi.fn();
    const detach = updater?.onState(listener);
    expect(await updater?.check()).toMatchObject({
      status: "available",
      availableVersion: "2.1.0",
      releaseNotes: "release notes"
    });
    expect(await updater?.download()).toMatchObject({
      status: "downloaded",
      progress: { percent: 100, transferred: 100, total: 100 }
    });
    expect(await updater?.install()).toBe(true);
    expect(install).toHaveBeenCalledTimes(1);
    expect(close).toHaveBeenCalledTimes(1);
    expect(listener).toHaveBeenCalledWith(expect.objectContaining({ status: "installing" }));
    detach?.();
  });

  it("keeps no-update and updater failures explicit without installing", async () => {
    const bridge = installTauriWindow();
    updaterModule.check.mockResolvedValueOnce(null);
    await installTauriUpdaterBridge();
    expect(await bridge.updater?.download()).toMatchObject({ status: "not-available" });
    expect(await bridge.updater?.install()).toBe(false);

    const failingBridge: StorydexDesktopBridge = { platform: "win32", versions: { tauri: "2.0.5" } };
    Object.defineProperty(window, "storydexDesktop", { configurable: true, value: failingBridge });
    updaterModule.check.mockRejectedValueOnce(new Error("signature mismatch"));
    await installTauriUpdaterBridge();
    expect(await failingBridge.updater?.check()).toMatchObject({
      status: "error",
      error: "signature mismatch"
    });
  });

  it("rechecks with a fresh updater resource after installation fails", async () => {
    const bridge = installTauriWindow();
    const failedClose = vi.fn().mockResolvedValue(undefined);
    const replacementDownload = vi.fn().mockResolvedValue(undefined);
    updaterModule.check
      .mockResolvedValueOnce({
        version: "2.1.0",
        download: vi.fn().mockResolvedValue(undefined),
        install: vi.fn().mockRejectedValue(new Error("installer failed")),
        close: failedClose
      })
      .mockResolvedValueOnce({
        version: "2.1.0",
        download: replacementDownload,
        install: vi.fn().mockResolvedValue(undefined),
        close: vi.fn().mockResolvedValue(undefined)
      });

    await installTauriUpdaterBridge();
    await bridge.updater?.download();
    expect(await bridge.updater?.install()).toBe(false);
    expect(failedClose).toHaveBeenCalledTimes(1);
    await bridge.updater?.download();
    expect(updaterModule.check).toHaveBeenCalledTimes(2);
    expect(replacementDownload).toHaveBeenCalledTimes(1);
  });
});
