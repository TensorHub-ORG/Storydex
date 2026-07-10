<template>
  <section class="trace-panel">
    <div class="trace-panel-header">
      <div>
        <div class="trace-panel-title">Coomi 追踪</div>
        <div class="trace-panel-subtitle">结构化的 Coomi 流事件、工具调用与用量记录。</div>
      </div>
      <div v-if="activeRun?.trace" class="trace-badge">追踪 {{ shortTrace(activeRun.trace.traceId) }}</div>
    </div>

    <div v-if="executionHistory.length > 0" class="trace-history">
      <button
        v-for="run in executionHistory"
        :key="run.traceId"
        class="trace-history-item"
        :class="{ active: run.traceId === activeRun?.traceId }"
        type="button"
        @click="agentStore.selectTraceRun(run.traceId)"
      >
        <div class="trace-history-top">
          <span class="trace-history-status" :class="statusClass(run.status, run.errorMessage)">
            {{ formatStatus(run.status, run.errorMessage) }}
          </span>
          <span class="trace-history-time">{{ formatDate(run.updatedAt) }}</span>
        </div>
        <div class="trace-history-prompt">{{ run.prompt || "未记录提示词" }}</div>
        <div class="trace-history-meta">
          <span>{{ shortTrace(run.traceId) }}</span>
          <span>{{ run.route || "coomi" }}</span>
        </div>
      </button>
    </div>

    <div v-if="activeRun" class="trace-body">
      <div class="trace-summary-card">
        <div class="trace-summary-top">
          <div class="trace-summary-title">{{ activeRun.prompt || "Coomi 运行" }}</div>
          <div class="trace-summary-meta">
            <span>{{ activeRun.route || "coomi" }}</span>
            <span>{{ formatStatus(activeRun.status, activeRun.errorMessage) }}</span>
          </div>
        </div>
        <div class="trace-summary-grid">
          <div class="trace-summary-item">
            <span class="trace-summary-label">耗时</span>
            <span class="trace-summary-value">{{ activeRun.trace?.durationMs ?? 0 }} ms</span>
          </div>
          <div class="trace-summary-item">
            <span class="trace-summary-label">工具</span>
            <span class="trace-summary-value">{{ activeRun.trace?.toolCalls ?? activeRun.audit.length }}</span>
          </div>
          <div class="trace-summary-item">
            <span class="trace-summary-label">LLM 调用</span>
            <span class="trace-summary-value">{{ activeRun.trace?.llmCalls ?? 0 }}</span>
          </div>
          <div class="trace-summary-item">
            <span class="trace-summary-label">输入 Tokens</span>
            <span class="trace-summary-value">{{ activeRun.trace?.promptTokens ?? 0 }}</span>
          </div>
          <div class="trace-summary-item">
            <span class="trace-summary-label">输出 Tokens</span>
            <span class="trace-summary-value">{{ activeRun.trace?.completionTokens ?? 0 }}</span>
          </div>
        </div>
      </div>

      <div v-if="activeRun.errorMessage" class="trace-error-card">
        <div class="trace-section-title">错误</div>
        <div class="trace-error-message">{{ activeRun.errorMessage }}</div>
        <div v-if="activeRun.errorCode" class="trace-error-code">{{ activeRun.errorCode }}</div>
      </div>

      <div class="trace-section">
        <div class="trace-section-title">事件</div>
        <div v-if="activeRun.events.length === 0" class="trace-empty">暂无结构化 Coomi 事件。</div>
        <div v-else class="trace-event-list">
          <div v-for="event in activeRun.events" :key="event.index + '-' + event.event" class="trace-event-item">
            <div class="trace-event-marker" :class="eventToneClass(event.status)"></div>
            <div class="trace-event-content">
              <div class="trace-event-top">
                <span class="trace-event-title">{{ event.event }}</span>
                <span class="trace-event-meta">{{ event.phase || "运行时" }} · {{ formatDate(event.timestamp) }}</span>
              </div>
              <div class="trace-event-detail">{{ event.detail || "无详情" }}</div>
              <pre v-if="hasEventData(event.data)" class="trace-event-data">{{ stringifyData(event.data) }}</pre>
            </div>
          </div>
        </div>
      </div>

      <div class="trace-section">
        <div class="trace-section-title">工具审计</div>
        <div v-if="activeRun.audit.length === 0" class="trace-empty">暂无 Coomi 工具审计记录。</div>
        <div v-else class="trace-audit-list">
          <pre v-for="(record, index) in activeRun.audit" :key="index" class="trace-audit-item">{{ stringifyData(record) }}</pre>
        </div>
      </div>
    </div>

    <div v-else class="trace-empty trace-empty-large">运行一次 Coomi 后即可查看追踪数据。</div>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useAgentStore } from "@/stores/agent";

const agentStore = useAgentStore();
const activeRun = computed(() => agentStore.activeTraceRun);
const executionHistory = computed(() => agentStore.executionHistory);

function shortTrace(traceId: string): string {
  if (!traceId) {
    return "unknown";
  }
  return `${traceId.slice(0, 8)}...${traceId.slice(-4)}`;
}

function formatDate(isoText: string): string {
  const date = new Date(isoText);
  if (Number.isNaN(date.getTime())) {
    return isoText;
  }
  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatStatus(status: string, errorMessage: string): string {
  if (errorMessage || status === "failed") return "错误";
  if (status === "running") return "运行中";
  if (status === "completed") return "已完成";
  if (status === "cancelled" || status === "stopped") return "已停止";
  return status || "空闲";
}

function statusClass(status: string, errorMessage: string): string {
  if (errorMessage || status === "failed") return "is-error";
  if (status === "running") return "is-running";
  if (status === "completed") return "is-success";
  return "is-neutral";
}

function eventToneClass(status: string): string {
  if (status === "error") return "is-error";
  if (status === "warning") return "is-warning";
  if (status === "success") return "is-success";
  return "is-info";
}

function hasEventData(value: unknown): boolean {
  return typeof value === "object" && value !== null && Object.keys(value as Record<string, unknown>).length > 0;
}

function stringifyData(value: unknown): string {
  return JSON.stringify(value, null, 2);
}
defineExpose({
  __testUtils: import.meta.env.MODE === "test" ? {
    activeRun, executionHistory, shortTrace, formatDate, formatStatus, statusClass, eventToneClass,
    hasEventData, stringifyData
  } : null
});
</script>
