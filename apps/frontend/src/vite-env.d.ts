/// <reference types="vite/client" />

interface StorydexDesktopBridge {
  getPendingOpenTarget?: () => Promise<StorydexDesktopOpenTarget | null>;
  ackOpenTarget?: (targetId: number) => Promise<boolean>;
  onOpenTarget?: (listener: (target: StorydexDesktopOpenTarget) => void) => () => void;
  platform: string;
  backendBaseUrl?: string;
  isTitleBarOverlaySupported?: boolean;
  versions: {
    electron: string;
    chrome: string;
    node: string;
  };
  setTitleBarTheme?: (theme: { color: string; symbolColor: string }) => Promise<{
    applied: boolean;
    color?: string;
    symbolColor?: string;
    height?: number;
  }>;
  pickDirectory?: (options?: { title?: string; defaultPath?: string }) => Promise<string>;
  revealPath?: (absolutePath: string) => Promise<boolean>;
  openWithDialog?: (absolutePath: string) => Promise<boolean>;
  openPreviewWindow?: (relativePath: string) => Promise<boolean>;
  onPreviewOpenFile?: (listener: (relativePath: string) => void) => () => void;
  updater?: StorydexDesktopUpdaterBridge;
}

interface StorydexDesktopUpdaterProgress {
  percent: number;
  transferred: number;
  total: number;
  bytesPerSecond: number;
}

interface StorydexDesktopUpdaterState {
  supported: boolean;
  status: string;
  currentVersion: string;
  availableVersion: string;
  releaseNotes: string;
  progress: StorydexDesktopUpdaterProgress | null;
  error: string;
  feedUrl: string;
}

interface StorydexDesktopUpdaterBridge {
  getState: () => Promise<StorydexDesktopUpdaterState>;
  check: () => Promise<StorydexDesktopUpdaterState>;
  download: () => Promise<StorydexDesktopUpdaterState>;
  install: () => Promise<boolean>;
  onState: (listener: (state: StorydexDesktopUpdaterState) => void) => () => void;
}

interface StorydexDesktopOpenTarget {
  id: number;
  path: string;
  isFile: boolean;
}

interface Window {
  storydexDesktop?: StorydexDesktopBridge;
}
