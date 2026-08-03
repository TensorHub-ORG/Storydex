<template>
  <div ref="containerRef" class="timeline-graph">
    <!-- 空状态 -->
    <p v-if="!hasNodes" class="timeline-empty">
      {{ emptyHint }}
    </p>

    <template v-else>
      <!-- detached HEAD 提示条 -->
      <div v-if="detached" class="timeline-detached-banner" role="status">
        <span class="material-symbols-rounded">timeline</span>
        <span>正在查看历史节点（游离 HEAD）。在此基础上提交将自动创建新的平行时空线。</span>
      </div>

      <!-- 分支图例（仅当分支多于 1 条时显示） -->
      <ul v-if="showLegend" class="timeline-legend" :aria-label="`共 ${branches.length} 条平行时空线`">
        <li
          v-for="branch in legendBranches"
          :key="branch.name"
          class="timeline-legend-item"
          :class="{ 'is-current': branch.isCurrent }"
          :title="branch.name"
        >
          <span class="timeline-legend-dot" :style="{ background: laneColor(branch.lane) }"></span>
          <span class="timeline-legend-name">{{ branch.name }}</span>
          <span v-if="branch.isCurrent" class="timeline-legend-tag">当前</span>
        </li>
      </ul>

      <div class="timeline-canvas-wrap" @scroll.passive="onScroll">
        <svg
          :width="svgWidth"
          :height="svgHeight"
          :viewBox="`0 0 ${svgWidth} ${svgHeight}`"
          class="timeline-svg"
          role="img"
          aria-label="平行时空线树状图"
        >
          <!-- 分支 lane 背景条（让同一世界线的节点视觉连成一条带） -->
          <rect
            v-for="lane in laneBackgrounds"
            :key="`lane-${lane.row}`"
            :x="PADDING_X"
            :y="lane.y - LANE_HEIGHT / 2"
            :width="svgWidth - PADDING_X * 2"
            :height="LANE_HEIGHT"
            :class="['timeline-lane-bg', { 'is-current': lane.isCurrent }]"
            :style="{ fill: laneColor(lane.row) }"
          />

          <!-- 边（父子提交连线） -->
          <path
            v-for="(edge, index) in edgePaths"
            :key="`edge-${index}`"
            :d="edge.path"
            :stroke="edge.color"
            :class="['timeline-edge', { 'is-current-line': edge.isCurrentLine }]"
            fill="none"
          />

          <!-- 节点 -->
          <g
            v-for="node in layoutNodes"
            :key="node.id"
            :transform="`translate(${node.x}, ${node.y})`"
            :class="['timeline-node-g', nodeClass(node)]"
            :tabindex="0"
            role="button"
            :aria-label="nodeAriaLabel(node)"
            @click="onNodeClick(node)"
            @keydown.enter.prevent="onNodeClick(node)"
            @keydown.space.prevent="onNodeClick(node)"
            @mouseenter="onNodeHover(node, $event)"
            @mouseleave="onNodeLeave"
            @focus="onNodeHover(node, $event)"
            @blur="onNodeLeave"
          >
            <!-- 分支 head 节点的外环 -->
            <circle
              v-if="node.isBranchHead"
              :r="NODE_RADIUS + 3"
              class="timeline-node-ring"
              :stroke="laneColor(node.row)"
              fill="none"
            />
            <circle
              :r="NODE_RADIUS"
              class="timeline-node-dot"
              :fill="laneColor(node.row)"
            />
            <!-- 当前 HEAD 节点的中心标记 -->
            <circle
              v-if="node.isCurrent"
              :r="NODE_RADIUS - 3"
              class="timeline-node-core"
            />
          </g>
        </svg>
      </div>

      <!-- hover tooltip -->
      <div
        v-if="hoveredNode"
        class="timeline-tooltip"
        :style="tooltipStyle"
        role="tooltip"
      >
        <div class="timeline-tooltip-subject">{{ hoveredNode.subject || "（无说明）" }}</div>
        <div class="timeline-tooltip-meta">
          <code>{{ hoveredNode.shortId }}</code>
          <span>{{ hoveredNode.authorName }}</span>
          <span>{{ formatTimestamp(hoveredNode.authoredAt) }}</span>
        </div>
        <div v-if="hoveredNode.headBranches.length > 0" class="timeline-tooltip-branches">
          <span
            v-for="name in hoveredNode.headBranches"
            :key="name"
            class="timeline-tooltip-branch"
            :class="{ 'is-current': isCurrentBranchName(name) }"
          >{{ name }}</span>
        </div>
        <div v-else-if="hoveredNode.branches.length > 0" class="timeline-tooltip-branches">
          <span
            v-for="name in hoveredNode.branches"
            :key="name"
            class="timeline-tooltip-branch timeline-tooltip-branch-muted"
          >{{ name }}</span>
        </div>
        <div class="timeline-tooltip-hint">
          <span v-if="hoveredNode.isCurrent">当前所在节点</span>
          <span v-else>点击跳转到此节点（进入游离 HEAD）</span>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import type {
  WorkspaceGitTimelineBranch,
  WorkspaceGitTimelineNode,
  WorkspaceGitTimelineResponse
} from "@/types/workspace";

const props = defineProps<{
  timeline: WorkspaceGitTimelineResponse | null;
  /** 是否正在加载（用于空状态文案区分）。 */
  loading?: boolean;
  /** 当前是否处于 detached HEAD 状态（来自 store，避免 timeline 刷新前的不一致）。 */
  detachedOverride?: boolean;
}>();

const emit = defineEmits<{
  (e: "jump", commitId: string): void;
}>();

// 布局常量
const NODE_RADIUS = 6;
const COLUMN_WIDTH = 32;
const ROW_HEIGHT = 40;
const LANE_HEIGHT = 28;
const PADDING_X = 20;
const PADDING_Y = 24;
const MIN_SVG_WIDTH = 280;

// 分支 lane 配色（循环取色）。当前分支用第一个颜色（蓝），其他分支按 lane 索引取。
const LANE_COLORS = [
  "#3b82f6", // 蓝（当前分支）
  "#22c55e", // 绿
  "#f59e0b", // 橙
  "#ec4899", // 粉
  "#8b5cf6", // 紫
  "#14b8a6", // 青
  "#ef4444", // 红
  "#6366f1"  // 靛
];

const containerRef = ref<HTMLElement | null>(null);
const hoveredNodeId = ref<string | null>(null);
const hoverClientX = ref(0);
const hoverClientY = ref(0);

const detached = computed(() => Boolean(props.detachedOverride || props.timeline?.detached));
const branches = computed<WorkspaceGitTimelineBranch[]>(() => props.timeline?.branches || []);
const rawNodes = computed<WorkspaceGitTimelineNode[]>(() => props.timeline?.nodes || []);
const hasNodes = computed(() => rawNodes.value.length > 0);
const showLegend = computed(() => branches.value.length > 1);

const emptyHint = computed(() => {
  if (props.loading) return "正在加载平行时空线…";
  if (!props.timeline?.initialized) return "尚未启用版本记录。";
  return "还没有任何提交，先提交一次留下第一个节点。";
});

/** 当前分支名（用于 tooltip 高亮）。 */
const currentBranchName = computed(() => {
  const b = branches.value.find((item) => item.isCurrent);
  return b?.name || "";
});

/** 图例中显示的分支列表（按 lane 排序）。 */
const legendBranches = computed(() =>
  [...branches.value].sort((a, b) => a.lane - b.lane)
);

const maxColumn = computed(() => {
  let max = 0;
  for (const node of rawNodes.value) {
    if (node.column > max) max = node.column;
  }
  return max;
});

const maxRow = computed(() => {
  let max = 0;
  for (const node of rawNodes.value) {
    if (node.row > max) max = node.row;
  }
  return max;
});

const svgWidth = computed(() => {
  const width = PADDING_X * 2 + (maxColumn.value + 1) * COLUMN_WIDTH;
  return Math.max(MIN_SVG_WIDTH, width);
});

const svgHeight = computed(() => PADDING_Y * 2 + (maxRow.value + 1) * ROW_HEIGHT);

/** 为节点附加 SVG 坐标。 */
const layoutNodes = computed(() =>
  rawNodes.value.map((node) => ({
    ...node,
    x: PADDING_X + node.column * COLUMN_WIDTH,
    y: PADDING_Y + node.row * ROW_HEIGHT
  }))
);

const nodeById = computed(() => {
  const map = new Map<string, (typeof layoutNodes.value)[number]>();
  for (const node of layoutNodes.value) {
    map.set(node.id, node);
  }
  return map;
});

interface EdgePath {
  path: string;
  color: string;
  isCurrentLine: boolean;
}

/** 计算每条边的 SVG path（cubic bezier 水平曲线）。 */
const edgePaths = computed<EdgePath[]>(() => {
  const edges = props.timeline?.edges || [];
  const result: EdgePath[] = [];
  for (const edge of edges) {
    const child = nodeById.value.get(edge.to);
    const parent = nodeById.value.get(edge.from);
    if (!child || !parent) continue;
    // child 在左（column 小），parent 在右（column 大）
    const x1 = child.x;
    const y1 = child.y;
    const x2 = parent.x;
    const y2 = parent.y;
    const dx = Math.max(8, Math.min(24, Math.abs(x2 - x1) / 2));
    const path = `M ${x1},${y1} C ${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
    const color = laneColor(child.row);
    const isCurrentLine = child.isCurrent || parent.isCurrent;
    result.push({ path, color, isCurrentLine });
  }
  return result;
});

interface LaneBg {
  row: number;
  y: number;
  isCurrent: boolean;
}

/** 每条分支 lane 的背景条。 */
const laneBackgrounds = computed<LaneBg[]>(() => {
  const seen = new Set<number>();
  const list: LaneBg[] = [];
  for (const branch of branches.value) {
    if (seen.has(branch.lane)) continue;
    seen.add(branch.lane);
    list.push({
      row: branch.lane,
      y: PADDING_Y + branch.lane * ROW_HEIGHT,
      isCurrent: branch.isCurrent
    });
  }
  return list;
});

const hoveredNode = computed(() => {
  if (!hoveredNodeId.value) return null;
  return layoutNodes.value.find((n) => n.id === hoveredNodeId.value) || null;
});

const tooltipStyle = computed(() => {
  if (!containerRef.value || !hoveredNode.value) {
    return { display: "none" };
  }
  const rect = containerRef.value.getBoundingClientRect();
  // tooltip 出现在节点右侧（最新节点在左侧，向右展开更自然），超出右边时翻到左侧。
  const nodeX = hoveredNode.value.x;
  const nodeY = hoveredNode.value.y;
  const tooltipWidth = 240;
  const tooltipHeight = 120;
  const placeLeft = nodeX + tooltipWidth + 16 > rect.width;
  const left = placeLeft ? nodeX - tooltipWidth - 12 : nodeX + 12;
  // 垂直居中节点，但不超过容器边界
  const top = Math.max(4, Math.min(rect.height - tooltipHeight - 4, nodeY - tooltipHeight / 2));
  return {
    left: `${left}px`,
    top: `${top}px`
  };
});

function laneColor(lane: number): string {
  return LANE_COLORS[lane % LANE_COLORS.length];
}

function nodeClass(node: (typeof layoutNodes.value)[number]): string {
  return [
    "timeline-node",
    node.isCurrent ? "is-current" : "",
    node.isBranchHead ? "is-head" : ""
  ].filter(Boolean).join(" ");
}

function nodeAriaLabel(node: (typeof layoutNodes.value)[number]): string {
  const parts = [node.subject, node.shortId, node.authorName, formatTimestamp(node.authoredAt)];
  if (node.isCurrent) parts.push("当前节点");
  if (node.headBranches.length > 0) parts.push(`分支: ${node.headBranches.join(", ")}`);
  return parts.join("，");
}

function isCurrentBranchName(name: string): boolean {
  return name === currentBranchName.value;
}

function onNodeClick(node: (typeof layoutNodes.value)[number]): void {
  if (node.isCurrent) return;
  emit("jump", node.id);
}

function onNodeHover(node: (typeof layoutNodes.value)[number], event: MouseEvent | FocusEvent): void {
  hoveredNodeId.value = node.id;
  if (event instanceof MouseEvent) {
    hoverClientX.value = event.clientX;
    hoverClientY.value = event.clientY;
  }
}

function onNodeLeave(): void {
  hoveredNodeId.value = null;
}

function onScroll(): void {
  // 滚动时隐藏 tooltip，避免定位错乱
  hoveredNodeId.value = null;
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

defineExpose({
  __testUtils: import.meta.env.MODE === "test" ? {
    NODE_RADIUS,
    COLUMN_WIDTH,
    ROW_HEIGHT,
    laneColor,
    nodeClass,
    onNodeClick,
    formatTimestamp
  } : null
});
</script>

<style scoped>
.timeline-graph {
  position: relative;
  width: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 12px;
}

.timeline-empty {
  margin: 12px 8px;
  padding: 16px 12px;
  text-align: center;
  color: var(--text-muted, #888);
  line-height: 1.6;
  background: var(--bg-ghost, rgba(0, 0, 0, 0.03));
  border-radius: 6px;
}

.timeline-detached-banner {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  margin: 0 4px;
  border-radius: 6px;
  background: rgba(245, 158, 11, 0.12);
  color: #b45309;
  font-size: 11px;
  line-height: 1.4;
}

.timeline-detached-banner .material-symbols-rounded {
  font-size: 16px;
  flex: 0 0 auto;
}

.timeline-legend {
  list-style: none;
  margin: 0 4px;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 4px 10px;
  font-size: 11px;
}

.timeline-legend-item {
  display: flex;
  align-items: center;
  gap: 4px;
  max-width: 160px;
  padding: 2px 6px;
  border-radius: 10px;
  background: var(--bg-ghost, rgba(0, 0, 0, 0.04));
}

.timeline-legend-item.is-current {
  background: rgba(59, 130, 246, 0.14);
}

.timeline-legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: 0 0 auto;
}

.timeline-legend-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-main);
}

.timeline-legend-tag {
  flex: 0 0 auto;
  padding: 0 5px;
  border-radius: 8px;
  background: var(--accent, #3b82f6);
  color: #fff;
  font-size: 10px;
  line-height: 16px;
}

.timeline-canvas-wrap {
  width: 100%;
  overflow-x: auto;
  overflow-y: hidden;
  padding: 4px 0;
  /* 让滚动条不占位 */
  scrollbar-width: thin;
}

.timeline-svg {
  display: block;
  /* 节点点击区域稍大一些 */
  cursor: default;
}

.timeline-lane-bg {
  opacity: 0.06;
  rx: 4;
  ry: 4;
}

.timeline-lane-bg.is-current {
  opacity: 0.1;
}

.timeline-edge {
  stroke-width: 2;
  fill: none;
  opacity: 0.7;
  transition: opacity 0.15s;
}

.timeline-edge.is-current-line {
  stroke-width: 2.5;
  opacity: 0.9;
}

.timeline-node-g {
  cursor: pointer;
  outline: none;
}

.timeline-node-g:focus-visible .timeline-node-dot {
  stroke: var(--accent, #3b82f6);
  stroke-width: 2;
}

.timeline-node-ring {
  stroke-width: 1.5;
  opacity: 0.5;
}

.timeline-node-dot {
  stroke: var(--bg-panel, #fff);
  stroke-width: 1.5;
  transition: r 0.15s;
}

.timeline-node-g:hover .timeline-node-dot {
  stroke-width: 2;
}

.timeline-node-core {
  fill: var(--bg-panel, #fff);
}

.timeline-node.is-current .timeline-node-dot {
  stroke-width: 2.5;
}

.timeline-tooltip {
  position: absolute;
  width: 240px;
  padding: 8px 10px;
  background: var(--bg-elevated, #fff);
  border: 1px solid var(--border-ghost, rgba(0, 0, 0, 0.1));
  border-radius: 6px;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.15);
  z-index: 10;
  pointer-events: none;
  font-size: 11px;
  color: var(--text-main);
}

.timeline-tooltip-subject {
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 4px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.timeline-tooltip-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 8px;
  color: var(--text-muted, #888);
  margin-bottom: 6px;
}

.timeline-tooltip-meta code {
  font-family: var(--font-mono, monospace);
  font-size: 10px;
  padding: 1px 4px;
  border-radius: 3px;
  background: var(--bg-ghost, rgba(0, 0, 0, 0.06));
  color: var(--text-main);
}

.timeline-tooltip-branches {
  display: flex;
  flex-wrap: wrap;
  gap: 3px;
  margin-bottom: 6px;
}

.timeline-tooltip-branch {
  padding: 1px 6px;
  border-radius: 8px;
  background: rgba(59, 130, 246, 0.14);
  color: #1d4ed8;
  font-size: 10px;
  line-height: 16px;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.timeline-tooltip-branch.is-current {
  background: #3b82f6;
  color: #fff;
}

.timeline-tooltip-branch-muted {
  background: var(--bg-ghost, rgba(0, 0, 0, 0.06));
  color: var(--text-muted, #888);
}

.timeline-tooltip-hint {
  font-size: 10px;
  color: var(--text-muted, #888);
  border-top: 1px solid var(--border-ghost, rgba(0, 0, 0, 0.08));
  padding-top: 4px;
  margin-top: 2px;
}
</style>
