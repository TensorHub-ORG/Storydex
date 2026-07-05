<template>
  <div class="message-bubble" :class="[`role-${message.role}`]">
    <div class="bubble-header">
      <span class="role-label">{{ roleLabel }}</span>
    </div>
    <div class="bubble-content">
      <template v-for="(block, idx) in contentBlocks" :key="idx">
        <div v-if="block.type === 'text'" class="text-block" v-html="renderMarkdown(block.text)"></div>
        <div v-else-if="block.type === 'tool_use'" class="tool-use-block">
          <ToolCallCard
            :tool-name="block.name || ''"
            :arguments="block.input"
            :status="getToolStatus(block.id || '')"
            :result-preview="getToolResult(block.id || '')"
            :progress-message="getToolProgress(block.id || '')"
          />
        </div>
        <details v-else-if="block.type === 'thinking'" class="thinking-block">
          <summary>💭 思考过程</summary>
          <div class="thinking-content">{{ block.thinking }}</div>
        </details>
      </template>
      <div v-if="message.role === 'user' && typeof message.content === 'string'" class="text-block" v-html="renderMarkdown(message.content)"></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import ToolCallCard from './ToolCallCard.vue'
import { createMarkdownRenderer } from '@/utils/markdown'

interface ContentBlock {
  type: string
  text?: string
  name?: string
  input?: Record<string, unknown>
  id?: string
  thinking?: string
}

interface MessageLike {
  role: string
  content: ContentBlock[] | string
}

const props = defineProps<{
  message: MessageLike
  toolStates?: Map<string, { status: 'running' | 'done' | 'error'; result?: string; progress?: string }>
}>()

const md = createMarkdownRenderer()

const contentBlocks = computed<ContentBlock[]>(() => {
  if (Array.isArray(props.message.content)) {
    return props.message.content
  }
  return []
})

const roleLabel = computed(() => {
  if (props.message.role === 'user') return '👤 You'
  if (props.message.role === 'assistant') return '🤖 Assistant'
  if (props.message.role === 'system') return '⚙️ System'
  return props.message.role
})

function renderMarkdown(text: string | undefined): string {
  return md.render(text || '')
}

function getToolStatus(toolId: string): 'running' | 'done' | 'error' {
  return props.toolStates?.get(toolId)?.status || 'running'
}

function getToolResult(toolId: string): string | undefined {
  return props.toolStates?.get(toolId)?.result
}

function getToolProgress(toolId: string): string | undefined {
  return props.toolStates?.get(toolId)?.progress
}
</script>

<style scoped>
.message-bubble {
  margin: 12px 0;
  padding: 12px 16px;
  border-radius: 12px;
  max-width: 100%;
}
.role-user {
  background: var(--color-user-bubble, #e3f2fd);
  margin-left: 48px;
}
.role-assistant {
  background: var(--color-assistant-bubble, #f5f5f5);
  margin-right: 48px;
}
.role-system {
  background: var(--color-surface, #fff3e0);
  font-style: italic;
}

.bubble-header {
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--color-text-secondary, #888);
}
.role-label { font-weight: 600; }

.bubble-content { line-height: 1.6; }
.text-block :deep(p) { margin: 4px 0; }
.text-block :deep(pre) {
  background: var(--color-code-bg, #1e1e1e);
  color: var(--color-code-fg, #d4d4d4);
  padding: 8px 12px;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 13px;
}

.thinking-block {
  margin: 4px 0;
  border: 1px dashed var(--color-border, #ccc);
  border-radius: 6px;
  padding: 4px 8px;
}
.thinking-block summary {
  cursor: pointer;
  font-size: 12px;
  color: var(--color-text-secondary, #888);
}
.thinking-content {
  font-size: 13px;
  color: var(--color-text-secondary, #666);
  white-space: pre-wrap;
  max-height: 200px;
  overflow-y: auto;
}
</style>
