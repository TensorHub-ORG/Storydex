import { afterEach, beforeEach, vi } from "vitest";
import { IDBFactory } from "fake-indexeddb";

beforeEach(() => { vi.stubGlobal("indexedDB", new IDBFactory()); });

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

