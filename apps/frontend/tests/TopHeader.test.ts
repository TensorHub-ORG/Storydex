import { shallowMount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { describe, expect, it } from "vitest";

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
});
