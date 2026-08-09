<script setup lang="ts">
import { computed } from 'vue'
import type { LoopProgress } from '@/stores/viewModel'
import CoomiIcon from './CoomiIcon.vue'

const props = defineProps<{ loop: LoopProgress }>()
const pct = computed(() => (props.loop.totalSteps > 0 ? Math.round((props.loop.currentStep / props.loop.totalSteps) * 100) : 0))
</script>

<template>
  <div v-if="loop.active" class="loop fade-in">
    <div class="row">
      <CoomiIcon name="subtask" :size="14" class="ic" />
      <span class="tag">循环模式</span>
      <span class="txt">{{ loop.currentDescription || loop.status || '执行中' }}</span>
      <span class="count">{{ loop.currentStep }}/{{ loop.totalSteps }}</span>
    </div>
    <div class="track"><div class="fill" :style="{ width: pct + '%' }" /></div>
  </div>
</template>

<style scoped>
.loop {
  margin: 2px 12px 4px; padding: 9px 12px 10px;
  border-radius: var(--r-md); background: var(--blue-soft);
}
.row { display: flex; align-items: center; gap: 7px; margin-bottom: 8px; }
.ic { color: var(--blue); }
.tag { flex-shrink: 0; font-size: 11.5px; font-weight: 700; color: var(--blue); }
.txt {
  flex: 1; min-width: 0; font-size: 12.5px; color: var(--text-2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.count { flex-shrink: 0; font-size: 11.5px; color: var(--text-3); font-variant-numeric: tabular-nums; }
.track { height: 4px; border-radius: 2px; background: var(--bg); overflow: hidden; }
.fill {
  height: 100%; border-radius: 2px;
  background: linear-gradient(90deg, var(--blue), #5b87dc);
  transition: width .35s ease;
}
</style>
