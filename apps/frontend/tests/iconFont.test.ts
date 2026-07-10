import { describe, expect, it, vi } from "vitest";
import {
  ICON_FONT_CLASS_FAILED,
  ICON_FONT_CLASS_READY,
  initializeIconFontState
} from "@/utils/iconFont";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("icon font startup", () => {
  it("marks ready when the font loads", async () => {
    const scheduled: Array<() => void> = [];
    const fonts = { load: vi.fn().mockResolvedValue([]), check: vi.fn(() => true), ready: Promise.resolve() };
    initializeIconFontState({ fonts, schedule: (callback) => (scheduled.push(callback), scheduled.length), retryDelays: [0] });
    scheduled[0]();
    await Promise.resolve();
    await Promise.resolve();
    expect(document.documentElement.classList.contains(ICON_FONT_CLASS_READY)).toBe(true);
    expect(document.documentElement.classList.contains(ICON_FONT_CLASS_FAILED)).toBe(false);
  });

  it("recovers after an initial load failure", async () => {
    const ready = deferred<void>();
    const scheduled: Array<() => void> = [];
    const fonts = {
      load: vi.fn().mockRejectedValueOnce(new Error("cold start")).mockResolvedValue([]),
      check: vi.fn(() => true),
      ready: ready.promise
    };
    initializeIconFontState({ fonts, schedule: (callback) => (scheduled.push(callback), scheduled.length), retryDelays: [0, 1] });
    scheduled[0]();
    await Promise.resolve();
    scheduled[1]();
    await Promise.resolve();
    await Promise.resolve();
    expect(fonts.load).toHaveBeenCalledTimes(2);
    expect(document.documentElement.classList.contains(ICON_FONT_CLASS_READY)).toBe(true);
  });

  it("uses a visible fallback when unsupported or past the deadline", () => {
    initializeIconFontState({ fonts: undefined, root: document.documentElement });
    // happy-dom exposes document.fonts differently across releases, so force the deadline path too.
    document.documentElement.className = "";
    const scheduled: Array<() => void> = [];
    const never = new Promise<void>(() => undefined);
    initializeIconFontState({
      fonts: { load: vi.fn(() => never as Promise<FontFace[]>), check: vi.fn(() => false), ready: never },
      schedule: (callback) => (scheduled.push(callback), scheduled.length),
      retryDelays: []
    });
    scheduled[0]();
    expect(document.documentElement.classList.contains(ICON_FONT_CLASS_FAILED)).toBe(true);
    expect(document.documentElement.classList.contains(ICON_FONT_CLASS_READY)).toBe(false);
  });
});
