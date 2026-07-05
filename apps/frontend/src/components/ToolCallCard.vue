<template>
  <div class="tool-call-card" :class="[statusClass]">
    <div class="tool-header" @click="expanded = !expanded">
      <span class="tool-icon">{{ toolIcon }}</span>
      <span class="tool-name">{{ toolName }}</span>
      <span class="tool-status">{{ statusLabel }}</span>
      <span class="tool-toggle">{{ expanded ? '▾' : '▸' }}</span>
    </div>
    <div v-if="expanded" class="tool-body">
      <div v-if="arguments" class="tool-arguments">
        <pre>{{ formattedArguments }}</pre>
      </div>
      <div v-if="status === 'running'" class="tool-progress">
        <span class="spinner"></span>
        <span>{{ progressMessage }}</span>
      </div>
      <div v-if="status === 'done' && resultPreview" class="tool-result">
        <pre>{{ resultPreview }}</pre>
      </div>
      <div v-if="status === 'error'" class="tool-error">
        {{ resultPreview }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{
  toolName: string
  arguments?: Record<string, unknown>
  status: 'running' | 'done' | 'error'
  resultPreview?: string
  progressMessage?: string
}>()

const expanded = ref(true)

const toolIcon = computed(() => {
  const name = props.toolName
  if (name.startsWith('read') || name.startsWith('list')) return '📖'
  if (name.startsWith('write') || name.startsWith('edit')) return '✏️'
  if (name.startsWith('delete')) return '🗑️'
  if (name.startsWith('search')) return '🔍'
  if (name.startsWith('extract')) return '🧠'
  if (name.startsWith('compact')) return '📦'
  return '🔧'
})

const statusLabel = computed(() => {
  if (props.status === 'running') return '⏳'
  if (props.status === 'done') return '✅'
  if (props.status === 'error') return '❌'
  return ''
})

const statusClass = computed(() => `status-${props.status}`)

const formattedArguments = computed(() => {
  if (!props.arguments) return ''
  return JSON.stringify(props.arguments, null, 2)
})
</script>

<style scoped>
.tool-call-card {
  border: 1px solid var(--color-border, #e0e0e0);
  border-radius: 8px;
  margin: 8px 0;
  overflow: hidden;
  font-size: 13px;
}
.status-running { border-left: 3px solid var(--color-info, #2196f3); }
.status-done { border-left: 3px solid var(--color-success, #4caf50); }
.status-error { border-left: 3px solid var(--color-error, #f44336); }

.tool-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  cursor: pointer;
  background: var(--color-surface, #f5f5f5);
}
.tool-icon { font-size: 14px; }
.tool-name { font-weight: 600; flex: 1; font-family: monospace; }
.tool-status { font-size: 12px; }
.tool-toggle { font-size: 10px; color: var(--color-text-secondary, #999); }

.tool-body { padding: 8px 10px; }
.tool-arguments pre, .tool-result pre {
  margin: 0;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
}
.tool-progress {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text-secondary, #666);
}
.spinner {
  width: 14px;
  height: 14px;
  border: 2px solid var(--color-border, #e0e0e0);
  border-top-color: var(--color-info, #2196f3);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.tool-error { color: var(--color-error, #f44336); }
</style>
