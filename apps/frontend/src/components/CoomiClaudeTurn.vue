<template>
  <article class="cct-turn" :class="[`is-${run.status}`]">
    <!-- 用户消息：左侧淡色圆角块；右侧 hover 浮现回滚操作 -->
    <div v-if="run.prompt" class="cct-user">
      <span class="cct-user-text">{{ run.prompt }}</span>
      <span v-if="promptTimeText" class="cct-time">{{ promptTimeText }}</span>
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
          <span v-if="entry.time" class="cct-time">{{ entry.time }}</span>
        </div>

        <!-- 活动阶段提示：运行中的一行灰字，收敛后由 store 覆盖 -->
        <div v-else-if="entry.kind === 'phase'" class="cct-phase-text" aria-live="polite">
          {{ entry.text }}
        </div>

        <!-- 思考过程：运行中保持展开；整轮结束后才折叠成一行灰字 -->
        <div v-else-if="entry.kind === 'reasoning'" class="cct-line-block">
          <button class="cct-summary" type="button" @click="toggle(entry.id, isRunning)">
            <span>{{ entry.live ? "正在思考…" : "思考过程" }}</span>
            <span class="cct-chev material-symbols-rounded">
              {{ isOpen(entry.id, isRunning) ? "expand_more" : "chevron_right" }}
            </span>
          </button>
          <div v-if="isOpen(entry.id, isRunning)" class="cct-reveal cct-reasoning-text">
            {{ entry.text }}
          </div>
        </div>

        <!-- 工具调用：运行中保持展开；整轮结束后才折叠成一行摘要 -->
        <div v-else-if="entry.kind === 'tools'" class="cct-line-block">
          <button class="cct-summary" type="button" @click="toggle(entry.id, isRunning)">
            <span>{{ toolsSummary(entry) }}</span>
            <span class="cct-chev material-symbols-rounded">
              {{ isOpen(entry.id, isRunning) ? "expand_more" : "chevron_right" }}
            </span>
          </button>
          <div v-if="isOpen(entry.id, isRunning)" class="cct-reveal cct-tool-list">
            <div v-for="tool in entry.tools" :key="tool.id" class="cct-tool">
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

        <!-- 助手正文 -->
        <div
          v-else-if="entry.kind === 'assistant'"
          class="cct-text cct-markdown"
          @click="emit('markdown-click', $event)"
          v-html="renderMarkdown(entry.text)"
        ></div>

        <div v-else-if="entry.kind === 'notice'" class="cct-notice-text">
          <strong>警告</strong>
          <span>{{ entry.text }}</span>
        </div>

        <div v-else-if="entry.kind === 'info'" class="cct-info-text">
          <strong>提示</strong>
          <span>{{ entry.text }}</span>
        </div>

        <div v-else-if="entry.kind === 'error'" class="cct-error-text">
          <strong>错误</strong>
          <div class="cct-error-content">
            <span class="cct-error-message">{{ entry.text }}</span>
            <button
              v-if="canRetry && entry.id === retryEntryId"
              class="cct-error-retry"
              type="button"
              title="重新生成"
              aria-label="重新生成"
              :disabled="actionsBusy"
              @click="emit('retry', run)"
            >
              <span class="material-symbols-rounded" :class="{ spinning: actionsBusy }">refresh</span>
            </button>
            <button
              v-if="entry.id === retryEntryId"
              class="cct-error-retry"
              type="button"
              title="反馈报错"
              aria-label="反馈报错"
              @click="emit('feedback', run)"
            >
              <span class="material-symbols-rounded">send</span>
            </button>
          </div>
        </div>
      </template>

      <div v-if="toolFeedbackVisible" class="cct-tool-feedback" role="status">
        <span class="material-symbols-rounded cct-tool-feedback-icon">warning_amber</span>
        <div class="cct-tool-feedback-copy">
          <strong>本轮有 {{ toolFailureCount }} 次工具调用失败</strong>
          <span>{{ toolFeedbackMessage }}</span>
          <small v-if="toolFeedbackError">{{ toolFeedbackError }}</small>
        </div>
        <button
          v-if="toolFeedbackState !== 'complete'"
          type="button"
          :disabled="toolFeedbackBusy"
          @click="handleToolFailureFeedback"
        >
          <span class="material-symbols-rounded">{{ toolFeedbackBusy ? "progress_activity" : "send" }}</span>
          {{ toolFeedbackButtonLabel }}
        </button>
        <span v-else class="cct-tool-feedback-done">
          <span class="material-symbols-rounded">check_circle</span>
          已完成
        </span>
      </div>

      <!-- 底部元信息：耗时 · token · 状态 · 无恢复点，纯文字不重复头部；右侧为完成时刻 -->
      <div class="cct-meta">
        <span class="cct-meta-text" :title="tokenTooltip || undefined">{{ metaLine }}</span>
        <span
          v-if="run.noRestorePoint"
          class="cct-meta-warn"
          title="本轮没有可用恢复点"
          aria-label="本轮没有可用恢复点"
        >
          <span class="material-symbols-rounded">warning_amber</span>
          无恢复点
        </span>
        <span v-if="completedTimeText" class="cct-meta-time">{{ completedTimeText }}</span>
      </div>
    </div>
  </article>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { analyzeToolFailures, submitFeedback } from "@/api/system";
import type { AgentExecutionRun, CoomiWaterfallItem, CoomiWaterfallItemStatus } from "@/types/agent";
import type { ToolFailureAnalysisResponse } from "@/types/system";
import { createMarkdownRenderer } from "@/utils/markdown";
import { buildToolFailureTrace } from "@/utils/toolFailureFeedback";

type FlowEntry =
  | { kind: "reasoning"; id: string; text: string; lines: number; live: boolean }
  | { kind: "tools"; id: string; tools: CoomiWaterfallItem[]; status: CoomiWaterfallItemStatus; live: boolean }
  | { kind: "assistant"; id: string; text: string }
  | { kind: "user"; id: string; text: string; time: string }
  | { kind: "phase"; id: string; text: string }
  | { kind: "notice"; id: string; text: string }
  | { kind: "info"; id: string; text: string }
  | { kind: "error"; id: string; text: string };

type ToolFeedbackState = "consent" | "analyzing" | "ready" | "uploading" | "analysis_failed" | "upload_failed" | "complete";

const props = defineProps<{
  run: AgentExecutionRun;
  /** 运行中的本地计时；完成后由 run.turnDurationMs 接管 */
  elapsedMs?: number;
  /** 本轮是否允许回滚（由父级按最新轮次与全局忙碌态判定） */
  canRollback?: boolean;
  /** 本轮失败后是否允许重新生成 */
  canRetry?: boolean;
  /** 回滚按钮是否置灰 */
  actionsBusy?: boolean;
}>();

const emit = defineEmits<{
  (event: "rollback-edit", run: AgentExecutionRun): void;
  (event: "rollback-delete", run: AgentExecutionRun): void;
  (event: "retry", run: AgentExecutionRun): void;
  (event: "feedback", run: AgentExecutionRun): void;
  (event: "markdown-click", payload: MouseEvent): void;
}>();

const md = createMarkdownRenderer({}, { linkifyWorkspaceMarkdownFiles: true });

const foldState = ref<Record<string, boolean>>({});
const toolFeedbackState = ref<ToolFeedbackState>("consent");
const toolFeedbackError = ref("");
const toolAnalysis = ref<ToolFailureAnalysisResponse | null>(null);

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
      list.push({ kind: "user", id: item.id, text: item.content, time: formatClock(item.timestamp) });
    } else if (item.type === "phase") {
      list.push({ kind: "phase", id: item.id, text: item.content });
    } else if (item.type === "notice") {
      list.push({ kind: "notice", id: item.id, text: item.content });
    } else if (item.type === "info") {
      list.push({ kind: "info", id: item.id, text: item.content });
    } else if (item.type === "error") {
      list.push({ kind: "error", id: item.id, text: item.content });
    }
  }
  flush(!isRunning.value);

  // live 只决定摘要文案（“正在思考…”/“正在调用工具…”）；
  // 展开态统一由 isRunning 决定：运行中全程展开，整轮结束后才折叠
  if (isRunning.value) {
    const last = [...list].reverse().find((entry) => entry.kind !== "phase");
    if (last?.kind === "reasoning") {
      last.live = true;
    }
  }
  const terminalError = props.run.errorMessage.trim();
  if (terminalError && !list.some((entry) => entry.kind === "error")) {
    list.push({ kind: "error", id: `${props.run.traceId}-terminal-error`, text: terminalError });
  }
  return list;
});

const retryEntryId = computed(() => {
  const error = [...entries.value].reverse().find((entry) => entry.kind === "error");
  return error?.id || "";
});

const toolFailureTrace = computed(() => buildToolFailureTrace(props.run));
const toolFailureCount = computed(() => toolFailureTrace.value.filter((item) => item.status === "error").length);
const toolFeedbackVisible = computed(() => !isRunning.value && toolFailureCount.value >= 3);
const toolFeedbackBusy = computed(() => ["analyzing", "uploading"].includes(toolFeedbackState.value));
const toolFeedbackButtonLabel = computed(() => {
  if (toolFeedbackState.value === "analyzing") return "正在本地整理";
  if (toolFeedbackState.value === "uploading") return "正在上传";
  if (toolFeedbackState.value === "analysis_failed") return "重新整理并反馈";
  if (toolFeedbackState.value === "upload_failed") return "重新上传";
  return "同意脱敏分析并反馈";
});
const toolFeedbackMessage = computed(() => {
  if (toolFeedbackState.value === "consent") {
    return "确认后才会额外调用一次当前 Provider（low、无工具），只分析脱敏工具轨迹并自动上传。";
  }
  if (toolFeedbackState.value === "analyzing") return "正在本机通过当前 Provider 生成脱敏工程分析，可以继续工作。";
  if (toolFeedbackState.value === "ready" || toolFeedbackState.value === "uploading") return "脱敏分析已生成，正在提交反馈。";
  if (toolFeedbackState.value === "analysis_failed") return "分析失败，未上传任何内容。";
  if (toolFeedbackState.value === "upload_failed") return "分析已保留；重新上传不会再次调用模型。";
  return "脱敏分析与反馈均已完成。";
});

async function handleToolFailureFeedback(): Promise<void> {
  if (toolFeedbackBusy.value || toolFeedbackState.value === "complete") return;
  toolFeedbackError.value = "";
  if (!toolAnalysis.value) {
    toolFeedbackState.value = "analyzing";
    try {
      const result = await analyzeToolFailures({
        providerId: props.run.llmProvider || undefined,
        trace: toolFailureTrace.value
      });
      toolAnalysis.value = result.data;
      toolFeedbackState.value = "ready";
    } catch (error) {
      toolFeedbackState.value = "analysis_failed";
      toolFeedbackError.value = error instanceof Error ? error.message : String(error);
      return;
    }
  }

  const analysis = toolAnalysis.value;
  if (!analysis) return;
  toolFeedbackState.value = "uploading";
  try {
    await submitFeedback({
      source: "error",
      category: "tool_failure_analysis",
      description: `本轮工具调用失败 ${analysis.failureCount} 次，已由本地模型生成脱敏工程分析。`,
      errorMessage: "Multiple tool calls failed in one agent turn.",
      errorType: "ToolFailureAnalysis",
      errorDetails: {
        feedbackType: "tool_failure_analysis",
        analysisStatus: "ready",
        failureCount: analysis.failureCount,
        analysisReport: analysis.analysis,
        programEvidence: analysis.programEvidence,
        redactionVersion: analysis.redactionVersion
      },
      diagnostics: {
        platform: window.storydexDesktop?.platform || navigator.platform,
        provider: props.run.llmProvider,
        model: props.run.llmModel,
        traceId: props.run.traceId,
        sessionId: props.run.sessionId,
        runtime: props.run.route || "coomi",
        analysisRequestId: analysis.requestId,
        analysisElapsedMs: analysis.elapsedMs,
        analysisResponseCategory: analysis.responseCategory,
        redactionVersion: analysis.redactionVersion,
        failureCount: analysis.failureCount
      }
    });
    toolFeedbackState.value = "complete";
  } catch (error) {
    toolFeedbackState.value = "upload_failed";
    toolFeedbackError.value = error instanceof Error ? error.message : String(error);
  }
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

// 时钟格式：当天只显示时分，跨天补日期，跨年补年份
function formatClock(value: string): string {
  const date = new Date(value || "");
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const clock = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  if (
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate()
  ) {
    return clock;
  }
  const day = `${date.getMonth() + 1}月${date.getDate()}日`;
  return date.getFullYear() === now.getFullYear()
    ? `${day} ${clock}`
    : `${date.getFullYear()}年${day} ${clock}`;
}

const promptTimeText = computed(() => formatClock(props.run.createdAt));

// 运行中输出尚未定稿，meta 行左侧已有实时计时；终态才展示完成时刻
const completedTimeText = computed(() => (isRunning.value ? "" : formatClock(props.run.updatedAt)));

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
  return `本轮总计 ${formatTokenCount(tokens)} tokens`;
});

function formatTokenCount(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}K` : String(value);
}

const turnUsage = computed(() => {
  for (let index = props.run.events.length - 1; index >= 0; index -= 1) {
    const event = props.run.events[index];
    if (event.event !== "UsageUpdate") continue;
    const rawUsage = event.data?.usage;
    if (!rawUsage || typeof rawUsage !== "object" || Array.isArray(rawUsage)) continue;
    const usage = rawUsage as Record<string, unknown>;
    const prompt = numericUsageValue(usage, ["prompt_tokens", "promptTokens", "input_tokens"]);
    const completion = numericUsageValue(usage, ["completion_tokens", "completionTokens", "output_tokens"]);
    if (prompt + completion > 0) return { prompt, completion };
  }
  return null;
});

const tokenTooltip = computed(() => {
  const tokens = props.run.turnTokens;
  if (tokens === null || tokens === undefined || tokens <= 0) return "";
  const usage = turnUsage.value;
  if (!usage) return "本轮 Token 总量，包含输入上下文与模型输出";
  return [
    `本轮总计：${formatTokenCount(tokens)} tokens`,
    `输入上下文：${formatTokenCount(usage.prompt)} tokens`,
    `模型输出：${formatTokenCount(usage.completion)} tokens`
  ].join("\n");
});

function numericUsageValue(value: Record<string, unknown>, keys: string[]): number {
  for (const key of keys) {
    const candidate = Number(value[key]);
    if (Number.isFinite(candidate) && candidate >= 0) return candidate;
  }
  return 0;
}

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

/* 用户消息：左侧一个很淡的圆角块，时间靠右对齐 */
.cct-user {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 6px;
}

.cct-user .cct-time {
  margin-left: auto;
}

.cct-user-inline {
  margin-top: 4px;
}

.cct-user-text {
  max-width: 90%;
  padding: 8px 12px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--surface-2);
  color: var(--text-main);
  font-size: 14px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* 消息尾部时间：淡色小字，不抢正文视觉 */
.cct-time {
  flex: 0 0 auto;
  color: var(--text-faint);
  font-size: 11.5px;
  white-space: nowrap;
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
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
}

.cct-user-action:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-main);
}

.cct-user-action.danger:hover:not(:disabled) {
  background: var(--danger-bg);
  color: var(--danger-fg);
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

/* 展开内容：左侧一条实色细线缩进，无背景卡片 */
.cct-reveal {
  margin-left: 2px;
  padding-left: 14px;
  border-left: 2px solid var(--border-subtle);
}

.cct-line-block:has(.cct-reasoning-text) > .cct-reveal {
  border-left-color: var(--warning-border);
}

.cct-line-block:has(.cct-tool-list) > .cct-reveal {
  border-left-color: var(--info-border);
}

.cct-reasoning-text {
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1.75;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* 工具展开列表（所有调用共用顶部摘要折叠栏） */
.cct-tool-list {
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
  color: var(--success-fg);
}

.cct-tool-state.status-error {
  color: var(--danger-fg);
}

.cct-tool-state.status-running {
  color: var(--info-fg);
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
  border-radius: var(--radius-md);
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
  display: grid;
  gap: 2px;
  color: var(--danger);
  font-size: 14px;
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.cct-error-content {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  min-width: 0;
}

.cct-error-message {
  flex: 1;
  min-width: 0;
}

.cct-error-retry {
  flex: 0 0 26px;
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  margin-top: 1px;
  padding: 0;
  border: 0;
  border-radius: var(--radius-sm);
  color: var(--danger);
  background: transparent;
  cursor: pointer;
}

.cct-error-retry:hover:not(:disabled) {
  background: color-mix(in srgb, var(--danger) 10%, transparent);
}

.cct-error-retry:focus-visible {
  outline: 2px solid color-mix(in srgb, var(--danger) 45%, transparent);
  outline-offset: 1px;
}

.cct-error-retry:disabled {
  opacity: 0.45;
  cursor: default;
}

.cct-error-retry .material-symbols-rounded {
  font-size: 18px;
}

.cct-error-retry .spinning {
  animation: cct-spin 1s linear infinite;
}

/* 客观验收未通过等警告类提示，与上游 coomi-notice-text 同色调 */
.cct-notice-text {
  display: grid;
  gap: 2px;
  color: var(--warning);
  font-size: 13px;
  line-height: 1.72;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.cct-info-text {
  display: grid;
  gap: 2px;
  color: var(--accent-strong);
  font-size: 13px;
  line-height: 1.72;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.cct-error-text strong,
.cct-notice-text strong,
.cct-info-text strong {
  font-size: 12px;
  font-weight: 650;
}

.cct-tool-feedback {
  display: grid;
  grid-template-columns: 20px minmax(0, 1fr) auto;
  align-items: start;
  gap: 8px;
  padding: 10px 0;
  border-block: 1px solid color-mix(in srgb, var(--warning) 28%, transparent);
  color: var(--warning);
}

.cct-tool-feedback-icon {
  margin-top: 1px;
  font-size: 18px;
}

.cct-tool-feedback-copy {
  display: grid;
  gap: 2px;
  min-width: 0;
  font-size: 12.5px;
  line-height: 1.55;
}

.cct-tool-feedback-copy strong {
  color: var(--text-main);
  font-size: 13px;
}

.cct-tool-feedback-copy span,
.cct-tool-feedback-copy small {
  overflow-wrap: anywhere;
}

.cct-tool-feedback-copy small {
  color: var(--danger);
}

.cct-tool-feedback button,
.cct-tool-feedback-done {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 30px;
  padding: 0 9px;
  border: 1px solid color-mix(in srgb, var(--warning) 45%, var(--border-subtle));
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
}

.cct-tool-feedback button {
  cursor: pointer;
}

.cct-tool-feedback button:hover:not(:disabled) {
  background: color-mix(in srgb, var(--warning) 9%, transparent);
}

.cct-tool-feedback button:disabled {
  opacity: 0.65;
  cursor: default;
}

.cct-tool-feedback button .material-symbols-rounded,
.cct-tool-feedback-done .material-symbols-rounded {
  font-size: 16px;
}

.cct-tool-feedback button:disabled .material-symbols-rounded {
  animation: cct-spin 1s linear infinite;
}

.cct-tool-feedback-done {
  border-color: color-mix(in srgb, var(--success) 45%, var(--border-subtle));
  color: var(--success);
}

@media (max-width: 560px) {
  .cct-tool-feedback {
    grid-template-columns: 20px minmax(0, 1fr);
  }

  .cct-tool-feedback button,
  .cct-tool-feedback-done {
    grid-column: 2;
    justify-self: start;
  }
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
  color: var(--warning);
  font-size: 12.5px;
}

.cct-meta-warn .material-symbols-rounded {
  font-size: 15px;
}

/* 完成时刻：靠右对齐，与左侧统计文字互不干扰 */
.cct-meta-time {
  margin-left: auto;
  color: var(--text-faint);
  font-size: 12px;
  white-space: nowrap;
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
  border-radius: var(--radius-lg);
  background: color-mix(in srgb, var(--text-main) 5%, transparent);
  overflow: auto;
  font-weight: 400;
}

.cct-markdown :deep(code) {
  padding: 1px 5px;
  border-radius: var(--radius-sm);
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

.cct-markdown :deep(table) {
  display: table;
  width: max-content;
  max-width: 100%;
  margin: 0.6em 0 0.8em;
  border-collapse: collapse;
  table-layout: auto;
  font-size: 13px;
  line-height: 1.5;
}

.cct-markdown :deep(th),
.cct-markdown :deep(td) {
  padding: 5px 9px;
  border: 1px solid var(--border-subtle);
  text-align: left;
  vertical-align: top;
  white-space: normal;
  overflow-wrap: anywhere;
}

.cct-markdown :deep(th) {
  background: color-mix(in srgb, var(--text-main) 6%, transparent);
  color: var(--text-main);
  font-weight: 700;
}

@keyframes cct-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
