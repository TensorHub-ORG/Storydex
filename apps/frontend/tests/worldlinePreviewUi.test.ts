import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import EditorPane from "@/components/EditorPane.vue";
import WorldlineMapPane from "@/components/WorldlineMapPane.vue";
import { useGitStore } from "@/stores/git";
import { useWorkspaceStore } from "@/stores/workspace";

function setDirtyWorldlineState(): ReturnType<typeof useGitStore> {
  const gitStore = useGitStore();
  gitStore.summary = {
    available: true,
    gitInstalled: true,
    initialized: true,
    branch: "develop",
    clean: false,
    changedFiles: [
      { status: " M", relativePath: "chapters/001.md", staged: false, unstaged: true }
    ],
    recentCommits: [],
    graphLines: [],
    defaultBranch: "develop",
    message: ""
  } as never;
  gitStore.refreshSummary = vi.fn().mockResolvedValue(undefined);
  gitStore.refreshBranches = vi.fn().mockResolvedValue(undefined);
  gitStore.refreshTimeline = vi.fn().mockResolvedValue(undefined);
  return gitStore;
}

describe("时空线主区预览", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("把必要状态并入唯一标题栏并移除虚拟文件元信息", async () => {
    const workspaceStore = useWorkspaceStore();
    workspaceStore.launchScreenVisible = false;
    await workspaceStore.openWorldlineMapDocument();
    const gitStore = setDirtyWorldlineState();

    const wrapper = mount(EditorPane, {
      global: {
        stubs: {
          teleport: true,
          WorldlineMapPane: { template: '<div class="worldline-map-stub" />' },
          WelcomeStartPage: true,
          GitReviewPane: true,
          LargeFileViewer: true
        }
      }
    });
    await nextTick();

    const header = wrapper.find(".editor-pane-head");
    expect(header.text()).toContain("时空线");
    expect(header.text()).toContain("develop");
    expect(header.text()).toContain("1 个文件有更改");
    expect(header.text()).not.toContain("0 字节");
    expect(header.find(".editor-pane-subtitle").exists()).toBe(false);
    expect(header.find('[aria-label="在文件中查找"]').exists()).toBe(false);
    expect(header.find(".editor-mode-switch").exists()).toBe(false);

    await header.find('[aria-label="刷新时空线"]').trigger("click");
    expect(gitStore.refreshSummary).toHaveBeenCalledWith({ force: true });
    expect(gitStore.refreshBranches).toHaveBeenCalledTimes(1);
    expect(gitStore.refreshTimeline).toHaveBeenCalledWith({ force: true });
    wrapper.unmount();
  });

  it("图谱容器不再重复渲染标题、说明和第二层状态框", async () => {
    const gitStore = setDirtyWorldlineState();
    const wrapper = mount(WorldlineMapPane, {
      global: {
        stubs: {
          TimelineGraph: { template: '<div class="timeline-graph-stub" />' },
          WorldlineDialog: true
        }
      }
    });
    await nextTick();

    expect(wrapper.find(".wlm-header").exists()).toBe(false);
    expect(wrapper.find(".timeline-graph-stub").exists()).toBe(true);
    expect(wrapper.text()).not.toContain("分支只分不合");
    expect(wrapper.text()).not.toContain("没有未提交更改");
    expect(gitStore.refreshSummary).toHaveBeenCalledWith({ force: true });
    expect(gitStore.refreshBranches).toHaveBeenCalledTimes(1);
    expect(gitStore.refreshTimeline).toHaveBeenCalledWith({ force: true });
    wrapper.unmount();
  });
});
