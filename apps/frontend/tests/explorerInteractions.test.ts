import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ExplorerSidebar from "@/components/ExplorerSidebar.vue";
import { useGitStore } from "@/stores/git";
import { useWorkspaceStore } from "@/stores/workspace";
import type { WorkspaceTreeNode } from "@/types/workspace";

const fileNode: WorkspaceTreeNode = {
  kind: "file",
  name: "one.md",
  relativePath: "one.md",
  extension: ".md",
  children: []
};

const directoryNode: WorkspaceTreeNode = {
  kind: "directory",
  name: "chapters",
  relativePath: "chapters",
  children: []
};

function mountExplorer() {
  const workspace = useWorkspaceStore();
  workspace.launchScreenVisible = false;
  workspace.currentProject = {
    projectName: "Demo",
    workspaceRoot: "C:/story",
    openedAt: ""
  } as never;
  workspace.tree = [fileNode, directoryNode];
  workspace.activeFile = "one.md";
  vi.spyOn(workspace, "openFile").mockResolvedValue(undefined);

  const git = useGitStore();
  vi.spyOn(git, "refreshSummary").mockResolvedValue(undefined);

  const wrapper = mount(ExplorerSidebar, { attachTo: document.body });
  return {
    wrapper,
    workspace,
    utils: (wrapper.vm as any).__testUtils as Record<string, any>
  };
}

describe("ExplorerSidebar creation and selection invariants", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("keeps a nested directory editor visible and focused after a create error", async () => {
    const { wrapper, workspace, utils } = mountExplorer();
    vi.spyOn(workspace, "createDirectory").mockImplementation(async () => {
      workspace.workspaceError = "目录已存在";
      throw new Error("目录已存在");
    });

    utils.startCreate("directory", directoryNode);
    await nextTick();
    const input = wrapper.find<HTMLInputElement>(".tree-inline-create-input");
    await input.setValue("existing");
    await input.trigger("keydown", { key: "Enter" });
    await flushPromises();

    const retained = wrapper.find<HTMLInputElement>(".tree-inline-create-input");
    expect(retained.exists()).toBe(true);
    expect(retained.element.value).toBe("existing");
    expect(document.activeElement).toBe(retained.element);
    expect(retained.element.selectionStart).toBe(0);
    expect(retained.element.selectionEnd).toBe("existing".length);
    expect(wrapper.find(".tree-error").text()).toContain("目录已存在");
    expect(wrapper.findAll("button.tree-row")).toHaveLength(2);

    wrapper.unmount();
  });

  it("prevents duplicate submissions and selects a directory after creation", async () => {
    const { wrapper, workspace, utils } = mountExplorer();
    let finishCreate: (() => void) | undefined;
    const pending = new Promise<void>((resolve) => {
      finishCreate = resolve;
    });
    const create = vi.spyOn(workspace, "createDirectory").mockImplementation(async () => {
      await pending;
      return { relativePath: "新目录", kind: "directory", exists: true } as never;
    });

    utils.startCreate("directory", null);
    await nextTick();
    await wrapper.find<HTMLInputElement>(".tree-inline-create-input").setValue("新目录");
    const first = utils.submitPendingCreate();
    const duplicate = utils.submitPendingCreate();

    expect(create).toHaveBeenCalledTimes(1);
    expect(utils.pendingCreateSubmitting.value).toBe(true);
    finishCreate?.();
    await Promise.all([first, duplicate]);
    await nextTick();

    expect(utils.pendingCreate.value).toBeNull();
    expect(utils.pendingCreateSubmitting.value).toBe(false);
    expect([...utils.selectedPaths.value]).toEqual(["新目录"]);
    expect(utils.selectionAnchor.value).toBe("新目录");

    wrapper.unmount();
  });

  it("keeps single-click selection exclusive and prunes removed nodes", async () => {
    const { wrapper, workspace, utils } = mountExplorer();
    await nextTick();
    const rows = wrapper.findAll("button.tree-row");

    await rows[0].trigger("click");
    await rows[1].trigger("click");
    expect(wrapper.findAll("button.tree-row.is-selected")).toHaveLength(1);
    expect(rows[1].classes()).toContain("is-selected");
    expect(rows[0].classes()).not.toContain("active");

    await rows[0].trigger("click", { ctrlKey: true });
    expect(wrapper.findAll("button.tree-row.is-selected")).toHaveLength(2);

    await rows[0].trigger("click");
    expect(wrapper.findAll("button.tree-row.is-selected")).toHaveLength(1);
    expect([...utils.selectedPaths.value]).toEqual(["one.md"]);

    await rows[1].trigger("click");
    workspace.tree = [fileNode];
    await nextTick();
    expect(utils.selectedPaths.value.size).toBe(0);
    expect(wrapper.findAll("button.tree-row.active")).toHaveLength(1);

    workspace.treeResetToken += 1;
    await nextTick();
    expect(utils.selectionAnchor.value).toBe("");

    wrapper.unmount();
  });
});
