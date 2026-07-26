<template>
  <article class="cct-turn" :class="[`is-${run.status}`]">
    <!-- 用户消息：左侧淡色圆角块；右侧 hover 浮现回滚操作 -->
    <div v-if="run.prompt" class="cct-user">
      <span class="cct-user-text">{{ run.prompt }}</span>
      <div v-if="canRollback" class="cct-user-actions">
        <button
          class="cct-user-action"
          type="button"
          title="编辑最新消息"
          aria-label="编辑最新消息"
          :disabled="actionsBusy"
          @click="emit('rollback-edit', run)"
        >
          <span class="material-symbols-rounded">edit</span>
        </button>
        <button
          class="cct-user-action danger"
          type="button"
          title="删除本轮"
          aria-label="删除本轮"
          :disabled="actionsBusy"
          @click="emit('rollback-delete', run)"
        >
          <span class="material-symbols-rounded">delete</span>
        </button>
      </div>
    </div>

    <!-- 模型回复区：完全扁平，无卡片无边框 -->
    <div class="cct-assistant">
      <template v-for="entry in entries" :key="entry.id">
        <!-- 中途插话（steer）：与首条用户消息同款 -->
        <div v-if="entry.kind === 'user'" class="cct-user cct-user-inline">
          <span class="cct-user-text">{{ entry.text }}</span>
        </div>

        <!-- 活动阶段提示：运行中的一行灰字，收敛后由 store 覆盖 -->
        <div v-else-if="entry.kind === 'phase'" class="cct-phase-text" aria-live="polite">
          {{ entry.text }}
        </div>

        <!-- 思考过程：仅当前活跃块展开，其余折叠成一行灰字 -->
        <div v-else-if="entry.kind === 'reasoning'" class="cct-line-block">
          <button class="cct-summary" type="button" @click="toggle(entry.id, entry.live)">
            <span>{{ entry.live ? "正在思考…" : "思考过程" }}</span>
            <span class="cct-chev material-symbols-rounded">
              {{ isOpen(entry.id, entry.live) ? "expand_more" : "chevron_right" }}
            </span>
          </button>
          <div v-if="isOpen(entry.id, entry.live)" class="cct-reveal cct-reasoning-text">
            {{ entry.text }}
          </div>
        </div>

        <!-- 工具调用：运行中默认展开，结束后折叠成一行摘要 -->
        <div v-else-if="entry.kind === 'tools'" class="cct-line-block">
          <button class="cct-summary" type="button" @click="toggle(entry.id, entry.live)">
            <span>{{ toolsSummary(entry) }}</span>
            <span class="cct-chev material-symbols-rounded">
              {{ isOpen(entry.id, entry.live) ? "expand_more" : "chevron_right" }}
            </span>
          </button>
          <div v-if="isOpen(entry.id, entry.live)" class="cct-reveal cct-tool-list">
            <div v-for="chunk in toolChunks(entry)" :key="chunk.id" class="cct-tool-chunk">
              <!-- 工具超过 5 个时分块，避免长列表一次铺开 -->
              <button
                v-if="entry.tools.length > TOOL_CHUNK_SIZE"
                class="cct-summary cct-chunk-head"
                type="button"
                @click="toggle(chunk.id, chunkDefaultOpen(entry, chunk))"
              >
                <span>{{ chunk.start }}–{{ chunk.end }} 共 {{ chunk.tools.length }} 项</span>
                <span class="cct-chev material-symbols-rounded">
                  {{ isOpen(chunk.id, chunkDefaultOpen(entry, chunk)) ? "expand_more" : "chevron_right" }}
                </span>
              </button>
              <div
                v-if="entry.tools.length <= TOOL_CHUNK_SIZE || isOpen(chunk.id, chunkDefaultOpen(entry, chunk))"
                class="cct-tool-chunk-list"
              >
                <div v-for="tool in chunk.tools" :key="tool.id" class="cct-tool">
                  <button class="cct-tool-head" type="button" @click="toggle(rowId(entry, tool), false)">
                    <span class="cct-tool-state material-symbols-rounded" :class="`status-${tool.status}`">
                      {{ toolStateIcon(tool.status) }}
                    </span>
                    <span class="cct-tool-name">{{ tool.toolName || tool.title || "工具" }}</span>
                    <span v-if="toolDetail(tool)" class="cct-tool-detail">{{ toolDetail(tool) }}</span>
                  </button>
                  <div v-if="isOpen(rowId(entry, tool), false)" class="cct-tool-expand">
                    <pre v-if="tool.arguments && Object.keys(tool.arguments).length">{{ compactJson(tool.arguments) }}</pre>
                    <pre v-if="tool.resultPreview" class="cct-tool-result">{{ compactText(tool.resultPreview) }}</pre>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 助手正文 -->
        <div
          v-else-if="entry.kind === 'assistant'"
          class="cct-text cct-markdown"
          @click="emit('markdown-click', $event)"
          v-html="renderMarkdown(entry.text)"
        ></div>

        <!-- 错误 -->
        <!-- 提示：客观验收未通过等警告级信息，不是错误也不该折叠 -->
        <div v-else-if="entry.kind === 'notice'" class="cct-notice-text">{{ entry.text }}</div>

        <div v-else-if="entry.kind === 'error'" class="cct-error-text">{{ entry.text }}</div>
      </template>

      <!-- 底部元信息：耗时 · token · 状态 · 无恢复点，纯文字不重复头部 -->
      <div class="cct-meta">
        <span class="cct-meta-text">{{ metaLine }}</span>
        <span
          v-if="run.noRestorePoint"
          class="cct-meta-warn"
          title="本轮没有可用恢复点"
          aria-label="本轮没有可用恢复点"
        >
          <span class="material-symbols-rounded">warning_amber</span>
          无恢复点
        </span>
      </div>
    </div>
  </article>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import MarkdownIt from "markdown-it";
import type { AgentExecutionRun, CoomiWaterfallItem, CoomiWaterfallItemStatus } from "@/types/agent";

type FlowEntry =
  | { kind: "reasoning"; id: string; text: string; lines: number; live: boolean }
  | { kind: "tools"; id: string; tools: CoomiWaterfallItem[]; status: CoomiWaterfallItemStatus; live: boolean }
  | { kind: "assistant"; id: string; text: string }
  | { kind: "user"; id: string; text: string }
  | { kind: "phase"; id: string; text: string }
  | { kind: "notice"; id: string; text: string }
  | { kind: "error"; id: string; text: string };

type ToolChunk = {
  id: string;
  start: number;
  end: number;
  tools: CoomiWaterfallItem[];
  status: CoomiWaterfallItemStatus;
};

const TOOL_CHUNK_SIZE = 5;

const props = defineProps<{
  run: AgentExecutionRun;
  /** 运行中的本地计时；完成后由 run.turnDurationMs 接管 */
  elapsedMs?: number;
  /** 本轮是否允许回滚（由父级按最新轮次与全局忙碌态判定） */
  canRollback?: boolean;
  /** 回滚按钮是否置灰 */
  actionsBusy?: boolean;
}>();

const emit = defineEmits<{
  (event: "rollback-edit", run: AgentExecutionRun): void;
  (event: "rollback-delete", run: AgentExecutionRun): void;
  (event: "markdown-click", payload: MouseEvent): void;
}>();

const md = new MarkdownIt({ html: false, linkify: true, breaks: true });

const foldState = ref<Record<string, boolean>>({});

function isOpen(id: string, fallback: boolean): boolean {
  return foldState.value[id] ?? fallback;
}
function toggle(id: string, fallback: boolean): void {
  foldState.value = { ...foldState.value, [id]: !isOpen(id, fallback) };
}

const isRunning = computed(() => props.run.status === "running");

const entries = computed<FlowEntry[]>(() => {
  const list: FlowEntry[] = [];
  let toolBucket: CoomiWaterfallItem[] = [];
  let toolSeq = 0;

  const flush = (terminal: boolean): void => {
    if (!toolBucket.length) return;
    const bucket = toolBucket;
    const running = !terminal && bucket.some((t) => t.status === "running");
    const status: CoomiWaterfallItemStatus = running
      ? "running"
      : bucket.some((t) => t.status === "error")
        ? "error"
        : "success";
    list.push({ kind: "tools", id: `${props.run.traceId}-tools-${toolSeq++}`, tools: bucket, status, live: running });
    toolBucket = [];
  };

  for (const item of props.run.items) {
    if (item.type === "usage" || item.type === "compression" || item.type === "system") continue;
    // 首条用户消息由 run.prompt 渲染，避免重复
    if (item.type === "user" && item.id === `${props.run.traceId}-user`) continue;
    if (item.type === "tool") {
      toolBucket.push(item);
      continue;
    }
    flush(true);
    if (item.type === "reasoning") {
      const lines = item.content.split(/\r?\n/).filter((line) => line.trim()).length || 1;
      list.push({ kind: "reasoning", id: item.id, text: item.content, lines, live: false });
    } else if (item.type === "assistant") {
      list.push({ kind: "assistant", id: item.id, text: item.content });
    } else if (item.type === "user") {
      list.push({ kind: "user", id: item.id, text: item.content });
    } else if (item.type === "phase") {
      list.push({ kind: "phase", id: item.id, text: item.content });
    } else if (item.type === "notice") {
      list.push({ kind: "notice", id: item.id, text: item.content });
    } else if (item.type === "error") {
      list.push({ kind: "error", id: item.id, text: item.content });
    }
  }
  flush(!isRunning.value);

  // 运行中：仅当末尾就是推理块时才算活跃（与 AgentPanel 的 isActiveReasoning 对齐）
  if (isRunning.value) {
    const last = [...list].reverse().find((entry) => entry.kind !== "phase");
    if (last?.kind === "reasoning") {
      last.live = true;
    }
  }
  return list;
});

// 运行中不显示 phase 之外的活动提示；完成后 store 会把 phase 内容覆盖为终态
function toolChunks(entry: Extract<FlowEntry, { kind: "tools" }>): ToolChunk[] {
  const chunks: ToolChunk[] = [];
  for (let index = 0; index < entry.tools.length; index += TOOL_CHUNK_SIZE) {
    const tools = entry.tools.slice(index, index + TOOL_CHUNK_SIZE);
    const running = entry.live && tools.some((t) => t.status === "running");
    chunks.push({
      id: `${entry.id}-chunk-${Math.floor(index / TOOL_CHUNK_SIZE)}`,
      start: index + 1,
      end: index + tools.length,
      tools,
      status: running ? "running" : tools.some((t) => t.status === "error") ? "error" : "success"
    });
  }
  return chunks;
}

function chunkDefaultOpen(entry: Extract<FlowEntry, { kind: "tools" }>, chunk: ToolChunk): boolean {
  return entry.live && chunk.status === "running";
}

// 折叠后的工具摘要，模仿 "Ran 2 commands, read 3 files"
function toolsSummary(entry: Extract<FlowEntry, { kind: "tools" }>): string {
  if (entry.live) {
    const running = entry.tools.find((t) => t.status === "running");
    return running ? `正在${toolVerb(running)}…` : "正在调用工具…";
  }
  const buckets = { cmd: 0, read: 0, edit: 0, search: 0, other: 0 };
  for (const tool of entry.tools) {
    buckets[toolKind(tool)] += 1;
  }
  const parts: string[] = [];
  if (buckets.cmd) parts.push(`运行 ${buckets.cmd} 条命令`);
  if (buckets.read) parts.push(`读取 ${buckets.read} 个文件`);
  if (buckets.edit) parts.push(`修改 ${buckets.edit} 个文件`);
  if (buckets.search) parts.push(`搜索 ${buckets.search} 次`);
  if (buckets.other) parts.push(`调用工具 ${buckets.other} 次`);
  return parts.join("，") || `调用工具 ${entry.tools.length} 次`;
}

function toolKind(tool: CoomiWaterfallItem): "cmd" | "read" | "edit" | "search" | "other" {
  const name = (tool.toolName || "").toLowerCase();
  if (name.startsWith("run") || name.startsWith("bash") || name.startsWith("exec") || name.startsWith("terminal")) return "cmd";
  if (name.startsWith("read") || name.startsWith("list") || name.startsWith("get") || name.startsWith("cat")) return "read";
  if (name.startsWith("write") || name.startsWith("edit") || name.startsWith("create") || name.startsWith("apply")) return "edit";
  if (name.startsWith("search") || name.startsWith("grep") || name.startsWith("find")) return "search";
  return "other";
}

function toolVerb(tool: CoomiWaterfallItem): string {
  return { cmd: "运行命令", read: "读取文件", edit: "修改文件", search: "搜索", other: "调用工具" }[toolKind(tool)];
}

function toolStateIcon(status: CoomiWaterfallItemStatus): string {
  if (status === "running") return "progress_activity";
  if (status === "error") return "close";
  return "check";
}

function toolDetail(tool: CoomiWaterfallItem): string {
  const args = tool.arguments;
  if (!args || typeof args !== "object") return "";
  for (const key of ["path", "file", "file_path", "relative_path", "query", "pattern", "command"]) {
    const value = (args as Record<string, unknown>)[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

const metaLine = computed(() => {
  const parts = [durationText.value, tokenText.value, statusText.value].filter(Boolean);
  return parts.join(" · ");
});

const durationText = computed(() => {
  // 运行中用父级传入的本地计时，完成后固定为后端权威值
  const ms = isRunning.value ? (props.elapsedMs ?? 0) : (props.run.turnDurationMs ?? props.elapsedMs ?? 0);
  if (ms <= 0) return "";
  const totalSeconds = Math.floor(ms / 1000);
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return m > 0 ? `${m}分${s}秒` : `${s}秒`;
});

const tokenText = computed(() => {
  // 单轮 token 只有 AgentCompleted 之后才有真实值，运行中留空而不是估算
  const tokens = props.run.turnTokens;
  if (tokens === null || tokens === undefined || tokens <= 0) return "";
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K tokens`;
  return `${tokens} tokens`;
});

const statusText = computed(() => {
  if (props.run.errorMessage) return "错误";
  const labels: Record<string, string> = {
    running: "正在思考…",
    completed: "已完成",
    committed: "已提交",
    preview: "待确认",
    discarded: "已丢弃",
    superseded: "已被替换",
    failed: "错误",
    stopped: "已停止",
    cancelled: "已停止"
  };
  return labels[props.run.status] || props.run.status;
});

function rowId(entry: Extract<FlowEntry, { kind: "tools" }>, tool: CoomiWaterfallItem): string {
  return `${entry.id}-${tool.id}`;
}

function renderMarkdown(value: string): string {
  return md.render(value || "");
}

function compactJson(value: unknown): string {
  try {
    return compactText(JSON.stringify(value, null, 2), 1200);
  } catch {
    return compactText(String(value ?? ""), 1200);
  }
}

function compactText(value: unknown, limit = 1200): string {
  const text = String(value ?? "").trim();
  return text.length > limit ? `${text.slice(0, limit)}\n……（已截断）` : text;
}
</script>

<style scoped>
.cct-turn {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

/* 用户消息：左侧一个很淡的圆角块 */
.cct-user {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 6px;
}

.cct-user-inline {
  margin-top: 4px;
}

.cct-user-text {
  max-width: 90%;
  padding: 8px 14px;
  border-radius: 14px;
  background: color-mix(in srgb, var(--text-main) 7%, transparent);
  color: var(--text-soft);
  font-size: 14px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* 回滚操作：hover 才浮现，避免常驻抢视觉 */
.cct-user-actions {
  display: flex;
  gap: 2px;
  opacity: 0;
  transition: opacity 0.12s ease;
}

.cct-user:hover .cct-user-actions,
.cct-user-actions:focus-within {
  opacity: 1;
}

.cct-user-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
}

.cct-user-action:hover:not(:disabled) {
  background: color-mix(in srgb, var(--text-main) 8%, transparent);
  color: var(--text-soft);
}

.cct-user-action.danger:hover:not(:disabled) {
  color: var(--danger);
}

.cct-user-action:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.cct-user-action .material-symbols-rounded {
  font-size: 16px;
}

/* 助手区：完全扁平 */
.cct-assistant {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.cct-phase-text {
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.6;
}

/* 折叠摘要行（思考/工具通用） */
.cct-line-block {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.cct-summary {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  align-self: flex-start;
  max-width: 100%;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text-muted);
  font: inherit;
  font-size: 13.5px;
  line-height: 1.5;
  text-align: left;
  cursor: pointer;
}

.cct-summary > span:first-child {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cct-summary:hover {
  color: var(--text-soft);
}

.cct-chev {
  flex: 0 0 auto;
  font-size: 17px;
  color: var(--text-faint);
}

/* 展开内容：左侧一条细线缩进，无背景卡片 */
.cct-reveal {
  margin-left: 2px;
  padding-left: 14px;
  border-left: 1px solid var(--border-subtle);
}

.cct-reasoning-text {
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* 工具展开列表 */
.cct-tool-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cct-tool-chunk {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cct-chunk-head {
  font-size: 12.5px;
}

.cct-tool-chunk-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cct-tool-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-height: 28px;
  padding: 2px 0;
  border: 0;
  background: transparent;
  color: var(--text-soft);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.cct-tool-state {
  flex: 0 0 auto;
  font-size: 15px;
  color: var(--text-faint);
}

.cct-tool-state.status-success {
  color: var(--success);
}

.cct-tool-state.status-error {
  color: var(--danger);
}

.cct-tool-state.status-running {
  color: var(--info);
  animation: cct-spin 1s linear infinite;
}

.cct-tool-name {
  flex: 0 0 auto;
  font-family: var(--font-mono);
  font-size: 12.5px;
  color: var(--text-main);
}

.cct-tool-detail {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 12px;
}

.cct-tool-head:hover .cct-tool-detail {
  color: var(--text-soft);
}

.cct-tool-expand {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 2px 0 6px 23px;
}

.cct-tool-expand pre {
  margin: 0;
  padding: 8px 10px;
  max-height: 200px;
  overflow: auto;
  border-radius: 6px;
  background: color-mix(in srgb, var(--text-main) 5%, transparent);
  color: var(--text-soft);
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* 助手正文：常规字重，无容器 */
.cct-text {
  color: var(--text-main);
  font-size: 15px;
  line-height: 1.78;
}

.cct-error-text {
  color: var(--danger);
  font-size: 14px;
  line-height: 1.7;
}

/* 客观验收未通过等警告类提示，与上游 coomi-notice-text 同色调 */
.cct-notice-text {
  color: var(--warning);
  font-size: 13px;
  line-height: 1.72;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* 底部元信息：纯文字，承载状态与时间，不与顶部重复 */
.cct-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 2px;
}

.cct-meta-text {
  color: var(--text-muted);
  font-size: 13px;
}

.cct-meta-warn {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  color: var(--warning, #d08700);
  font-size: 12.5px;
}

.cct-meta-warn .material-symbols-rounded {
  font-size: 15px;
}

/* markdown */
.cct-markdown :deep(p) {
  margin: 0 0 0.6em;
}

.cct-markdown :deep(p:last-child) {
  margin-bottom: 0;
}

.cct-markdown :deep(strong) {
  font-weight: 700;
}

.cct-markdown :deep(pre) {
  margin: 0.6em 0;
  padding: 10px 12px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--text-main) 5%, transparent);
  overflow: auto;
  font-weight: 400;
}

.cct-markdown :deep(code) {
  padding: 1px 5px;
  border-radius: 4px;
  background: color-mix(in srgb, var(--text-main) 9%, transparent);
  font-family: var(--font-mono);
  font-size: 0.86em;
  font-weight: 400;
}

.cct-markdown :deep(pre code) {
  padding: 0;
  background: transparent;
}

.cct-markdown :deep(ul),
.cct-markdown :deep(ol) {
  margin: 0.4em 0 0.7em;
  padding-left: 1.4em;
  font-weight: 400;
}

.cct-markdown :deep(li) {
  font-weight: 400;
}

.cct-markdown :deep(li + li) {
  margin-top: 0.25em;
}

@keyframes cct-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
