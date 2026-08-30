import { shallowMount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { describe, expect, it, vi } from "vitest";

import TopHeader from "@/components/TopHeader.vue";

describe("TopHeader", () => {
  it("keeps the workspace menus without duplicating the native title-bar brand", () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const wrapper = shallowMount(TopHeader, {
      global: { plugins: [pinia] }
    });

    expect(wrapper.find(".topbar-brand").exists()).toBe(false);
    expect(wrapper.findAll(".file-menu-trigger")).toHaveLength(2);

    wrapper.unmount();
  });

  it("integrates Tauri window controls into the application header", async () => {
    const minimizeMainWindow = vi.fn().mockResolvedValue(undefined);
    const toggleMainWindowMaximized = vi.fn().mockResolvedValue(true);
    const closeMainWindow = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window, "storydexDesktop", {
      configurable: true,
      value: {
        platform: "win32",
        versions: { tauri: "2.0.5" },
        minimizeMainWindow,
        toggleMainWindowMaximized,
        isMainWindowMaximized: vi.fn().mockResolvedValue(false),
        closeMainWindow
      } satisfies StorydexDesktopBridge
    });
    const pinia = createPinia();
    setActivePinia(pinia);
    const wrapper = shallowMount(TopHeader, { global: { plugins: [pinia] } });

    expect(wrapper.find(".topbar-brand-name").text()).toBe("Storydex");
    const controls = wrapper.findAll(".window-control-btn");
    expect(controls).toHaveLength(3);
    await controls[0].trigger("click");
    await controls[1].trigger("click");
    await controls[2].trigger("click");
    expect(minimizeMainWindow).toHaveBeenCalledTimes(1);
    expect(toggleMainWindowMaximized).toHaveBeenCalledTimes(1);
    expect(closeMainWindow).toHaveBeenCalledTimes(1);
    expect(controls[1].attributes("aria-label")).toBe("还原窗口");

    wrapper.unmount();
    Object.defineProperty(window, "storydexDesktop", { configurable: true, value: undefined });
  });
});
