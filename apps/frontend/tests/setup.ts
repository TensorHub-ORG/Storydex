import { afterEach, vi } from "vitest";

Object.defineProperty(window, "confirm", {
  configurable: true,
  writable: true,
  value: vi.fn(() => false)
});

afterEach(() => {
  document.body.innerHTML = "";
  document.documentElement.className = "";
  vi.clearAllMocks();
  vi.useRealTimers();
});

