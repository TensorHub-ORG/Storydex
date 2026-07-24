import { isThemeCode } from "@/constants/themes";
import type { ThemeCode } from "@/constants/themes";

export const THEME_CACHE_KEY = "storydex.ui.theme";

export function readCachedThemeCode(): ThemeCode | null {
  if (typeof window === "undefined") {
    return null;
  }

  try {
    const cached = window.localStorage.getItem(THEME_CACHE_KEY);
    return isThemeCode(cached) ? cached : null;
  } catch {
    return null;
  }
}

export function writeCachedThemeCode(theme: ThemeCode): void {
  if (typeof window === "undefined") {
    return;
  }

  try {
    window.localStorage.setItem(THEME_CACHE_KEY, theme);
  } catch {
    // Ignore cache write failures so theme switching continues to work.
  }
}

export function applyThemeSnapshot(theme: ThemeCode | null | undefined): void {
  if (typeof document === "undefined") {
    return;
  }

  const resolvedTheme: ThemeCode = isThemeCode(theme) ? theme : "white";
  if (resolvedTheme === "default") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", resolvedTheme);
  }
  document.documentElement.style.colorScheme = resolvedTheme === "dark" ? "dark" : "light";
}

export function applyCachedThemeSnapshot(): ThemeCode {
  const cachedTheme = readCachedThemeCode() || "white";
  applyThemeSnapshot(cachedTheme);
  return cachedTheme;
}
