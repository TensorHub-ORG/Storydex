import { describe, expect, it } from "vitest";
import { computeForceLayout } from "@/utils/forceLayout";
import {
  buildWikiEdgeGeometry,
  computeWikiGraphLayout,
  estimateWikiTextWidth,
  placeWikiEdgeLabels,
  placeWikiNodeLabels,
  pointOnWikiEdge,
  wikiEdgeBendOffsets,
} from "@/utils/wikiGraphLayout";

const chapter = (index: number) => ({ id: `chapter:${index}`, radius: 14, tone: "plot", narrativeOrder: index });

describe("knowledge graph band layout", () => {
  it("orders chapters on a narrative spine and shelves unconnected entries below the network", () => {
    const nodes = [
      { id: "lin", radius: 18, tone: "character" },
      { id: "xue", radius: 16, tone: "character" },
      chapter(3), chapter(1), chapter(2),
      { id: "lonely-setting", radius: 13, tone: "setting" },
      { id: "lonely-character", radius: 13, tone: "character" },
    ];
    const edges = [
      { source: "lin", target: "xue" },
      { source: "chapter:1", target: "chapter:2" },
      { source: "chapter:2", target: "chapter:3" },
    ];
    const layout = computeWikiGraphLayout(nodes, edges, { width: 960, height: 640 });

    expect(layout.bands).toMatchObject({
      lin: "network",
      xue: "network",
      "chapter:1": "spine",
      "chapter:2": "spine",
      "chapter:3": "spine",
      "lonely-setting": "shelf",
      "lonely-character": "shelf",
    });
    const p = layout.positions;
    // 主线按叙事序号从左到右，同一行。
    expect(p["chapter:1"].x).toBeLessThan(p["chapter:2"].x);
    expect(p["chapter:2"].x).toBeLessThan(p["chapter:3"].x);
    expect(p["chapter:1"].y).toBe(p["chapter:3"].y);
    // 三段自上而下：关系网 → 主线 → 独立条目。
    expect(Math.max(p.lin.y, p.xue.y)).toBeLessThan(p["chapter:1"].y);
    expect(p["chapter:1"].y).toBeLessThan(p["lonely-setting"].y);
    // 独立条目按分类色排序：角色在设定之前。
    expect(p["lonely-character"].x).toBeLessThan(p["lonely-setting"].x);
    expect(layout.captions.map((caption) => [caption.band, caption.count])).toEqual([["spine", 3], ["shelf", 2]]);
  });

  it("wraps a long spine into serpentine rows so consecutive chapters stay adjacent", () => {
    const nodes = Array.from({ length: 6 }, (_, index) => chapter(index + 1));
    const edges = nodes.slice(1).map((node, index) => ({ source: nodes[index].id, target: node.id }));
    const { positions } = computeWikiGraphLayout(nodes, edges, { width: 420, height: 600 });
    const rows = [...new Set(nodes.map((node) => positions[node.id].y))];
    expect(rows.length).toBeGreaterThan(1);
    // 第二行反向排列：上一行末尾与下一行开头在同一列。
    const perRow = nodes.filter((node) => positions[node.id].y === rows[0]).length;
    expect(positions[nodes[perRow - 1].id].x).toBeCloseTo(positions[nodes[perRow].id].x, 5);
  });

  it("is deterministic and keeps every coordinate finite", () => {
    const nodes = [
      { id: "a", radius: 16, tone: "character" },
      { id: "b", radius: 14, tone: "character" },
      { id: "c", radius: 14, tone: "setting" },
      { id: "d", radius: 13, tone: "misc" },
    ];
    const edges = [{ source: "a", target: "b" }, { source: "b", target: "c" }, { source: "a", target: "missing" }];
    const first = computeWikiGraphLayout(nodes, edges, { width: 800, height: 520 });
    const second = computeWikiGraphLayout(nodes, edges, { width: 800, height: 520 });
    expect(second).toEqual(first);
    expect(Object.values(first.positions).every(({ x, y }) => Number.isFinite(x) && Number.isFinite(y))).toBe(true);
    expect(computeWikiGraphLayout([], [], { width: 800, height: 520 })).toEqual({ positions: {}, bands: {}, captions: [] });
  });
});

describe("knowledge graph edge geometry and labels", () => {
  it("trims edges to the node rims and draws an arrow at the target", () => {
    const geometry = buildWikiEdgeGeometry({ x: 0, y: 0, radius: 10 }, { x: 100, y: 0, radius: 10 }, 0, { gap: 3, arrow: 7 });
    expect(geometry.pathD).toBe("M 13 0 L 80 0");
    expect(geometry.arrowD.startsWith("M 87 0")).toBe(true);
    expect(pointOnWikiEdge(geometry, 0.5)).toEqual({ x: 50, y: 0 });
    expect(buildWikiEdgeGeometry({ x: 5, y: 5, radius: 4 }, { x: 5, y: 5, radius: 4 }, 0).pathD).toBe("");
  });

  it("bends parallel edges to opposite sides regardless of direction", () => {
    const bends = wikiEdgeBendOffsets([
      { source: "a", target: "b" },
      { source: "b", target: "a" },
      { source: "a", target: "c" },
    ]);
    expect(bends[2]).toBe(0);
    const curveA = buildWikiEdgeGeometry({ x: 0, y: 0, radius: 8 }, { x: 100, y: 0, radius: 8 }, bends[0]);
    const curveB = buildWikiEdgeGeometry({ x: 100, y: 0, radius: 8 }, { x: 0, y: 0, radius: 8 }, bends[1]);
    expect(Math.sign(curveA.control.y)).toBe(-Math.sign(curveB.control.y));
    expect(curveA.control.y).not.toBe(0);
  });

  it("moves a colliding node label off its default position", () => {
    const labels = placeWikiNodeLabels([
      { id: "top", x: 100, y: 100, radius: 12, width: 60, height: 15, priority: 10 },
      { id: "below", x: 100, y: 140, radius: 12, width: 60, height: 15, priority: 1 },
    ]);
    // 「top」下方会压到「below」的圆点，标签改到上方；「below」下方空闲，保持默认。
    expect(labels.top.dy).toBeLessThan(0);
    expect(labels.below.dy).toBeGreaterThan(0);
    const a = labels.top.rect;
    const b = labels.below.rect;
    const overlap = Math.max(0, Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x))
      * Math.max(0, Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y));
    expect(overlap).toBe(0);
  });

  it("slides edge labels along the edge to avoid each other", () => {
    const placements = placeWikiEdgeLabels([
      { id: "one", width: 40, height: 18, candidates: [{ x: 50, y: 50 }, { x: 30, y: 50 }] },
      { id: "two", width: 40, height: 18, candidates: [{ x: 50, y: 50 }, { x: 95, y: 50 }] },
    ], []);
    expect(placements.one).toEqual({ x: 50, y: 50 });
    expect(placements.two).toEqual({ x: 95, y: 50 });
  });

  it("estimates CJK labels wider than Latin labels of the same length", () => {
    expect(estimateWikiTextWidth("林晚", 12)).toBe(24);
    expect(estimateWikiTextWidth("ab", 12)).toBeLessThan(24);
  });
});

describe("force layout fixed anchors", () => {
  it("keeps fixed anchors in place while pulling free nodes toward them", () => {
    const layout = computeForceLayout(
      [{ id: "anchor", x: 300, y: 400, fixed: true, radius: 10 }, { id: "free", radius: 10 }],
      [{ source: "anchor", target: "free" }],
      { width: 600, height: 300, idealEdgeLength: 80, padding: 0 },
    );
    expect(layout.anchor).toEqual({ x: 300, y: 400 });
    expect(layout.free.y).toBeLessThanOrEqual(300);
    expect(Math.abs(layout.free.x - 300)).toBeLessThan(200);
  });
});
