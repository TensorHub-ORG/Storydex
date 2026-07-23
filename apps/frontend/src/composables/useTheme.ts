import type { ThemeCode } from "@/constants/themes";
import { paneFontScaleStyle } from "@/utils/paneFontScale";

export function useTheme() {
  function applyTheme(theme: ThemeCode): void {
    if (theme === "default") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", theme);
    }

    document.documentElement.style.colorScheme = theme === "dark" ? "dark" : "light";
    syncDesktopTitleBar();
  }

  function applyPaneFontScale(fontScale?: number): void {
    for (const [property, value] of Object.entries(paneFontScaleStyle(fontScale))) {
      document.documentElement.style.setProperty(property, value);
    }
  }

  return {
    applyTheme,
    applyPaneFontScale
  };
}

function syncDesktopTitleBar(): void {
  if (!window.storydexDesktop?.setTitleBarTheme) {
    return;
  }

  const computedStyle = window.getComputedStyle(document.documentElement);
  const headerColor = resolveOpaqueColor(
    computedStyle.getPropertyValue("--bg-header"),
    computedStyle.getPropertyValue("--bg-app")
  );
  const symbolColor = getContrastColor(headerColor);

  void window.storydexDesktop.setTitleBarTheme({
    color: headerColor,
    symbolColor
  });
}

function resolveOpaqueColor(primaryColor: string, fallbackColor: string): string {
  const primary = parseCssColor(primaryColor);
  const fallback = parseCssColor(fallbackColor);

  if (!primary && !fallback) {
    return "#f5f7fb";
  }

  if (!primary) {
    return toHex(fallback);
  }

  if (!fallback || primary.a >= 1) {
    return toHex(primary);
  }

  const alpha = primary.a;
  return toHex({
    r: Math.round(primary.r * alpha + fallback.r * (1 - alpha)),
    g: Math.round(primary.g * alpha + fallback.g * (1 - alpha)),
    b: Math.round(primary.b * alpha + fallback.b * (1 - alpha)),
    a: 1
  });
}

function getContrastColor(backgroundHex: string): string {
  const background = parseCssColor(backgroundHex);
  if (!background) {
    return "#162030";
  }

  const channels = [background.r, background.g, background.b].map((channel) => {
    const normalized = channel / 255;
    return normalized <= 0.03928 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
  });
  const luminance = 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  return luminance > 0.5 ? "#162030" : "#f8fafc";
}

function parseCssColor(value: string): { r: number; g: number; b: number; a: number } | null {
  const normalized = String(value || "").trim();
  if (!normalized) {
    return null;
  }

  const hexMatch = normalized.match(/^#([\da-f]{3,8})$/i);
  if (hexMatch) {
    const hex = hexMatch[1];
    if (hex.length === 3 || hex.length === 4) {
      const channels = hex.split("").map((channel) => parseInt(channel + channel, 16));
      return {
        r: channels[0],
        g: channels[1],
        b: channels[2],
        a: typeof channels[3] === "number" ? channels[3] / 255 : 1
      };
    }

    if (hex.length === 6 || hex.length === 8) {
      return {
        r: parseInt(hex.slice(0, 2), 16),
        g: parseInt(hex.slice(2, 4), 16),
        b: parseInt(hex.slice(4, 6), 16),
        a: hex.length === 8 ? parseInt(hex.slice(6, 8), 16) / 255 : 1
      };
    }
  }

  const rgbMatch = normalized.match(/^rgba?\((.+)\)$/i);
  if (!rgbMatch) {
    return null;
  }

  const channels = rgbMatch[1].split(",").map((segment) => segment.trim());
  if (channels.length < 3) {
    return null;
  }

  const [r, g, b] = channels.slice(0, 3).map((channel) => parseNumber(channel));
  const alpha = channels[3] === undefined ? 1 : parseNumber(channels[3]);
  if ([r, g, b, alpha].some((channel) => Number.isNaN(channel))) {
    return null;
  }

  return {
    r: clampChannel(r),
    g: clampChannel(g),
    b: clampChannel(b),
    a: clampAlpha(alpha)
  };
}

function parseNumber(value: string): number {
  if (value.endsWith("%")) {
    return (Number.parseFloat(value) / 100) * 255;
  }
  return Number.parseFloat(value);
}

function clampChannel(value: number): number {
  return Math.max(0, Math.min(255, Math.round(value)));
}

function clampAlpha(value: number): number {
  return Math.max(0, Math.min(1, value));
}

function toHex(color: { r: number; g: number; b: number; a?: number } | null): string {
  if (!color) {
    return "#f5f7fb";
  }

  return `#${[color.r, color.g, color.b]
    .map((channel) => clampChannel(channel).toString(16).padStart(2, "0"))
    .join("")}`;
}
