import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { ConnectionState } from '@/bridge'
import { isDemoMode } from '@/bridge/demoMode'

export const useConnectionStore = defineStore('connection', () => {
  const state = ref<ConnectionState>('connecting')
  const retryMessage = ref<string | null>(null)
  const wsUrl = ref('')
  const isOpen = computed(() => state.value === 'open')
  /** 演示模式：底下接的是脚本，不是引擎。所有状态文案都要说清这件事。 */
  const demo = ref(isDemoMode())

  const label = computed(() => {
    if (demo.value) return '演示模式（未连引擎）'
    switch (state.value) {
      case 'connecting': return '连接中…'
      case 'open': return '已连接'
      case 'closed': return '已断开'
      case 'error': return '连接错误'
    }
  })

  function setState(s: ConnectionState) { state.value = s }
  function setRetry(msg: string | null) { retryMessage.value = msg }
  function setWsUrl(url: string) { wsUrl.value = url }

  return { state, retryMessage, wsUrl, isOpen, demo, label, setState, setRetry, setWsUrl }
})
