import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount } from "@vue/test-utils";

vi.mock("@/api/workspace", () => ({
  fetchWorkspaceGitTimeline: vi.fn().mockResolvedValue({ data: null, trace: null, audit: [] }),
  fetchWorkspaceGitSummary: vi.fn().mockResolvedValue({ data: null, trace: null, audit: [] }),
  fetchWorkspaceGitBranches: vi.fn().mockResolvedValue({ data: { branches: [] }, trace: null, audit: [] })
}));

import TimelineGraph from "@/components/TimelineGraph.vue";
import type { WorkspaceGitTimelineNode, WorkspaceGitTimelineResponse } from "@/types/workspace";

function node(
  id: string,
  column: number,
  row: number,
  laneBranch: string,
  extra: Partial<WorkspaceGitTimelineNode> = {}
): WorkspaceGitTimelineNode {
  return {
    id,
    shortId: id,
    authorName: "作者",
    authoredAt: "2026-08-04T10:00:00+08:00",
    subject: `节点 ${id}`,
    refs: "",
    parents: [],
    branches: [laneBranch],
    headBranches: [],
    isBranchHead: false,
    isCurrent: false,
    column,
    row,
    laneBranch,
    ...extra
  };
}

/**
 * 两条世界线的最小树：develop 上 c1→c2→c3，alt/dark 从 c1 分出去写了 a1。
 *
 * column 是拓扑深度，所以 a1 和 c2 都在第 1 列——分叉点必须垂直对齐。这是整个
 * 重构的核心不变量：列数取决于最长世界线的长度，而不是提交总数。
 */
function buildTimeline(): WorkspaceGitTimelineResponse {
  return {
    available: true,
    gitInstalled: true,
    initialized: true,
    currentBranch: "develop",
    currentHead: null,
    detached: false,
    branches: [
      { name: "develop", head: "c3", isCurrent: true, lane: 0, forkColumn: 0, tipColumn: 2, commitCount: 3, totalCount: 3 },
      { name: "alt/dark", head: "a1", isCurrent: false, lane: 1, forkColumn: 1, tipColumn: 1, commitCount: 1, totalCount: 2 }
    ],
    nodes: [
      node("c1", 0, 0, "develop", { branches: ["develop", "alt/dark"] }),
      node("c2", 1, 0, "develop"),
      node("a1", 1, 1, "alt/dark", { isBranchHead: true, headBranches: ["alt/dark"] }),
      node("c3", 2, 0, "develop", { isBranchHead: true, headBranches: ["develop"], isCurrent: true })
    ],
    edges: [
      { from: "c1", to: "c2" },
      { from: "c1", to: "a1" },
      { from: "c2", to: "c3" }
    ],
    message: ""
  };
}

function mountGraph(props: Record<string, unknown> = {}) {
  return mount(TimelineGraph, {
    props: { timeline: buildTimeline(), ...props }
  });
}

type Graph = ReturnType<typeof mountGraph>;

function utilsOf(wrapper: Graph): Record<string, unknown> {
  return (wrapper.vm as unknown as { __testUtils: Record<string, unknown> }).__testUtils;
}

/**
 * defineExpose 不会解包嵌套对象里的 ref/computed，所以 __testUtils 上拿到的是
 * ref 本身。统一在这里 .value 一次，测试正文只跟普通值打交道。
 */
function peek<T>(wrapper: Graph, key: string): T {
  const entry = utilsOf(wrapper)[key] as { value?: unknown } | undefined;
  return (entry && typeof entry === "object" && "value" in entry ? entry.value : entry) as T;
}

function fn<T>(wrapper: Graph, key: string): T {
  return utilsOf(wrapper)[key] as T;
}

describe("平行时空线树状图", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it("按拓扑深度把 column/row 映射成像素坐标，分叉点垂直对齐", () => {
    const wrapper = mountGraph();
    const laid = peek<Array<{ id: string; x: number; y: number }>>(wrapper, "laidOutNodes");
    const byId = new Map(laid.map((item) => [item.id, item]));

    // c2 与 a1 同为第 1 列：一个在母线上，一个是分出去的新世界线，x 必须相同。
    expect(byId.get("c2")!.x).toBe(byId.get("a1")!.x);
    // 但它们在不同轨道上，y 必须不同。
    expect(byId.get("c2")!.y).not.toBe(byId.get("a1")!.y);
    // 最右 = 拓扑最深 = 该线最新节点。
    expect(byId.get("c3")!.x).toBeGreaterThan(byId.get("c2")!.x);
    wrapper.unmount();
  });

  it("分叉边走曲线，同线边走直线", () => {
    const wrapper = mountGraph();
    const edges = peek<Array<{ key: string; path: string; isFork: boolean }>>(wrapper, "edgePaths");

    const sameLine = edges.find((edge) => edge.key === "c1-c2")!;
    const fork = edges.find((edge) => edge.key === "c1-a1")!;
    expect(sameLine.isFork).toBe(false);
    expect(sameLine.path).toContain(" L ");
    expect(fork.isFork).toBe(true);
    expect(fork.path).toContain(" C ");
    wrapper.unmount();
  });

  it("工作区有改动时在当前节点右侧画幽灵节点，干净时不画", () => {
    const dirty = mountGraph({ dirty: true });
    expect(peek(dirty, "ghost")).not.toBeNull();
    dirty.unmount();

    const clean = mountGraph({ dirty: false });
    expect(peek(clean, "ghost")).toBeNull();
    clean.unmount();
  });

  it("点击节点钉住动作浮层，再点一次收起", () => {
    const wrapper = mountGraph();
    const laid = peek<Array<{ id: string }>>(wrapper, "laidOutNodes");
    const target = laid.find((item) => item.id === "c1")!;
    const onNodeClick = fn<(node: unknown) => void>(wrapper, "onNodeClick");

    onNodeClick(target);
    expect(peek(wrapper, "pinnedId")).toBe("c1");

    onNodeClick(target);
    expect(peek(wrapper, "pinnedId")).toBeNull();
    wrapper.unmount();
  });

  it("节点动作向上抛出事件，并且关掉浮层", () => {
    const wrapper = mountGraph();
    const act = fn<(action: string, payload: string) => void>(wrapper, "act");

    act("jump", "c1");
    act("fork", "c2");
    act("inspect", "a1");
    act("renameWorldline", "alt/dark");
    act("deleteWorldline", "alt/dark");

    expect(wrapper.emitted("jump")?.[0]).toEqual(["c1"]);
    expect(wrapper.emitted("fork")?.[0]).toEqual(["c2"]);
    expect(wrapper.emitted("inspect")?.[0]).toEqual(["a1"]);
    expect(wrapper.emitted("renameWorldline")?.[0]).toEqual(["alt/dark"]);
    expect(wrapper.emitted("deleteWorldline")?.[0]).toEqual(["alt/dark"]);
    expect(peek(wrapper, "pinnedId")).toBeNull();
    wrapper.unmount();
  });

  it("跳转文案区分「切换世界线」与「进入观测态」", () => {
    const wrapper = mountGraph();
    const label = fn<(node: unknown) => string>(wrapper, "jumpActionLabel");
    const laid = peek<Array<{ id: string }>>(wrapper, "laidOutNodes");

    // a1 是 alt/dark 的最新节点 → 切换世界线
    expect(label(laid.find((item) => item.id === "a1"))).toContain("切换");
    // c1 是历史中间节点 → 观测态
    expect(label(laid.find((item) => item.id === "c1"))).toContain("观测");
    wrapper.unmount();
  });

  it("当前世界线不能删除，其它线要说明会丢多少版本", () => {
    const wrapper = mountGraph();
    const tracks = peek<Array<{ name: string; isCurrent: boolean }>>(wrapper, "laneTracks");
    const title = fn<(lane: unknown) => string>(wrapper, "deleteLaneTitle");

    expect(title(tracks.find((lane) => lane.isCurrent))).toContain("不能删除");
    expect(title(tracks.find((lane) => !lane.isCurrent))).toContain("永久");
    wrapper.unmount();
  });

  it("缩放被夹在合法区间内", () => {
    const wrapper = mountGraph();
    const zoomBy = fn<(delta: number) => void>(wrapper, "zoomBy");

    for (let i = 0; i < 40; i += 1) zoomBy(1);
    expect(peek<number>(wrapper, "zoom")).toBeLessThanOrEqual(2.4);
    for (let i = 0; i < 80; i += 1) zoomBy(-1);
    expect(peek<number>(wrapper, "zoom")).toBeGreaterThanOrEqual(0.4);
    wrapper.unmount();
  });

  it("空时空线显示对应的引导文案", () => {
    const empty = mount(TimelineGraph, {
      props: { timeline: { ...buildTimeline(), nodes: [], branches: [] } }
    });
    expect(empty.text()).toContain("还没有任何版本节点");
    empty.unmount();

    const uninitialized = mount(TimelineGraph, {
      props: { timeline: { ...buildTimeline(), nodes: [], branches: [], initialized: false } }
    });
    expect(uninitialized.text()).toContain("还没有启用版本记录");
    uninitialized.unmount();
  });
});
