type TauriEventModule = typeof import("@tauri-apps/api/event");

export function isTauriRuntime(): boolean {
  if (typeof window === "undefined") {
    return false;
  }

  const internals = (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  if (!internals) {
    return false;
  }

  return window.location.protocol === "tauri:"
    || window.location.hostname === "tauri.localhost"
    || (window.location.protocol === "http:"
      && (window.location.hostname === "127.0.0.1" || window.location.hostname === "localhost"));
}

export async function installTauriDesktopBridge(): Promise<void> {
  if (!isTauriRuntime() || !window.storydexDesktop) {
    return;
  }

  const bridge = window.storydexDesktop;
  const eventModule = await loadEventModule();
  const openTargetListeners = new Set<(target: StorydexDesktopOpenTarget) => void>();
  const previewListeners = new Set<(relativePath: string) => void>();
  const closeRequestListeners = new Set<() => void>();
  const detachOpenTargets = await eventModule.listen<StorydexDesktopOpenTarget>("storydex:open-target", (event) => {
    for (const listener of openTargetListeners) {
      listener(event.payload);
    }
  });
  const detachPreview = await eventModule.listen<string>("storydex:preview-open-file", (event) => {
    for (const listener of previewListeners) {
      listener(event.payload);
    }
  });
  const detachCloseRequested = await eventModule.listen("storydex:close-requested", () => {
    for (const listener of closeRequestListeners) {
      listener();
    }
  });
  bridge.onOpenTarget = (listener) => subscribe(openTargetListeners, listener);
  bridge.onPreviewOpenFile = (listener) => subscribe(previewListeners, listener);
  bridge.onCloseRequested = (listener) => subscribe(closeRequestListeners, listener);
  window.addEventListener("pagehide", () => {
    detachOpenTargets();
    detachPreview();
    detachCloseRequested();
  }, { once: true });
}

async function loadEventModule(): Promise<TauriEventModule> {
  try {
    return await import("@tauri-apps/api/event");
  } catch (error) {
    throw new Error(`Tauri 桌面事件组件加载失败：${error instanceof Error ? error.message : String(error)}`);
  }
}

function subscribe<T>(listeners: Set<(payload: T) => void>, listener: (payload: T) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
