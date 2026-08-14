<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageHead from '@/components/PageHead.vue'
import CoomiIcon from '@/components/CoomiIcon.vue'
import { THEME_MODES, useConfigStore } from '@/stores/config'
import { useSessionStore } from '@/stores/session'
import { useStoryStore, type AgentMode, type NarrativeMode, type ReasoningEffort } from '@/stores/story'
import { useProjectStore, type ManagedItem, type MemoryFact } from '@/stores/project'
import {
  KEYWORD_LIBRARY_LABELS, type KeywordLibrary, type KeywordLibraryKind, useKeywordLibraryStore,
} from '@/stores/keywordLibraries'
import type { CharacterGenderMode } from '@/story/randomMechanics'
import { authedFetch } from '@/bridge/http'

type Tab = 'basic' | 'presets' | 'random' | 'scripts' | 'memory' | 'time' | 'theme'
type CollectionKind = 'presets' | 'scripts'
type AsyncAction = () => Promise<void>

const router = useRouter()
const route = useRoute()
const config = useConfigStore()
const session = useSessionStore()
const story = useStoryStore()
const project = useProjectStore()
const libraries = useKeywordLibraryStore()
const notice = ref('')
const error = ref('')
const tabs: Array<{ id: Tab; label: string; icon: string }> = [
  { id: 'basic', label: '基础设置', icon: 'settings' },
  { id: 'presets', label: '风格预设', icon: 'sparkle' },
  { id: 'random', label: '随机系统', icon: 'dice' },
  { id: 'scripts', label: '剧本管理', icon: 'fileWrite' },
  { id: 'memory', label: '记忆系统', icon: 'cpu' },
  { id: 'time', label: '时间系统', icon: 'clock' },
  { id: 'theme', label: '主题外观', icon: 'palette' },
]
const requestedTab = String(route.query.tab ?? 'basic') as Tab
const activeTab = ref<Tab>(tabs.some(tab => tab.id === requestedTab) ? requestedTab : 'basic')

const STORY_MODES: Array<{ mode: AgentMode; label: string; desc: string }> = [
  { mode: 'story', label: '剧情模式', desc: '推进并归档剧情，模型文件权限只读' },
  { mode: 'narrator', label: '剧情旁白', desc: '只解释已发生内容，不续写、不剧透' },
  { mode: 'agent', label: 'Agent', desc: '完整管理当前故事项目' },
]
const NARRATIVE_MODES: Array<{ mode: NarrativeMode; label: string }> = [
  { mode: 'immersive', label: '沉浸' }, { mode: 'narrative', label: '叙事' }, { mode: 'free', label: '自由' },
]
const REASONING: Array<{ value: ReasoningEffort; label: string }> = [
  { value: 'auto', label: '自动' }, { value: 'low', label: '低' }, { value: 'medium', label: '中' },
  { value: 'high', label: '高' }, { value: 'xhigh', label: '超高' },
]
const GENDERS: Array<{ value: CharacterGenderMode; label: string }> = [
  { value: 'random', label: '随机' }, { value: 'male', label: '男性' }, { value: 'female', label: '女性' },
]
const LIBRARY_KINDS: KeywordLibraryKind[] = ['event', 'male', 'female']
const effortStats = computed(() => session.usage?.project.reasoning_efforts ?? {})

function formatAverageTokens(effort: ReasoningEffort) {
  const value = effortStats.value[effort]
  if (!value?.turns) return '--'
  const tokens = value.average_tokens
  return tokens >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : String(tokens)
}

function formatAverageDuration(effort: ReasoningEffort) {
  const value = effortStats.value[effort]
  if (!value?.turns) return '--'
  const seconds = Math.round(value.average_duration_ms / 1000)
  if (seconds < 60) return `${seconds}s`
  return `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, '0')}s`
}

function selectTab(tab: Tab) {
  activeTab.value = tab
  void router.replace({ query: { ...route.query, tab } })
}

async function run(action: AsyncAction, success = '') {
  error.value = ''
  try {
    await action()
    if (success) notice.value = success
  } catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause) }
}

const mutation = ref<{ action: AsyncAction; success: string } | null>(null)
const queuedMutation = ref<{ action: AsyncAction; success: string } | null>(null)
function mutate(action: AsyncAction, success: string) {
  if (session.isBusy) mutation.value = { action, success }
  else void applyMutation({ action, success })
}
async function applyMutation(target: { action: AsyncAction; success: string }) {
  await run(target.action, target.success)
  session.resetStoryContext()
}
function resolveMutation(choice: 'stop' | 'after' | 'cancel') {
  const target = mutation.value
  mutation.value = null
  if (!target || choice === 'cancel') return
  if (choice === 'after') { queuedMutation.value = target; return }
  session.cancel()
  queuedMutation.value = target
}
watch(() => session.isBusy, busy => {
  if (!busy && queuedMutation.value) {
    const target = queuedMutation.value
    queuedMutation.value = null
    void applyMutation(target)
  }
})

const confirmBox = ref<{ title: string; message: string; action: AsyncAction; success: string } | null>(null)
function askConfirm(title: string, message: string, action: AsyncAction, success: string) {
  confirmBox.value = { title, message, action, success }
}
function acceptConfirm() {
  const target = confirmBox.value
  confirmBox.value = null
  if (target) mutate(target.action, target.success)
}

async function patchProjectSettings(patch: Parameters<typeof project.patchSettings>[0]) {
  await project.patchSettings(patch)
  if (patch.fortuneEnabled != null) story.setFortuneEnabled(patch.fortuneEnabled)
  if (patch.eventEnabled != null) story.setEventEnabled(patch.eventEnabled)
  if (patch.characterEnabled != null) story.setCharacterEnabled(patch.characterEnabled)
  if (patch.characterGender) story.setCharacterGender(patch.characterGender)
}

// Random libraries
const libraryEditor = ref<{ kind: KeywordLibraryKind; draft: KeywordLibrary } | null>(null)
const keywordSearch = ref('')
const newCategory = ref('')
const visibleLibrary = computed(() => {
  if (!libraryEditor.value) return []
  const query = keywordSearch.value.trim().toLocaleLowerCase()
  return Object.entries(libraryEditor.value.draft).filter(([category, words]) =>
    !query || category.toLocaleLowerCase().includes(query) || words.some(word => word.toLocaleLowerCase().includes(query)),
  )
})
function openLibrary(kind: KeywordLibraryKind) {
  libraryEditor.value = { kind, draft: JSON.parse(JSON.stringify(libraries.active(kind))) }
  keywordSearch.value = ''
}
function addCategory() {
  const name = newCategory.value.trim()
  if (!name || !libraryEditor.value || libraryEditor.value.draft[name]) return
  libraryEditor.value.draft[name] = ['新词条']
  newCategory.value = ''
}
function addKeyword(category: string) { libraryEditor.value?.draft[category].push('新词条') }
function removeKeyword(category: string, index: number) { libraryEditor.value?.draft[category].splice(index, 1) }
function saveLibrary() {
  const editor = libraryEditor.value
  if (!editor) return
  mutate(async () => { await libraries.replaceLibrary(editor.kind, editor.draft); libraryEditor.value = null }, '词库已保存并重建上下文')
}
const fileInput = ref<HTMLInputElement | null>(null)
const importKind = ref<KeywordLibraryKind | null>(null)
function startLibraryImport(kind: KeywordLibraryKind) {
  importKind.value = kind
  if (window.CoomiAndroid?.importFilesForRequest) window.CoomiAndroid.importFilesForRequest(`keyword-library:${kind}`)
  else fileInput.value?.click()
}
async function importLibraryRaw(kind: KeywordLibraryKind, raw: string) {
  askConfirm('完整替换词库', `所选 JSON 将完整替换当前${KEYWORD_LIBRARY_LABELS[kind]}词库。分类数量可以不同，指令式内容会被拒绝。`,
    async () => { await libraries.importJson(kind, raw) }, `${KEYWORD_LIBRARY_LABELS[kind]}已导入`)
}
async function onBrowserImport(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (file && importKind.value) await importLibraryRaw(importKind.value, await file.text())
  importKind.value = null
}
async function onNativeImport(event: Event) {
  const detail = (event as CustomEvent<{ requestId?: string; paths?: string[] }>).detail
  if (!detail?.requestId?.startsWith('keyword-library:') || !detail.paths?.[0]) return
  const kind = detail.requestId.slice('keyword-library:'.length) as KeywordLibraryKind
  const response = await authedFetch(`/api/fs/raw?path=${encodeURIComponent(detail.paths[0])}`)
  if (!response.ok) { error.value = `读取导入文件失败（HTTP ${response.status}）`; return }
  await importLibraryRaw(kind, await response.text())
}

// Presets and scripts
const sortMode = ref<Record<CollectionKind, boolean>>({ presets: false, scripts: false })
const currentCollectionKind = computed<CollectionKind>(() => activeTab.value === 'scripts' ? 'scripts' : 'presets')
const currentCollection = computed(() => currentCollectionKind.value === 'scripts' ? project.scripts : project.presets)
const itemEditor = ref<{ kind: CollectionKind; item: ManagedItem | null; title: string; content: string; condition: string; route: string; readOnly: boolean } | null>(null)
async function openItem(kind: CollectionKind, item: ManagedItem | null, readOnly = false) {
  itemEditor.value = {
    kind, item, title: item?.title ?? '', content: item ? await project.readItem(kind, item) : '',
    condition: item?.completionCondition ?? '', route: item?.defaultRoute ?? '', readOnly,
  }
}
function saveItem() {
  const editor = itemEditor.value
  if (!editor) return
  mutate(async () => {
    if (editor.item) await project.updateItem(editor.kind, editor.item, editor.title, editor.content, editor.condition, editor.route)
    else await project.addItem(editor.kind, editor.title, editor.content, editor.condition, editor.route)
    itemEditor.value = null
  }, editor.kind === 'presets' ? '风格预设已保存' : '剧本已保存')
}
function removeManaged(kind: CollectionKind, item: ManagedItem) {
  askConfirm('永久删除条目', `“${item.title}”及其项目文件将被永久删除，无法恢复。`,
    async () => project.removeItem(kind, item), '条目已永久删除')
}
const managedImport = ref<CollectionKind | null>(null)
const managedFileInput = ref<HTMLInputElement | null>(null)
function startManagedImport(kind: CollectionKind) { managedImport.value = kind; managedFileInput.value?.click() }
async function onManagedFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || !managedImport.value) return
  const kind = managedImport.value
  managedImport.value = null
  await openItem(kind, null)
  if (itemEditor.value) { itemEditor.value.title = file.name.replace(/\.[^.]+$/, ''); itemEditor.value.content = await file.text() }
}

// Memory
function saveFact(fact: MemoryFact) { mutate(() => project.saveMemory(), '记忆事实已保存') }
function requestRebuild(all: boolean) {
  mutate(async () => {
    project.memoryPending = true
    if (all) project.memoryFacts.forEach(fact => { if (!fact.locked) fact.stale = true })
    await project.saveMemory()
  }, all ? '已请求全部重建，当前显示“记忆待同步”' : '已请求局部重建')
}
function deleteFact(fact: MemoryFact) {
  askConfirm('删除记忆事实', '该事实将从结构化记忆中永久删除；锁定不会阻止手工删除。',
    () => project.removeMemoryFact(fact), '记忆事实已删除')
}

// Time
const timeDraft = ref('')
const correctionDraft = ref('')
async function saveTimeDisplay() {
  await project.patchTime({ display: timeDraft.value.trim() || project.currentTimeLabel })
  notice.value = '故事时间已更新'
}
async function correctTime(choice: 'record' | 'rollback' | 'snapshot') {
  const next = correctionDraft.value.trim()
  if (!next) return
  if (choice === 'snapshot') await project.createTimeRevision(next)
  else await project.patchTime({ display: next })
  correctionDraft.value = ''
  session.resetStoryContext()
  notice.value = choice === 'record' ? '仅修正了时间记录' : choice === 'rollback' ? '时间已修正，并标记受影响状态回滚' : '已创建时间修订快照'
}

onMounted(async () => {
  await Promise.all([project.initialize(), libraries.initialize(), session.refreshProjectUsage()])
  timeDraft.value = project.currentTimeLabel
  window.addEventListener('coomi:files-imported', onNativeImport)
})
onBeforeUnmount(() => window.removeEventListener('coomi:files-imported', onNativeImport))
</script>

<template>
  <div class="page">
    <PageHead title="剧情设置" @back="router.push('/')" />
    <nav class="tab-strip" aria-label="设置分类">
      <button v-for="tab in tabs" :key="tab.id" :class="{ active: activeTab === tab.id }" @click="selectTab(tab.id)">
        <CoomiIcon :name="tab.icon" :size="17" /><span>{{ tab.label }}</span>
      </button>
    </nav>
    <main class="body">
      <p v-if="notice" class="notice ok">{{ notice }}</p>
      <p v-if="error || project.error" class="notice err">{{ error || project.error }}</p>

      <template v-if="activeTab === 'basic'">
        <h2>工作模式</h2>
        <section class="group">
          <button v-for="item in STORY_MODES" :key="item.mode" class="row" @click="session.switchAgentMode(item.mode)">
            <span><b>{{ item.label }}</b><small>{{ item.desc }}</small></span><CoomiIcon v-if="story.agentMode === item.mode" name="check" class="selected" />
          </button>
        </section>
        <h2>剧情控制</h2>
        <section class="group compact">
          <div class="field"><span>叙事自由度</span><div class="segments"><button v-for="item in NARRATIVE_MODES" :key="item.mode" :class="{ on: story.narrativeMode === item.mode }" @click="story.setNarrativeMode(item.mode)">{{ item.label }}</button></div></div>
          <div class="field"><span>推理强度</span><div class="segments five"><button v-for="item in REASONING" :key="item.value" :class="{ on: story.reasoningEffort === item.value }" @click="session.setReasoningEffort(item.value)">{{ item.label }}</button></div></div>
          <div class="reasoning-metrics" aria-label="当前故事项目推理强度均轮统计">
            <div class="metric-head"><span>推理强度</span><b v-for="item in REASONING" :key="item.value">{{ item.label }}</b></div>
            <div><span>均轮消耗</span><b v-for="item in REASONING" :key="item.value">{{ formatAverageTokens(item.value) }}</b></div>
            <div class="metric-reference"><span>均轮耗时</span><b v-for="item in REASONING" :key="item.value">{{ formatAverageDuration(item.value) }}</b></div>
          </div>
          <label class="field"><span>最近完整正文</span><input type="number" min="1" max="20" :value="project.settings.recentFragments" @change="patchProjectSettings({ recentFragments: Math.min(20, Math.max(1, Number(($event.target as HTMLInputElement).value))) })" /></label>
          <div class="field"><span>记忆检查点</span><div class="segments five"><button v-for="n in [5,10,15,20,30]" :key="n" :class="{ on: project.settings.memoryCheckpoint === n }" @click="patchProjectSettings({ memoryCheckpoint: n as 5|10|15|20|30 })">{{ n }}</button></div></div>
          <label class="field"><span>片段字数</span><span class="dual"><input type="number" :value="story.fragmentMin" @change="story.setFragmentLength(Number(($event.target as HTMLInputElement).value), story.fragmentMax)" /><i>至</i><input type="number" :value="story.fragmentMax" @change="story.setFragmentLength(story.fragmentMin, Number(($event.target as HTMLInputElement).value))" /></span></label>
        </section>
        <p class="footnote">一个推理周期最多 10 个剧情片段；修改预设、剧本、记忆或词库会结束当前周期。</p>
      </template>

      <template v-if="activeTab === 'presets' || activeTab === 'scripts'">
        <div class="section-head">
          <div><h2>{{ activeTab === 'presets' ? '风格预设' : '剧本管理' }}</h2><p>{{ activeTab === 'presets' ? '显示越靠前优先级越高；注入时按相反顺序组织。' : '可并行激活；剧情按故事时间持续发展，不等待主角。' }}</p></div>
          <button class="icon-action" :class="{ on: sortMode[currentCollectionKind] }" title="排序" @click="sortMode[currentCollectionKind] = !sortMode[currentCollectionKind]"><CoomiIcon name="settings" :size="16" /></button>
          <button class="icon-action" title="导入" @click="startManagedImport(currentCollectionKind)"><CoomiIcon name="arrowUp" :size="16" /></button>
          <button class="icon-action primary" title="新增" @click="openItem(currentCollectionKind, null)"><CoomiIcon name="plus" :size="16" /></button>
        </div>
        <section class="item-list">
          <article v-for="(item, index) in currentCollection" :key="item.id" class="item-row">
            <button class="toggle" :class="{ on: item.enabled }" :aria-label="item.enabled ? '停用' : '激活'" @click="mutate(() => project.toggleItem(currentCollectionKind, item), item.enabled ? '已停用' : '已激活')"><i /></button>
            <div class="item-copy"><b>{{ item.title }}</b><small v-if="activeTab === 'scripts'">{{ item.status === 'completed' ? '已完成' : item.status === 'pending' ? '待处理' : '进行中' }} · {{ item.completionCondition || '未填写完成条件' }}</small><small v-else>{{ item.enabled ? '已激活' : '未激活' }}</small></div>
            <div v-if="sortMode[currentCollectionKind]" class="sort-actions"><button :disabled="index === 0" @click="project.moveItem(currentCollectionKind, item, -1)">↑</button><button :disabled="index === currentCollection.length - 1" @click="project.moveItem(currentCollectionKind, item, 1)">↓</button></div>
            <div v-else class="item-actions">
              <button title="查看" @click="openItem(currentCollectionKind, item, true)"><CoomiIcon name="eye" :size="15" /></button>
              <button title="编辑或重命名" @click="openItem(currentCollectionKind, item)"><CoomiIcon name="pencil" :size="15" /></button>
              <button title="导出" @click="run(() => project.exportItem(currentCollectionKind, item), '正在导出')"><CoomiIcon name="arrowDown" :size="15" /></button>
              <button title="删除" class="danger" @click="removeManaged(currentCollectionKind, item)"><CoomiIcon name="trash" :size="15" /></button>
            </div>
            <div v-if="activeTab === 'scripts'" class="script-status"><button @click="project.markScript(item, item.status === 'completed' ? 'active' : 'completed')">{{ item.status === 'completed' ? '撤销完成' : '标记完成' }}</button><button @click="project.markScript(item, 'pending')">待处理</button></div>
          </article>
          <p v-if="currentCollection.length === 0" class="empty">暂无条目</p>
        </section>
      </template>

      <template v-if="activeTab === 'random'">
        <h2>触发机制</h2>
        <section class="group compact">
          <button class="switch-row" @click="patchProjectSettings({ fortuneEnabled: !project.settings.fortuneEnabled })"><span><b>随机气运</b><small>每次玩家行动都进行判定</small></span><i :class="{ on: project.settings.fortuneEnabled }" /></button>
          <button class="switch-row" @click="patchProjectSettings({ eventEnabled: !project.settings.eventEnabled })"><span><b>随机事件</b><small>可与随机人物在同一轮触发并组成因果链</small></span><i :class="{ on: project.settings.eventEnabled }" /></button>
          <button class="switch-row" @click="patchProjectSettings({ characterEnabled: !project.settings.characterEnabled })"><span><b>随机人物</b><small>人物命名禁止模板化、AI 化和烂大街风雅名</small></span><i :class="{ on: project.settings.characterEnabled }" /></button>
          <div class="field"><span>人物性别</span><div class="segments"><button v-for="gender in GENDERS" :key="gender.value" :class="{ on: project.settings.characterGender === gender.value }" @click="patchProjectSettings({ characterGender: gender.value })">{{ gender.label }}</button></div></div>
        </section>
        <h2>项目词库</h2>
        <section class="item-list">
          <article v-for="kind in LIBRARY_KINDS" :key="kind" class="library-row">
            <div class="item-copy"><b>{{ KEYWORD_LIBRARY_LABELS[kind] }}</b><small>{{ libraries.stats(kind).source === 'custom' ? '当前项目自定义' : '内置通用' }} · {{ libraries.stats(kind).categories }} 类 · {{ libraries.stats(kind).keywords }} 词</small></div>
            <div class="text-actions"><button @click="openLibrary(kind)">查看编辑</button><button @click="startLibraryImport(kind)">导入</button><button @click="run(() => libraries.exportCurrent(kind), '正在导出')">导出</button><button :disabled="!libraries.custom[kind]" @click="askConfirm('恢复内置词库', '当前项目的自定义词库将被移除，改用内置通用版本。', () => libraries.restoreBuiltin(kind), '已恢复内置词库')">恢复内置</button></div>
          </article>
        </section>
        <input ref="fileInput" hidden type="file" accept=".json,application/json" @change="onBrowserImport" />
      </template>

      <template v-if="activeTab === 'memory'">
        <div class="section-head"><div><h2>结构化记忆</h2><p>锁定事实不会被模型自动修改；章节改动会使关联事实过期。</p></div><button class="text-button" @click="project.addMemoryFact('新记忆事实')">新增事实</button></div>
        <p v-if="project.memoryPending" class="sync-state"><CoomiIcon name="alert" :size="15" />记忆待同步</p>
        <section class="memory-list">
          <article v-for="fact in project.memoryFacts" :key="fact.id" :class="{ stale: fact.stale }">
            <textarea v-model="fact.text" rows="2" @change="saveFact(fact)" />
            <div><select v-model="fact.scope" @change="saveFact(fact)"><option value="objective">客观事实</option><option value="protagonist">主角已知</option></select><button :class="{ on: fact.locked }" @click="fact.locked = !fact.locked; saveFact(fact)">{{ fact.locked ? '已锁定' : '锁定' }}</button><span v-if="fact.stale">已过期</span><button class="danger" @click="deleteFact(fact)">删除</button></div>
          </article>
          <p v-if="project.memoryFacts.length === 0" class="empty">暂无结构化事实</p>
        </section>
        <div class="wide-actions"><button @click="requestRebuild(false)">局部重建</button><button @click="requestRebuild(true)">全部重建</button></div>
      </template>

      <template v-if="activeTab === 'time'">
        <div class="time-now"><span>当前故事时间</span><strong>{{ project.currentTimeLabel }}</strong><small>{{ project.time.locked ? '已锁定' : '随剧情推进' }}</small></div>
        <section class="group compact">
          <label class="field"><span>历法</span><select v-model="project.time.calendar" @change="project.patchTime({ calendar: project.time.calendar })"><option value="relative">相对历</option><option value="gregorian">公历</option><option value="custom">自定义历法</option></select></label>
          <label v-if="project.time.calendar === 'custom'" class="field"><span>历法名称</span><input v-model="project.time.calendarName" @change="project.patchTime({ calendarName: project.time.calendarName })" /></label>
          <label class="field"><span>显示时间</span><span class="inline-input"><input v-model="timeDraft" /><button @click="saveTimeDisplay">保存</button></span></label>
          <div class="field"><span>时间精度</span><div class="segments"><button :class="{ on: project.time.precision === 'fuzzy' }" @click="project.patchTime({ precision: 'fuzzy' })">模糊</button><button :class="{ on: project.time.precision === 'day' }" @click="project.patchTime({ precision: 'day' })">天</button><button :class="{ on: project.time.precision === 'hour' }" @click="project.patchTime({ precision: 'hour' })">小时</button></div></div>
          <button class="switch-row" @click="project.patchTime({ locked: !project.time.locked })"><span><b>锁定当前时间</b><small>禁止模型自动修改</small></span><i :class="{ on: project.time.locked }" /></button>
          <button class="switch-row" @click="project.patchTime({ flashback: project.time.flashback ? null : { active: true, at: project.currentTimeLabel, returnTo: project.currentTimeLabel } })"><span><b>闪回状态</b><small>第一版支持完整闪回，不创建并行时间线</small></span><i :class="{ on: !!project.time.flashback }" /></button>
        </section>
        <h2>时间纠错</h2>
        <input v-model="correctionDraft" class="correction" placeholder="输入修正后的故事时间" />
        <div class="correction-actions"><button @click="correctTime('record')">仅修正时间记录</button><button @click="correctTime('rollback')">回滚受影响状态</button><button @click="correctTime('snapshot')">创建时间修订快照</button></div>
      </template>

      <template v-if="activeTab === 'theme'">
        <h2>主题外观</h2>
        <section class="group">
          <button v-for="item in THEME_MODES" :key="item.mode" class="row" @click="config.setThemeMode(item.mode)"><span><b>{{ item.label }}</b><small>{{ item.desc }}</small></span><CoomiIcon v-if="config.themeMode === item.mode" name="check" class="selected" /></button>
        </section>
      </template>
    </main>

    <div v-if="libraryEditor" class="mask" @click.self="libraryEditor = null"><section class="sheet tall">
      <div class="sheet-head"><div><b>{{ KEYWORD_LIBRARY_LABELS[libraryEditor.kind] }}</b><small>分类与词条可自由增删</small></div><button @click="libraryEditor = null"><CoomiIcon name="close" /></button></div>
      <input v-model="keywordSearch" class="search" placeholder="搜索分类或词条" />
      <div class="category-list"><article v-for="[category, words] in visibleLibrary" :key="category"><div class="category-head"><input :value="category" readonly /><button @click="delete libraryEditor!.draft[category]">删除分类</button></div><div v-for="(_, index) in words" :key="index" class="word"><input v-model="words[index]" /><button title="删除词条" @click="removeKeyword(category, index)"><CoomiIcon name="close" :size="14" /></button></div><button class="add-word" @click="addKeyword(category)">+ 添加词条</button></article></div>
      <div class="new-category"><input v-model="newCategory" placeholder="新分类名称" /><button @click="addCategory">添加分类</button></div>
      <div class="sheet-actions"><button @click="libraryEditor = null">取消</button><button class="primary" @click="saveLibrary">保存为项目词库</button></div>
    </section></div>

    <div v-if="itemEditor" class="mask" @click.self="itemEditor = null"><section class="sheet tall">
      <div class="sheet-head"><div><b>{{ itemEditor.readOnly ? '查看' : itemEditor.item ? '编辑与重命名' : '新增条目' }}</b><small>{{ itemEditor.kind === 'presets' ? '风格预设' : '剧情剧本' }}</small></div><button @click="itemEditor = null"><CoomiIcon name="close" /></button></div>
      <label>名称<input v-model="itemEditor.title" :readonly="itemEditor.readOnly" /></label>
      <label v-if="itemEditor.kind === 'scripts'">完成条件<input v-model="itemEditor.condition" :readonly="itemEditor.readOnly" placeholder="由模型据此判断完成" /></label>
      <label v-if="itemEditor.kind === 'scripts'">默认路线<input v-model="itemEditor.route" :readonly="itemEditor.readOnly" placeholder="未填写时遇到分叉将标记待处理" /></label>
      <label class="grow">内容<textarea v-model="itemEditor.content" :readonly="itemEditor.readOnly" /></label>
      <div class="sheet-actions"><button @click="itemEditor = null">关闭</button><button v-if="!itemEditor.readOnly" class="primary" @click="saveItem">保存</button></div>
    </section></div>

    <div v-if="confirmBox" class="mask" @click.self="confirmBox = null"><section class="sheet compact"><div class="warning-icon"><CoomiIcon name="alert" /></div><b>{{ confirmBox.title }}</b><p>{{ confirmBox.message }}</p><div class="sheet-actions"><button @click="confirmBox = null">取消</button><button class="danger-fill" @click="acceptConfirm">确认</button></div></section></div>
    <div v-if="mutation" class="mask"><section class="sheet compact"><b>当前推理周期正在执行</b><p>修改项目约束后必须重建上下文。请选择应用时机。</p><div class="stack-actions"><button class="danger-fill" @click="resolveMutation('stop')">停止并应用</button><button @click="resolveMutation('after')">本轮结束后应用</button><button @click="resolveMutation('cancel')">取消修改</button></div></section></div>
    <input ref="managedFileInput" hidden type="file" accept=".md,.txt,text/plain,text/markdown" @change="onManagedFile" />
  </div>
</template>

<style scoped>
.page { display:flex; flex-direction:column; height:100%; background:var(--page); }
.tab-strip { display:flex; gap:8px; overflow-x:auto; padding:8px 12px 10px; scrollbar-width:none; background:var(--bg); border-bottom:1px solid var(--border); }
.tab-strip::-webkit-scrollbar { display:none; }
.tab-strip button { display:flex; align-items:center; gap:6px; flex:0 0 auto; min-height:42px; padding:0 13px; border:1px solid var(--border); border-radius:7px; background:var(--bg-card); color:var(--text-3); font-size:12.5px; }
.tab-strip button.active { border-color:var(--blue); background:var(--blue-soft); color:var(--blue); }
.body { flex:1; min-height:0; overflow-y:auto; padding:14px 12px calc(28px + var(--safe-bottom)); }
h2 { margin:18px 2px 8px; font-size:13px; font-weight:650; color:var(--text-2); } h2:first-child { margin-top:2px; }
.group { overflow:hidden; border:1px solid var(--border); border-radius:7px; background:var(--bg); }
.row,.switch-row { display:flex; align-items:center; width:100%; min-height:58px; padding:10px 13px; text-align:left; color:var(--text); }
.row + .row,.switch-row + .switch-row,.compact > * + * { border-top:1px solid var(--border); }
.row > span,.switch-row > span { display:flex; flex:1; min-width:0; flex-direction:column; gap:3px; }
b { font-size:13.5px; font-weight:650; } small { color:var(--text-3); font-size:11.5px; line-height:1.45; }
.selected { color:var(--blue); }
.field { display:flex; align-items:center; justify-content:space-between; gap:12px; min-height:54px; padding:9px 13px; color:var(--text-2); font-size:13px; }
.field > span:first-child { flex:1; }
input,select,textarea { border:1px solid var(--border-strong); border-radius:6px; background:var(--bg-input); color:var(--text); font:inherit; }
.field > input,.field > select { width:min(150px,45vw); min-height:36px; padding:0 9px; }
.segments { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); width:min(218px,61vw); padding:3px; border-radius:6px; background:var(--fill-strong); }
.segments.five { grid-template-columns:repeat(5,minmax(0,1fr)); width:min(260px,68vw); }
.segments button { min-height:31px; padding:0 5px; border-radius:4px; color:var(--text-3); font-size:11.5px; }
.segments button.on { background:var(--bg); color:var(--blue); box-shadow:var(--shadow-1); }
.reasoning-metrics { padding:9px 8px 10px; overflow:hidden; }
.reasoning-metrics > div { display:grid; grid-template-columns:58px repeat(5,minmax(0,1fr)); align-items:center; min-height:22px; column-gap:2px; text-align:center; }
.reasoning-metrics span { color:var(--text-3); font-size:10px; text-align:left; white-space:nowrap; }
.reasoning-metrics b { min-width:0; overflow:hidden; color:var(--text-2); font-size:10.5px; font-weight:600; letter-spacing:0; text-overflow:ellipsis; white-space:nowrap; }
.reasoning-metrics .metric-head { padding-bottom:4px; border-bottom:1px solid var(--border); }
.reasoning-metrics .metric-head b { color:var(--text-3); font-size:10px; font-weight:500; }
.reasoning-metrics .metric-reference b,.reasoning-metrics .metric-reference span { color:var(--text-3); font-size:9.5px; font-weight:500; }
.dual { display:flex; align-items:center; gap:5px; }.dual input { width:68px; min-height:34px; padding:0 7px; }.dual i { color:var(--text-3); font-style:normal; }
.footnote { margin:9px 3px; color:var(--text-3); font-size:11.5px; line-height:1.6; }
.notice { margin:0 0 10px; padding:9px 11px; border-radius:6px; font-size:12px; }.notice.ok { background:var(--ok-soft); color:var(--ok); }.notice.err { background:var(--danger-soft); color:var(--danger); }
.section-head { display:flex; align-items:center; gap:7px; margin-bottom:10px; }.section-head > div { flex:1; min-width:0; }.section-head h2 { margin:0; }.section-head p { margin:3px 0 0; color:var(--text-3); font-size:11.5px; line-height:1.45; }
.icon-action { display:grid; place-items:center; width:36px; height:36px; border-radius:6px; background:var(--fill-strong); color:var(--text-2); }.icon-action.primary,.icon-action.on { background:var(--blue-soft); color:var(--blue); }
.item-list,.memory-list { display:flex; flex-direction:column; gap:7px; }.item-row,.library-row,.memory-list article { display:flex; align-items:center; flex-wrap:wrap; gap:9px; padding:11px; border:1px solid var(--border); border-radius:7px; background:var(--bg); }
.toggle { width:34px; height:21px; padding:2px; border-radius:11px; background:var(--fill-strong); }.toggle i { display:block; width:17px; height:17px; border-radius:50%; background:var(--bg); box-shadow:var(--shadow-1); transition:transform .15s; }.toggle.on { background:var(--blue); }.toggle.on i { transform:translateX(13px); }
.item-copy { display:flex; flex:1; min-width:140px; flex-direction:column; gap:3px; }.item-actions,.sort-actions { display:flex; gap:3px; }.item-actions button,.sort-actions button { display:grid; place-items:center; width:31px; height:31px; border-radius:5px; background:var(--fill); color:var(--text-2); }.danger { color:var(--danger)!important; }
.script-status { display:flex; width:100%; gap:7px; padding-top:7px; border-top:1px solid var(--border); }.script-status button,.text-actions button,.wide-actions button,.text-button { min-height:32px; padding:0 10px; border-radius:5px; background:var(--fill-strong); color:var(--text-2); font-size:11.5px; }
.empty { width:100%; padding:28px 0; color:var(--text-3); text-align:center; font-size:12.5px; }
.switch-row > i { position:relative; width:38px; height:22px; border-radius:11px; background:var(--fill-strong); }.switch-row > i::after { position:absolute; top:3px; left:3px; width:16px; height:16px; border-radius:50%; background:var(--bg); box-shadow:var(--shadow-1); content:''; }.switch-row > i.on { background:var(--blue); }.switch-row > i.on::after { transform:translateX(16px); }
.text-actions { display:flex; flex-wrap:wrap; width:100%; gap:6px; }.text-actions button:disabled { opacity:.4; }
.sync-state { display:flex; align-items:center; gap:6px; padding:8px 10px; border-radius:6px; background:var(--orange-soft); color:var(--orange); font-size:12px; }
.memory-list article { align-items:stretch; }.memory-list article.stale { border-color:var(--orange); }.memory-list textarea { width:100%; padding:8px; resize:vertical; }.memory-list article > div { display:flex; align-items:center; gap:7px; }.memory-list article button { padding:5px 9px; border-radius:5px; background:var(--fill); font-size:11.5px; }.memory-list article button.on { color:var(--blue); background:var(--blue-soft); }.memory-list article span { flex:1; color:var(--orange); font-size:11px; }
.wide-actions,.correction-actions { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin-top:10px; }.wide-actions button { min-height:40px; }
.time-now { display:flex; flex-direction:column; gap:4px; padding:4px 2px 14px; }.time-now span { color:var(--text-3); font-size:12px; }.time-now strong { font-size:25px; letter-spacing:0; }.time-now small { color:var(--blue); }
.inline-input { display:flex; gap:6px; }.inline-input input { width:130px; padding:0 8px; }.inline-input button { padding:0 10px; border-radius:5px; background:var(--blue-soft); color:var(--blue); }
.correction { width:100%; min-height:42px; padding:0 10px; }.correction-actions { grid-template-columns:1fr; }.correction-actions button { min-height:39px; border-radius:6px; background:var(--fill-strong); color:var(--text-2); }
.mask { position:fixed; z-index:80; inset:0; display:flex; align-items:flex-end; background:rgba(0,0,0,.44); }.sheet { display:flex; width:100%; max-height:91vh; flex-direction:column; gap:11px; padding:16px 14px calc(14px + var(--safe-bottom)); border-radius:14px 14px 0 0; background:var(--bg-card); }.sheet.tall { height:min(88vh,760px); }.sheet.compact { max-height:none; }.sheet-head { display:flex; align-items:center; }.sheet-head > div { display:flex; flex:1; flex-direction:column; gap:2px; }.sheet-head > button { display:grid; place-items:center; width:36px; height:36px; }.sheet > label { display:flex; flex-direction:column; gap:5px; color:var(--text-3); font-size:11.5px; }.sheet > label input { min-height:40px; padding:0 9px; }.sheet label.grow { min-height:0; flex:1; }.sheet textarea { min-height:160px; flex:1; padding:9px; resize:none; line-height:1.6; }.sheet p { margin:0; color:var(--text-3); font-size:13px; line-height:1.65; }
.sheet-actions { display:flex; gap:8px; }.sheet-actions button { min-height:41px; flex:1; border-radius:6px; background:var(--fill-strong); color:var(--text-2); }.sheet-actions .primary { background:var(--blue); color:#fff; }.danger-fill { background:var(--danger)!important; color:#fff!important; }
.search { min-height:40px; padding:0 10px; }.category-list { min-height:0; flex:1; overflow-y:auto; }.category-list article { padding:9px 0; border-bottom:1px solid var(--border); }.category-head,.word,.new-category { display:flex; gap:6px; margin-bottom:6px; }.category-head input,.word input,.new-category input { min-width:0; flex:1; min-height:36px; padding:0 8px; }.category-head button,.word button,.new-category button,.add-word { padding:0 9px; border-radius:5px; background:var(--fill); color:var(--text-3); font-size:11px; }.add-word { min-height:31px; }.warning-icon { display:grid; place-items:center; width:42px; height:42px; border-radius:50%; background:var(--danger-soft); color:var(--danger); }.stack-actions { display:grid; gap:8px; }.stack-actions button { min-height:42px; border-radius:6px; background:var(--fill-strong); color:var(--text-2); }
</style>
