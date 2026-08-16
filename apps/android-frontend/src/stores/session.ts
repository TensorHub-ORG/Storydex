import { defineStore } from 'pinia'
import { ref, computed, shallowRef, watch } from 'vue'
import { createTransport, type Transport } from '@/bridge'
import { authedFetch } from '@/bridge/http'
import { isDemoMode } from '@/bridge/demoMode'
import type { AgentEvent } from '@/protocol/events'
import type { InboundEnvelope } from '@/protocol/commands'
import { nextId } from '@/bridge/envelope'
import { useConnectionStore } from './connection'
import { useConfigStore } from './config'
import { useSessionsStore } from './sessions'
import { useStoryStore, type ReasoningEffort } from './story'
import type { AssistantMessage, LoopProgress, QuestionCard, ReasoningBlock, RunState, Timelineitem, ToolCard, ToolDiagnosticTrace } from './viewModel'

export const useSessionStore = defineStore('session', () => {
  const connection = useConnectionStore()
  const config = useConfigStore()
  const sessions = useSessionsStore()
  const story = useStoryStore()
  sessions.setCurrentMode(story.agentMode)
  if (story.projectPath) sessions.setCurrentCwd(story.projectPath)

  const sessionId = ref(createSessionId())
  const timeline = ref<Timelineitem[]>([])
  const runState = ref<RunState>('idle')
  const usage = ref<{
    total: number; input: number; output: number; contextRatio: number
    contextUsed: number; contextWindow: number
    cachedInput: number; reasoning: number; turnInput: number; turnCachedInput: number
    turnOutput: number; turnReasoning: number; turnCacheRate: number
    categories: Record<string, number>; mode: 'story' | 'narrator' | 'agent'
    project: NonNullable<import('@/protocol/events').UsageInfo['project']>
  } | null>(null)
  /** 当前会话的工作目录（会话标记路径，绑定为会话执行目录）。 */
  const cwd = ref('')
  const loop = ref<LoopProgress>({ active: false, currentStep: 0, totalSteps: 0, status: '' })

  let currentAssistant: AssistantMessage | null = null
  let connectedSessionId = ''
  let persistTimer: ReturnType<typeof setTimeout> | null = null
  let turnToolTrace: ToolDiagnosticTrace[] = []
  const transport = shallowRef<Transport | null>(null)

  const isBusy = computed(() => runState.value !== 'idle')
  const pendingApproval = computed(() => timeline.value.find((t): t is ToolCard => t.kind === 'tool' && t.status === 'awaiting_approval'))
  const pendingQuestion = computed(() => timeline.value.find((t): t is QuestionCard => t.kind === 'question' && !t.answered))

  async function refreshProjectUsage() {
    try {
      const projectPath = story.projectPath
      const query = projectPath ? `?path=${encodeURIComponent(projectPath)}` : ''
      const response = await authedFetch(`/api/storydex/usage${query}`)
      if (!response.ok) return
      const project = await response.json() as NonNullable<import('@/protocol/events').UsageInfo['project']>
      const previous = usage.value
      usage.value = {
        total: previous?.total ?? 0, input: previous?.input ?? 0, output: previous?.output ?? 0,
        contextRatio: previous?.contextRatio ?? 0, contextUsed: previous?.contextUsed ?? 0,
        contextWindow: previous?.contextWindow ?? 0, cachedInput: previous?.cachedInput ?? 0,
        reasoning: previous?.reasoning ?? 0, turnInput: previous?.turnInput ?? 0,
        turnCachedInput: previous?.turnCachedInput ?? 0, turnOutput: previous?.turnOutput ?? 0,
        turnReasoning: previous?.turnReasoning ?? 0, turnCacheRate: previous?.turnCacheRate ?? 0,
        categories: previous?.categories ?? {}, mode: previous?.mode ?? story.agentMode, project,
      }
    } catch { /* Engine startup and reconnect retries will load this again. */ }
  }

  /** 通知原生层任务状态：更新通知栏常驻通知（执行中 / 已完成）。 */
  function syncTaskStatus(status: 'running' | 'done') {
    window.CoomiAndroid?.updateTaskStatus?.(status)
  }
  watch(runState, (state) => {
    if (state === 'idle') syncTaskStatus('done')
    else if (state !== 'awaiting_approval' && state !== 'awaiting_question') syncTaskStatus('running')
  })

  /** 发送内置引导（EmptyState 引导卡）：先置用户标题消息，再让引擎流式推正文。 */
  const GUIDE_TITLES: Record<string, string> = {
    newbie: 'Storydex 新手使用指南',
    extension: '自定义拓展进化指南',
  }
  function sendGuide(key: string) {
    const trimmed = (GUIDE_TITLES[key] ?? 'Storydex 指南').trim()
    // 首条用户消息作为会话标题，抽屉里就不会全是「新对话」。
    const isFirst = !timeline.value.some(t => t.kind === 'user')
    if (isFirst) sessions.touch(sessionId.value, { title: sessions.deriveTitle(trimmed) })
    timeline.value.push({ kind: 'user', id: nextId(), content: trimmed })
    runState.value = 'thinking'
    transport.value?.send({ command: 'send_guide', key })
    persistSoon()
  }

  /** 时间线写回 localStorage 有节流：流式期间不要每个 chunk 都序列化。 */
  function persistSoon() {
    if (isDemoMode()) return // 演示内容不该混进真实历史
    if (persistTimer) return
    persistTimer = setTimeout(() => {
      persistTimer = null
      const items = timeline.value.filter(t => t.kind !== 'notice')
      if (items.length === 0) return
      sessions.touch(sessionId.value, { turns: timeline.value.filter(t => t.kind === 'user').length, cwd: story.projectPath, mode: story.agentMode })
      sessions.saveTranscript(sessionId.value, timeline.value)
    }, 1200)
  }

  function flushPersistence() {
    if (persistTimer) {
      clearTimeout(persistTimer)
      persistTimer = null
    }
    if (isDemoMode()) return
    const items = timeline.value.filter(t => t.kind !== 'notice')
    if (items.length === 0) return
    sessions.touch(sessionId.value, { turns: timeline.value.filter(t => t.kind === 'user').length, cwd: story.projectPath, mode: story.agentMode })
    sessions.saveTranscript(sessionId.value, timeline.value)
  }

  /** 换 sessionId 后必须重连：WS 的路径里带着 session id。 */
  function connect(wsUrl?: string) {
    if (transport.value && connectedSessionId === sessionId.value) return
    const targetSessionId = sessionId.value
    const previous = transport.value
    transport.value = null
    connectedSessionId = ''
    previous?.close()
    if (wsUrl) connection.setWsUrl(wsUrl)
    const t = createTransport(targetSessionId, wsUrl)
    transport.value = t
    connectedSessionId = targetSessionId
    t.onStateChange(s => {
      if (transport.value !== t || sessionId.value !== targetSessionId) return
      connection.setState(s)
      if (s === 'open') {
        t.send({ command: 'set_permission_mode', mode: config.permissionMode })
        t.send({ command: 'set_reasoning_effort', effort: story.reasoningEffort })
        t.send({ command: 'set_storydex_mode', mode: story.agentMode })
        if (config.currentProviderId && config.currentModel) {
          t.send({ command: 'select_model', provider_id: config.currentProviderId, model: config.currentModel })
        }
        void refreshProjectUsage()
      }
    })
    t.onMessage(env => {
      if (transport.value !== t || sessionId.value !== targetSessionId) return
      onInbound(env)
    })
    t.connect()
  }

  function disconnect() { transport.value?.close(); transport.value = null; connectedSessionId = '' }

  function onInbound(env: InboundEnvelope) {
    if (env.type === 'event') applyEvent(env.payload)
    else if (env.type === 'error') pushNotice('error', env.payload.message)
  }

  function applyEvent(ev: AgentEvent) {
    switch (ev.event_type) {
      // 兜底：turn_end 之后又开始吐字（引擎续了一轮），状态得跟着回到忙。
      case 'text_chunk': if (runState.value === 'idle') runState.value = 'thinking'; appendAssistant(ev.content); break
      case 'reasoning_chunk': if (runState.value === 'idle') runState.value = 'thinking'; appendReasoning(ev.content); break
      case 'tool_start':
        endAssistantStream()
        timeline.value.push({ kind: 'tool', callId: ev.call_id, toolName: ev.tool_name, arguments: ev.arguments, status: 'starting', expanded: ev.tool_name === 'show_image' })
        turnToolTrace.push({
          callId: ev.call_id, sequence: turnToolTrace.length + 1, tool: sanitizeToolName(ev.tool_name),
          argumentShape: summarizeArguments(ev.arguments), status: 'running',
        })
        runState.value = 'executing'
        break
      case 'tool_running': patchTool(ev.call_id, c => c.status = 'running'); runState.value = 'executing'; break
      case 'tool_done':
        patchTool(ev.call_id, c => {
          c.status = ev.is_error ? 'error' : 'success'
          c.elapsed = ev.elapsed
          c.resultPreview = ev.result_preview
          c.isError = ev.is_error
          // 工具产生的图片：瀑布流渲染（历史恢复时由 messages.images 补回）
          if (Array.isArray(ev.images) && ev.images.length > 0) c.images = ev.images
        })
        // 工具跑完不等于一轮结束 —— 模型接着想下一步。回 idle 只认 turn_end /
        // 取消 / 致命错误，否则输入区会在循环中途闪回「下达任务」和发送箭头。
        runState.value = 'thinking'
        {
          const trace = turnToolTrace.find(item => item.callId === ev.call_id)
          if (trace) {
            trace.status = ev.is_error ? 'error' : 'success'
            trace.elapsedMs = Math.max(0, Math.round(ev.elapsed * 1000))
            if (ev.is_error) {
              trace.category = classifyToolError(ev.result_preview)
              trace.errorSummary = sanitizeDiagnosticText(ev.result_preview)
            }
          }
        }
        break
      case 'tool_cache_hit':
        patchTool(ev.call_id, c => c.status = 'cache_hit')
        { const trace = turnToolTrace.find(item => item.callId === ev.call_id); if (trace) trace.status = 'success' }
        break
      case 'tool_approval_request':
        endAssistantStream()
        if (!patchTool(ev.call_id, c => { c.status = 'awaiting_approval'; c.access = ev.access; c.riskSummary = ev.risk_summary; c.expanded = true })) {
          timeline.value.push({ kind: 'tool', callId: ev.call_id, toolName: ev.tool_name, arguments: ev.arguments, status: 'awaiting_approval', access: ev.access, riskSummary: ev.risk_summary, expanded: true })
        }
        runState.value = 'awaiting_approval'
        break
      case 'user_question_request':
        endAssistantStream()
        timeline.value.push({ kind: 'question', callId: ev.call_id, question: ev.question, options: ev.options, allowFreeText: ev.allow_free_text ?? true, answered: false })
        runState.value = 'awaiting_question'
        break
      case 'file_transfer_request':
        if (ev.operation === 'import') {
          window.CoomiAndroid?.importFilesForRequest?.(ev.request_id)
        } else if (ev.path) {
          window.CoomiAndroid?.exportFileForRequest?.(
            ev.request_id,
            ev.path,
          ev.suggested_name ?? ev.path.split('/').pop() ?? 'storydex-export',
          )
        }
        break
      case 'usage_update': {
        const previous = usage.value
        usage.value = {
          total: ev.usage.total_tokens ?? previous?.total ?? 0,
          input: ev.usage.input_tokens ?? previous?.input ?? 0,
          output: ev.usage.output_tokens ?? previous?.output ?? 0,
          contextRatio: ev.usage.context_ratio ?? previous?.contextRatio ?? 0,
          contextUsed: ev.usage.context_used_tokens ?? previous?.contextUsed ?? 0,
          contextWindow: ev.usage.context_window_tokens ?? previous?.contextWindow ?? 0,
          cachedInput: ev.usage.cached_input_tokens ?? previous?.cachedInput ?? 0,
          reasoning: ev.usage.reasoning_tokens ?? previous?.reasoning ?? 0,
          turnInput: ev.usage.turn_input_tokens ?? previous?.turnInput ?? 0,
          turnCachedInput: ev.usage.turn_cached_input_tokens ?? previous?.turnCachedInput ?? 0,
          turnOutput: ev.usage.turn_output_tokens ?? previous?.turnOutput ?? 0,
          turnReasoning: ev.usage.turn_reasoning_tokens ?? previous?.turnReasoning ?? 0,
          turnCacheRate: ev.usage.turn_cache_rate ?? previous?.turnCacheRate ?? 0,
          categories: ev.usage.categories ?? previous?.categories ?? {},
          mode: ev.usage.mode ?? previous?.mode ?? story.agentMode,
          project: ev.usage.project ?? previous?.project ?? {},
        }
        break
      }
      case 'compression': pushNotice('info', `上下文已压缩 ${fmtTokens(ev.before)} → ${fmtTokens(ev.after)}`); break
      case 'connection_retry': connection.setRetry(`${ev.message}（${ev.attempt}/${ev.max_attempts}）`); break
      case 'agent_error': endAssistantStream(); pushNotice('error', ev.message); if (ev.is_fatal) runState.value = 'idle'; persistSoon(); break
      case 'agent_cancelled': endAssistantStream(); cancelRunningTools(); pushNotice('warn', '已停止本轮执行'); break
      case 'bg_task_detached': pushNotice('info', `↪ 已转入后台任务 #${ev.task_id}（${ev.tool_name}）`); break
      case 'bg_task_completed': pushNotice(ev.is_error ? 'error' : 'success', `${ev.is_error ? '✕' : '✓'} 后台任务 #${ev.task_id} ${ev.is_error ? '失败' : '完成'}`); break
      case 'loop_progress':
        loop.value = { active: ev.status !== 'done', currentStep: ev.current_step, totalSteps: ev.total_steps, status: ev.status, currentDescription: loop.value.currentDescription }
        break
      case 'loop_step_start':
        loop.value = { ...loop.value, active: true, totalSteps: ev.total_steps, currentStep: ev.step_index, currentDescription: ev.step_description }
        break
      case 'turn_end':
        endAssistantStream(); cancelRunningTools(); connection.setRetry(null); runState.value = 'idle'
        {
          const failures = turnToolTrace.filter(item => item.status === 'error').length
          if (failures >= 3) {
            timeline.value.push({
              kind: 'notice', id: nextId(), tone: 'warn', analysisStatus: 'consent', feedbackEligible: true,
              text: `本轮工具调用失败 ${failures} 次。可在明确同意后，额外轻量调用一次当前模型生成脱敏工程分析并上传。`,
              analysisTrace: turnToolTrace.map(({ callId: _callId, ...item }) => item), failureCount: failures,
            })
          }
          turnToolTrace = []
        }
        void story.captureTurn(timeline.value, sessionId.value)
          .then(fragment => { if (fragment && story.fragments.length % 10 === 0) resetStoryContext() })
          .catch(error => pushNotice('error', error instanceof Error ? error.message : '剧情片段写入失败'))
          .finally(persistSoon)
        break
      case 'session_state': {
        // 重连后引擎告知本会话是否仍在后台执行（切走会话后任务继续跑）。
        sessions.refreshRunning()
        if (ev.running && runState.value === 'idle') runState.value = 'thinking'
        break
      }
      case 'session_loaded': {
        // 打开历史会话时，引擎把持久化的累计用量推过来，避免显示 0。
        const u = ev.usage ?? {}
        usage.value = {
          total: u.total_tokens ?? usage.value?.total ?? 0,
          input: u.input_tokens ?? usage.value?.input ?? 0,
          output: u.output_tokens ?? usage.value?.output ?? 0,
          contextRatio: usage.value?.contextRatio ?? 0,
          contextUsed: usage.value?.contextUsed ?? 0,
          contextWindow: usage.value?.contextWindow ?? 0,
          cachedInput: usage.value?.cachedInput ?? 0,
          reasoning: usage.value?.reasoning ?? 0,
          turnInput: usage.value?.turnInput ?? 0,
          turnCachedInput: usage.value?.turnCachedInput ?? 0,
          turnOutput: usage.value?.turnOutput ?? 0,
          turnReasoning: usage.value?.turnReasoning ?? 0,
          turnCacheRate: usage.value?.turnCacheRate ?? 0,
          categories: usage.value?.categories ?? {},
          mode: usage.value?.mode ?? story.agentMode,
          project: usage.value?.project ?? {},
        }
        if (typeof ev.cwd === 'string' && ev.cwd) cwd.value = ev.cwd
        break
      }
    }
  }

  function cancelRunningTools() {
    // 停止后引擎可能不会逐个补发 tool_done：把仍在运行/准备中的工具卡片
    // 收尾为「已取消」，否则卡片会永远停在旋转的「运行中」状态。
    let changed = false
    for (const item of timeline.value) {
      if (item.kind === 'tool' && (item.status === 'running' || item.status === 'starting')) {
        item.status = 'cancelled'
        item.isError = true
        changed = true
      }
    }
    if (changed) persistSoon()
  }

  function sendMessage(text: string) {
    const trimmed = text.trim()
    if (!trimmed) return
    const agentPrompt = story.promptFor(trimmed)
    // 首条用户消息作为会话标题，抽屉里就不会全是「新对话」。
    const isFirst = !timeline.value.some(t => t.kind === 'user')
    if (isFirst) sessions.touch(sessionId.value, { title: sessions.deriveTitle(trimmed) })
    if (isBusy.value) {
      timeline.value.push({ kind: 'user', id: nextId(), content: trimmed })
      transport.value?.send({ command: 'jump_in', text: agentPrompt })
      persistSoon()
      return
    }
    turnToolTrace = []
    timeline.value.push({ kind: 'user', id: nextId(), content: trimmed })
    runState.value = 'thinking'
    transport.value?.send({ command: 'send_message', text: agentPrompt })
    persistSoon()
  }

  function cancel() { transport.value?.send({ command: 'cancel' }) }
  function approve(callId: string, decision: 'allow' | 'deny' | 'always') {
    patchTool(callId, c => { c.status = decision === 'deny' ? 'error' : 'running'; if (decision === 'deny') { c.resultPreview = '（用户拒绝执行）'; c.isError = true } })
    transport.value?.send({ command: 'approve_tool', call_id: callId, decision })
    if (runState.value === 'awaiting_approval') runState.value = 'executing'
  }
  function answerQuestion(callId: string, answer: string) {
    patchQuestion(callId, q => { q.answered = true; q.answer = answer })
    transport.value?.send({ command: 'answer_question', call_id: callId, answer })
    if (runState.value === 'awaiting_question') runState.value = 'thinking'
  }
  function setPermissionMode(mode: 'ask' | 'auto' | 'full') { config.setPermissionMode(mode); transport.value?.send({ command: 'set_permission_mode', mode }) }
  function togglePlanMode() { const entering = !config.planMode; config.togglePlanMode(); transport.value?.send({ command: entering ? 'enter_plan_mode' : 'exit_plan_mode' }) }
  function selectModel(providerId: string, model: string) { config.selectModel(providerId, model); transport.value?.send({ command: 'select_model', provider_id: providerId, model }) }
  function setReasoningEffort(effort: ReasoningEffort) {
    story.setReasoningEffort(effort)
    transport.value?.send({ command: 'set_reasoning_effort', effort })
  }
  function resetStoryContext() { transport.value?.send({ command: 'reset_story_context' }) }
  function completeFileTransfer(requestId: string, paths: string[]) {
    transport.value?.send({ command: 'file_transfer_result', request_id: requestId, paths })
  }

  function newSession() {
    flushPersistence()
    endAssistantStream(); timeline.value = []; usage.value = null
    loop.value = { active: false, currentStep: 0, totalSteps: 0, status: '' }; runState.value = 'idle'
    sessionId.value = createSessionId()
    connect()
    if (story.projectPath) void setSessionCwd(story.projectPath)
  }

  /** 主模式互切必须换引擎会话，确保上下文窗口与旧模式完全隔离。 */
  function switchAgentMode(mode: import('./story').AgentMode) {
    if (mode === story.agentMode) return
    if (isBusy.value) transport.value?.send({ command: 'cancel' })
    endAssistantStream()
    cancelRunningTools()
    flushPersistence()
    story.setAgentMode(mode)
    sessions.setCurrentMode(mode)
    config.setPermissionMode(mode === 'agent' ? 'full' : 'auto')
    timeline.value = []
    usage.value = null
    loop.value = { active: false, currentStep: 0, totalSteps: 0, status: '' }
    runState.value = 'idle'
    sessionId.value = createSessionId()
    connect()
    if (story.projectPath) void setSessionCwd(story.projectPath)
  }

  /** Return to the latest story fragment without creating or reopening a chat session. */
  function continueStory() {
    endAssistantStream()
    runState.value = 'idle'
    const fragment = story.latest
    timeline.value = fragment
      ? [{ kind: 'assistant', id: fragment.sourceMessageId ?? nextId(), content: fragment.content, streaming: false }]
      : []
  }

  /** 返回剧情首页：清空当前展示，不换会话、不中断后台任务。 */
  function standby() {
    endAssistantStream()
    timeline.value = []
    usage.value = null
    loop.value = { active: false, currentStep: 0, totalSteps: 0, status: '' }
    runState.value = 'idle'
  }

  /** 从引擎 /api/sessions/{id} 恢复完整历史；成功返回 true。 */
  async function restoreFromEngine(id: string): Promise<boolean> {
    try {
      const res = await authedFetch(`/api/sessions/${id}`)
      if (!res.ok) return false
      const session = await res.json()
      const messages = (session.messages ?? []) as ChatMessageJson[]
      if (messages.length === 0) return false
      if (messages.some(m => m.compaction_summary)) {
        // 上下文已压缩：引擎只剩摘要 + 截断的部分历史。前端从未收到压缩版，
        // 本机 localStorage 缓存仍是完整时间线 —— 优先用它恢复展示，
        // 并把压缩摘要折叠成一条提示附在末尾。
        const cached = sessions.loadTranscript(id)
        if (cached && cached.length > 0) {
          const detail = messages.find(m => m.compaction_summary)?.content ?? ''
          timeline.value = [
            ...cached,
            { kind: 'notice', id: nextId(), tone: 'info', text: '（上下文已压缩 · 点击查看摘要）', detail },
          ]
          return true
        }
      }
      timeline.value = messagesToTimeline(messages)
      return true
    } catch {
      return false
    }
  }

  /** 把引擎磁盘会话消息转换为前端时间线（含工具调用卡片与结果回填）。 */
  function messagesToTimeline(messages: ChatMessageJson[]): Timelineitem[] {
    const items: Timelineitem[] = []
    const toolResults = new Map<string, string>()
    const toolImages = new Map<string, string[]>()
    for (const m of messages) {
      if (m.internal) continue
      if (m.compaction_summary) {
        items.push({ kind: 'notice', id: nextId(), tone: 'info', text: '（上下文已压缩 · 点击查看摘要）', detail: m.content ?? '' })
        continue
      }
      if (m.role === 'user') {
        items.push({ kind: 'user', id: nextId(), content: displayUserContent(m.content) })
      } else if (m.role === 'assistant') {
        if (m.content) items.push({ kind: 'assistant', id: nextId(), content: m.content, streaming: false })
        for (const tc of m.tool_calls ?? []) {
          items.push({
            kind: 'tool', callId: tc.id, toolName: tc.name,
            arguments: tc.arguments as Record<string, unknown>,
            status: 'success', expanded: tc.name === 'show_image',
            images: (tc.images ?? []).map((img: { media_type: string; data: string }) =>
              `data:${img.media_type};base64,${img.data}`),
          })
        }
      } else if (m.role === 'tool' && m.tool_call_id) {
        toolResults.set(m.tool_call_id, m.content)
        if (m.images?.length) {
          toolImages.set(m.tool_call_id, m.images.map((img: { media_type: string; data: string }) =>
            `data:${img.media_type};base64,${img.data}`))
        }
      }
    }
    for (const item of items) {
      if (item.kind === 'tool') {
        const result = toolResults.get(item.callId)
        if (result != null) {
          const preview = result.length > 200 ? result.slice(0, 200) + '…' : result
          item.resultPreview = preview
          item.isError = /error|fail|exception|panic/i.test(result.slice(0, 500))
        } else {
          // 没有结果回填（比如被取消/未执行）的调用收尾为已取消
          item.status = 'cancelled'
          item.isError = true
        }
        const imgs = toolImages.get(item.callId)
        if (imgs?.length) {
          item.images = imgs
        } else if (item.toolName === 'show_image' && item.expanded && item.status === 'success') {
          // show_image 历史恢复但图片数据不可用（如已被上下文压缩清理）
          item.imageMissing = true
        }
      }
    }
    return items
  }

  /**
   * 打开一条历史会话：优先从引擎磁盘拉完整历史（权威源，修复“会话消失/串话”），
   * 引擎不可用才回退本机 localStorage 记录。
   */
  async function openSession(id: string) {
    if (id === sessionId.value) return
    flushPersistence()
    endAssistantStream()
    usage.value = null
    loop.value = { active: false, currentStep: 0, totalSteps: 0, status: '' }
    runState.value = 'idle'
    const targetId = isUuid(id) ? id : sessions.migrateId(id, createSessionId())
    sessionId.value = targetId
    const restoredFromEngine = await restoreFromEngine(targetId)
    if (!restoredFromEngine) {
      const restored = sessions.loadTranscript(targetId)
      timeline.value = restored
      if (restored.length > 0) {
        timeline.value.push({
          kind: 'notice', id: nextId(), tone: 'info',
          text: '已恢复本机记录。若引擎重启过，模型这边的上下文可能已经清空。',
        })
      }
    }
    connect()
  }

  function deleteSession(id: string) {
    // 先停掉待落盘的持久化定时器：被删会话不应再写回（否则会“复活”成空标题的新会话）。
    if (persistTimer) { clearTimeout(persistTimer); persistTimer = null }
    if (id === sessionId.value) {
      // 删除的是当前会话：先切到新会话并重连（关闭旧 id 的 WS 连接、清空时间线），
      // 避免 flushPersistence 把已删会话写回，也避免引擎在文件删除后重建同 id 会话。
      endAssistantStream(); timeline.value = []; usage.value = null
      loop.value = { active: false, currentStep: 0, totalSteps: 0, status: '' }; runState.value = 'idle'
      sessionId.value = createSessionId()
      connect()
      if (story.projectPath) void setSessionCwd(story.projectPath)
    }
    sessions.remove(id)
    try { localStorage.removeItem(`coomi.draft.${id}`) } catch { /* ignore */ }
  }

  /** 更新当前会话的工作目录（会话标记路径）。成功后引擎后续 turn 都在该目录执行。 */
  async function setSessionCwd(path: string): Promise<boolean> {
    const id = sessionId.value
    try {
      const res = await authedFetch(`/api/sessions/${id}/cwd`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cwd: path, mode: story.agentMode }),
      })
      if (!res.ok) return false
      cwd.value = path
      sessions.setCurrentCwd(path)
      sessions.setCurrentMode(story.agentMode)
      // 新会话只有目录配置时还不算一条会话；首条用户内容会负责创建元数据。
      if (sessions.find(id)) sessions.touch(id, { cwd: path, mode: story.agentMode })
      return true
    } catch {
      return false
    }
  }

  function appendAssistant(content: string) {
    if (!currentAssistant) {
      timeline.value.push({ kind: 'assistant', id: nextId(), content: '', streaming: true })
      // 必须拿 push 之后数组里的那个对象：ref 会把它包成代理，
      // 直接改 push 进去的原始对象不触发渲染，流式文本就只会停在第一片。
      currentAssistant = timeline.value[timeline.value.length - 1] as AssistantMessage
    }
    currentAssistant.content += content
  }
  function endAssistantStream() { if (currentAssistant) { currentAssistant.streaming = false; currentAssistant = null } }
  function appendReasoning(content: string) {
    const last = timeline.value[timeline.value.length - 1]
    if (last && last.kind === 'reasoning') { (last as ReasoningBlock).content += content }
    else { timeline.value.push({ kind: 'reasoning', id: nextId(), content, expanded: false }) }
  }
  function patchTool(callId: string, fn: (c: ToolCard) => void): boolean {
    for (let i = timeline.value.length - 1; i >= 0; i--) { const t = timeline.value[i]; if (t.kind === 'tool' && t.callId === callId) { fn(t); return true } }
    return false
  }
  function patchQuestion(callId: string, fn: (q: QuestionCard) => void) {
    for (let i = timeline.value.length - 1; i >= 0; i--) { const t = timeline.value[i]; if (t.kind === 'question' && t.callId === callId) { fn(t); return } }
  }
  function pushNotice(tone: 'info' | 'warn' | 'error' | 'success', text: string) { timeline.value.push({ kind: 'notice', id: nextId(), tone, text }) }

  async function consentToolFailureFeedback(noticeId: string): Promise<string> {
    const notice = timeline.value.find(item => item.kind === 'notice' && item.id === noticeId)
    if (notice?.kind !== 'notice' || !notice.analysisTrace?.length) return ''
    if (!['consent', 'failed'].includes(notice.analysisStatus ?? '')) return notice.detail ?? ''
    notice.analysisStatus = 'analyzing'; notice.feedbackEligible = false
    notice.text = '正在本地整理脱敏工具错误；可以继续对话。'
    try {
      const response = await authedFetch('/api/tool-failure-analysis', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider_id: config.currentProviderId, trace: notice.analysisTrace }),
      })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const data = await response.json() as { analysis?: string }
      const analysis = data.analysis?.trim() ?? ''
      if (!analysis) throw new Error('分析结果为空')
      notice.analysisStatus = 'ready'; notice.detail = `${analysis}\n\n${buildLocalEvidence(notice.analysisTrace)}`
      notice.text = '脱敏分析已完成，正在上传。'
      return notice.detail
    } catch (error) {
      notice.analysisStatus = 'failed'; notice.feedbackEligible = true
      notice.text = `反馈整理失败，未上传任何内容：${error instanceof Error ? error.message : String(error)}`
      return ''
    }
  }

  function finishToolFailureFeedback(noticeId: string, ok: boolean, reason = '') {
    const notice = timeline.value.find(item => item.kind === 'notice' && item.id === noticeId)
    if (notice?.kind !== 'notice') return
    if (ok) {
      notice.analysisStatus = 'complete'; notice.feedbackEligible = false; notice.analysisTrace = undefined
      notice.text = '工具错误已完成脱敏分析并上传，感谢反馈。'
    } else {
      notice.analysisStatus = 'ready'; notice.feedbackEligible = true
      notice.text = `分析已完成，但上传失败${reason ? `：${reason}` : ''}。重试不会再次调用模型。`
    }
  }

  return { sessionId, timeline, runState, usage, cwd, loop, isBusy, pendingApproval, pendingQuestion, connect, disconnect, sendMessage, cancel, approve, answerQuestion, setPermissionMode, togglePlanMode, selectModel, setReasoningEffort, refreshProjectUsage, resetStoryContext, completeFileTransfer, newSession, switchAgentMode, continueStory, standby, openSession, deleteSession, setSessionCwd, sendGuide, consentToolFailureFeedback, finishToolFailureFeedback }
})

function fmtTokens(n: number): string { return n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n) }

function displayUserContent(content: string): string {
  for (const marker of ['\n玩家行动：', '\n玩家输入：', '\n用户指令：']) {
    const index = content.lastIndexOf(marker)
    if (index >= 0) return content.slice(index + marker.length).trim()
  }
  return content
}

function sanitizeToolName(name: string): string { return name.replace(/[^a-zA-Z0-9_.:-]/g, '').slice(0, 80) || 'unknown_tool' }
function classifyToolError(message: string): string {
  const text = message.toLowerCase()
  if (/permission|denied|allowed area/.test(text)) return 'permission_or_sandbox'
  if (/timeout|timed out/.test(text)) return 'timeout'
  if (/not found|enoent/.test(text)) return 'not_found'
  if (/invalid|schema|argument|parse/.test(text)) return 'invalid_arguments'
  if (/network|connect|dns|http/.test(text)) return 'network_or_upstream'
  return 'execution_error'
}
function summarizeArguments(value: unknown, key = '', depth = 0): unknown {
  if (depth > 4) return '[max_depth]'
  if (value === null) return '[null]'
  if (Array.isArray(value)) return value.slice(0, 12).map(item => summarizeArguments(item, key, depth + 1))
  if (typeof value === 'object') return Object.fromEntries(Object.entries(value as Record<string, unknown>).slice(0, 30).map(([childKey, child]) => [childKey.replace(/[^a-zA-Z0-9_.:-]/g, '').slice(0, 80) || 'field', summarizeArguments(child, childKey, depth + 1)]))
  if (typeof value === 'boolean') return value
  if (typeof value === 'number') return '[number]'
  if (typeof value !== 'string') return `[${typeof value}]`
  const text = value.trim(); const lowerKey = key.toLowerCase()
  if (/key|token|secret|password|authorization|credential/.test(lowerKey)) return '[redacted_secret]'
  if (/path|file|dir|cwd|destination|source/.test(lowerKey) || /^(?:\/|[a-z]:\\)/i.test(text)) return `[${/^(?:\/|[a-z]:\\)/i.test(text) ? 'absolute' : 'relative'}_path]`
  if (/command|cmd|script/.test(lowerKey) || /[\s;&|><]/.test(text)) return { kind: 'command_shape', token_count: text.split(/\s+/).filter(Boolean).length, has_shell_operators: /[;&|><]/.test(text) }
  if (/^https?:\/\//i.test(text)) return '[url_redacted]'
  if (/^[a-zA-Z][a-zA-Z0-9_.:-]{0,31}$/.test(text)) return text
  return `[string length=${text.length}]`
}
function sanitizeDiagnosticText(message: string): string {
  return message.slice(0, 1200).replace(/\b(?:sk-|Bearer\s+)[a-zA-Z0-9._-]{8,}\b/gi, '[redacted_secret]').replace(/https?:\/\/[^\s"']+/gi, '[redacted_url]').replace(/(?:[a-zA-Z]:\\|\/data\/|\/storage\/|\/sdcard\/|\/home\/)[^\s"']+/g, '[redacted_path]').replace(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g, '[redacted_email]').replace(/\b[0-9a-f]{24,}\b/gi, '[redacted_identifier]')
}
function buildLocalEvidence(items: ToolDiagnosticTrace[]): string {
  return ['【程序采集的脱敏证据】', '不含玩家消息、小说正文、原始参数值、文件内容、真实路径、URL、密钥或模型隐藏思维。', ...items.map(item => `#${item.sequence} ${item.tool} | ${item.status}${item.category ? ` | ${item.category}` : ''}${item.elapsedMs !== undefined ? ` | ${item.elapsedMs}ms` : ''}\n参数结构: ${JSON.stringify(item.argumentShape)}${item.errorSummary ? `\n错误摘要: ${item.errorSummary}` : ''}`)].join('\n')
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

function isUuid(value: string): boolean {
  return UUID_PATTERN.test(value)
}

/** 引擎磁盘上会话文件的原始消息结构（与 coomi-engine 的 ChatMessage 对应）。 */
interface ChatMessageJson {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
  tool_calls?: Array<{
    id: string
    name: string
    arguments: unknown
    images?: Array<{ media_type: string; data: string }>
  }>
  tool_call_id?: string
  compaction_summary?: boolean
  internal?: boolean
  images?: Array<{ media_type: string; data: string }>
}

function createSessionId(): string {
  const cryptoApi = globalThis.crypto
  if (typeof cryptoApi?.randomUUID === 'function') return cryptoApi.randomUUID()
  const bytes = new Uint8Array(16)
  cryptoApi.getRandomValues(bytes)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
