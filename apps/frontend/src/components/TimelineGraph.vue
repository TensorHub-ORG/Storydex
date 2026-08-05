<template>
  <div ref="rootRef" class="wl" :class="[`is-${density}`]" :style="rootStyle">
    <!-- 工具条 -->
    <header v-if="hasNodes" class="wl-toolbar">
      <span class="wl-stat" :title="`${branches.length} 条世界线 · ${rawNodes.length} 个版本节点`">
        <span class="material-symbols-rounded">account_tree</span>
        <span>{{ branches.length }} 条线 · {{ rawNodes.length }} 个节点</span>
      </span>

      <span class="wl-toolbar-spacer"></span>

      <button class="wl-tool-btn" type="button" title="缩小" :disabled="zoom <= MIN_ZOOM" @click="zoomBy(-ZOOM_STEP)">
        <span class="material-symbols-rounded">zoom_out</span>
      </button>
      <button class="wl-tool-btn" type="button" title="放大" :disabled="zoom >= MAX_ZOOM" @click="zoomBy(ZOOM_STEP)">
        <span class="material-symbols-rounded">zoom_in</span>
      </button>
      <button class="wl-tool-btn" type="button" title="回到当前所在节点" @click="centerOnCurrent(true)">
        <span class="material-symbols-rounded">my_location</span>
      </button>
      <button class="wl-tool-btn" type="button" title="铺满视图" @click="fitToView">
        <span class="material-symbols-rounded">fit_screen</span>
      </button>
      <button
        v-if="density === 'compact'"
        class="wl-tool-btn"
        type="button"
        title="在主区放大查看时空线"
        @click="emit('expand')"
      >
        <span class="material-symbols-rounded">open_in_full</span>
      </button>
    </header>

    <!-- 观测态提示 -->
    <div v-if="hasNodes && detached" class="wl-observing" role="status">
      <span class="material-symbols-rounded">visibility</span>
      <span>
        观测态：你正停在一个历史节点上，不在任何世界线上。此处一旦写入并提交，会自动开辟一条新的世界线，原线不受影响。
      </span>
    </div>

    <!-- 空状态 -->
    <p v-if="!hasNodes" class="wl-empty">{{ emptyHint }}</p>

    <!-- 画布 -->
    <div
      v-else
      ref="canvasRef"
      class="wl-canvas"
      :class="{ 'is-panning': isPanning }"
      @pointerdown="onCanvasPointerDown"
      @wheel.prevent="onWheel"
      @contextmenu.prevent
    >
      <svg class="wl-svg" role="img" aria-label="时空线分支图">
        <g :transform="`translate(${panX}, ${panY}) scale(${zoom})`">
          <!-- 轨道底线：从该世界线的分叉点一直画到它的最新节点 -->
          <line
            v-for="lane in laneTracks"
            :key="`track-${lane.name}`"
            :x1="lane.x1"
            :y1="lane.y"
            :x2="lane.x2"
            :y2="lane.y"
            class="wl-track"
            :class="{ 'is-current': lane.isCurrent }"
            :stroke="laneColor(lane.lane)"
          />

          <!-- 父子连线 -->
          <path
            v-for="edge in edgePaths"
            :key="edge.key"
            :d="edge.path"
            :stroke="edge.color"
            class="wl-edge"
            :class="{ 'is-fork': edge.isFork, 'is-dimmed': edge.isDimmed }"
            fill="none"
          />

          <!-- 未提交改动：当前节点右侧的幽灵节点 -->
          <template v-if="ghost">
            <path :d="ghost.path" class="wl-edge wl-edge-ghost" fill="none" />
            <circle :cx="ghost.x" :cy="ghost.y" :r="metrics.radius" class="wl-ghost-dot" />
          </template>

          <!-- 节点 -->
          <g
            v-for="node in laidOutNodes"
            :key="node.id"
            :transform="`translate(${node.x}, ${node.y})`"
            class="wl-node"
            :class="{
              'is-current': node.isCurrent,
              'is-tip': node.isBranchHead,
              'is-active': node.id === activeId,
              'is-dimmed': isDimmed(node)
            }"
            tabindex="0"
            role="button"
            :aria-label="nodeAriaLabel(node)"
            @pointerdown.stop
            @click.stop="onNodeClick(node)"
            @contextmenu.prevent.stop="onNodeClick(node)"
            @keydown.enter.prevent="onNodeClick(node)"
            @keydown.space.prevent="onNodeClick(node)"
            @mouseenter="hoveredId = node.id"
            @mouseleave="hoveredId = null"
            @focus="hoveredId = node.id"
            @blur="hoveredId = null"
          >
            <circle
              v-if="node.isCurrent"
              :r="metrics.radius + 5"
              class="wl-node-halo"
              :stroke="laneColor(node.row)"
              fill="none"
            />
            <circle
              v-if="node.isBranchHead"
              :r="metrics.radius + 2.5"
              class="wl-node-ring"
              :stroke="laneColor(node.row)"
              fill="none"
            />
            <circle :r="metrics.radius" class="wl-node-dot" :fill="laneColor(node.row)" />
            <circle v-if="node.isCurrent" :r="metrics.radius - 2.5" class="wl-node-core" />
          </g>
        </g>
      </svg>

      <!-- 世界线名（固定在左侧，不随缩放变小） -->
      <div class="wl-lane-labels">
        <button
          v-for="lane in laneTracks"
          :key="`label-${lane.name}`"
          class="wl-lane-chip"
          :class="{ 'is-current': lane.isCurrent }"
          type="button"
          :style="{ top: `${panY + lane.y * zoom}px`, borderColor: laneColor(lane.lane) }"
          :title="laneChipTitle(lane)"
          @pointerdown.stop
          @click.stop="openLaneMenu(lane)"
        >
          <span class="wl-lane-dot" :style="{ background: laneColor(lane.lane) }"></span>
          <span class="wl-lane-name">{{ lane.name }}</span>
          <span v-if="lane.isCurrent" class="wl-lane-badge">当前</span>
        </button>
      </div>

      <!-- 悬浮信息 / 节点动作 -->
      <div
        v-if="popoverNode"
        ref="popoverRef"
        class="wl-popover"
        :class="{ 'is-pinned': Boolean(pinnedId) }"
        :style="popoverStyle"
        role="dialog"
        @pointerdown.stop
        @click.stop
      >
        <div class="wl-pop-subject">{{ popoverNode.subject || "（没有写说明）" }}</div>
        <div class="wl-pop-meta">
          <code>{{ popoverNode.shortId }}</code>
          <span>{{ popoverNode.authorName }}</span>
          <span>{{ formatTimestamp(popoverNode.authoredAt) }}</span>
        </div>
        <div v-if="popoverTags.length > 0" class="wl-pop-tags">
          <span
            v-for="tag in popoverTags"
            :key="tag.label"
            class="wl-pop-tag"
            :class="tag.tone"
          >{{ tag.label }}</span>
        </div>

        <template v-if="pinnedId">
          <div class="wl-pop-actions">
            <button
              class="wl-pop-action"
              type="button"
              :disabled="popoverNode.isCurrent || busy"
              :title="popoverNode.isCurrent ? '已经在这个节点上了' : '把项目所有文件恢复到这个节点的状态'"
              @click="act('jump', popoverNode.id)"
            >
              <span class="material-symbols-rounded">restart_alt</span>
              <span>{{ jumpActionLabel(popoverNode) }}</span>
            </button>
            <button
              class="wl-pop-action"
              type="button"
              :disabled="busy"
              title="从这个节点分出一条新的世界线，并给它起个名字"
              @click="act('fork', popoverNode.id)"
            >
              <span class="material-symbols-rounded">alt_route</span>
              <span>从这里开辟新世界线</span>
            </button>
            <button
              class="wl-pop-action"
              type="button"
              :disabled="busy"
              title="查看这个节点相对上一个节点改了哪些文件"
              @click="act('inspect', popoverNode.id)"
            >
              <span class="material-symbols-rounded">difference</span>
              <span>这个节点改了什么</span>
            </button>
          </div>
          <p class="wl-pop-hint">按 Esc 关闭</p>
        </template>
        <p v-else class="wl-pop-hint">点击节点查看可执行的操作</p>
      </div>

      <!-- 世界线菜单 -->
      <div
        v-if="laneMenu"
        class="wl-popover wl-lane-menu is-pinned"
        :style="laneMenuStyle"
        role="dialog"
        @pointerdown.stop
        @click.stop
      >
        <div class="wl-pop-subject">{{ laneMenu.name }}</div>
        <div class="wl-pop-meta">
          <span>独有 {{ laneMenu.commitCount }} 个版本</span>
          <span>共 {{ laneMenu.totalCount }} 个</span>
        </div>
        <div class="wl-pop-actions">
          <button
            class="wl-pop-action"
            type="button"
            :disabled="laneMenu.isCurrent || busy"
            :title="laneMenu.isCurrent ? '已经在这条世界线上了' : '切换到这条世界线的最新节点'"
            @click="act('jump', laneMenu.head)"
          >
            <span class="material-symbols-rounded">restart_alt</span>
            <span>切换到这条世界线</span>
          </button>
          <button class="wl-pop-action" type="button" :disabled="busy" @click="act('renameWorldline', laneMenu.name)">
            <span class="material-symbols-rounded">edit</span>
            <span>重命名</span>
          </button>
          <button
            class="wl-pop-action is-danger"
            type="button"
            :disabled="laneMenu.isCurrent || branches.length <= 1 || busy"
            :title="deleteLaneTitle(laneMenu)"
            @click="act('deleteWorldline', laneMenu.name)"
          >
            <span class="material-symbols-rounded">delete</span>
            <span>删除这条世界线</span>
          </button>
        </div>
        <p class="wl-pop-hint">删除不可逆：这条线独有的版本会被永久丢弃</p>
      </div>
    </div>

    <!-- 高度拖拽手柄（仅侧栏紧凑模式） -->
    <div
      v-if="density === 'compact'"
      class="wl-resize"
      role="separator"
      aria-orientation="horizontal"
      aria-label="拖动调整时空线高度"
      tabindex="0"
      @pointerdown.prevent="startResize"
      @keydown="onResizeKeydown"
    >
      <span class="wl-resize-grip"></span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import type {
  WorkspaceGitTimelineBranch,
  WorkspaceGitTimelineNode,
  WorkspaceGitTimelineResponse
} from "@/types/workspace";

const props = withDefaults(
  defineProps<{
    timeline: WorkspaceGitTimelineResponse | null;
    /** 是否正在加载（用于空状态文案区分）。 */
    loading?: boolean;
    /** 当前是否处于观测态（来自 store，避免 timeline 刷新前的不一致）。 */
    detachedOverride?: boolean;
    /** 工作区是否有未提交改动，用于在当前节点右侧画一个幽灵节点。 */
    dirty?: boolean;
    /** 有写操作在进行中时禁用节点动作。 */
    busy?: boolean;
    /** compact = 侧栏，full = 主区放大视图。 */
    density?: "compact" | "full";
  }>(),
  { density: "compact" }
);

const emit = defineEmits<{
  (e: "jump", commitId: string): void;
  (e: "fork", commitId: string): void;
  (e: "inspect", commitId: string): void;
  (e: "renameWorldline", name: string): void;
  (e: "deleteWorldline", name: string): void;
  (e: "expand"): void;
}>();

// ---- 布局常量 ----
const DENSITY = {
  compact: { column: 38, row: 34, radius: 6, padX: 18, padY: 24 },
  full: { column: 64, row: 56, radius: 9, padX: 32, padY: 40 }
} as const;

const MIN_ZOOM = 0.4;
const MAX_ZOOM = 2.4;
const ZOOM_STEP = 0.2;
const MIN_HEIGHT = 140;
const MAX_HEIGHT = 640;
const DEFAULT_HEIGHT = 280;
const STORAGE_KEY_HEIGHT = "storydex.worldline.height";

/**
 * 世界线配色。lane 0（当前世界线）固定用强调蓝，其余循环取色。写作者靠颜色
 * 区分平行世界线，所以相邻 lane 的颜色必须拉开距离。
 */
const LANE_COLORS = [
  "#3b82f6", "#22c55e", "#f59e0b", "#ec4899",
  "#8b5cf6", "#14b8a6", "#ef4444", "#6366f1"
];

const rootRef = ref<HTMLElement | null>(null);
const canvasRef = ref<HTMLElement | null>(null);
const popoverRef = ref<HTMLElement | null>(null);

const zoom = ref(1);
const panX = ref(0);
const panY = ref(0);
const isPanning = ref(false);
const hoveredId = ref<string | null>(null);
const pinnedId = ref<string | null>(null);
const laneMenu = ref<LaneTrack | null>(null);
const containerHeight = ref(DEFAULT_HEIGHT);

let panStart = { x: 0, y: 0, panX: 0, panY: 0 };
let resizeStart = { y: 0, height: 0 };

try {
  const saved = localStorage.getItem(STORAGE_KEY_HEIGHT);
  const value = saved ? parseInt(saved, 10) : NaN;
  if (Number.isFinite(value) && value >= MIN_HEIGHT && value <= MAX_HEIGHT) {
    containerHeight.value = value;
  }
} catch {
  // localStorage 不可用时用默认高度即可
}

const metrics = computed(() => DENSITY[props.density]);
/** 紧凑模式下高度由用户拖拽决定；放大模式铺满主区。 */
const rootStyle = computed(() =>
  props.density === "compact" ? { height: `${containerHeight.value}px` } : {}
);
const detached = computed(() => Boolean(props.detachedOverride || props.timeline?.detached));
const branches = computed<WorkspaceGitTimelineBranch[]>(() => props.timeline?.branches || []);
const rawNodes = computed<WorkspaceGitTimelineNode[]>(() => props.timeline?.nodes || []);
const hasNodes = computed(() => rawNodes.value.length > 0);
const busy = computed(() => Boolean(props.busy));

const emptyHint = computed(() => {
  if (props.loading) return "正在读取时空线…";
  if (!props.timeline?.initialized) return "还没有启用版本记录。启用后，每次提交都会在这里留下一个节点。";
  return "还没有任何版本节点。先提交一次，为这条世界线留下起点。";
});

interface LaidOutNode extends WorkspaceGitTimelineNode {
  x: number;
  y: number;
}

const laidOutNodes = computed<LaidOutNode[]>(() =>
  rawNodes.value.map((node) => ({
    ...node,
    x: metrics.value.padX + node.column * metrics.value.column,
    y: metrics.value.padY + node.row * metrics.value.row
  }))
);

const nodeById = computed(() => {
  const map = new Map<string, LaidOutNode>();
  for (const node of laidOutNodes.value) map.set(node.id, node);
  return map;
});

const currentNode = computed(() => laidOutNodes.value.find((node) => node.isCurrent) || null);

/** 当前聚焦的节点：钉住的优先于悬浮的。 */
const activeId = computed(() => pinnedId.value || hoveredId.value);
const popoverNode = computed(() => (activeId.value ? nodeById.value.get(activeId.value) || null : null));

/** 聚焦某个节点时，把不在它那条世界线上的节点压暗，让这条线自己浮出来。 */
const focusBranch = computed(() => (popoverNode.value ? popoverNode.value.laneBranch : ""));

function isDimmed(node: LaidOutNode): boolean {
  if (!focusBranch.value) return false;
  return !node.branches.includes(focusBranch.value);
}

interface LaneTrack {
  name: string;
  lane: number;
  isCurrent: boolean;
  head: string;
  commitCount: number;
  totalCount: number;
  x1: number;
  x2: number;
  y: number;
}

/**
 * 每条世界线的轨道底线：从它的分叉列画到它最新节点所在的列。这条底线让
 * "这条线从哪分出去、写到哪儿了" 在没有节点的空档处也能看清。
 */
const laneTracks = computed<LaneTrack[]>(() =>
  branches.value.map((branch) => ({
    name: branch.name,
    lane: branch.lane,
    isCurrent: branch.isCurrent,
    head: branch.head,
    commitCount: branch.commitCount ?? 0,
    totalCount: branch.totalCount ?? 0,
    x1: metrics.value.padX + Math.max(0, branch.forkColumn - 1) * metrics.value.column,
    x2: metrics.value.padX + branch.tipColumn * metrics.value.column,
    y: metrics.value.padY + branch.lane * metrics.value.row
  }))
);

interface EdgePath {
  key: string;
  path: string;
  color: string;
  isFork: boolean;
  isDimmed: boolean;
}

/**
 * 父子连线。父节点在左（column 小），子节点在右。同轨道走直线；跨轨道说明
 * 这里是一次分叉，走一段 S 形曲线落到子节点的轨道上。
 */
const edgePaths = computed<EdgePath[]>(() => {
  const result: EdgePath[] = [];
  for (const edge of props.timeline?.edges || []) {
    const parent = nodeById.value.get(edge.from);
    const child = nodeById.value.get(edge.to);
    if (!parent || !child) continue;
    const isFork = parent.y !== child.y;
    const bend = Math.max(10, Math.abs(child.x - parent.x) * 0.55);
    const path = isFork
      ? `M ${parent.x},${parent.y} C ${parent.x + bend},${parent.y} ${child.x - bend},${child.y} ${child.x},${child.y}`
      : `M ${parent.x},${parent.y} L ${child.x},${child.y}`;
    result.push({
      key: `${edge.from}-${edge.to}`,
      path,
      color: laneColor(child.row),
      isFork,
      isDimmed: isDimmed(parent) && isDimmed(child)
    });
  }
  return result;
});

/**
 * 未提交改动的幽灵节点。写作者最常问的问题是"我刚写的东西进版本了吗"，
 * 在当前节点右边画一个虚线节点，比任何文字都直接。
 */
const ghost = computed(() => {
  if (!props.dirty || !currentNode.value) return null;
  const from = currentNode.value;
  const x = from.x + metrics.value.column;
  const y = from.y;
  return { x, y, path: `M ${from.x},${from.y} L ${x},${y}` };
});

const contentSize = computed(() => {
  let maxX = 0;
  let maxY = 0;
  for (const node of laidOutNodes.value) {
    if (node.x > maxX) maxX = node.x;
    if (node.y > maxY) maxY = node.y;
  }
  return {
    width: maxX + metrics.value.padX + metrics.value.column,
    height: maxY + metrics.value.padY
  };
});

// ---- 悬浮层定位 ----
// 位置由 panX/panY/zoom 直接算出，全部是响应式 ref。旧实现在 computed 里读
// getBoundingClientRect，缩放和滚动都不会触发重算，浮层会飘在错误的位置。
const POPOVER_WIDTH = 236;

function anchorFor(x: number, y: number) {
  const canvas = canvasRef.value;
  const width = canvas?.clientWidth ?? 320;
  const height = canvas?.clientHeight ?? 240;
  const screenX = panX.value + x * zoom.value;
  const screenY = panY.value + y * zoom.value;
  const flip = screenX + POPOVER_WIDTH + 24 > width;
  const left = flip ? screenX - POPOVER_WIDTH - 14 : screenX + 16;
  return {
    left: `${Math.round(Math.min(Math.max(6, left), Math.max(6, width - POPOVER_WIDTH - 6)))}px`,
    top: `${Math.round(Math.min(Math.max(6, screenY - 30), Math.max(6, height - 96)))}px`
  };
}

const popoverStyle = computed(() =>
  popoverNode.value ? anchorFor(popoverNode.value.x, popoverNode.value.y) : { display: "none" }
);

const laneMenuStyle = computed(() =>
  laneMenu.value ? anchorFor(laneMenu.value.x1, laneMenu.value.y) : { display: "none" }
);

const popoverTags = computed(() => {
  const node = popoverNode.value;
  if (!node) return [] as Array<{ label: string; tone: string }>;
  const tags: Array<{ label: string; tone: string }> = [];
  if (node.isCurrent) tags.push({ label: detached.value ? "当前（观测态）" : "当前所在", tone: "is-current" });
  for (const name of node.headBranches) {
    tags.push({ label: `${name} 的最新`, tone: "is-tip" });
  }
  if (node.headBranches.length === 0 && node.laneBranch) {
    tags.push({ label: node.laneBranch, tone: "is-muted" });
  }
  return tags;
});

function laneColor(lane: number): string {
  return LANE_COLORS[Math.abs(lane) % LANE_COLORS.length];
}

function laneChipTitle(lane: LaneTrack): string {
  const own = `独有 ${lane.commitCount} 个版本，共 ${lane.totalCount} 个`;
  return lane.isCurrent ? `${lane.name}（当前所在）\n${own}` : `${lane.name}\n${own}\n点击查看操作`;
}

function deleteLaneTitle(lane: LaneTrack): string {
  if (lane.isCurrent) return "不能删除自己正待着的世界线";
  if (branches.value.length <= 1) return "这是项目里最后一条世界线";
  return `永久丢弃这条线独有的 ${lane.commitCount} 个版本`;
}

function jumpActionLabel(node: LaidOutNode): string {
  if (node.isCurrent) return "已在此节点";
  return node.isBranchHead ? "切换到这条世界线" : "跳转到此节点（观测）";
}

function nodeAriaLabel(node: LaidOutNode): string {
  const parts = [node.subject, node.shortId, node.authorName, formatTimestamp(node.authoredAt)];
  if (node.isCurrent) parts.push("当前所在节点");
  if (node.headBranches.length > 0) parts.push(`世界线最新节点：${node.headBranches.join("、")}`);
  return parts.join("，");
}

// ---- 交互 ----
function onNodeClick(node: LaidOutNode): void {
  laneMenu.value = null;
  pinnedId.value = pinnedId.value === node.id ? null : node.id;
  hoveredId.value = node.id;
}

function openLaneMenu(lane: LaneTrack): void {
  pinnedId.value = null;
  laneMenu.value = laneMenu.value?.name === lane.name ? null : lane;
}

function act(
  action: "jump" | "fork" | "inspect" | "renameWorldline" | "deleteWorldline",
  payload: string
): void {
  closeOverlays();
  switch (action) {
    case "jump":
      emit("jump", payload);
      break;
    case "fork":
      emit("fork", payload);
      break;
    case "inspect":
      emit("inspect", payload);
      break;
    case "renameWorldline":
      emit("renameWorldline", payload);
      break;
    case "deleteWorldline":
      emit("deleteWorldline", payload);
      break;
  }
}

function closeOverlays(): void {
  pinnedId.value = null;
  laneMenu.value = null;
  hoveredId.value = null;
}

function onCanvasPointerDown(event: PointerEvent): void {
  closeOverlays();
  if (event.button !== 0 && event.button !== 1) return;
  isPanning.value = true;
  panStart = { x: event.clientX, y: event.clientY, panX: panX.value, panY: panY.value };
  window.addEventListener("pointermove", onPanMove);
  window.addEventListener("pointerup", onPanUp);
}

function onPanMove(event: PointerEvent): void {
  if (!isPanning.value) return;
  panX.value = panStart.panX + (event.clientX - panStart.x);
  panY.value = panStart.panY + (event.clientY - panStart.y);
}

function onPanUp(): void {
  isPanning.value = false;
  window.removeEventListener("pointermove", onPanMove);
  window.removeEventListener("pointerup", onPanUp);
}

/** 滚轮始终以指针为锚点缩放；拖拽画布负责平移。 */
function onWheel(event: WheelEvent): void {
  const canvas = canvasRef.value;
  const rect = canvas?.getBoundingClientRect();
  const originX = rect ? event.clientX - rect.left : 0;
  const originY = rect ? event.clientY - rect.top : 0;
  zoomAt(event.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP, originX, originY);
}

function zoomAt(delta: number, originX: number, originY: number): void {
  const next = clamp(zoom.value + delta, MIN_ZOOM, MAX_ZOOM);
  if (next === zoom.value) return;
  // 让锚点下的内容保持不动：先换算出锚点对应的内容坐标，再反推新的平移量。
  const contentX = (originX - panX.value) / zoom.value;
  const contentY = (originY - panY.value) / zoom.value;
  zoom.value = next;
  panX.value = originX - contentX * next;
  panY.value = originY - contentY * next;
}

function zoomBy(delta: number): void {
  const canvas = canvasRef.value;
  zoomAt(delta, (canvas?.clientWidth ?? 300) / 2, (canvas?.clientHeight ?? 200) / 2);
}

/** 把当前所在节点移到视图中央。项目一大，"我在哪" 就是第一个问题。 */
function centerOnCurrent(closeMenus = false): void {
  if (closeMenus) closeOverlays();
  const node = currentNode.value || laidOutNodes.value[laidOutNodes.value.length - 1];
  const canvas = canvasRef.value;
  if (!node || !canvas) return;
  panX.value = canvas.clientWidth / 2 - node.x * zoom.value;
  panY.value = canvas.clientHeight / 2 - node.y * zoom.value;
}

/** 缩放到整棵树刚好放得下，用于快速看清全局结构。 */
function fitToView(): void {
  closeOverlays();
  const canvas = canvasRef.value;
  const { width, height } = contentSize.value;
  if (!canvas || width <= 0 || height <= 0) return;
  const next = clamp(
    Math.min(canvas.clientWidth / width, canvas.clientHeight / height),
    MIN_ZOOM,
    1
  );
  zoom.value = next;
  panX.value = (canvas.clientWidth - width * next) / 2;
  panY.value = (canvas.clientHeight - height * next) / 2;
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape" && (pinnedId.value || laneMenu.value)) {
    event.stopPropagation();
    closeOverlays();
  }
}

// ---- 高度拖拽（紧凑模式） ----
function startResize(event: PointerEvent): void {
  resizeStart = { y: event.clientY, height: containerHeight.value };
  window.addEventListener("pointermove", onResizeMove);
  window.addEventListener("pointerup", stopResize);
  document.body.style.cursor = "ns-resize";
  document.body.style.userSelect = "none";
}

function onResizeMove(event: PointerEvent): void {
  containerHeight.value = clamp(resizeStart.height + (event.clientY - resizeStart.y), MIN_HEIGHT, MAX_HEIGHT);
}

function stopResize(): void {
  window.removeEventListener("pointermove", onResizeMove);
  window.removeEventListener("pointerup", stopResize);
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
  persistHeight();
}

function onResizeKeydown(event: KeyboardEvent): void {
  const delta = event.key === "ArrowUp" ? -20 : event.key === "ArrowDown" ? 20 : 0;
  if (!delta) return;
  event.preventDefault();
  containerHeight.value = clamp(containerHeight.value + delta, MIN_HEIGHT, MAX_HEIGHT);
  persistHeight();
}

function persistHeight(): void {
  try {
    localStorage.setItem(STORAGE_KEY_HEIGHT, String(containerHeight.value));
  } catch {
    // 存不下就算了，高度只是个体验偏好
  }
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
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

// 首次拿到节点数据时把当前节点居中，之后切换所在节点也跟着走。
watch(
  () => currentNode.value?.id,
  (id) => {
    if (!id) return;
    void nextTick(() => centerOnCurrent());
  },
  { immediate: true }
);

watch(
  () => props.density,
  () => {
    zoom.value = 1;
    void nextTick(() => fitToView());
  }
);

onMounted(() => {
  window.addEventListener("keydown", onKeydown);
  void nextTick(() => centerOnCurrent());
});

onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeydown);
  onPanUp();
  stopResize();
});

defineExpose({
  __testUtils: import.meta.env.MODE === "test" ? {
    zoom, panX, panY, pinnedId, hoveredId, laneMenu, containerHeight,
    laidOutNodes, laneTracks, edgePaths, ghost, popoverTags, contentSize,
    laneColor, laneChipTitle, deleteLaneTitle, jumpActionLabel, nodeAriaLabel,
    isDimmed, onNodeClick, openLaneMenu, act, closeOverlays,
    onWheel, zoomBy, zoomAt, centerOnCurrent, fitToView, clamp, formatTimestamp
  } : null
});
</script>

<style scoped>
.wl {
  position: relative;
  display: flex;
  flex-direction: column;
  width: 100%;
  min-height: 0;
  font-size: 12px;
  overflow: hidden;
}

.wl.is-compact {
  flex: 0 0 auto;
}

.wl.is-full {
  height: 100%;
  flex: 1 1 auto;
}

/* ---------- 工具条 ---------- */

.wl-toolbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 4px 6px;
  border-bottom: 1px solid var(--border-ghost);
}

.wl-toolbar-spacer {
  flex: 1 1 auto;
}

.wl-stat {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding-left: 2px;
  color: var(--text-muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
}

.wl-stat .material-symbols-rounded {
  font-size: 14px;
}

.wl-tool-btn {
  flex: 0 0 auto;
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: var(--radius-sm, 4px);
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.wl-tool-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-main);
}

.wl-tool-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.wl-tool-btn .material-symbols-rounded {
  font-size: 16px;
}

/* ---------- 观测态提示 ---------- */

.wl-observing {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 6px 6px 0;
  padding: 6px 8px;
  border-radius: var(--radius-sm, 4px);
  background: color-mix(in srgb, var(--warning) 12%, transparent);
  color: var(--warning);
  font-size: 11px;
  line-height: 1.5;
}

.wl-observing .material-symbols-rounded {
  flex: 0 0 auto;
  font-size: 15px;
}

.wl-empty {
  margin: 12px 10px;
  padding: 18px 12px;
  text-align: center;
  color: var(--text-muted);
  line-height: 1.7;
  background: var(--bg-card);
  border: 1px solid var(--border-ghost);
  border-radius: var(--radius-md, 6px);
}

/* ---------- 画布 ---------- */

.wl-canvas {
  position: relative;
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
  cursor: grab;
  touch-action: none;
}

.wl-canvas.is-panning {
  cursor: grabbing;
}

.wl-svg {
  display: block;
  width: 100%;
  height: 100%;
}

.wl-track {
  stroke-width: 1.5;
  opacity: 0.18;
  stroke-linecap: round;
}

.wl-track.is-current {
  opacity: 0.3;
}

.wl-edge {
  stroke-width: 2;
  stroke-linecap: round;
  opacity: 0.75;
  transition: opacity 0.15s ease;
}

.wl-edge.is-fork {
  stroke-width: 2.2;
}

.wl-edge.is-dimmed {
  opacity: 0.18;
}

.wl-edge-ghost {
  stroke: var(--warning);
  stroke-dasharray: 3 3;
  opacity: 0.7;
}

.wl-ghost-dot {
  fill: none;
  stroke: var(--warning);
  stroke-width: 1.6;
  stroke-dasharray: 3 3;
}

.wl-node {
  cursor: pointer;
  outline: none;
  transition: opacity 0.15s ease;
}

.wl-node.is-dimmed {
  opacity: 0.28;
}

.wl-node-dot {
  stroke: var(--bg-sidebar);
  stroke-width: 1.5;
}

.wl-node:hover .wl-node-dot,
.wl-node.is-active .wl-node-dot {
  stroke: var(--text-main);
  stroke-width: 2;
}

.wl-node:focus-visible .wl-node-dot {
  stroke: var(--accent-strong);
  stroke-width: 2.5;
}

.wl-node-ring {
  stroke-width: 1.4;
  opacity: 0.55;
}

.wl-node-halo {
  stroke-width: 2;
  opacity: 0.75;
  animation: wl-pulse 2.4s ease-in-out infinite;
}

@keyframes wl-pulse {
  0%, 100% { opacity: 0.35; }
  50% { opacity: 0.85; }
}

@media (prefers-reduced-motion: reduce) {
  .wl-node-halo { animation: none; }
}

.wl-node-core {
  fill: var(--bg-sidebar);
}

/* ---------- 世界线名 ---------- */

.wl-lane-labels {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 0;
  pointer-events: none;
}

.wl-lane-chip {
  position: absolute;
  left: 6px;
  transform: translateY(-50%);
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 150px;
  height: 19px;
  padding: 0 6px;
  border: 1px solid;
  border-radius: 10px;
  background: var(--bg-elevated);
  color: var(--text-soft);
  font: inherit;
  font-size: 10px;
  line-height: 1;
  cursor: pointer;
  pointer-events: auto;
  opacity: 0.9;
}

.wl-lane-chip:hover {
  opacity: 1;
  color: var(--text-main);
}

.wl-lane-chip.is-current {
  color: var(--text-main);
  font-weight: 700;
}

.wl-lane-dot {
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.wl-lane-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wl-lane-badge {
  flex: 0 0 auto;
  padding: 0 4px;
  border-radius: 6px;
  background: var(--accent);
  color: var(--accent-contrast);
  font-size: 9px;
}

/* ---------- 悬浮层 ---------- */

.wl-popover {
  position: absolute;
  width: 236px;
  padding: 8px 10px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md, 6px);
  background: var(--bg-elevated);
  box-shadow: var(--shadow-popover, 0 6px 20px rgba(0, 0, 0, 0.18));
  color: var(--text-main);
  font-size: 11px;
  z-index: 8;
  pointer-events: none;
}

.wl-popover.is-pinned {
  pointer-events: auto;
  z-index: 9;
}

.wl-pop-subject {
  font-size: 12px;
  font-weight: 600;
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.wl-pop-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 3px 8px;
  margin-top: 4px;
  color: var(--text-muted);
}

.wl-pop-meta code {
  padding: 0 4px;
  border-radius: 3px;
  background: var(--bg-hover);
  color: var(--text-soft);
  font-family: var(--font-mono);
  font-size: 10px;
}

.wl-pop-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 3px;
  margin-top: 6px;
}

.wl-pop-tag {
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--bg-hover);
  color: var(--text-muted);
  font-size: 10px;
  line-height: 15px;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wl-pop-tag.is-current {
  background: var(--accent);
  color: var(--accent-contrast);
  font-weight: 600;
}

.wl-pop-tag.is-tip {
  background: color-mix(in srgb, var(--accent) 16%, transparent);
  color: var(--accent-strong);
}

.wl-pop-actions {
  display: flex;
  flex-direction: column;
  gap: 1px;
  margin: 7px -4px 0;
  padding-top: 6px;
  border-top: 1px solid var(--border-ghost);
}

.wl-pop-action {
  display: flex;
  align-items: center;
  gap: 7px;
  width: 100%;
  padding: 5px 6px;
  border: 0;
  border-radius: var(--radius-sm, 4px);
  background: transparent;
  color: var(--text-main);
  font: inherit;
  font-size: 11px;
  text-align: left;
  cursor: pointer;
}

.wl-pop-action:hover:not(:disabled) {
  background: var(--bg-hover);
}

.wl-pop-action:disabled {
  color: var(--text-faint);
  cursor: not-allowed;
}

.wl-pop-action.is-danger:not(:disabled) {
  color: var(--danger);
}

.wl-pop-action.is-danger:hover:not(:disabled) {
  background: color-mix(in srgb, var(--danger) 12%, transparent);
}

.wl-pop-action .material-symbols-rounded {
  flex: 0 0 auto;
  font-size: 15px;
}

.wl-pop-hint {
  margin: 6px 0 0;
  color: var(--text-faint);
  font-size: 10px;
  line-height: 1.5;
}

.wl-lane-menu {
  width: 216px;
}

/* ---------- 高度手柄 ---------- */

.wl-resize {
  flex: 0 0 auto;
  height: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-top: 1px solid var(--border-ghost);
  cursor: ns-resize;
  touch-action: none;
}

.wl-resize:hover,
.wl-resize:focus-visible {
  background: var(--bg-hover);
  outline: none;
}

.wl-resize-grip {
  width: 34px;
  height: 3px;
  border-radius: 2px;
  background: var(--text-faint);
}
</style>
