export const DEFAULT_PANE_FONT_SCALE = 100;
export const MIN_PANE_FONT_SCALE = 75;
export const MAX_PANE_FONT_SCALE = 150;
export const PANE_FONT_SCALE_STEP = 5;

// Keep this list in sync with the absolute font-size/line-height values used by
// the frontend styles. The Vite transform replaces these pixel lengths with
// precomputed custom properties so Chromium never has to evaluate unsupported
// expressions such as `calc(16px * 1.25)`.
export const PANE_RELATIVE_PIXEL_VALUES = [
  8,
  9,
  9.5,
  10,
  10.5,
  11,
  12,
  13,
  14,
  15,
  16,
  17,
  18,
  19,
  22,
  24,
  28,
  34,
  38,
  40,
  42,
  44,
  56,
  60
] as const;

const ABSOLUTE_PIXEL_VALUE_RE = /(^|[^\w.-])(\d+(?:\.\d+)?)px\b/g;
const SCALED_PIXEL_VARIABLE_PREFIX = "--ui-pane-scaled-px-";
const PANE_RELATIVE_PIXEL_VALUE_SET = new Set<number>(PANE_RELATIVE_PIXEL_VALUES);

export function normalizePaneFontScale(value: unknown, fallback = DEFAULT_PANE_FONT_SCALE): number {
  const parsed = value === null || value === "" ? Number.NaN : Number(value);
  const fallbackValue = Number.isFinite(Number(fallback)) ? Number(fallback) : DEFAULT_PANE_FONT_SCALE;
  const normalized = Number.isFinite(parsed) ? parsed : fallbackValue;
  return Math.min(Math.max(Math.round(normalized), MIN_PANE_FONT_SCALE), MAX_PANE_FONT_SCALE);
}

export function legacyFileFontSizeToPaneScale(value: unknown): number {
  const parsed = value === null || value === "" ? Number.NaN : Number(value);
  const legacySize = Number.isFinite(parsed) ? Math.min(Math.max(parsed, 12), 24) : 16;
  const percentage = (legacySize / 16) * 100;
  return normalizePaneFontScale(Math.round(percentage / PANE_FONT_SCALE_STEP) * PANE_FONT_SCALE_STEP);
}

export function paneFontScaleCssValue(value: unknown): string {
  return String(normalizePaneFontScale(value) / 100);
}

export function paneFontScaleStyle(value: unknown): Record<string, string> {
  const scale = normalizePaneFontScale(value) / 100;
  const style: Record<string, string> = {
    "--ui-pane-font-scale": String(scale)
  };

  for (const pixelValue of PANE_RELATIVE_PIXEL_VALUES) {
    style[paneScaledPixelVariableName(pixelValue)] = `${formatCssNumber(pixelValue * scale)}px`;
  }

  return style;
}

export function paneScaledPixelVariableName(value: number | string): string {
  const normalized = formatCssNumber(Number(value));
  return `${SCALED_PIXEL_VARIABLE_PREFIX}${normalized.replace(".", "-")}`;
}

export function transformPaneRelativePixelValue(value: string): string {
  if (!value || value.includes(SCALED_PIXEL_VARIABLE_PREFIX)) {
    return value;
  }
  return value.replace(
    ABSOLUTE_PIXEL_VALUE_RE,
    (match, prefix: string, size: string) => {
      const pixelValue = Number(size);
      if (!PANE_RELATIVE_PIXEL_VALUE_SET.has(pixelValue)) {
        return match;
      }
      return `${prefix}var(${paneScaledPixelVariableName(pixelValue)}, ${size}px)`;
    }
  );
}

export function isMaterialSymbolSelector(selector: string): boolean {
  return String(selector || "").toLowerCase().includes("material-symbols");
}

function formatCssNumber(value: number): string {
  if (!Number.isFinite(value)) {
    return "0";
  }
  return Number(value.toFixed(4)).toString();
}
