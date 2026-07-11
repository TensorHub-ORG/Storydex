export const ICON_FONT_CLASS_READY = "icon-font-ready";
export const ICON_FONT_CLASS_FAILED = "icon-font-failed";
export const ICON_FONT_SPEC = '400 16px "Material Symbols Rounded"';
export const ICON_FONT_RETRY_DELAYS = [0, 120, 360, 900, 2400, 5000, 10000] as const;
export const ICON_FONT_DEADLINE_MS = 2400;

type FontFaceSetLike = Pick<FontFaceSet, "load" | "check" | "ready">;

export interface IconFontRuntime {
  root?: HTMLElement;
  fonts?: FontFaceSetLike;
  schedule?: (callback: () => void, delay: number) => number;
  retryDelays?: readonly number[];
  deadlineMs?: number;
}

export function setIconFontState(root: HTMLElement, state: "ready" | "failed"): void {
  root.classList.toggle(ICON_FONT_CLASS_READY, state === "ready");
  root.classList.toggle(ICON_FONT_CLASS_FAILED, state === "failed");
}

export function initializeIconFontState(runtime: IconFontRuntime = {}): void {
  const root = runtime.root ?? document.documentElement;
  const fonts = runtime.fonts ?? ("fonts" in document ? document.fonts : undefined);
  const schedule = runtime.schedule ?? ((callback, delay) => window.setTimeout(callback, delay));
  const retryDelays = runtime.retryDelays ?? ICON_FONT_RETRY_DELAYS;
  const deadlineMs = runtime.deadlineMs ?? ICON_FONT_DEADLINE_MS;
  if (!fonts) {
    setIconFontState(root, "failed");
    return;
  }

  let settled = false;
  const markReadyWhenAvailable = async (): Promise<boolean> => {
    if (settled) return true;
    let loadedFaces: FontFace[] | undefined;
    try {
      loadedFaces = await fonts.load(ICON_FONT_SPEC, "home history settings arrow_upward");
    } catch {
      return false;
    }
    if (!(loadedFaces?.length) && !fonts.check(ICON_FONT_SPEC)) return false;
    settled = true;
    setIconFontState(root, "ready");
    return true;
  };

  for (const delay of retryDelays) {
    schedule(() => void markReadyWhenAvailable(), delay);
  }
  void fonts.ready.then(() => markReadyWhenAvailable());
  schedule(() => {
    if (!settled) {
      setIconFontState(root, "failed");
    }
  }, deadlineMs);
}

