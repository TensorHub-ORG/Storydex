import { mount } from "@vue/test-utils";
import { expect, it, vi } from "vitest";
import App from "@/App.vue";

const workspace = vi.hoisted(() => ({ draftWritesPending: 0, draftPersistenceError: "" }));
vi.mock("@/stores/workspace", () => ({ useWorkspaceStore: () => workspace }));

it("protects unbacked drafts on reload and removes the guard when unmounted", () => {
  const wrapper = mount(App, { global: { stubs: { RouterView: true } } });
  const prevented = () => {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  };
  expect(prevented()).toBe(false);
  workspace.draftWritesPending = 1;
  expect(prevented()).toBe(true);
  workspace.draftWritesPending = 0;
  workspace.draftPersistenceError = "backup failed";
  expect(prevented()).toBe(true);
  workspace.draftPersistenceError = "";
  expect(prevented()).toBe(false);
  wrapper.unmount();
  workspace.draftWritesPending = 1;
  expect(prevented()).toBe(false);
  workspace.draftWritesPending = 0;
});
