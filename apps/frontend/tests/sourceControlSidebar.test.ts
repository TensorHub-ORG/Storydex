import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount } from "@vue/test-utils";

import SourceControlSidebar from "@/components/SourceControlSidebar.vue";
import { useGitStore } from "@/stores/git";
import { useWorkspaceStore } from "@/stores/workspace";

function initializedSummary() {
  return {
    available: true,
    gitInstalled: true,
    initialized: true,
    branch: "develop",
    clean: true,
    changedFiles: [],
    recentCommits: [],
    graphLines: [],
    defaultBranch: "develop",
    message: ""
  };
}

function mountSidebar() {
  const workspaceStore = useWorkspaceStore();
  workspaceStore.launchScreenVisible = false;
  workspaceStore.projectLabel = "测试故事";
  workspaceStore.reloadProjectContext = vi.fn().mockResolvedValue(undefined);

  const gitStore = useGitStore();
  gitStore.summary = initializedSummary();
  gitStore.timeline = {
    available: true,
    gitInstalled: true,
    initialized: true,
    currentBranch: "develop",
    currentHead: null,
    detached: false,
    branches: [],
    nodes: [],
    edges: [],
    message: ""
  };
  gitStore.branches = [
    { name: "develop", current: true },
    { name: "ending/alternate", current: false }
  ];
  gitStore.refreshSummary = vi.fn().mockResolvedValue(undefined);
  gitStore.refreshBranches = vi.fn().mockResolvedValue(undefined);
  gitStore.refreshTimeline = vi.fn().mockResolvedValue(undefined);

  return { wrapper: mount(SourceControlSidebar), gitStore, workspaceStore };
}

function utilsOf(wrapper: ReturnType<typeof mountSidebar>["wrapper"]): Record<string, any> {
  return (wrapper.vm as unknown as { __testUtils: Record<string, any> }).__testUtils;
}

describe("时空线侧栏", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("显示正确模块名、更改标题和分支下拉", async () => {
    const { wrapper } = mountSidebar();
    expect(wrapper.text()).toContain("时空线");
    expect(wrapper.text()).toContain("更改");
    expect(wrapper.text()).not.toContain("平行时空线");
    expect(wrapper.text()).not.toContain("还没进版本的改动");

    await wrapper.get(".scm-branch-trigger").trigger("click");
    expect(wrapper.text()).toContain("develop");
    expect(wrapper.text()).toContain("ending/alternate");
    expect(wrapper.text()).toContain("创建新时空线");
    wrapper.unmount();
  });

  it("可创建新时空线并在成功后关闭菜单", async () => {
    const { wrapper, gitStore } = mountSidebar();
    gitStore.createBranch = vi.fn().mockResolvedValue(true);
    const utils = utilsOf(wrapper);
    utils.branchMenuOpen.value = true;
    utils.newBranchName.value = "ending/new";

    await utils.createWorldline();

    expect(gitStore.createBranch).toHaveBeenCalledWith("ending/new");
    expect(utils.branchMenuOpen.value).toBe(false);
    wrapper.unmount();
  });

  it("切换时空线后刷新项目上下文，有未提交更改时禁止切换", async () => {
    const { wrapper, gitStore, workspaceStore } = mountSidebar();
    gitStore.switchBranch = vi.fn().mockResolvedValue(true);
    const utils = utilsOf(wrapper);

    await utils.switchWorldline("ending/alternate");
    expect(gitStore.switchBranch).toHaveBeenCalledWith("ending/alternate");
    expect(workspaceStore.reloadProjectContext).toHaveBeenCalledOnce();

    gitStore.summary = {
      ...initializedSummary(),
      clean: false,
      changedFiles: [{ status: " M", relativePath: "chapters/001.md", staged: false, unstaged: true }]
    };
    await utils.switchWorldline("ending/alternate");
    expect(gitStore.switchBranch).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });

  it("在请求前校验时空线名称", () => {
    const { wrapper } = mountSidebar();
    const validate = utilsOf(wrapper).validateBranchName as (name: string) => string;
    expect(validate("")).toContain("请输入");
    expect(validate("中文名")).toContain("仅支持");
    expect(validate("develop")).toContain("已存在");
    expect(validate("ending/good")).toBe("");
    wrapper.unmount();
  });
});
