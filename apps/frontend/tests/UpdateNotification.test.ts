import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import UpdateNotification from "@/components/UpdateNotification.vue";

function updaterState(patch: Partial<StorydexDesktopUpdaterState> = {}): StorydexDesktopUpdaterState {
  return {
    supported: true,
    status: "idle",
    currentVersion: "1.0.0",
    availableVersion: "",
    releaseNotes: "",
    progress: null,
    error: "",
    feedUrl: "https://updates.example.test/",
    ...patch
  };
}

function installBridge(updater: StorydexDesktopUpdaterBridge): void {
  Object.defineProperty(window, "storydexDesktop", {
    configurable: true,
    value: {
      platform: "win32",
      versions: { electron: "", chrome: "", node: "" },
      updater
    } satisfies StorydexDesktopBridge
  });
}

describe("UpdateNotification", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(window, "storydexDesktop", { configurable: true, value: undefined });
  });

  afterEach(() => {
    Object.defineProperty(window, "storydexDesktop", { configurable: true, value: undefined });
  });

  it("automatically checks, shows a new version, and installs it with one click", async () => {
    const detach = vi.fn();
    const check = vi.fn().mockResolvedValue(updaterState({ status: "available", availableVersion: "1.2.0" }));
    const download = vi.fn().mockResolvedValue(updaterState({ status: "downloaded", availableVersion: "1.2.0" }));
    const install = vi.fn().mockResolvedValue(true);
    installBridge({
      getState: vi.fn().mockResolvedValue(updaterState()),
      check,
      download,
      install,
      onState: vi.fn().mockReturnValue(detach)
    });

    const wrapper = mount(UpdateNotification);
    await flushPromises();

    expect(check).toHaveBeenCalledTimes(1);
    expect(wrapper.get("[data-testid='update-notification']").text()).toContain("发现新版本 v1.2.0");

    await wrapper.get(".update-notification-action").trigger("click");
    await flushPromises();

    expect(download).toHaveBeenCalledTimes(1);
    expect(install).toHaveBeenCalledTimes(1);
    wrapper.unmount();
    expect(detach).toHaveBeenCalledTimes(1);
  });

  it("renders download progress and installs only once when events arrive before download resolves", async () => {
    let emitState: ((state: StorydexDesktopUpdaterState) => void) | null = null;
    let resolveDownload!: (state: StorydexDesktopUpdaterState) => void;
    const download = vi.fn().mockImplementation(() => new Promise<StorydexDesktopUpdaterState>((resolve) => {
      resolveDownload = resolve;
    }));
    const install = vi.fn().mockResolvedValue(true);
    installBridge({
      getState: vi.fn().mockResolvedValue(updaterState({ status: "available", availableVersion: "1.2.0" })),
      check: vi.fn(),
      download,
      install,
      onState: vi.fn((listener) => {
        emitState = listener;
        return vi.fn();
      })
    });

    const wrapper = mount(UpdateNotification);
    await flushPromises();
    await wrapper.get(".update-notification-action").trigger("click");

    emitState?.(updaterState({
      status: "downloading",
      availableVersion: "1.2.0",
      progress: { percent: 25, transferred: 250, total: 1000, bytesPerSecond: 100 }
    }));
    await flushPromises();
    expect(wrapper.get("[role='progressbar']").text()).toContain("25%");

    emitState?.(updaterState({
      status: "downloading",
      availableVersion: "1.2.0",
      progress: { percent: 68, transferred: 680, total: 1000, bytesPerSecond: 120 }
    }));
    await flushPromises();
    expect(wrapper.get("[role='progressbar']").text()).toContain("68%");

    const downloaded = updaterState({ status: "downloaded", availableVersion: "1.2.0" });
    emitState?.(downloaded);
    await flushPromises();
    expect(install).toHaveBeenCalledTimes(1);

    resolveDownload(downloaded);
    await flushPromises();
    expect(download).toHaveBeenCalledTimes(1);
    expect(install).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });

  it("stays closed for the dismissed version and reappears for a newer version", async () => {
    let emitState: ((state: StorydexDesktopUpdaterState) => void) | null = null;
    installBridge({
      getState: vi.fn().mockResolvedValue(updaterState({ status: "available", availableVersion: "1.2.0" })),
      check: vi.fn(),
      download: vi.fn(),
      install: vi.fn(),
      onState: vi.fn((listener) => {
        emitState = listener;
        return vi.fn();
      })
    });

    const wrapper = mount(UpdateNotification);
    await flushPromises();
    expect(wrapper.find("[data-testid='update-notification']").exists()).toBe(true);

    await wrapper.get(".update-notification-close").trigger("click");
    expect(wrapper.find("[data-testid='update-notification']").exists()).toBe(false);

    emitState?.(updaterState({ status: "available", availableVersion: "1.2.0" }));
    await flushPromises();
    expect(wrapper.find("[data-testid='update-notification']").exists()).toBe(false);

    emitState?.(updaterState({ status: "available", availableVersion: "1.3.0" }));
    await flushPromises();
    expect(wrapper.get("[data-testid='update-notification']").text()).toContain("v1.3.0");
    wrapper.unmount();
  });

  it("renders nothing outside the packaged desktop bridge", async () => {
    const wrapper = mount(UpdateNotification);
    await flushPromises();
    expect(wrapper.find("[data-testid='update-notification']").exists()).toBe(false);
    wrapper.unmount();
  });
});
