<template>
  <div ref="containerRef" class="timeline-graph" :style="{ height: containerHeight + 'px' }">
    <!-- 拖动手柄（调整高度） -->
    <div
      class="timeline-resize-handle"
      role="separator"
      aria-orientation="horizontal"
      aria-label="拖动调整时空线高度"
      tabindex="0"
      @mousedown.prevent="startResize"
      @touchstart.prevent="startResize"
      @keydown="onResizeKeydown"
    >
      <span class="timeline-resize-grip"></span>
    </div>

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

      <!-- 方向标注 -->
      <div class="timeline-axis-hint">
        <span>旧</span>
        <span class="timeline-axis-arrow">→</span>
        <span>新</span>
      </div>

      <!-- SVG 画布区域（占据剩余高度，横向滚动） -->
      <div ref="canvasWrapRef" class="timeline-canvas-wrap">
        <svg
          :width="svgWidth"
          :height="svgHeight"
          :viewBox="`0 0 ${svgWidth} ${svgHeight}`"
          class="timeline-svg"
          role="img"
          aria-label="平行时空线树状图"
          preserveAspectRatio="xMin yMin meet"
        >
          <!-- 分支 lane 背景条 -->
          <rect
            v-for="lane in laneBackgrounds"
            :key="`lane-${lane.row}`"
            :x="0"
            :y="lane.y - LANE_HEIGHT / 2"
            :width="svgWidth"
            :height="LANE_HEIGHT"
            :class="['timeline-lane-bg', { 'is-current': lane.isCurrent }]"
            :style="{ fill: laneColor(lane.row) }"
          />

          <!-- 分支标签（左侧） -->
          <text
            v-for="lane in laneBackgrounds"
            :key="`lane-label-${lane.row}`"
            :x="4"
            :y="lane.y + 3"
            class="timeline-lane-label"
            :style="{ fill: laneColor(lane.row) }"
          >{{ laneLabel(lane.row) }}</text>

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
            <!-- 当前 HEAD 节点：双环 + 中心十字标记 -->
            <circle
              v-if="node.isCurrent"
              :r="NODE_RADIUS + 5"
              class="timeline-node-current-ring"
              :stroke="laneColor(node.row)"
              fill="none"
            />
            <circle
              v-if="node.isCurrent"
              :r="NODE_RADIUS - 2"
              class="timeline-node-core"
            />
            <!-- 当前节点文字标注 -->
            <text
              v-if="node.isCurrent"
              :x="0"
              :y="-NODE_RADIUS - 8"
              class="timeline-node-label"
              text-anchor="middle"
            >当前</text>
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
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
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
const NODE_RADIUS = 7;
const COLUMN_WIDTH = 36;
const ROW_HEIGHT = 44;
const LANE_HEIGHT = 30;
const PADDING_X = 24;
const PADDING_Y = 20;
const MIN_SVG_WIDTH = 280;
const MIN_HEIGHT = 120;
const MAX_HEIGHT = 600;
const DEFAULT_HEIGHT = 260;
const STORAGE_KEY_HEIGHT = "storydex.timeline.height";

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
const canvasWrapRef = ref<HTMLElement | null>(null);
const hoveredNodeId = ref<string | null>(null);
const containerHeight = ref(DEFAULT_HEIGHT);
let resizing = false;
let resizeStartY = 0;
let resizeStartHeight = 0;

// 从 localStorage 恢复高度
try {
  const saved = localStorage.getItem(STORAGE_KEY_HEIGHT);
  if (saved) {
    const val = parseInt(saved, 10);
    if (Number.isFinite(val) && val >= MIN_HEIGHT && val <= MAX_HEIGHT) {
      containerHeight.value = val;
    }
  }
} catch {
  // ignore
}

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

/** SVG 高度至少容纳所有 lane，但不小于容器高度（让 lane 背景填满）。 */
const svgHeight = computed(() => {
  const laneHeight = PADDING_Y * 2 + (maxRow.value + 1) * ROW_HEIGHT;
  return laneHeight;
});

/** 为节点附加 SVG 坐标。column=0 在左（旧），column=max 在右（新）。 */
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

/**
 * 计算每条边的 SVG path。
 * edge.from = parent（旧，左），edge.to = child（新，右）。
 * 线从 parent(x小,左) 指向 child(x大,右)。
 */
const edgePaths = computed<EdgePath[]>(() => {
  const edges = props.timeline?.edges || [];
  const result: EdgePath[] = [];
  for (const edge of edges) {
    const parent = nodeById.value.get(edge.from);
    const child = nodeById.value.get(edge.to);
    if (!parent || !child) continue;
    // parent 在左（column 小），child 在右（column 大）
    const x1 = parent.x;
    const y1 = parent.y;
    const x2 = child.x;
    const y2 = child.y;
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
  if (!canvasWrapRef.value || !hoveredNode.value) {
    return { display: "none" };
  }
  const wrap = canvasWrapRef.value;
  const wrapRect = wrap.getBoundingClientRect();
  const containerRect = containerRef.value?.getBoundingClientRect();
  if (!containerRect) return { display: "none" };
  // tooltip 相对于 containerRef 定位
  const nodeX = hoveredNode.value.x;
  const nodeY = hoveredNode.value.y;
  // 考虑横向滚动偏移
  const scrollLeft = wrap.scrollLeft;
  const tooltipWidth = 240;
  const tooltipHeight = 120;
  // 节点在容器中的实际 X 位置（减去画布偏移）
  const visibleX = nodeX - scrollLeft + (wrapRect.left - containerRect.left);
  const visibleY = nodeY + (wrapRect.top - containerRect.top);
  const placeLeft = visibleX + tooltipWidth + 16 > containerRect.width;
  const left = placeLeft ? visibleX - tooltipWidth - 12 : visibleX + 14;
  const top = Math.max(4, Math.min(containerRect.height - tooltipHeight - 4, visibleY - tooltipHeight / 2));
  return {
    left: `${Math.max(4, left)}px`,
    top: `${top}px`
  };
});

function laneColor(lane: number): string {
  return LANE_COLORS[lane % LANE_COLORS.length];
}

function laneLabel(row: number): string {
  const branch = branches.value.find((b) => b.lane === row);
  if (!branch) return "";
  const name = branch.name;
  // 截断长分支名
  return name.length > 12 ? name.slice(0, 11) + "…" : name;
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
    // 不再需要 client 坐标，tooltip 基于 SVG 坐标定位
  }
}

function onNodeLeave(): void {
  hoveredNodeId.value = null;
}

function onScroll(): void {
  hoveredNodeId.value = null;
}

// ---- 拖动调整高度 ----
function startResize(event: MouseEvent | TouchEvent): void {
  resizing = true;
  const clientY = "touches" in event ? event.touches[0].clientY : event.clientY;
  resizeStartY = clientY;
  resizeStartHeight = containerHeight.value;
  document.addEventListener("mousemove", onResizeMove);
  document.addEventListener("mouseup", stopResize);
  document.addEventListener("touchmove", onResizeMove, { passive: false });
  document.addEventListener("touchend", stopResize);
  document.body.style.cursor = "ns-resize";
  document.body.style.userSelect = "none";
}

function onResizeMove(event: MouseEvent | TouchEvent): void {
  if (!resizing) return;
  event.preventDefault();
  const clientY = "touches" in event ? event.touches[0].clientY : (event as MouseEvent).clientY;
  const delta = clientY - resizeStartY;
  const newHeight = Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, resizeStartHeight + delta));
  containerHeight.value = newHeight;
}

function stopResize(): void {
  resizing = false;
  document.removeEventListener("mousemove", onResizeMove);
  document.removeEventListener("mouseup", stopResize);
  document.removeEventListener("touchmove", onResizeMove);
  document.removeEventListener("touchend", stopResize);
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
  try {
    localStorage.setItem(STORAGE_KEY_HEIGHT, String(containerHeight.value));
  } catch {
    // ignore
  }
}

function onResizeKeydown(event: KeyboardEvent): void {
  let delta = 0;
  if (event.key === "ArrowUp") delta = -20;
  else if (event.key === "ArrowDown") delta = 20;
  else return;
  event.preventDefault();
  containerHeight.value = Math.max(MIN_HEIGHT, Math.min(MAX_HEIGHT, containerHeight.value + delta));
  try {
    localStorage.setItem(STORAGE_KEY_HEIGHT, String(containerHeight.value));
  } catch {
    // ignore
  }
}

onMounted(() => {
  if (canvasWrapRef.value) {
    canvasWrapRef.value.addEventListener("scroll", onScroll, { passive: true });
  }
});

onBeforeUnmount(() => {
  stopResize();
  if (canvasWrapRef.value) {
    canvasWrapRef.value.removeEventListener("scroll", onScroll);
  }
});

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
  display: flex;
  flex-direction: column;
  font-size: 12px;
  overflow: hidden;
}

/* 拖动手柄 */
.timeline-resize-handle {
  flex: 0 0 auto;
  height: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: ns-resize;
  background: var(--bg-ghost, rgba(0, 0, 0, 0.04));
  border-top: 1px solid var(--border-ghost, rgba(0, 0, 0, 0.06));
  border-bottom: 1px solid var(--border-ghost, rgba(0, 0, 0, 0.06));
  transition: background 0.15s;
}

.timeline-resize-handle:hover,
.timeline-resize-handle:focus-visible {
  background: var(--bg-hover, rgba(0, 0, 0, 0.08));
  outline: none;
}

.timeline-resize-grip {
  width: 36px;
  height: 3px;
  border-radius: 2px;
  background: var(--text-faint, #aaa);
  transition: background 0.15s;
}

.timeline-resize-handle:hover .timeline-resize-grip {
  background: var(--text-muted, #666);
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
  padding: 5px 10px;
  margin: 0 4px;
  border-radius: 6px;
  background: rgba(245, 158, 11, 0.12);
  color: #b45309;
  font-size: 11px;
  line-height: 1.4;
  flex: 0 0 auto;
}

.timeline-detached-banner .material-symbols-rounded {
  font-size: 16px;
  flex: 0 0 auto;
}

.timeline-legend {
  list-style: none;
  margin: 2px 4px;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 3px 8px;
  font-size: 11px;
  flex: 0 0 auto;
}

.timeline-legend-item {
  display: flex;
  align-items: center;
  gap: 4px;
  max-width: 160px;
  padding: 1px 6px;
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

.timeline-axis-hint {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 8px 2px;
  font-size: 10px;
  color: var(--text-faint, #999);
  flex: 0 0 auto;
}

.timeline-axis-arrow {
  flex: 1 1 auto;
  text-align: center;
  border-bottom: 1px dashed var(--border-ghost, rgba(0, 0, 0, 0.1));
  padding-bottom: 1px;
}

/* SVG 画布区域：占据剩余高度，底部滚动条 */
.timeline-canvas-wrap {
  flex: 1 1 auto;
  min-height: 0;
  overflow-x: auto;
  overflow-y: auto;
  scrollbar-width: thin;
}

.timeline-svg {
  display: block;
  cursor: default;
}

.timeline-lane-bg {
  opacity: 0.05;
  rx: 4;
  ry: 4;
}

.timeline-lane-bg.is-current {
  opacity: 0.09;
}

.timeline-lane-label {
  font-size: 9px;
  font-family: var(--font-mono, monospace);
  opacity: 0.5;
  pointer-events: none;
}

.timeline-edge {
  stroke-width: 2;
  fill: none;
  opacity: 0.65;
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

.timeline-node-current-ring {
  stroke-width: 2;
  opacity: 0.8;
  animation: timeline-pulse 2s ease-in-out infinite;
}

@keyframes timeline-pulse {
  0%, 100% { opacity: 0.4; }
  50% { opacity: 0.9; }
}

.timeline-node-core {
  fill: var(--bg-panel, #fff);
}

.timeline-node-label {
  font-size: 9px;
  font-weight: 600;
  fill: var(--accent, #3b82f6);
  pointer-events: none;
  text-shadow: 0 0 4px var(--bg-panel, #fff);
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
