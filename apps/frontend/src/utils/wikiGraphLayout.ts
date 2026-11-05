/**
 * 知识图谱分区布局与标签避让（纯函数、确定性，不引入 d3 等布局库）。
 *
 * 画面自上而下分三段：
 * - 关系网：有连线的条目，跑力导向布局；
 * - 章节主线：带叙事序号的章节按顺序排成一行，放不下时蛇形折行；
 * - 独立条目：当前视图里没有任何连线的条目，整齐排成网格，不再被斥力推到画布角落。
 *
 * 布局只依赖节点 id、半径、分区与连线，不依赖标签文字，
 * 因此改名或补充证据不会让节点位置跳动。
 */
import { computeForceLayout, type ForceEdge, type ForceNode } from "@/utils/forceLayout";

export interface WikiLayoutNode {
  id: string;
  radius: number;
  /** 视觉分组，用于独立条目区的排序（同色相邻）。 */
  tone: string;
  /** 章节叙事序号；> 0 才参与主线排列。 */
  narrativeOrder?: number;
  seedX?: number;
  seedY?: number;
}

export interface WikiLayoutEdge {
  source: string;
  target: string;
  weight?: number;
}

export type WikiLayoutBand = "network" | "spine" | "shelf";

export interface WikiLayoutCaption {
  band: Exclude<WikiLayoutBand, "network">;
  x: number;
  y: number;
  width: number;
  count: number;
}

export interface WikiLayoutResult {
  positions: Record<string, { x: number; y: number }>;
  bands: Record<string, WikiLayoutBand>;
  captions: WikiLayoutCaption[];
}

export interface WikiLayoutOptions {
  width: number;
  height: number;
}

export const WIKI_LAYOUT = {
  margin: 40,
  /** 关系网理想边长：固定值让疏密与画布尺寸无关，交给「适配视图」统一缩放。 */
  edgeLength: 124,
  /** 节点下方标签的保留高度。 */
  labelReserve: 22,
  bandGap: 56,
  captionSpace: 30,
  /** 主线相邻章节的间距：优先一行排完（不小于 spineMinStep），放不下才按 spineStep 折行。 */
  spineStep: 150,
  spineMinStep: 124,
  spineRowGap: 92,
  shelfCellWidth: 120,
  shelfRowHeight: 74,
} as const;

const TONE_ORDER = ["character", "plot", "setting", "misc", "hub"];

export function computeWikiGraphLayout(
  nodes: WikiLayoutNode[],
  edges: WikiLayoutEdge[],
  options: WikiLayoutOptions,
): WikiLayoutResult {
  const result: WikiLayoutResult = { positions: {}, bands: {}, captions: [] };
  if (!nodes.length) {
    return result;
  }
  const L = WIKI_LAYOUT;
  const width = Math.max(320, options.width);
  const height = Math.max(280, options.height);
  const contentWidth = Math.max(160, width - L.margin * 2);
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const validEdges = edges.filter((edge) => (
    edge.source !== edge.target && byId.has(edge.source) && byId.has(edge.target)
  ));
  const degree = new Map<string, number>();
  validEdges.forEach((edge) => {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
  });

  const ordered = nodes.filter((node) => (node.narrativeOrder ?? 0) > 0);
  const spine = ordered.length >= 2
    ? [...ordered].sort((a, b) => (a.narrativeOrder! - b.narrativeOrder!) || a.id.localeCompare(b.id))
    : [];
  const spineIds = new Set(spine.map((node) => node.id));
  const shelf = nodes
    .map((node, index) => ({ node, index }))
    .filter(({ node }) => !spineIds.has(node.id) && (degree.get(node.id) ?? 0) === 0)
    .sort((a, b) => toneRank(a.node.tone) - toneRank(b.node.tone) || a.index - b.index)
    .map(({ node }) => node);
  const shelfIds = new Set(shelf.map((node) => node.id));
  const network = nodes.filter((node) => !spineIds.has(node.id) && !shelfIds.has(node.id));

  // ---- 各段尺寸 ----
  const spineFitsOneRow = spine.length > 1 && (spine.length - 1) * L.spineMinStep <= contentWidth;
  const spineStep = spineFitsOneRow
    ? Math.min(L.spineStep, contentWidth / (spine.length - 1))
    : L.spineStep;
  const spinePerRow = spineFitsOneRow
    ? spine.length
    : Math.max(1, Math.min(spine.length, Math.floor(contentWidth / L.spineStep) + 1));
  const spineRows = spine.length ? Math.ceil(spine.length / spinePerRow) : 0;
  const spineHeight = spineRows ? (spineRows - 1) * L.spineRowGap : 0;
  const shelfColumns = Math.max(1, Math.min(shelf.length, Math.floor(contentWidth / L.shelfCellWidth)));
  const shelfRows = shelf.length ? Math.ceil(shelf.length / shelfColumns) : 0;
  const shelfHeight = shelfRows ? (shelfRows - 1) * L.shelfRowHeight : 0;
  const reservedBelow = (spine.length ? L.bandGap + L.captionSpace + spineHeight + L.labelReserve : 0)
    + (shelf.length ? L.bandGap + L.captionSpace + shelfHeight + L.labelReserve : 0);

  // 关系网的自然尺寸约为 √n·边长；画布更小时允许超出，最后由「适配视图」缩放。
  const natural = Math.ceil(Math.sqrt(Math.max(1, network.length))) * L.edgeLength * 1.15;
  const networkWidth = network.length ? Math.max(contentWidth, natural) : 0;
  const networkHeight = network.length
    ? Math.max(Math.min(240, natural), height - L.margin * 2 - reservedBelow, natural * 0.72)
    : 0;
  const networkTop = L.margin;
  const networkLeft = width / 2 - networkWidth / 2;

  // ---- 章节主线（暂定位置，关系网落定后整体上移贴合）----
  const spineTop = network.length ? networkTop + networkHeight + L.bandGap + L.captionSpace : L.margin + L.captionSpace;
  const spineSpan = (spinePerRow - 1) * spineStep;
  const spineLeft = width / 2 - spineSpan / 2;
  spine.forEach((node, index) => {
    const row = Math.floor(index / spinePerRow);
    const offset = index % spinePerRow;
    const column = row % 2 === 0 ? offset : spinePerRow - 1 - offset;
    result.positions[node.id] = { x: spineLeft + column * spineStep, y: spineTop + row * L.spineRowGap };
    result.bands[node.id] = "spine";
  });

  // ---- 关系网：在局部坐标系里跑力导向，章节节点作为固定锚点参与 ----
  if (network.length) {
    const frameWidth = networkWidth + L.margin * 2;
    const frameHeight = networkHeight + L.margin * 2;
    const toLocalX = (x: number) => x - networkLeft + L.margin;
    const toLocalY = (y: number) => y - networkTop + L.margin;
    const networkIds = new Set(network.map((node) => node.id));
    const anchorIds = new Set<string>();
    validEdges.forEach((edge) => {
      if (networkIds.has(edge.source) && spineIds.has(edge.target)) anchorIds.add(edge.target);
      if (networkIds.has(edge.target) && spineIds.has(edge.source)) anchorIds.add(edge.source);
    });
    const forceNodes: ForceNode[] = [
      ...network.map((node) => ({
        id: node.id,
        radius: node.radius + 16,
        x: typeof node.seedX === "number" ? toLocalX(node.seedX) : undefined,
        y: typeof node.seedY === "number" ? toLocalY(node.seedY) : undefined,
      })),
      ...[...anchorIds].map((id) => ({
        id,
        radius: byId.get(id)!.radius + 16,
        x: toLocalX(result.positions[id].x),
        y: toLocalY(result.positions[id].y),
        fixed: true,
      })),
    ];
    const forceIds = new Set(forceNodes.map((node) => node.id));
    // 枢纽节点的辐条放长：度数越高，连线引力越弱，关系名有空间围成一圈而不挤在一起。
    const hubWeight = (edge: WikiLayoutEdge) => {
      const busiest = Math.max(degree.get(edge.source) ?? 0, degree.get(edge.target) ?? 0);
      return busiest > 3 ? Math.pow(3 / busiest, 1.5) : 1;
    };
    const forceEdges: ForceEdge[] = validEdges
      .filter((edge) => forceIds.has(edge.source) && forceIds.has(edge.target))
      .filter((edge) => networkIds.has(edge.source) || networkIds.has(edge.target))
      .map((edge) => ({ source: edge.source, target: edge.target, weight: (edge.weight ?? 1) * hubWeight(edge) }));
    const local = computeForceLayout(forceNodes, forceEdges, {
      width: frameWidth,
      height: frameHeight,
      padding: 0,
      safeRect: { left: L.margin, top: L.margin, right: L.margin + networkWidth, bottom: L.margin + networkHeight },
      idealEdgeLength: L.edgeLength,
    });
    network.forEach((node) => {
      const point = local[node.id];
      result.positions[node.id] = point
        ? { x: point.x + networkLeft - L.margin, y: point.y + networkTop - L.margin }
        : { x: width / 2, y: networkTop + networkHeight / 2 };
      result.bands[node.id] = "network";
    });
  }

  // ---- 关系网落定后，把主线与独立条目区整体上移，段间距保持一致 ----
  let networkBottom = network.length
    ? Math.max(...network.map((node) => result.positions[node.id].y + node.radius + L.labelReserve))
    : L.margin - L.bandGap;
  if (spine.length) {
    const desiredTop = networkBottom + L.bandGap + L.captionSpace;
    const delta = desiredTop - spineTop;
    spine.forEach((node) => {
      result.positions[node.id].y += delta;
    });
    result.captions.push(captionFor("spine", desiredTop, spine.length));
    networkBottom = desiredTop + spineHeight + L.labelReserve;
  }
  if (shelf.length) {
    const shelfTop = networkBottom + L.bandGap + L.captionSpace;
    const gridWidth = (shelfColumns - 1) * L.shelfCellWidth;
    const gridLeft = width / 2 - gridWidth / 2;
    shelf.forEach((node, index) => {
      const row = Math.floor(index / shelfColumns);
      const column = index % shelfColumns;
      result.positions[node.id] = { x: gridLeft + column * L.shelfCellWidth, y: shelfTop + row * L.shelfRowHeight };
      result.bands[node.id] = "shelf";
    });
    result.captions.push(captionFor("shelf", shelfTop, shelf.length));
  }

  // 分区标题统一左对齐到内容的最左缘，宽度覆盖整张图。
  if (result.captions.length) {
    const xs = nodes.map((node) => result.positions[node.id]);
    const left = Math.min(...nodes.map((node, i) => xs[i].x - node.radius)) - 12;
    const right = Math.max(...nodes.map((node, i) => xs[i].x + node.radius)) + 12;
    result.captions.forEach((caption) => {
      caption.x = left;
      caption.width = Math.max(160, right - left);
    });
  }
  return result;

  function captionFor(band: "spine" | "shelf", bandTop: number, count: number): WikiLayoutCaption {
    return { band, x: 0, y: bandTop - L.captionSpace - 6, width: 0, count };
  }
}

function toneRank(tone: string): number {
  const index = TONE_ORDER.indexOf(tone);
  return index === -1 ? TONE_ORDER.length : index;
}

// ---------------- 连线几何 ----------------

export interface WikiEdgeGeometry {
  pathD: string;
  arrowD: string;
  /** 二次贝塞尔控制点（直线时为中点），用于沿线取标签候选位置。 */
  start: { x: number; y: number };
  control: { x: number; y: number };
  end: { x: number; y: number };
}

interface GeometryEndpoint {
  x: number;
  y: number;
  radius: number;
}

/**
 * 连线端点裁到节点圆周外 `gap` 处；`bend` 为垂直于连线的弯曲量（0 为直线）；
 * `arrow` > 0 时在终点画实心箭头，路径止于箭头根部。
 */
export function buildWikiEdgeGeometry(
  source: GeometryEndpoint,
  target: GeometryEndpoint,
  bend: number,
  options: { gap?: number; arrow?: number } = {},
): WikiEdgeGeometry {
  const gap = options.gap ?? 3;
  const arrow = options.arrow ?? 0;
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const distance = Math.hypot(dx, dy);
  if (distance < 1) {
    const point = { x: source.x, y: source.y };
    return { pathD: "", arrowD: "", start: point, control: point, end: point };
  }
  const nx = -dy / distance;
  const ny = dx / distance;
  const control = {
    x: (source.x + target.x) / 2 + nx * bend,
    y: (source.y + target.y) / 2 + ny * bend,
  };
  const towards = (from: { x: number; y: number }, to: { x: number; y: number }, length: number) => {
    const vx = to.x - from.x;
    const vy = to.y - from.y;
    const len = Math.hypot(vx, vy) || 1;
    return { x: from.x + (vx / len) * length, y: from.y + (vy / len) * length, ux: vx / len, uy: vy / len };
  };
  const startTrim = Math.min(source.radius + gap, distance * 0.45);
  const endTrim = Math.min(target.radius + gap, distance * 0.45);
  const start = towards(source, control, startTrim);
  const tip = towards(target, control, endTrim);
  const arrowLength = arrow > 0 ? Math.min(arrow, Math.max(0, distance - startTrim - endTrim) * 0.4) : 0;
  const end = arrowLength > 0 ? towards(tip, control, arrowLength) : tip;
  const pathD = bend === 0
    ? `M ${round(start.x)} ${round(start.y)} L ${round(end.x)} ${round(end.y)}`
    : `M ${round(start.x)} ${round(start.y)} Q ${round(control.x)} ${round(control.y)} ${round(end.x)} ${round(end.y)}`;
  let arrowD = "";
  if (arrowLength > 0) {
    // 箭头方向：从箭头根部指向尖端。
    const ux = -tip.ux;
    const uy = -tip.uy;
    const half = arrowLength * 0.58;
    const baseX = tip.x - ux * arrowLength;
    const baseY = tip.y - uy * arrowLength;
    arrowD = `M ${round(tip.x)} ${round(tip.y)} L ${round(baseX - uy * half)} ${round(baseY + ux * half)} L ${round(baseX + uy * half)} ${round(baseY - ux * half)} Z`;
  }
  return {
    pathD,
    arrowD,
    start: { x: start.x, y: start.y },
    control,
    end: { x: tip.x, y: tip.y },
  };
}

export function pointOnWikiEdge(geometry: WikiEdgeGeometry, t: number): { x: number; y: number } {
  const { start, control, end } = geometry;
  const inverse = 1 - t;
  return {
    x: inverse * inverse * start.x + 2 * inverse * t * control.x + t * t * end.x,
    y: inverse * inverse * start.y + 2 * inverse * t * control.y + t * t * end.y,
  };
}

/** 同一对节点之间的多条连线对称弯开；单条连线保持直线。 */
export function wikiEdgeBendOffsets(edges: Array<{ source: string; target: string }>, spacing = 44): number[] {
  const groups = new Map<string, number[]>();
  edges.forEach((edge, index) => {
    const key = [edge.source, edge.target].sort().join("\u0000");
    const list = groups.get(key) ?? [];
    list.push(index);
    groups.set(key, list);
  });
  const bends = new Array<number>(edges.length).fill(0);
  groups.forEach((indexes) => {
    if (indexes.length < 2) {
      return;
    }
    indexes.forEach((edgeIndex, position) => {
      const edge = edges[edgeIndex];
      const canonical = edge.source <= edge.target ? 1 : -1;
      bends[edgeIndex] = (position - (indexes.length - 1) / 2) * spacing * canonical;
    });
  });
  return bends;
}

// ---------------- 标签避让 ----------------

export interface WikiLabelRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface WikiNodeLabelInput {
  id: string;
  x: number;
  y: number;
  radius: number;
  width: number;
  height: number;
  priority: number;
}

export interface WikiNodeLabelPlacement {
  dx: number;
  dy: number;
  anchor: "middle" | "start" | "end";
  rect: WikiLabelRect;
}

/** 节点名默认放在圆点下方；与其他节点或已放置的标签重叠时依次尝试上、右、左。 */
export function placeWikiNodeLabels(nodes: WikiNodeLabelInput[]): Record<string, WikiNodeLabelPlacement> {
  const discs = nodes.map((node) => ({
    id: node.id,
    x: node.x - node.radius - 2,
    y: node.y - node.radius - 2,
    width: node.radius * 2 + 4,
    height: node.radius * 2 + 4,
  }));
  const placed: WikiLabelRect[] = [];
  const result: Record<string, WikiNodeLabelPlacement> = {};
  const order = [...nodes].sort((a, b) => b.priority - a.priority || a.id.localeCompare(b.id));
  for (const node of order) {
    const gap = 5;
    const candidates: WikiNodeLabelPlacement[] = [
      { dx: 0, dy: node.radius + gap + node.height / 2, anchor: "middle", rect: rectAt(node.x - node.width / 2, node.y + node.radius + gap, node) },
      { dx: 0, dy: -(node.radius + gap + node.height / 2), anchor: "middle", rect: rectAt(node.x - node.width / 2, node.y - node.radius - gap - node.height, node) },
      { dx: node.radius + gap + 2, dy: 0, anchor: "start", rect: rectAt(node.x + node.radius + gap + 2, node.y - node.height / 2, node) },
      { dx: -(node.radius + gap + 2), dy: 0, anchor: "end", rect: rectAt(node.x - node.radius - gap - 2 - node.width, node.y - node.height / 2, node) },
    ];
    let best = candidates[0];
    let bestScore = Infinity;
    for (const candidate of candidates) {
      let score = 0;
      for (const disc of discs) {
        if (disc.id !== node.id) score += overlapArea(candidate.rect, disc);
      }
      for (const rect of placed) score += overlapArea(candidate.rect, rect);
      if (score < bestScore - 0.5) {
        best = candidate;
        bestScore = score;
      }
      if (score === 0) break;
    }
    placed.push(best.rect);
    result[node.id] = best;
  }
  return result;

  function rectAt(x: number, y: number, node: WikiNodeLabelInput): WikiLabelRect {
    return { x, y, width: node.width, height: node.height };
  }
}

export interface WikiEdgeLabelInput {
  id: string;
  width: number;
  height: number;
  /** 沿线候选点，按偏好排序（首个通常为中点）。 */
  candidates: Array<{ x: number; y: number }>;
}

/** 关系标签沿连线滑动，避开节点、节点名与先放置的关系标签；都冲突时取重叠最少的位置。 */
export function placeWikiEdgeLabels(
  labels: WikiEdgeLabelInput[],
  obstacles: WikiLabelRect[],
): Record<string, { x: number; y: number }> {
  const placed: WikiLabelRect[] = [];
  const result: Record<string, { x: number; y: number }> = {};
  for (const label of labels) {
    let best = label.candidates[0] ?? { x: 0, y: 0 };
    let bestRect = centeredRect(best, label);
    let bestScore = Infinity;
    for (const candidate of label.candidates) {
      const rect = centeredRect(candidate, label);
      let score = 0;
      for (const obstacle of obstacles) score += overlapArea(rect, obstacle);
      for (const other of placed) score += overlapArea(rect, other) * 1.5;
      if (score < bestScore - 0.5) {
        best = candidate;
        bestRect = rect;
        bestScore = score;
      }
      if (score === 0) break;
    }
    placed.push(bestRect);
    result[label.id] = best;
  }
  return result;

  function centeredRect(point: { x: number; y: number }, label: WikiEdgeLabelInput): WikiLabelRect {
    return { x: point.x - label.width / 2, y: point.y - label.height / 2, width: label.width, height: label.height };
  }
}

/** 估算标签宽度（px）：CJK 字符按 1em，拉丁字母/数字约 0.6em。 */
export function estimateWikiTextWidth(text: string, fontSize: number): number {
  let units = 0;
  for (const char of String(text || "")) {
    const code = char.codePointAt(0) ?? 0;
    if (code >= 0x2e80 || (code >= 0xff00 && code <= 0xffef)) units += 1;
    else if (char === " ") units += 0.32;
    else if (/[A-Z0-9]/.test(char)) units += 0.64;
    else units += 0.56;
  }
  return units * fontSize;
}

function overlapArea(a: WikiLabelRect, b: WikiLabelRect): number {
  const width = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
  const height = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
  return width > 0 && height > 0 ? width * height : 0;
}

function round(value: number): number {
  return Math.round(value * 10) / 10;
}
