<script setup lang="ts">
/**
 * 主聊天窗口。
 *
 * 三件事和别处不一样：
 * 1) 抽屉打开时整个 shell 往右推 + 轻微缩放，是 DeepSeek 的那种层次感；
 * 2) 跟随滚动交给 useAutoScroll，高度变化用 ResizeObserver 兜住 ——
 *    markdown 重排、工具卡展开、软键盘弹出都会改高度，只 watch 数组长度会漏；
 * 3) 连续的工具调用合并成一个 ToolGroup，避免长任务把时间线冲成一堵卡片墙。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useSessionStore } from '@/stores/session'
import { useSessionsStore } from '@/stores/sessions'
import { useConfigStore } from '@/stores/config'
import { useStoryStore, type StoryFragment } from '@/stores/story'
import { useProjectStore } from '@/stores/project'
import { DEMO_PROMPT, isUnattended, shouldAutoplay } from '@/bridge/demoMode'
import { useAutoScroll } from '@/composables/useAutoScroll'
import type { AssistantMessage, Timelineitem, ToolCard } from '@/stores/viewModel'
import type { ApprovalDecision } from '@/protocol/commands'
import TopBar from '@/components/TopBar.vue'
import SideDrawer from '@/components/SideDrawer.vue'
import StatusBar from '@/components/StatusBar.vue'
import Composer from '@/components/Composer.vue'
import EmptyState from '@/components/EmptyState.vue'
import MessageBubble from '@/components/MessageBubble.vue'
import ToolGroup from '@/components/ToolGroup.vue'
import ReasoningBlock from '@/components/ReasoningBlock.vue'
import NoticeItem from '@/components/NoticeItem.vue'
import LoopProgressBar from '@/components/LoopProgressBar.vue'
import ApprovalSheet from '@/components/ApprovalSheet.vue'
import QuestionSheet from '@/components/QuestionSheet.vue'
import CoomiIcon from '@/components/CoomiIcon.vue'
import BottomSheet from '@/components/ui/BottomSheet.vue'

type Block =
  | { t: 'one'; key: string; item: Timelineitem }
  | { t: 'tools'; key: string; cards: ToolCard[] }

const session = useSessionStore()
const sessions = useSessionsStore()
const config = useConfigStore()
const story = useStoryStore()
const project = useProjectStore()

const scroller = ref<HTMLElement | null>(null)
const content = ref<HTMLElement | null>(null)
const drawerOpen = ref(false)
const executionOpen = ref(false)
const storyProjectionReady = ref(story.agentMode !== 'story')
const editingFragment = ref<StoryFragment | null>(null)
const editText = ref('')
const editError = ref('')
const savingFragment = ref(false)
/** 全局轮询「后台运行中」状态的定时器（会话列表转圈的数据源）。 */
let runningPoll: ReturnType<typeof setInterval> | null = null

/** 长会话动态加载：只渲染最近一段，顶部可「加载更早记录」。 */
const RENDER_WINDOW = 60
const windowSize = ref(RENDER_WINDOW)
const hasMore = computed(() => session.timeline.length > windowSize.value)
function loadMore() { windowSize.value += RENDER_WINDOW }
// 新消息到达时 slice(-windowSize) 自动包含最新，无需额外 watch

const { following, follow, jumpToBottom } = useAutoScroll(scroller)

function idOf(i: Timelineitem): string { return 'id' in i ? i.id : i.callId }

function buildBlocks(items: Timelineitem[]): Block[] {
  const out: Block[] = []
  for (const item of items) {
    if (item.kind === 'tool') {
      const last = out[out.length - 1]
      if (last && last.t === 'tools') { last.cards.push(item); continue }
      out.push({ t: 'tools', key: 'g:' + item.callId, cards: [item] })
      continue
    }
    out.push({ t: 'one', key: item.kind + ':' + idOf(item), item })
  }
  return out
}

const blocks = computed<Block[]>(() => buildBlocks(session.timeline.slice(-windowSize.value)))
const lastUserIndex = computed(() => {
  for (let index = session.timeline.length - 1; index >= 0; index--) {
    if (session.timeline[index].kind === 'user') return index
  }
  return -1
})
const currentTurnItems = computed(() => lastUserIndex.value >= 0
  ? session.timeline.slice(lastUserIndex.value + 1)
  : session.timeline)
const currentStory = computed(() => {
  const assistants = currentTurnItems.value.filter(item => item.kind === 'assistant')
  return assistants[assistants.length - 1] ?? null
})
const previousStory = computed(() => {
  if (!session.isBusy || lastUserIndex.value < 0) return null
  for (let index = lastUserIndex.value - 1; index >= 0; index--) {
    const item = session.timeline[index]
    if (item.kind === 'assistant') return item
  }
  return null
})
const currentTurnBlocks = computed(() => buildBlocks(currentTurnItems.value))
const executionBlocks = computed(() => currentTurnBlocks.value.filter(block => block.t === 'tools'
  || (block.t === 'one' && !['user', 'assistant'].includes(block.item.kind))))
const currentSuggestions = computed(() => {
  if (session.isBusy || project.consistency.required || !currentStory.value || !story.latest) return []
  const isCapturedTurn = story.latest.sourceMessageId === currentStory.value.id
  const isContinuedFragment = session.timeline.length === 1 && story.latest.content === currentStory.value.content
  return isCapturedTurn || isContinuedFragment ? story.latest.suggestions : []
})
const historicalMessage = computed<AssistantMessage | null>(() => story.viewedFragment
  ? {
      kind: 'assistant',
      id: `fragment-view:${story.viewedFragment.id}`,
      content: story.viewedFragment.content,
      streaming: false,
    }
  : null)

function returnToLatest() { session.continueStory(); void nextTick(follow) }
function editViewedFragment() {
  if (!story.viewedFragment) return
  editingFragment.value = story.viewedFragment
  editText.value = story.viewedFragment.content
  editError.value = ''
  askDiscard.value = false
}

/** 编辑器有未保存改动。它同时关掉遮罩点击关闭——原先点一下遮罩，整段改写就没了，
 *  而这个弹层几乎占满屏幕，误触的是它上方那条窄缝，代价却是整段正文。 */
const editorDirty = computed(() => !!editingFragment.value && editText.value !== editingFragment.value.content)
const askDiscard = ref(false)
function closeEditor() {
  if (editorDirty.value) { askDiscard.value = true; return }
  discardEdit()
}
function discardEdit() {
  editingFragment.value = null
  askDiscard.value = false
}
async function saveViewedFragment() {
  const fragment = editingFragment.value
  if (!fragment) return
  savingFragment.value = true
  editError.value = ''
  try {
    const saved = await story.updateFragment(fragment.id, editText.value, session.sessionId)
    if (!saved) throw new Error('剧情片段不存在或已被移除')
    discardEdit()
  } catch (error) {
    editError.value = error instanceof Error ? error.message : '保存失败，请稍后重试'
  } finally {
    savingFragment.value = false
  }
}

let ro: ResizeObserver | null = null

onMounted(async () => {
  sessions.setCurrentMode(story.agentMode)
  if (story.projectPath) sessions.setCurrentCwd(story.projectPath)
  await session.restoreRetainedContext()
  await story.loadFragmentsFromProject()
  if (story.agentMode === 'story') session.continueStory()
  session.connect()
  if (story.projectPath) {
    void session.setSessionCwd(story.projectPath).then(bound => {
      if (bound) return story.syncFragments(session.sessionId)
      return 0
    }).catch(() => { /* A failed future turn will surface the write error in the timeline. */ })
  }
  // restoreRetainedContext 已同步引擎权威列表并恢复当前项目/模式。
  if (config.providers.length === 0) void config.fetchProviders()
  // 全局记忆开关以引擎为权威：启动即同步，避免「开关显示关、引擎实际开」的脱节。
  void config.syncGlobalMemoryFromEngine()
  // 全局轮询各会话的「后台运行中」状态：切走会话后任务在引擎侧继续跑，
  // 抽屉/会话页据此显示转圈。轮询常驻（本地 API 开销极小），不依赖抽屉打开。
  void sessions.refreshRunning()
  runningPoll = setInterval(() => sessions.refreshRunning(), 2000)
  // 高度只要变就重新贴底（内部有 rAF 合并，不怕高频触发）
  if (typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(() => follow())
    if (content.value) ro.observe(content.value)
    if (scroller.value) ro.observe(scroller.value)
  }
  nextTick(follow)
  // 演示模式自动播一轮，省得进来还要先打字才能看见瀑布流。
  if (shouldAutoplay() && session.timeline.length === 0) {
    setTimeout(() => { if (session.timeline.length === 0) session.sendMessage(DEMO_PROMPT) }, 700)
  }
})

onBeforeUnmount(() => {
  if (runningPoll) { clearInterval(runningPoll); runningPoll = null }
  ro?.disconnect(); ro = null
})

/**
 * 无人值守演示（?demo=1&auto=1）：授权弹层和提问弹层过一会儿自己点掉。
 * 走的是 approve / answerQuestion —— 和真手指按下去完全同一条路，
 * 所以卡片状态、「已回答」气泡都跟着变。截图、录屏、摆着自演都靠它。
 */
if (isUnattended()) {
  const AUTOPILOT_DELAY = 1600
  watch(() => session.pendingApproval?.callId, id => {
    if (!id) return
    setTimeout(() => { if (session.pendingApproval?.callId === id) session.approve(id, 'allow') }, AUTOPILOT_DELAY)
  })
  watch(() => session.pendingQuestion?.callId, id => {
    if (!id) return
    setTimeout(() => {
      const q = session.pendingQuestion
      if (q?.callId === id) session.answerQuestion(id, q.options?.[0] ?? '你定就行')
    }, AUTOPILOT_DELAY)
  })
}

// ResizeObserver 不可用时的兜底：至少条目增减能跟上。
watch(() => session.timeline.length, () => nextTick(follow))
watch(() => session.isBusy, busy => { executionOpen.value = busy })
watch(() => story.agentMode, mode => { storyProjectionReady.value = mode !== 'story' })
watch(() => story.liveViewRevision, () => { storyProjectionReady.value = true })

function onDecide(callId: string, decision: ApprovalDecision) { session.approve(callId, decision) }
function onAnswer(callId: string, text: string) { session.answerQuestion(callId, text) }
</script>

<template>
  <div class="chat">
    <div class="shell" :class="{ pushed: drawerOpen }">
      <TopBar @menu="drawerOpen = true" />

      <main ref="scroller" class="stream">
        <div ref="content" class="inner">
          <EmptyState v-if="session.timeline.length === 0 && !story.viewingHistory && storyProjectionReady" />

          <button v-if="story.agentMode !== 'story' && hasMore" class="load-more" @click="loadMore">
            加载更早记录（还有 {{ session.timeline.length - windowSize }} 条）
          </button>

          <template v-if="story.agentMode === 'story'">
            <template v-if="!storyProjectionReady" />
            <template v-else-if="story.viewingHistory && historicalMessage">
              <div class="fragment-view-head">
                <button class="back-latest" @click="returnToLatest"><CoomiIcon name="arrowLeft" :size="16" /><span>返回最新剧情</span></button>
                <span class="fragment-name">{{ story.viewedFragment?.filename }}</span>
                <button class="edit-fragment" @click="editViewedFragment"><CoomiIcon name="pencil" :size="15" /><span>编辑</span></button>
              </div>
              <MessageBubble :msg="historicalMessage" />
            </template>
            <template v-else>
              <MessageBubble v-if="previousStory" :msg="previousStory" />
              <section v-if="session.isBusy || executionBlocks.length" class="execution" :class="{ collapsed: !executionOpen }">
                <button class="execution-head" @click="executionOpen = !executionOpen">
                  <span v-if="session.isBusy" class="exec-dot" />
                  <CoomiIcon v-else name="check" :size="15" />
                  <span>{{ session.isBusy ? 'Agent 正在推进剧情' : '本轮执行过程' }}</span>
                  <CoomiIcon :name="executionOpen ? 'chevronDown' : 'chevronRight'" :size="15" />
                </button>
                <div v-if="executionOpen" class="execution-body">
                  <template v-for="b in executionBlocks" :key="b.key">
                    <ToolGroup v-if="b.t === 'tools'" :cards="b.cards" />
                    <ReasoningBlock v-else-if="b.item.kind === 'reasoning'" :block="b.item" />
                    <NoticeItem v-else-if="b.item.kind === 'notice'" :notice="b.item" />
                  </template>
                </div>
              </section>
              <MessageBubble v-if="currentStory" :msg="currentStory" />
              <div v-if="currentSuggestions.length" class="story-actions">
                <button v-for="suggestion in currentSuggestions" :key="suggestion" @click="session.sendMessage(suggestion)">{{ suggestion }}</button>
              </div>
            </template>
          </template>

          <template v-else v-for="b in blocks" :key="b.key">
            <ToolGroup v-if="b.t === 'tools'" :cards="b.cards" />
            <template v-else>
              <MessageBubble
                v-if="b.item.kind === 'user' || b.item.kind === 'assistant'"
                :msg="b.item"
              />
              <ReasoningBlock v-else-if="b.item.kind === 'reasoning'" :block="b.item" />
              <NoticeItem v-else-if="b.item.kind === 'notice'" :notice="b.item" />
              <div
                v-else-if="b.item.kind === 'question' && b.item.answered"
                class="q-answered cascade"
              >
                <span class="q-label">已回答</span> {{ b.item.answer }}
              </div>
            </template>
          </template>
        </div>
      </main>

      <!-- 零高度锚点：让「回到底部」浮在流区底边之上。原先是对 .shell 定位再写死
           bottom:116px 去猜「状态栏 + 输入区」的总高——而输入区会随文字长到 132px、
           历史模式下整块不渲染、循环进行时上面还多一条进度条，那个常数在这三种
           情况下都是错的。锚在这里就永远贴着下方那一摞控件的顶边。 -->
      <div class="to-bottom-anchor">
        <Transition name="pop">
          <button v-if="!following" class="to-bottom" aria-label="回到底部" @click="jumpToBottom">
            <CoomiIcon name="arrowDown" :size="18" />
          </button>
        </Transition>
      </div>

      <LoopProgressBar v-if="session.loop.active" :loop="session.loop" />
      <StatusBar />
      <Composer v-if="!story.viewingHistory && (story.agentMode !== 'story' || storyProjectionReady)" />
    </div>

    <SideDrawer :open="drawerOpen" @close="drawerOpen = false" />

    <ApprovalSheet
      v-if="session.pendingApproval"
      :card="session.pendingApproval"
      @decide="(d: ApprovalDecision) => onDecide(session.pendingApproval!.callId, d)"
    />
    <QuestionSheet
      v-else-if="session.pendingQuestion"
      :card="session.pendingQuestion"
      @answer="(t: string) => onAnswer(session.pendingQuestion!.callId, t)"
    />

    <BottomSheet
      v-if="editingFragment"
      :grip="false"
      height="min(72vh, 620px)"
      :dismissible="!editorDirty"
      @close="closeEditor"
    >
      <template #head>
        <span class="ename">{{ editingFragment.filename }}</span>
        <button class="ex" aria-label="关闭" @click="closeEditor"><CoomiIcon name="close" :size="18" /></button>
      </template>

      <textarea v-model="editText" class="etext" aria-label="编辑剧情片段" />
      <p v-if="editError" class="edit-error">{{ editError }}</p>

      <template #actions>
        <template v-if="askDiscard">
          <button class="btn" @click="askDiscard = false">继续编辑</button>
          <button class="btn btn-danger" @click="discardEdit">放弃修改</button>
        </template>
        <button v-else class="save-fragment" :disabled="savingFragment" @click="saveViewedFragment">{{ savingFragment ? '同步中…' : '保存并同步文件' }}</button>
      </template>
    </BottomSheet>
  </div>
</template>

<style scoped>
.chat { height: 100%; min-height: 0; background: var(--bg); }

.shell {
  position: relative;
  display: flex; flex-direction: column; height: 100%; min-height: 0;
  background: var(--bg);
  transform-origin: left center;
  /* 只保留 transform 动画：Android WebView 里 transform+border-radius 同时
     过渡会反复重建合成层，表现为打开侧边栏时主内容文字闪烁。
     will-change 让合成层常驻，避免动画开始/结束时闪一下。 */
  transition: transform .3s cubic-bezier(.22, .68, .19, 1);
  will-change: transform;
}
.shell.pushed {
  /* origin 为 left center 时，scale(.94) 使右边缘内缩 6%；
     translateX(6%) 精确抵消，保证右侧始终贴住屏幕右缘（不会右侧被裁）。 */
  transform: translateX(6%) scale(.94);
  border-radius: 20px;
  overflow: hidden;
  box-shadow: var(--shadow-2);
}

.stream {
  flex: 1; min-width: 0; min-height: 0; max-width: 100%; overflow-x: hidden; overflow-y: auto;
  -webkit-overflow-scrolling: touch; overscroll-behavior-y: contain;
}
.inner {
  display: flex; flex-direction: column; gap: 12px;
  width: 100%; min-width: 0; min-height: 100%; padding: 10px 12px 18px; overflow-x: hidden;
}

.to-bottom-anchor { position: relative; z-index: 8; height: 0; }
.to-bottom {
  position: absolute; left: 50%; bottom: 10px;
  display: grid; place-items: center;
  width: 38px; height: 38px; margin-left: -19px;
  border: 1px solid var(--border); border-radius: 50%;
  background: var(--bg); color: var(--text-2);
  box-shadow: var(--shadow-2);
}
/* 画 38px 是为了不压着正文，但拇指够得着的范围要满 44px：用一层透明伪元素把
   命中区域向外撑开 3px，视觉不变。 */
.to-bottom::after { content: ''; position: absolute; inset: -3px; border-radius: 50%; }
.to-bottom:active { background: var(--fill); }
.load-more {
  display: block; margin: 4px auto 12px; padding: 7px 16px;
  border-radius: var(--r-pill); border: 1px dashed var(--border-strong);
  background: transparent; color: var(--text-3);
  font-size: 12.5px; font-weight: 550;
}
.load-more:active { background: var(--fill); }
.pop-enter-active, .pop-leave-active { transition: opacity .18s ease, transform .18s ease; }
.pop-enter-from, .pop-leave-to { opacity: 0; transform: translateY(8px) scale(.9); }

.q-answered {
  align-self: flex-end; max-width: 84%;
  padding: 7px 13px; border-radius: var(--r-pill);
  background: var(--fill); font-size: 12.5px; color: var(--text-2);
}
.q-label { color: var(--blue); font-weight: 600; }
.execution {
  display: flex; flex: 0 0 auto; flex-direction: column;
  width: 100%; max-height: 50vh; overflow: hidden;
  border: 1px solid var(--border); border-radius: var(--r-sm); background: var(--fill);
}
.execution-head {
  display: flex; flex: 0 0 42px; align-items: center; gap: 8px;
  width: 100%; min-height: 42px; padding: 0 12px;
  color: var(--text-2); font-size: 13px;
}
.execution-head span:nth-child(2) { flex: 1; text-align: left; }
.execution-body {
  display: flex; flex: 0 1 auto; flex-direction: column; gap: 8px;
  min-height: 0; max-height: calc(50vh - 42px); overflow-y: auto;
  padding: 0 8px 8px; overscroll-behavior: contain; -webkit-overflow-scrolling: touch;
}
.execution-body > * { flex: 0 0 auto; min-width: 0; }
.exec-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--blue); animation: coomi-pulse-ring 1.2s infinite; }
.story-actions { display: grid; gap: 7px; padding: 2px 0 8px; }
.story-actions button { min-height: 42px; padding: 8px 12px; border: 1px solid var(--border); border-radius: var(--r-sm); background: var(--bg); color: var(--text-2); text-align: left; font-size: 13.5px; }
.story-actions button:active { background: var(--blue-soft); color: var(--blue); }
.fragment-view-head { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 8px; min-height: 42px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }
.fragment-view-head button { display: inline-flex; align-items: center; gap: 5px; min-height: 34px; color: var(--blue); font-size: 12.5px; }
.fragment-name { overflow: hidden; color: var(--text-3); font-family: var(--font-mono); font-size: 10.5px; text-align: center; text-overflow: ellipsis; white-space: nowrap; }
.ename { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--font-mono); font-size: 12px; color: var(--text-2); }
.ex { display: grid; place-items: center; flex-shrink: 0; width: 36px; height: 36px; margin: -2px -8px 0 0; color: var(--text-3); }
.etext { flex: 1; min-height: 0; width: 100%; margin-top: 6px; resize: none; padding: 12px; border: 1px solid var(--border); border-radius: var(--r-sm); background: var(--bg-input); color: var(--text); font: 15px/1.75 var(--font-ui); }
.edit-error { margin: 8px 0 0; color: var(--danger); font-size: 12.5px; }
.save-fragment { min-height: 44px; border-radius: var(--r-sm); background: var(--blue); color: var(--on-accent); font-size: 15px; font-weight: 650; }
.save-fragment:disabled { opacity: .55; }
</style>
