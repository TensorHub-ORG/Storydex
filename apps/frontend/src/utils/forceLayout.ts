/**
 * 轻量力导向布局（Fruchterman-Reingold 变体 + 向心力 + 退火）。
 *
 * 纯函数、确定性（无随机初值，未给坐标时按圆环铺开），与项目现有纯 SVG 几何风格一致，
 * 不引入 d3 等布局库。节点数通常 < 100，O(N²) 迭代成本可忽略。
 */

export interface ForceNode {
  id: string;
  /** 节点半径，用于碰撞最小间距；缺省 34。 */
  radius?: number;
  /** 初始坐标（如上一轮 radial 结果），用于减少重算抖动。 */
  x?: number;
  y?: number;
}

export interface ForceEdge {
  source: string;
  target: string;
  /** 关系强度，越大理想边长越短（节点被拉得更近）。 */
  weight?: number;
}

export interface ForceLayoutOptions {
  width: number;
  height: number;
  iterations?: number;
  padding?: number;
}

interface MutablePoint {
  x: number;
  y: number;
}

const DEFAULT_RADIUS = 34;

export function computeForceLayout(
  nodes: ForceNode[],
  edges: ForceEdge[],
  options: ForceLayoutOptions,
): Record<string, { x: number; y: number }> {
  const { width, height } = options;
  const iterations = options.iterations ?? 300;
  const padding = options.padding ?? 48;
  const centerX = width / 2;
  const centerY = height / 2;

  const count = nodes.length;
  if (count === 0) {
    return {};
  }
  if (count === 1) {
    return { [nodes[0].id]: { x: centerX, y: centerY } };
  }

  const positions = new Map<string, MutablePoint>();
  const radii = new Map<string, number>();
  nodes.forEach((node, index) => {
    radii.set(node.id, node.radius ?? DEFAULT_RADIUS);
    if (typeof node.x === "number" && typeof node.y === "number") {
      positions.set(node.id, { x: node.x, y: node.y });
    } else {
      // 未提供初值：沿圆环均匀铺开（确定性，不用随机）。
      const angle = (Math.PI * 2 * index) / count - Math.PI / 2;
      const spread = Math.min(width, height) * 0.36;
      positions.set(node.id, {
        x: centerX + Math.cos(angle) * spread,
        y: centerY + Math.sin(angle) * spread,
      });
    }
  });

  const validEdges = edges.filter(
    (edge) => positions.has(edge.source) && positions.has(edge.target) && edge.source !== edge.target,
  );

  // 连接度：度数高的节点被更强地拉向中心，孤立节点漂在外圈，主干结构自然居中。
  const degrees = new Map<string, number>();
  for (const edge of validEdges) {
    degrees.set(edge.source, (degrees.get(edge.source) ?? 0) + 1);
    degrees.set(edge.target, (degrees.get(edge.target) ?? 0) + 1);
  }

  const area = width * height;
  const k = Math.sqrt(area / count) * 0.72; // 理想节点间距
  let temperature = Math.min(width, height) * 0.12;
  const cooling = 0.96;
  const minTemperature = 0.6;

  for (let step = 0; step < iterations; step += 1) {
    const disp = new Map<string, MutablePoint>();
    nodes.forEach((node) => disp.set(node.id, { x: 0, y: 0 }));

    // 斥力：所有节点两两互斥。
    for (let i = 0; i < count; i += 1) {
      const nodeA = nodes[i];
      const posA = positions.get(nodeA.id)!;
      for (let j = i + 1; j < count; j += 1) {
        const nodeB = nodes[j];
        const posB = positions.get(nodeB.id)!;
        let dx = posA.x - posB.x;
        let dy = posA.y - posB.y;
        let distance = Math.hypot(dx, dy);
        if (distance < 0.01) {
          // 完全重合时给一个确定性微扰，避免除零。
          dx = (i - j) || 1;
          dy = (j - i) || 1;
          distance = Math.hypot(dx, dy);
        }
        const minGap = (radii.get(nodeA.id)! + radii.get(nodeB.id)!) + 18;
        // 基础库仑斥力 + 近距离时的强碰撞分离力。
        let force = (k * k) / distance;
        if (distance < minGap) {
          force += (minGap - distance) * 0.9;
        }
        const ux = dx / distance;
        const uy = dy / distance;
        const dispA = disp.get(nodeA.id)!;
        const dispB = disp.get(nodeB.id)!;
        dispA.x += ux * force;
        dispA.y += uy * force;
        dispB.x -= ux * force;
        dispB.y -= uy * force;
      }
    }

    // 引力：边把两端拉近，weight 越大越近。
    for (const edge of validEdges) {
      const posU = positions.get(edge.source)!;
      const posV = positions.get(edge.target)!;
      let dx = posU.x - posV.x;
      let dy = posU.y - posV.y;
      const distance = Math.max(0.01, Math.hypot(dx, dy));
      const weight = Math.max(1, edge.weight ?? 1);
      const force = ((distance * distance) / k) * Math.min(2.4, Math.sqrt(weight));
      const ux = dx / distance;
      const uy = dy / distance;
      const dispU = disp.get(edge.source)!;
      const dispV = disp.get(edge.target)!;
      dispU.x -= ux * force;
      dispU.y -= uy * force;
      dispV.x += ux * force;
      dispV.y += uy * force;
    }

    // 向心力：轻微拉向画布中心，防止孤立子图飘散出界；连接度越高拉力越强。
    for (const node of nodes) {
      const pos = positions.get(node.id)!;
      const disp0 = disp.get(node.id)!;
      const degree = degrees.get(node.id) ?? 0;
      const pull = degree > 0 ? 0.01 + Math.min(0.028, degree * 0.004) : 0.005;
      disp0.x += (centerX - pos.x) * pull;
      disp0.y += (centerY - pos.y) * pull;
    }

    // 按当前温度限制位移，并夹到画布边界内。
    for (const node of nodes) {
      const pos = positions.get(node.id)!;
      const disp0 = disp.get(node.id)!;
      const magnitude = Math.hypot(disp0.x, disp0.y);
      if (magnitude > 0.001) {
        const limited = Math.min(magnitude, temperature);
        pos.x += (disp0.x / magnitude) * limited;
        pos.y += (disp0.y / magnitude) * limited;
      }
      const radius = radii.get(node.id)!;
      pos.x = clamp(pos.x, padding + radius, width - padding - radius);
      pos.y = clamp(pos.y, padding + radius, height - padding - radius);
    }

    temperature = Math.max(minTemperature, temperature * cooling);
  }

  const result: Record<string, { x: number; y: number }> = {};
  for (const node of nodes) {
    const pos = positions.get(node.id)!;
    result[node.id] = { x: pos.x, y: pos.y };
  }
  return result;
}

function clamp(value: number, min: number, max: number): number {
  if (max < min) {
    return (min + max) / 2;
  }
  return Math.min(max, Math.max(min, value));
}
