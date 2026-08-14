<script setup lang="ts">
import { computed, ref } from 'vue'
import type { NoticeItem } from '@/stores/viewModel'
import { useConfigStore } from '@/stores/config'
import { useSessionStore } from '@/stores/session'
import CoomiIcon from './CoomiIcon.vue'
import { submitAndroidFeedback } from '@/utils/feedback'

const props = defineProps<{ notice: NoticeItem }>()

const config = useConfigStore()
const session = useSessionStore()
const open = ref(false)
const confirm = ref(false)
const sending = ref(false)
const sent = ref<'ok' | 'fail' | null>(null)
const failReason = ref('')

const icon = computed(() => {
  switch (props.notice.tone) {
    case 'error': return 'alert'
    case 'warn': return 'alert'
    case 'success': return 'check'
    default: return ''
  }
})

function toggle() { if (props.notice.detail) open.value = !open.value }

/**
 * 上报报错：仅上传报错日志 + 设备诊断，不含任何对话内容。
 * 双通道：自建端点（国内可达）优先，随后尝试 GitHub issue（失败静默）。
 */
async function sendFeedback() {
  if (sending.value) return
  sending.value = true
  sent.value = null
  failReason.value = ''
  const result = await submitAndroidFeedback({
    source: 'error', category: 'bug', description: props.notice.text,
    error: { message: props.notice.text, detail: props.notice.detail ?? '' },
    diagnostics: { provider: config.currentProviderId, model: config.currentModel, permissionMode: config.permissionMode },
  })
  const ok = result.ok
  const reason = result.error ?? ''
  // 仅使用自建服务器通道（用户决定不用 GitHub issue）。
  sending.value = false
  sent.value = ok ? 'ok' : 'fail'
  failReason.value = reason
  // 已发送提示停留片刻后自动收起
  if (ok) setTimeout(() => { sent.value = null; confirm.value = false }, 2600)
}

async function sendToolFailureFeedback() {
  if (sending.value) return
  sending.value = true; sent.value = null; failReason.value = ''
  let detail = props.notice.detail ?? ''
  if (props.notice.analysisStatus !== 'ready') detail = await session.consentToolFailureFeedback(props.notice.id)
  if (!detail) { sending.value = false; return }
  const result = await submitAndroidFeedback({
    source: 'error', category: 'tool_failure_analysis',
    description: `本轮工具调用失败 ${props.notice.failureCount ?? 3} 次，已由本地模型生成脱敏工程分析。`,
    error: { feedbackType: 'tool_failure_analysis', analysisStatus: 'ready', failureCount: props.notice.failureCount ?? 3, detail },
    diagnostics: { provider: config.currentProviderId, model: config.currentModel, permissionMode: config.permissionMode, failureCount: props.notice.failureCount ?? 3 },
  })
  sending.value = false; sent.value = result.ok ? 'ok' : 'fail'; failReason.value = result.error ?? ''
  session.finishToolFailureFeedback(props.notice.id, result.ok, result.error)
}
</script>

<template>
  <div class="notice cascade" :class="notice.tone" @click="toggle">
    <CoomiIcon v-if="icon" :name="icon" :size="14" />
    <span>{{ notice.text }}</span>
    <CoomiIcon v-if="notice.detail" name="chevronRight" :size="14" class="chev" :class="{ open }" />
  </div>
  <div v-if="notice.detail && open" class="notice-detail cascade">
    <pre>{{ notice.detail }}</pre>
  </div>

  <div v-if="notice.analysisStatus" class="fb analysis-feedback">
    <button
      v-if="notice.feedbackEligible"
      class="fb-btn"
      :disabled="sending"
      @click.stop="sendToolFailureFeedback"
    >
      <CoomiIcon name="send" :size="13" />
      <span>{{ notice.analysisStatus === 'ready' ? '重新上传' : notice.analysisStatus === 'failed' ? '重新整理并反馈' : '同意反馈' }}</span>
    </button>
    <span v-else-if="notice.analysisStatus === 'analyzing'" class="fb-result">正在本地脱敏整理</span>
    <span v-else-if="notice.analysisStatus === 'complete'" class="fb-result ok">已完成</span>
    <span v-if="sent === 'fail' && failReason" class="fb-reason">{{ failReason }}</span>
  </div>

  <div v-if="notice.tone === 'error'" class="fb">
    <template v-if="!confirm">
      <button class="fb-btn" @click.stop="confirm = true">
        <CoomiIcon name="send" :size="13" />
        <span>反馈报错</span>
      </button>
    </template>
    <template v-else-if="sent === null">
      <div class="fb-confirm">
        <span>本次反馈仅上传报错日志与设备信息，<b>不含任何对话内容</b>。确认上报？</span>
        <div class="fb-actions">
          <button class="fb-btn ghost" @click.stop="confirm = false; sent = null">取消</button>
          <button class="fb-btn" :disabled="sending" @click.stop="sendFeedback()">
            {{ sending ? '上传中…' : '确认上传' }}
          </button>
        </div>
      </div>
    </template>
    <template v-else>
      <span class="fb-result" :class="sent">{{ sent === 'ok' ? '已收到，感谢反馈' : '上传失败，可稍后重试' }}</span>
      <span v-if="sent === 'fail' && failReason" class="fb-reason">{{ failReason }}</span>
    </template>
  </div>
</template>

<style scoped>
.notice {
  align-self: center; display: inline-flex; align-items: center; gap: 6px;
  min-width: 0; max-width: 92%; padding: 6px 14px;
  border-radius: var(--r-pill); background: var(--fill);
  font-size: 12.5px; line-height: 1.5; color: var(--text-3);
}
.notice span { min-width: 0; max-width: 100%; overflow-wrap: anywhere; word-break: break-word; }
.notice.warn { background: var(--orange-soft); color: var(--orange); }
.notice.success { background: var(--ok-soft); color: var(--ok); }
/* 报错：红字、无背景底框、无圆角——只保留文字颜色区分。 */
.notice.error {
  align-self: stretch; width: 100%; max-width: 100%; align-items: flex-start;
  padding: 4px 2px;
  background: transparent; color: var(--danger);
  text-align: left; word-break: break-word;
}
.notice.error :deep(svg) { flex-shrink: 0; margin-top: 1px; color: var(--danger); }
.chev { flex-shrink: 0; transition: transform .18s; }
.chev.open { transform: rotate(90deg); }

.notice-detail {
  align-self: center; width: 100%; max-width: 92%;
  margin-top: -4px; padding: 9px 13px;
  border-radius: var(--r-md); background: var(--code-bg);
}
.notice-detail pre {
  margin: 0; font-family: var(--font-mono); font-size: 11.8px; line-height: 1.6;
  color: var(--code-text); white-space: pre-wrap; word-break: break-word;
  max-height: 260px; overflow-y: auto;
}

/* 报错反馈按钮（仅 error 通知显示） */
.fb {
  align-self: center; width: 100%; max-width: 92%;
  display: flex; flex-direction: column; gap: 8px;
  margin-top: -2px;
}
.fb-btn {
  display: inline-flex; align-items: center; gap: 5px;
  align-self: flex-start; height: 30px; padding: 0 12px;
  border: 1px solid var(--border); border-radius: var(--r-pill);
  background: var(--bg); color: var(--text-2);
  font-size: 12.5px; font-weight: 600;
}
.fb-btn:active { background: var(--fill); }
.fb-btn:disabled { opacity: .6; }
.fb-btn.ghost { color: var(--text-3); }
.fb-confirm {
  padding: 10px 12px; border: 1px solid var(--border);
  border-radius: var(--r-md); background: var(--bg);
  font-size: 12.5px; line-height: 1.55; color: var(--text-2); text-align: left;
}
.fb-confirm b { color: var(--danger); }
.fb-actions { display: flex; gap: 8px; margin-top: 8px; }
.fb-result { font-size: 12.5px; font-weight: 600; }
.fb-result.ok { color: var(--ok); }
.fb-result.fail { color: var(--danger); }
.fb-reason { font-size: 11.5px; color: var(--text-3); word-break: break-all; }
</style>
