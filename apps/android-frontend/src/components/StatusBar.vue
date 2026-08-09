<script setup lang="ts">
/**
 * 状态条：只在有话可说的时候出现（忙 / 有用量 / 正在重连）。
 * 停止按钮归输入区的圆形按钮管，这里不重复放一个。
 */
import { computed } from 'vue'
import { useSessionStore } from '@/stores/session'
import { useConnectionStore } from '@/stores/connection'
import CoomiIcon from './CoomiIcon.vue'

const session = useSessionStore()
const connection = useConnectionStore()

const runLabel = computed(() => {
  switch (session.runState) {
    case 'thinking': return '思考中'
    case 'executing': return '执行中'
    case 'awaiting_approval': return '等你授权'
    case 'awaiting_question': return '等你回答'
    default: return ''
  }
})

</script>

<template>
  <div v-if="session.isBusy || connection.retryMessage" class="sbar">
    <div v-if="connection.retryMessage" class="retry">
      <CoomiIcon name="alert" :size="14" />
      <span>{{ connection.retryMessage }}</span>
    </div>
    <div class="row">
      <span v-if="session.isBusy" class="dots"><i /><i /><i /></span>
      <span v-if="runLabel" class="run">{{ runLabel }}</span>
      <span class="gap" />
    </div>
  </div>
</template>

<style scoped>
.sbar { padding: 2px 16px 4px; background: var(--bg); }
.retry {
  display: flex; align-items: center; gap: 6px; margin-bottom: 3px;
  font-size: 12px; color: var(--orange);
}
.row { display: flex; align-items: center; gap: 8px; min-height: 18px; }
.gap { flex: 1; }
.run { font-size: 12.5px; color: var(--text-2); }
.dots { display: inline-flex; align-items: center; gap: 3px; }
.dots i {
  width: 5px; height: 5px; border-radius: 50%; background: var(--blue);
  animation: bounce 1.2s ease-in-out infinite;
}
.dots i:nth-child(2) { animation-delay: .15s; }
.dots i:nth-child(3) { animation-delay: .3s; }
@keyframes bounce {
  0%, 60%, 100% { opacity: .25; transform: none; }
  30% { opacity: 1; transform: translateY(-3px); }
}
</style>
