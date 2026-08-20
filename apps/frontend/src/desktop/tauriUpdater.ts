type UpdaterState = StorydexDesktopUpdaterState;

type TauriUpdate = {
  version: string;
  body?: string;
  download: (
    onEvent?: (event: {
      event: "Started" | "Progress" | "Finished";
      data?: { contentLength?: number; chunkLength?: number };
    }) => void
  ) => Promise<void>;
  install: () => Promise<void>;
  close?: () => Promise<void>;
};

type TauriUpdaterModule = {
  check: () => Promise<TauriUpdate | null>;
};

import { isTauriRuntime } from "@/desktop/tauriDesktop";

const INITIAL_STATE: UpdaterState = {
  supported: true,
  status: "idle",
  currentVersion: "",
  availableVersion: "",
  releaseNotes: "",
  progress: null,
  error: "",
  feedUrl: "https://updates.septemc.com/storydex/windows/latest.json",
  diagnosticLog: ""
};

function copyState(state: UpdaterState): UpdaterState {
  return {
    ...state,
    progress: state.progress ? { ...state.progress } : null
  };
}

export async function installTauriUpdaterBridge(): Promise<void> {
  if (!isTauriRuntime() || !window.storydexDesktop || window.storydexDesktop.updater) {
    return;
  }

  const state = refState();
  let update: TauriUpdate | null = null;
  let downloaded = false;
  const listeners = new Set<(next: UpdaterState) => void>();
  const emit = (): UpdaterState => {
    const next = copyState(state.value);
    for (const listener of listeners) {
      listener(next);
    }
    return next;
  };
  const fail = (error: unknown): UpdaterState => {
    state.value = {
      ...state.value,
      status: "error",
      error: error instanceof Error ? error.message : String(error)
    };
    return emit();
  };

  const loadModule = async (): Promise<TauriUpdaterModule> => {
    try {
      return await import("@tauri-apps/plugin-updater");
    } catch (error) {
      throw new Error(`Tauri 更新组件加载失败：${error instanceof Error ? error.message : String(error)}`);
    }
  };

  const bridge: StorydexDesktopUpdaterBridge = {
    getState: async () => copyState(state.value),
    check: async () => {
      state.value = { ...state.value, status: "checking", error: "" };
      emit();
      try {
        const previous = update;
        update = null;
        downloaded = false;
        await previous?.close?.().catch(() => undefined);
        const updater = await loadModule();
        const candidate = await updater.check();
        update = candidate;
        if (!candidate) {
          state.value = { ...state.value, status: "not-available", availableVersion: "", releaseNotes: "", progress: null };
        } else {
          state.value = {
            ...state.value,
            status: "available",
            availableVersion: candidate.version,
            releaseNotes: candidate.body || "",
            progress: null
          };
        }
        return emit();
      } catch (error) {
        return fail(error);
      }
    },
    download: async () => {
      try {
        if (!update) {
          await bridge.check();
        }
        if (!update) {
          return copyState(state.value);
        }
        state.value = { ...state.value, status: "downloading", error: "", progress: null };
        emit();
        let transferred = 0;
        let total = 0;
        await update.download((event) => {
          if (event.event === "Started") {
            total = Number(event.data?.contentLength || 0);
          } else if (event.event === "Progress") {
            transferred += Number(event.data?.chunkLength || 0);
          }
          state.value = {
            ...state.value,
            status: "downloading",
            progress: {
              percent: total > 0 ? Math.min(100, (transferred / total) * 100) : 0,
              transferred,
              total,
              bytesPerSecond: 0
            }
          };
          emit();
        });
        downloaded = true;
        state.value = { ...state.value, status: "downloaded", progress: { percent: 100, transferred, total, bytesPerSecond: 0 } };
        return emit();
      } catch (error) {
        return fail(error);
      }
    },
    install: async () => {
      if (!update || !downloaded) {
        return false;
      }
      const installingUpdate = update;
      try {
        state.value = { ...state.value, status: "installing", error: "" };
        emit();
        await installingUpdate.install();
        return true;
      } catch (error) {
        fail(error);
        return false;
      } finally {
        await installingUpdate.close?.().catch(() => undefined);
        if (update === installingUpdate) {
          update = null;
          downloaded = false;
        }
      }
    },
    onState: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    }
  };

  window.storydexDesktop.updater = bridge;
}

function refState(): { value: UpdaterState } {
  const currentVersion = String(window.storydexDesktop?.versions?.tauri || "").trim();
  return { value: { ...INITIAL_STATE, currentVersion } };
}
