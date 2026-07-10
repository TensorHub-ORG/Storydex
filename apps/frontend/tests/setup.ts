import { afterEach, vi } from "vitest";

afterEach(() => {
  document.body.innerHTML = "";
  document.documentElement.className = "";
  vi.clearAllMocks();
  vi.useRealTimers();
});

