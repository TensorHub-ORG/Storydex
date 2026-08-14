export interface FeedbackPayload {
  source: 'error' | 'settings'
  category: string
  description: string
  contact?: string
  error?: Record<string, unknown>
  diagnostics?: Record<string, unknown>
  images?: Array<{ name: string; mimeType: string; dataBase64: string }>
}

type FeedbackResult = { ok: boolean; error?: string; detail?: string }
const callbacks = new Map<string, (result: FeedbackResult) => void>()
let sequence = 0

;(window as unknown as { __coomiFeedbackResult?: (id: string, resultJson: string) => void }).__coomiFeedbackResult =
  (id, resultJson) => {
    const callback = callbacks.get(id)
    if (!callback) return
    callbacks.delete(id)
    try { callback(JSON.parse(resultJson) as FeedbackResult) }
    catch { callback({ ok: false, error: '反馈服务返回无效结果' }) }
  }

function diagnostics(): Record<string, unknown> {
  try {
    const raw = window.CoomiAndroid?.getDiagnostics?.()
    return raw ? JSON.parse(raw) as Record<string, unknown> : {}
  } catch { return {} }
}

export async function submitAndroidFeedback(input: FeedbackPayload): Promise<FeedbackResult> {
  const payload = {
    submissionId: globalThis.crypto?.randomUUID?.() ?? `android-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    submittedAt: new Date().toISOString(),
    platform: 'android',
    source: input.source,
    category: input.category,
    description: input.description.trim(),
    contact: input.contact?.trim() ?? '',
    error: input.error ?? {},
    diagnostics: { ...diagnostics(), ...(input.diagnostics ?? {}), platform: 'android' },
    privacy: { conversation: false, projectFiles: false, apiKeys: false },
    images: input.images ?? [],
  }
  if (window.CoomiAndroid?.sendFeedback) {
    return await new Promise(resolve => {
      const id = `feedback-${++sequence}-${Date.now()}`
      callbacks.set(id, resolve)
      setTimeout(() => {
        if (callbacks.delete(id)) resolve({ ok: false, error: '提交超时' })
      }, 12000)
      window.CoomiAndroid!.sendFeedback!(JSON.stringify(payload), id)
    })
  }
  try {
    const response = await fetch('https://updates.septemc.com/storydex/feedback/api', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    })
    return response.ok ? { ok: true } : { ok: false, error: `HTTP ${response.status}` }
  } catch (cause) { return { ok: false, error: cause instanceof Error ? cause.message : String(cause) } }
}
