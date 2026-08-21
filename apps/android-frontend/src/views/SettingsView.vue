<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import PageHead from '@/components/PageHead.vue'
import CoomiIcon from '@/components/CoomiIcon.vue'
import BottomSheet from '@/components/ui/BottomSheet.vue'
import Switch from '@/components/ui/Switch.vue'
import SwitchRow from '@/components/ui/SwitchRow.vue'
import { THEME_MODES, useConfigStore } from '@/stores/config'
import { useSessionStore } from '@/stores/session'
import { useStoryStore, type AgentMode, type NarrativeMode, type ReasoningEffort } from '@/stores/story'
import {
  materialCanBulkRefactor,
  normalizeScriptType,
  scriptMinorCountForRefactor,
  useProjectStore,
  type ManagedItem,
  type MaterialRefactorProgress,
  type MaterialRefactorQuantityMode,
  type MaterialRefactorSource,
  type MemoryFact,
  type ScriptRefactorPreview,
} from '@/stores/project'
import {
  KEYWORD_LIBRARY_KINDS, KEYWORD_LIBRARY_LABELS, type KeywordLibrary, type KeywordLibraryKind, useKeywordLibraryStore,
} from '@/stores/keywordLibraries'
import type { CharacterGenderMode, EncounterFrequency } from '@/story/randomMechanics'
import type { StoryPace } from '@/story/directorMechanics'
import {
  estimatePlotSize, MAJOR_PHASE_LABELS, MAJOR_PHASES, MINOR_STORY_TYPES, MINOR_TYPE_LABELS,
  normalizePlotMechanics, plotSettingsForScale, validatePlotMechanics,
  type CountRange, type MajorStoryPhase, type MajorStoryScale, type MinorStoryType,
} from '@/story/plotMechanics'
import { authedFetch } from '@/bridge/http'

type Tab = 'basic' | 'director' | 'presets' | 'random' | 'scripts' | 'memory' | 'time' | 'theme'
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
  { id: 'director', label: '剧情推进', icon: 'target' },
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
const ENCOUNTER_FREQUENCIES: Array<{ value: EncounterFrequency; label: string; desc: string }> = [
  { value: 'restrained', label: '克制', desc: '约 15% 触发' },
  { value: 'balanced', label: '均衡', desc: '约 21% 触发' },
  { value: 'active', label: '活跃', desc: '约 37% 触发' },
]
const STORY_PACES: Array<{ value: StoryPace; label: string; desc: string }> = [
  { value: 'deliberate', label: '从容', desc: '允许更多铺垫，但停滞仍会被纠偏' },
  { value: 'balanced', label: '均衡', desc: '兼顾铺垫、变化和完整收束' },
  { value: 'urgent', label: '紧凑', desc: '更快进入里程碑、高潮和结局' },
]
const MAJOR_SCALES: Array<{ value: MajorStoryScale; label: string; desc: string }> = [
  { value: 'fast', label: '快速', desc: '5–10 个小剧情' },
  { value: 'balanced', label: '均衡', desc: '15–20 个小剧情' },
  { value: 'detailed', label: '详细', desc: '25–30 个小剧情' },
  { value: 'custom', label: '自定义', desc: '完整控制数量' },
]
const plotDraft = ref(normalizePlotMechanics(project.settings.plotMechanics))
const plotErrors = computed(() => validatePlotMechanics(plotDraft.value, project.settings.majorHookEnabled))
const plotEstimate = computed(() => estimatePlotSize(plotDraft.value))
watch(() => project.settings.plotMechanics, value => { plotDraft.value = normalizePlotMechanics(value) }, { deep: true })
function selectMajorScale(scale: MajorStoryScale) {
  plotDraft.value = scale === 'custom'
    ? { ...normalizePlotMechanics(plotDraft.value), scale: 'custom' }
    : plotSettingsForScale(scale)
}
function updateRange(target: CountRange, field: keyof CountRange, event: Event, minimum = 0) {
  const value = Math.max(minimum, Math.round(Number((event.target as HTMLInputElement).value) || minimum))
  target[field] = value
  if (target.max < target.min) target.max = target.min
  plotDraft.value.scale = 'custom'
}
function savePlotMechanics() {
  if (plotErrors.value.length) { error.value = plotErrors.value[0]; return }
  mutate(() => patchProjectSettings({ plotMechanics: normalizePlotMechanics(plotDraft.value) }), '剧情规模与预算已保存')
}
function applyPlotToCurrent() {
  askConfirm('应用到当前大剧情', '已完成的小剧情不会重算；如果新预算已经到达上限，当前阶段会立即进入收束。',
    () => project.applyPlotSettingsToCurrent().then(applied => { if (!applied) throw new Error('当前没有正在执行的大剧情') }),
    '新预算已应用到当前大剧情')
}
const LIBRARY_KINDS: KeywordLibraryKind[] = KEYWORD_LIBRARY_KINDS
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
  if (patch.encounterEnabled != null) story.setEncounterEnabled(patch.encounterEnabled)
  if (patch.encounterFrequency) story.setEncounterFrequency(patch.encounterFrequency)
  if (patch.eventEnabled != null) story.setEventEnabled(patch.eventEnabled)
  if (patch.characterEnabled != null) story.setCharacterEnabled(patch.characterEnabled)
  if (patch.characterGender) story.setCharacterGender(patch.characterGender)
  if (patch.tragedyEnabled != null) story.setTragedyEnabled(patch.tragedyEnabled)
  if (patch.payoffEnabled != null) story.setPayoffEnabled(patch.payoffEnabled)
}

function clearCurrentContext() {
  error.value = ''
  if (!session.clearContextWindow()) {
    error.value = '执行中无法清空上下文，请先停止本轮'
    return
  }
  notice.value = '当前项目、当前模式的上下文窗口已清空'
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
type ScriptLevel = NonNullable<ManagedItem['scriptType']>
const SCRIPT_LEVEL_LABELS: Record<ScriptLevel, string> = { stage: '阶段', major: '大剧情', minor: '小剧情' }
const SCRIPT_LEVEL_OPTIONS: { value: ScriptLevel; label: string; desc: string }[] = [
  { value: 'stage', label: '阶段', desc: '最上层框架，只写全局方向与边界' },
  { value: 'major', label: '大剧情', desc: '主线框架，承载推进状态机' },
  { value: 'minor', label: '小剧情', desc: '具体剧情内容，唯一可归档单元' },
]
const itemEditor = ref<{
  kind: CollectionKind
  item: ManagedItem | null
  title: string
  content: string
  condition: string
  route: string
  readOnly: boolean
  scriptType: ScriptLevel
  parentId: string
} | null>(null)
const scriptRefactor = ref<ScriptRefactorPreview | null>(null)
const scriptRefactorSource = ref<MaterialRefactorSource | null>(null)
const materialRefactor = ref<{ source: MaterialRefactorSource; prompt: string } | null>(null)
const refactorLoading = ref(false)
const bulkRefactorSetup = ref<{
  kind: CollectionKind
  sources: MaterialRefactorSource[]
  quantityMode: MaterialRefactorQuantityMode
} | null>(null)
type BulkRefactorEvent = MaterialRefactorProgress & {
  id: string
  sourcePath: string
  sourceTitle: string
}
const bulkRefactor = ref<{
  kind: CollectionKind
  total: number
  completed: number
  succeeded: number
  failed: number
  current: string
  running: boolean
  quantityMode: MaterialRefactorQuantityMode
  events: BulkRefactorEvent[]
} | null>(null)
const bulkWaterfall = ref<HTMLElement | null>(null)
watch(() => bulkRefactor.value?.events.length ?? 0, async () => {
  await nextTick()
  const target = bulkWaterfall.value
  if (target) target.scrollTop = target.scrollHeight
})
async function openItem(kind: CollectionKind, item: ManagedItem | null, readOnly = false,
  seed?: { scriptType: ScriptLevel; parentId: string }) {
  itemEditor.value = {
    kind, item, title: item?.title ?? '', content: item ? await project.readItem(kind, item) : '',
    condition: item?.completionCondition ?? '', route: item?.defaultRoute ?? '', readOnly,
    scriptType: seed?.scriptType ?? normalizeScriptType(item?.scriptType),
    parentId: seed?.parentId ?? item?.parentId ?? '',
  }
}
/** 可选父级：大剧情只能选阶段，小剧情只能选大剧情（与 store 的校验规则一致）。 */
const scriptParentOptions = computed(() => {
  const editor = itemEditor.value
  if (!editor || editor.kind !== 'scripts' || editor.scriptType === 'stage') return []
  const wanted: ScriptLevel = editor.scriptType === 'major' ? 'stage' : 'major'
  return project.scripts.filter(item => !item.refactoredTo
    && item.id !== editor.item?.id && normalizeScriptType(item.scriptType) === wanted)
})
/** 背景时钟按「非阶段条目中的位次」判断：阶段不参与推选，不应把它下面的主剧本挤成背景。 */
function scriptIsBackgroundClock(item: ManagedItem) {
  if (item.status !== 'active' || normalizeScriptType(item.scriptType) === 'stage') return false
  return project.scripts.filter(candidate => !candidate.refactoredTo
    && normalizeScriptType(candidate.scriptType) !== 'stage').findIndex(candidate => candidate.id === item.id) > 0
}
/** 主剧本：非阶段条目里位次第一且进行中的那个。与 scriptIsBackgroundClock 共用同一套位次口径。 */
function scriptIsPrimary(item: ManagedItem) {
  if (item.status !== 'active' || normalizeScriptType(item.scriptType) === 'stage') return false
  return !scriptIsBackgroundClock(item)
}

/* ── 三级剧本树 ─────────────────────────────────────────────────────────── */

type ScriptRow = {
  item: ManagedItem
  level: ScriptLevel
  depth: number
  /** 子节点数量：阶段数大剧情，大剧情数小剧情，小剧情恒为 0。 */
  childCount: number
  childDone: number
  expandable: boolean
  expanded: boolean
  /** 父级为空或指向已失效条目。单独成组显示，否则会从树里整个消失。 */
  orphan: boolean
  canMoveUp: boolean
  canMoveDown: boolean
}

/**
 * 展开状态：只记「与默认不同」的条目。
 *
 * 阶段默认展开（否则一进界面只看到几个标题，看不到任何剧情），大剧情默认收起
 * （小剧情数量多，全展开会把列表撑得很长）。只存偏移量的好处是项目重新加载、条目增删
 * 之后都不用重新播种，也不会把已删条目的 id 一直留在集合里。
 */
const scriptExpandOverride = ref<Record<string, boolean>>({})
function scriptExpanded(item: ManagedItem) {
  const override = scriptExpandOverride.value[item.id]
  if (override !== undefined) return override
  return normalizeScriptType(item.scriptType) === 'stage'
}
function toggleScriptExpanded(item: ManagedItem) {
  scriptExpandOverride.value = { ...scriptExpandOverride.value, [item.id]: !scriptExpanded(item) }
}

/**
 * 把扁平的 scripts 数组摊成带深度的渲染行；收起的子树直接不产出行。
 *
 * 保持扁平数组的相对顺序不动——数组顺序就是优先级顺序（首个进行中的非阶段条目是唯一主剧本），
 * 树只表达归属，不重排条目。
 *
 * 用「带 depth 的一维列表」而不是嵌套 DOM：手机上宽度紧张，嵌套容器每层都要吃掉一截
 * padding，三层下来卡片就没地方放字了；depth 交给 CSS 变量画缩进和引导线，只让出一点。
 */
const scriptTree = computed(() => {
  const all = project.scripts
  const live = all.filter(item => !item.refactoredTo)
  const at = (level: ScriptLevel) => live.filter(item => normalizeScriptType(item.scriptType) === level)
  const stages = at('stage')
  const majors = at('major')
  const minors = at('minor')
  const stageIds = new Set(stages.map(item => item.id))
  const majorIds = new Set(majors.map(item => item.id))
  const rows: ScriptRow[] = []

  const siblingFlags = (item: ManagedItem, siblings: ManagedItem[]) => {
    const index = siblings.findIndex(candidate => candidate.id === item.id)
    return { canMoveUp: index > 0, canMoveDown: index >= 0 && index < siblings.length - 1 }
  }
  const pushMajor = (major: ManagedItem, depth: number, siblings: ManagedItem[], orphan: boolean) => {
    const children = minors.filter(item => item.parentId === major.id)
    const expanded = scriptExpanded(major)
    rows.push({
      item: major, level: 'major', depth,
      childCount: children.length,
      childDone: children.filter(item => item.status === 'completed').length,
      expandable: children.length > 0, expanded, orphan, ...siblingFlags(major, siblings),
    })
    if (!expanded) return
    for (const minor of children) {
      rows.push({
        item: minor, level: 'minor', depth: depth + 1, childCount: 0, childDone: 0,
        expandable: false, expanded: false, orphan: false, ...siblingFlags(minor, children),
      })
    }
  }

  for (const stage of stages) {
    const children = majors.filter(item => item.parentId === stage.id)
    rows.push({
      item: stage, level: 'stage', depth: 0,
      childCount: children.length,
      childDone: children.filter(item => item.status === 'completed').length,
      expandable: children.length > 0, expanded: scriptExpanded(stage), orphan: false,
      ...siblingFlags(stage, stages),
    })
    if (scriptExpanded(stage)) for (const major of children) pushMajor(major, 1, children, false)
  }

  // 未分组与归属失效的条目排在最后单独成组。悬空 parentId 也归到这里：若只按 parentId
  // 匹配、匹配不上就跳过，这些条目会从界面上彻底消失，只能翻文件才发现。
  const orphanMajors = majors.filter(item => !item.parentId || !stageIds.has(item.parentId))
  for (const major of orphanMajors) pushMajor(major, 0, orphanMajors, true)
  const orphanMinors = minors.filter(item => !item.parentId || !majorIds.has(item.parentId))
  for (const minor of orphanMinors) {
    rows.push({
      item: minor, level: 'minor', depth: 0, childCount: 0, childDone: 0,
      expandable: false, expanded: false, orphan: true, ...siblingFlags(minor, orphanMinors),
    })
  }
  return {
    rows,
    groupableMajors: orphanMajors.filter(item => !item.parentId).length,
    backups: all.filter(item => item.refactoredTo),
  }
})

const SCRIPT_STATUS_LABELS: Record<NonNullable<ManagedItem['status']>, string> = {
  active: '进行中', pending: '待处理', completed: '已完成',
}
/** 卡片副标题：每层只放该层真正有意义的信息，不再把所有字段拼成一条长串。 */
function scriptCardMeta(row: ScriptRow) {
  const item = row.item
  if (row.level === 'stage') {
    return row.childCount ? `${row.childCount} 个大剧情 · ${row.childDone} 已完成` : '尚无大剧情'
  }
  const parts: string[] = []
  if (row.level === 'major') {
    if (item.majorPhase) parts.push(MAJOR_PHASE_LABELS[item.majorPhase])
    parts.push(row.childCount ? `${row.childCount} 个小剧情 · ${row.childDone} 已完成` : '尚无小剧情')
    if (scriptIsBackgroundClock(item)) parts.push(`背景时钟 ${item.clock ?? 0}/${item.clockMax ?? 4}`)
  } else {
    if (item.minorType) parts.push(MINOR_TYPE_LABELS[item.minorType])
    if (item.fragmentBudget) parts.push(`片段 ${item.fragmentBudget.min}-${item.fragmentBudget.max}`)
  }
  if (item.formatVersion !== 2) parts.push('旧格式')
  return parts.join(' · ')
}
/** 阶段用「已完成大剧情 / 总数」，大剧情用「已完成小剧情 / 总数」；没有子节点就不画进度条。 */
function scriptProgress(row: ScriptRow) {
  if (!row.childCount) return 0
  return Math.round((row.childDone / row.childCount) * 100)
}
function groupUngroupedMajors() {
  mutate(async () => { await project.groupUngroupedMajorsIntoStage('第一阶段') },
    '未分组的大剧情已收入新建阶段')
}
/**
 * 从卡片直接新增下一级条目：层级和归属都已由所在卡片决定，省掉「新增 → 选层级 → 在下拉里
 * 找父级」这三步。顺手把父级展开，否则新条目落在收起的子树里，看着像没保存成功。
 */
function addScriptChild(row: ScriptRow) {
  scriptExpandOverride.value = { ...scriptExpandOverride.value, [row.item.id]: true }
  void openItem('scripts', null, false, {
    scriptType: row.level === 'stage' ? 'major' : 'minor', parentId: row.item.id,
  })
}
/** 原始备份默认收起：它们不参与推进，展开只在需要找回旧内容时。 */
const showScriptBackups = ref(false)
function saveItem() {
  const editor = itemEditor.value
  if (!editor) return
  mutate(async () => {
    if (editor.item) {
      await project.updateItem(editor.kind, editor.item, editor.title, editor.content, editor.condition, editor.route)
      // 层级只在新增时确定；已有条目只允许改归属，且统一走带校验的 setScriptParent。
      if (editor.kind === 'scripts' && editor.scriptType !== 'stage') await project.setScriptParent(editor.item, editor.parentId)
    } else {
      const created = await project.addItem(editor.kind, editor.title, editor.content, editor.condition, editor.route,
        { scriptType: editor.scriptType })
      if (editor.kind === 'scripts' && editor.parentId) await project.setScriptParent(created, editor.parentId)
    }
    itemEditor.value = null
  }, editor.kind === 'presets' ? '风格预设已保存' : '剧本已保存')
}
function removeManaged(kind: CollectionKind, item: ManagedItem) {
  askConfirm('永久删除条目', `“${item.title}”及其项目文件将被永久删除，无法恢复。`,
    async () => project.removeItem(kind, item), '条目已永久删除')
}
const managedImport = ref<CollectionKind | null>(null)
const managedFileInput = ref<HTMLInputElement | null>(null)
function startManagedImport(kind: CollectionKind) {
  managedImport.value = kind
  if (window.CoomiAndroid?.importFilesForRequest) window.CoomiAndroid.importFilesForRequest(`managed-material:${kind}`)
  else managedFileInput.value?.click()
}
async function stageManagedText(kind: CollectionKind, filename: string, content: string) {
  const source = await project.stageImportedMaterial(kind, filename, content)
  openMaterialRefactor(source)
}
async function onManagedFile(event: Event) {
  const input = event.target as HTMLInputElement
  const files = [...(input.files ?? [])]
  input.value = ''
  if (!files.length || !managedImport.value) return
  const kind = managedImport.value
  managedImport.value = null
  for (const file of files) {
    if (/\.(?:docx|pdf)$/i.test(file.name)) {
      error.value = `浏览器调试环境无法直接解析“${file.name}”，请在 Android 客户端中导入 DOCX/PDF`
      continue
    }
    try {
      await stageManagedText(kind, file.name, await file.text())
    } catch (cause) {
      error.value = `读取“${file.name}”失败：${cause instanceof Error ? cause.message : String(cause)}`
    }
  }
}
async function onManagedNativeImport(event: Event) {
  const detail = (event as CustomEvent<{ requestId?: string; paths?: string[] }>).detail
  if (!detail?.requestId?.startsWith('managed-material:') || !detail.paths?.length) return
  const kind = detail.requestId.slice('managed-material:'.length) as CollectionKind
  for (const path of detail.paths) {
    try {
      const response = await authedFetch('/api/storydex/read-import-material', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }),
      })
      if (!response.ok) {
        let detail = `HTTP ${response.status}`
        try { detail = (await response.json())?.error ?? detail } catch { /* plain response */ }
        throw new Error(detail)
      }
      const imported = await response.json() as { filename?: string; content?: string }
      const filename = imported.filename || path.replace(/\\/g, '/').split('/').pop() || (kind === 'scripts' ? '导入剧本' : '导入预设')
      await stageManagedText(kind, filename, imported.content ?? '')
    } catch (cause) {
      const filename = path.replace(/\\/g, '/').split('/').pop() || path
      error.value = `读取“${filename}”失败：${cause instanceof Error ? cause.message : String(cause)}`
    }
  }
}

// Memory
const editingFactId = ref('')
function memoryPreview(text: string) {
  const compact = text.replace(/\s+/g, ' ').trim()
  const sentence = compact.match(/^.*?[。！？!?…](?:[”’」』】》])?/)?.[0] ?? compact
  return sentence || '空记忆事实'
}
function openMaterialRefactor(source: MaterialRefactorSource) {
  materialRefactor.value = { source, prompt: refactorPromptFor(source) }
}
function refactorPromptFor(source: MaterialRefactorSource) {
  const key = source.kind === 'scripts'
    ? source.mode === 'import' ? 'scriptImport' : 'scriptExisting'
    : source.mode === 'import' ? 'presetImport' : 'presetExisting'
  return project.refactorPrompts[key]
}
function bulkRefactorSources(kind: CollectionKind): MaterialRefactorSource[] {
  const formal = (kind === 'scripts' ? project.scripts : project.presets)
    .filter(item => materialCanBulkRefactor(kind, item))
    .map(item => project.existingMaterialSource(kind, item))
  const staged = project.stagedMaterials.filter(item => item.kind === kind)
  return [...formal, ...staged]
}
const bulkRefactorCount = computed(() => bulkRefactorSources(currentCollectionKind.value).length)
function requestBulkRefactor(kind: CollectionKind) {
  const sources = bulkRefactorSources(kind)
  if (!sources.length) {
    notice.value = kind === 'scripts' ? '当前没有可重构的大剧本、旧格式剧本或待导入剧本' : '当前没有可重构的风格预设或待导入预设'
    return
  }
  bulkRefactor.value = null
  bulkRefactorSetup.value = { kind, sources, quantityMode: 'preserve' }
}
function startBulkRefactor() {
  const setup = bulkRefactorSetup.value
  if (!setup) return
  bulkRefactorSetup.value = null
  mutate(() => runBulkRefactor(setup.kind, setup.sources, setup.quantityMode), '')
}
function recordBulkProgress(source: MaterialRefactorSource, progress: MaterialRefactorProgress) {
  const state = bulkRefactor.value
  if (!state) return
  if (progress.status === 'error') {
    state.events.forEach(event => {
      if (event.sourcePath === source.path && event.status === 'running') {
        event.status = 'error'
        event.detail = progress.detail
      }
    })
  }
  const existing = state.events.findIndex(event => event.sourcePath === source.path
    && event.stage === progress.stage
    && (event.status === 'running' || (event.status === 'error' && progress.status === 'error')))
  const entry: BulkRefactorEvent = {
    ...progress,
    id: `${source.path}:${progress.stage}:${Date.now()}`,
    sourcePath: source.path,
    sourceTitle: source.title,
  }
  if (existing >= 0) state.events.splice(existing, 1, entry)
  else state.events.push(entry)
}
async function runBulkRefactor(
  kind: CollectionKind,
  sources: MaterialRefactorSource[],
  quantityMode: MaterialRefactorQuantityMode,
) {
  const state = {
    kind, total: sources.length, completed: 0, succeeded: 0, failed: 0,
    current: '', running: true, quantityMode, events: [] as BulkRefactorEvent[],
  }
  bulkRefactor.value = state
  refactorLoading.value = true
  const failures: string[] = []
  try {
    for (const source of sources) {
      state.current = source.title
      recordBulkProgress(source, { stage: 'prepare', label: '加入批量执行队列', status: 'done' })
      try {
        const onProgress = (progress: MaterialRefactorProgress) => recordBulkProgress(source, progress)
        const preview = await project.executeMaterialRefactor(
          source, refactorPromptFor(source), config.currentProviderId, story.reasoningEffort,
          {
            quantityMode,
            sourceItemCount: quantityMode === 'preserve'
              ? scriptMinorCountForRefactor(source, project.scripts) : undefined,
            onProgress,
          },
        )
        if (kind === 'scripts') {
          if (!preview) throw new Error('没有返回剧本重构预览')
          await project.commitScriptRefactor(preview, source, onProgress)
        }
        state.succeeded += 1
      } catch (cause) {
        state.failed += 1
        const detail = cause instanceof Error ? cause.message : String(cause)
        recordBulkProgress(source, { stage: 'complete', label: '条目重构失败', status: 'error', detail })
        failures.push(`${source.title}：${detail}`)
      } finally {
        state.completed += 1
      }
    }
    const label = kind === 'scripts' ? '剧本' : '风格预设'
    notice.value = `${label}批量重构完成：成功 ${state.succeeded} 个，失败 ${failures.length} 个`
    error.value = failures.length ? failures.slice(0, 8).join('\n') : ''
    if (state.succeeded > 0) session.resetStoryContext()
  } finally {
    refactorLoading.value = false
    state.running = false
    state.current = ''
  }
}
async function executeMaterialRefactor() {
  const editor = materialRefactor.value
  if (!editor || refactorLoading.value) return
  refactorLoading.value = true
  error.value = ''
  try {
    const preview = await project.executeMaterialRefactor(
      editor.source, editor.prompt, config.currentProviderId, story.reasoningEffort,
    )
    if (preview) {
      scriptRefactor.value = preview
      scriptRefactorSource.value = editor.source
    } else {
      notice.value = '风格预设已格式化并写入正式目录'
      session.resetStoryContext()
    }
    materialRefactor.value = null
  } catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause) }
  finally { refactorLoading.value = false }
}
function commitScriptRefactor() {
  const preview = scriptRefactor.value
  if (!preview) return
  const source = scriptRefactorSource.value ?? undefined
  mutate(async () => {
    await project.commitScriptRefactor(preview, source)
    scriptRefactor.value = null
    scriptRefactorSource.value = null
  }, '剧本已重构并同步项目文件')
}
function saveFact(fact: MemoryFact) { mutate(() => project.saveMemory(), '记忆事实已保存') }
function requestRebuild(all: boolean) {
  mutate(async () => {
    if (all) project.memoryFacts.forEach(fact => { if (!fact.locked) fact.stale = true })
    await project.markMemoryPending()
    const rebuilt = await session.rebuildStoryConsistency()
    if (!rebuilt) throw new Error(project.consistency.lastError || '记忆与剧情状态更新失败')
  }, all ? '已完成全部记忆与剧情状态重建' : '已完成记忆与剧情状态更新')
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
  window.addEventListener('coomi:files-imported', onManagedNativeImport)
})
onBeforeUnmount(() => {
  window.removeEventListener('coomi:files-imported', onNativeImport)
  window.removeEventListener('coomi:files-imported', onManagedNativeImport)
})
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
        <section class="card group">
          <button v-for="item in STORY_MODES" :key="item.mode" class="row" @click="session.switchAgentMode(item.mode)">
            <span><b>{{ item.label }}</b><small>{{ item.desc }}</small></span><CoomiIcon v-if="story.agentMode === item.mode" name="check" class="selected" />
          </button>
        </section>
        <h2>会话上下文</h2>
        <section class="card group compact">
          <SwitchRow label="保留上下文窗口" desc="退出软件或切换模式后，继续各模式上次的模型上下文" :checked="config.retainContextWindow" @change="config.setRetainContextWindow($event)" />
          <button class="row danger-row" :disabled="session.isBusy" @click="clearCurrentContext"><span><b>清空当前上下文</b><small>只清空当前项目、当前模式，不影响其他模式</small></span><CoomiIcon name="trash" :size="17" /></button>
        </section>
        <h2>剧情控制</h2>
        <section class="card group compact">
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

      <template v-if="activeTab === 'director'">
        <h2>隐藏剧情导演</h2>
        <section class="card group compact">
          <SwitchRow label="启用剧情推进" desc="按主线阶段、停滞压力和实际结果调度剧情" :checked="project.settings.directorEnabled" @change="patchProjectSettings({ directorEnabled: $event })" />
          <div v-if="project.settings.directorEnabled" class="field"><span>当前主线</span><b>{{ project.directorState.activeArc?.title || '等待下一轮建立' }}</b></div>
          <div v-if="project.settings.directorEnabled" class="field"><span>剧情阶段</span><b>{{ project.directorState.activeArc ? MAJOR_PHASE_LABELS[project.directorState.activeArc.phase] : '未建立' }}</b></div>
          <div v-if="project.settings.directorEnabled" class="field"><span>唯一主剧本</span><b>{{ project.primaryScriptFocus?.title || '未选择' }}</b></div>
          <div v-if="project.settings.directorEnabled && project.directorState.activeArc" class="field"><span>阶段小剧情</span><b>{{ project.directorState.activeArc.phaseMinorCompleted?.[project.directorState.activeArc.phase] ?? 0 }} / {{ project.directorState.activeArc.budgetSnapshot?.phaseTargets?.[project.directorState.activeArc.phase] ?? '待冻结' }}</b></div>
          <div v-if="project.settings.directorEnabled" class="field"><span>绑定小剧本</span><b>{{ project.primaryScriptFocus?.minorScript?.title || '当前阶段动态生成' }}</b></div>
          <div v-if="project.settings.directorEnabled && project.directorState.subArcs[0]" class="field"><span>运行中小剧情</span><b>{{ project.directorState.subArcs[0].title }} · {{ project.directorState.subArcs[0].fragmentCount ?? 0 }}/{{ project.directorState.subArcs[0].fragmentBudget?.max ?? '?' }} 片段</b></div>
          <div v-if="project.settings.directorEnabled" class="field"><span>连续停滞</span><b>{{ project.directorState.pacing.stagnationCount }} / {{ project.settings.stagnationWarningThreshold }}{{ project.directorState.pacing.stagnationCount >= project.settings.stagnationWarningThreshold ? ' · 下一轮严厉推进' : '' }}</b></div>
          <div class="field"><span>故事节奏</span><div class="segments"><button v-for="item in STORY_PACES" :key="item.value" :class="{ on: project.settings.storyPace === item.value }" :title="item.desc" @click="patchProjectSettings({ storyPace: item.value })">{{ item.label }}</button></div></div>
          <label class="field"><span>停滞警告阈值</span><input type="number" min="1" max="20" :value="project.settings.stagnationWarningThreshold" @change="patchProjectSettings({ stagnationWarningThreshold: Math.min(20, Math.max(1, Math.round(Number(($event.target as HTMLInputElement).value) || 3))) })" /></label>
          <SwitchRow label="重大剧情使用引子" desc="关闭后新主线从开端直接建立目标和阻力" :checked="project.settings.majorHookEnabled" @change="patchProjectSettings({ majorHookEnabled: $event })" />
        </section>
        <h2>大剧情规模</h2>
        <section class="card group compact">
          <div class="field stack-field"><span>规模预设</span><div class="segments four"><button v-for="item in MAJOR_SCALES" :key="item.value" :class="{ on: plotDraft.scale === item.value }" :title="item.desc" @click="selectMajorScale(item.value)">{{ item.label }}</button></div></div>
          <div class="field"><span>小剧情总数</span><span class="dual"><input type="number" min="1" :value="plotDraft.totalMinorPlots.min" @change="updateRange(plotDraft.totalMinorPlots, 'min', $event, 1)" /><i>至</i><input type="number" min="1" :value="plotDraft.totalMinorPlots.max" @change="updateRange(plotDraft.totalMinorPlots, 'max', $event, 1)" /></span></div>
        </section>
        <h2>各阶段小剧情数量</h2>
        <section class="card group compact budget-list">
          <div v-for="phase in MAJOR_PHASES" :key="phase" class="field" :class="{ disabled: phase === 'hook' && !project.settings.majorHookEnabled }">
            <span>{{ MAJOR_PHASE_LABELS[phase] }}</span>
            <span class="dual"><input type="number" min="1" :disabled="phase === 'hook' && !project.settings.majorHookEnabled" :value="plotDraft.phaseMinorPlots[phase].min" @change="updateRange(plotDraft.phaseMinorPlots[phase], 'min', $event, 1)" /><i>至</i><input type="number" min="1" :disabled="phase === 'hook' && !project.settings.majorHookEnabled" :value="plotDraft.phaseMinorPlots[phase].max" @change="updateRange(plotDraft.phaseMinorPlots[phase], 'max', $event, 1)" /></span>
          </div>
        </section>
        <h2>小剧情片段预算</h2>
        <section class="card group compact budget-list">
          <div v-for="type in MINOR_STORY_TYPES" :key="type" class="field"><span>{{ MINOR_TYPE_LABELS[type] }}小剧情</span><span class="dual"><input type="number" min="1" :value="plotDraft.minorFragments[type].min" @change="updateRange(plotDraft.minorFragments[type], 'min', $event, 1)" /><i>至</i><input type="number" min="1" :value="plotDraft.minorFragments[type].max" @change="updateRange(plotDraft.minorFragments[type], 'max', $event, 1)" /></span></div>
          <div v-for="type in MINOR_STORY_TYPES" :key="`${type}-mix`" class="field"><span>{{ MINOR_TYPE_LABELS[type] }}占比</span><span class="percent-input"><input v-model.number="plotDraft.minorTypeMix[type]" type="number" min="0" max="100" @change="plotDraft.scale = 'custom'" /><i>%</i></span></div>
        </section>
        <h2>动态控制</h2>
        <section class="card group compact">
          <SwitchRow label="允许提前完成" desc="达到阶段或小剧情最小值且结果成立时允许提前收束" :checked="plotDraft.allowEarlyCompletion" @change="plotDraft.allowEarlyCompletion = $event" />
          <SwitchRow label="允许动态调整类型" desc="因玩家选择或重大事件可在快速、标准、重点之间调整一次" :checked="plotDraft.allowDynamicTypeChange" @change="plotDraft.allowDynamicTypeChange = $event" />
          <label class="field"><span>阶段收束保留数</span><input v-model.number="plotDraft.phaseClosureReserve" type="number" min="0" max="5" /></label>
        </section>
        <div class="plot-estimate"><span>预计规模</span><b>{{ plotEstimate.minorMin }}–{{ plotEstimate.minorMax }} 个小剧情</b><small>约 {{ plotEstimate.fragmentMin }}–{{ plotEstimate.fragmentMax }} 个剧情片段</small></div>
        <p v-if="plotErrors.length" class="validation-error">{{ plotErrors[0] }}</p>
        <div class="wide-actions"><button :disabled="plotErrors.length > 0" class="primary" @click="savePlotMechanics">保存默认配置</button><button :disabled="!project.directorState.activeArc" @click="applyPlotToCurrent">应用到当前大剧情</button></div>
        <p class="footnote">大剧情阶段按已完成的小剧情统计，小剧情按有效归档片段统计；独立事件不占小剧情数量。达到硬上限后程序强制收束和顺序切换，不再依赖模型主动切换阶段。</p>
      </template>

      <template v-if="activeTab === 'presets' || activeTab === 'scripts'">
        <div class="section-head material-section-head">
          <div><h2>{{ activeTab === 'presets' ? '风格预设' : '剧本管理' }}</h2><p>{{ activeTab === 'presets' ? '显示越靠前优先级越高；只控制表达，不得覆盖推进与事实约束。' : '三级结构：阶段 → 大剧情 → 小剧情。阶段只界定全局方向，首个进行中的大剧情为唯一主剧本，随后最多两个只作背景时钟。' }}</p></div>
          <button v-if="activeTab === 'scripts' && scriptTree.groupableMajors" class="bulk-refactor-action" :title="`把 ${scriptTree.groupableMajors} 个未分组大剧情收进一个新阶段`" @click="groupUngroupedMajors"><CoomiIcon name="folder" :size="15" /><span>归入阶段</span></button>
          <button class="bulk-refactor-action" :disabled="refactorLoading || bulkRefactorCount === 0" :title="bulkRefactorCount ? `格式化全部 ${bulkRefactorCount} 个条目` : '当前没有可重构条目'" @click="requestBulkRefactor(currentCollectionKind)"><CoomiIcon name="sparkle" :size="15" /><span>全部重构</span></button>
          <button class="icon-action" :class="{ on: sortMode[currentCollectionKind] }" title="排序" @click="sortMode[currentCollectionKind] = !sortMode[currentCollectionKind]"><CoomiIcon name="settings" :size="16" /></button>
          <button class="icon-action" title="导入" @click="startManagedImport(currentCollectionKind)"><CoomiIcon name="arrowUp" :size="16" /></button>
          <button class="icon-action primary" title="新增" @click="openItem(currentCollectionKind, null)"><CoomiIcon name="plus" :size="16" /></button>
        </div>

        <!-- 风格预设仍是扁平列表：它没有层级，优先级就是显示顺序。 -->
        <section v-if="activeTab === 'presets'" class="item-list">
          <article v-for="(item, index) in project.presets" :key="item.id" class="item-row">
            <Switch size="sm" :checked="item.enabled" :aria-label="item.enabled ? '停用' : '激活'" @change="mutate(() => project.toggleItem('presets', item), item.enabled ? '已停用' : '已激活')" />
            <div class="item-copy"><b>{{ item.title }}</b><small>{{ item.formatVersion === 2 ? '标准格式' : '旧格式' }} · {{ item.enabled ? '已激活' : '未激活' }}</small></div>
            <div v-if="sortMode.presets" class="sort-actions"><button :disabled="index === 0" @click="project.moveItem('presets', item, -1)">↑</button><button :disabled="index === project.presets.length - 1" @click="project.moveItem('presets', item, 1)">↓</button></div>
            <div v-else class="item-actions">
              <button title="查看" @click="openItem('presets', item, true)"><CoomiIcon name="eye" :size="15" /></button>
              <button title="编辑或重命名" @click="openItem('presets', item)"><CoomiIcon name="pencil" :size="15" /></button>
              <button title="导出" @click="run(() => project.exportItem('presets', item), '正在导出')"><CoomiIcon name="arrowDown" :size="15" /></button>
              <button title="格式化重构" :disabled="refactorLoading" @click="openMaterialRefactor(project.existingMaterialSource('presets', item))"><CoomiIcon name="sparkle" :size="15" /></button>
              <button title="删除" class="danger" @click="removeManaged('presets', item)"><CoomiIcon name="trash" :size="15" /></button>
            </div>
          </article>
          <p v-if="!project.presets.length" class="empty">暂无条目</p>
        </section>

        <!-- 剧本：三级树。阶段展开才有大剧情，大剧情展开才有小剧情。 -->
        <section v-else class="script-tree">
          <article
            v-for="row in scriptTree.rows"
            :key="row.item.id"
            class="script-card"
            :class="[`lv-${row.level}`, { orphan: row.orphan, off: !row.item.enabled }]"
            :data-depth="row.depth"
          >
            <div class="script-head">
              <button v-if="row.expandable" class="script-chev" :class="{ open: row.expanded }" :aria-label="row.expanded ? '收起' : '展开'" @click="toggleScriptExpanded(row.item)"><CoomiIcon name="chevronRight" :size="14" /></button>
              <span v-else class="script-chev leaf"><i /></span>
              <div class="script-copy"><b>{{ row.item.title }}</b><small>{{ scriptCardMeta(row) }}</small></div>
              <Switch size="sm" :checked="row.item.enabled" :aria-label="row.item.enabled ? '停用' : '激活'" @change="mutate(() => project.toggleItem('scripts', row.item), row.item.enabled ? '已停用' : '已激活')" />
            </div>

            <div class="script-tags">
              <span class="tag lv">{{ SCRIPT_LEVEL_LABELS[row.level] }}</span>
              <span v-if="row.level !== 'stage'" class="tag" :class="`st-${row.item.status ?? 'active'}`">{{ SCRIPT_STATUS_LABELS[row.item.status ?? 'active'] }}</span>
              <span v-if="scriptIsPrimary(row.item)" class="tag hot">主剧本</span>
              <span v-if="row.orphan" class="tag warn">{{ row.item.parentId ? '归属已失效' : '未分组' }}</span>
              <span v-if="row.expandable && !row.expanded" class="tag">{{ row.childCount }} 项已收起</span>
            </div>

            <div v-if="row.childCount" class="script-bar" :title="`${row.childDone} / ${row.childCount} 已完成`"><i :style="{ width: scriptProgress(row) + '%' }" /></div>
            <p v-if="row.item.completionCondition" class="script-cond">{{ row.level === 'stage' ? '阶段完成标志' : '完成条件' }}：{{ row.item.completionCondition }}</p>

            <div class="script-foot">
              <template v-if="sortMode.scripts">
                <button class="ghost" :disabled="!row.canMoveUp" @click="run(() => project.moveScriptSibling(row.item, -1))">↑ 上移</button>
                <button class="ghost" :disabled="!row.canMoveDown" @click="run(() => project.moveScriptSibling(row.item, 1))">↓ 下移</button>
                <span class="script-gap" />
              </template>
              <template v-else>
                <template v-if="row.level !== 'stage'">
                  <button class="ghost" @click="project.markScript(row.item, row.item.status === 'completed' ? 'active' : 'completed')">{{ row.item.status === 'completed' ? '撤销完成' : '标记完成' }}</button>
                  <button class="ghost" @click="project.markScript(row.item, 'pending')">待处理</button>
                </template>
                <button v-if="row.level !== 'minor'" class="ghost add" @click="addScriptChild(row)">+ {{ row.level === 'stage' ? '大剧情' : '小剧情' }}</button>
                <span class="script-gap" />
                <button title="查看" @click="openItem('scripts', row.item, true)"><CoomiIcon name="eye" :size="15" /></button>
                <button title="编辑或重命名" @click="openItem('scripts', row.item)"><CoomiIcon name="pencil" :size="15" /></button>
                <button title="导出" @click="run(() => project.exportItem('scripts', row.item), '正在导出')"><CoomiIcon name="arrowDown" :size="15" /></button>
                <button v-if="row.level === 'major'" title="格式化重构" :disabled="refactorLoading" @click="openMaterialRefactor(project.existingMaterialSource('scripts', row.item))"><CoomiIcon name="sparkle" :size="15" /></button>
                <button title="删除" class="danger" @click="removeManaged('scripts', row.item)"><CoomiIcon name="trash" :size="15" /></button>
              </template>
            </div>
          </article>
          <p v-if="!scriptTree.rows.length" class="empty">暂无剧本条目</p>

          <!-- 重构前的原始备份单独收成一组：它不参与推进，混在树里只会干扰层级判断。 -->
          <template v-if="scriptTree.backups.length">
            <button class="script-backup-head" :class="{ open: showScriptBackups }" @click="showScriptBackups = !showScriptBackups">
              <CoomiIcon name="chevronRight" :size="13" /><span>重构前的原始备份 · {{ scriptTree.backups.length }} 份</span>
            </button>
            <article v-for="item in (showScriptBackups ? scriptTree.backups : [])" :key="item.id" class="script-card backup">
              <div class="script-head"><span class="script-chev leaf"><i /></span><div class="script-copy"><b>{{ item.title }}</b><small>原始备份 · 不参与剧情推进</small></div></div>
              <div class="script-foot">
                <span class="script-gap" />
                <button title="查看" @click="openItem('scripts', item, true)"><CoomiIcon name="eye" :size="15" /></button>
                <button title="导出" @click="run(() => project.exportItem('scripts', item), '正在导出')"><CoomiIcon name="arrowDown" :size="15" /></button>
                <button title="删除" class="danger" @click="removeManaged('scripts', item)"><CoomiIcon name="trash" :size="15" /></button>
              </div>
            </article>
          </template>
        </section>
        <template v-if="project.stagedMaterials.some(item => item.kind === currentCollectionKind)">
          <h2>待格式化导入</h2>
          <section class="item-list staged-list">
            <article v-for="source in project.stagedMaterials.filter(item => item.kind === currentCollectionKind)" :key="source.path" class="item-row">
              <div class="item-copy"><b>{{ source.title }}</b><small>临时文件 · 关闭软件后自动清空</small></div>
              <button class="text-button" @click="openMaterialRefactor(source)">格式化重构</button>
            </article>
          </section>
        </template>
      </template>

      <template v-if="activeTab === 'random'">
        <h2>随机遭遇</h2>
        <section class="card group compact">
          <SwitchRow label="随机气运" desc="每次玩家行动都进行判定" :checked="project.settings.fortuneEnabled" @change="patchProjectSettings({ fortuneEnabled: $event })" />
          <SwitchRow label="启用随机遭遇" desc="每轮最多生成一条有因果链的遭遇计划" :checked="project.settings.encounterEnabled" @change="patchProjectSettings({ encounterEnabled: $event })" />
        </section>
        <div class="encounter-options" :class="{ disabled: !project.settings.encounterEnabled }" :inert="!project.settings.encounterEnabled || undefined" :aria-disabled="!project.settings.encounterEnabled">
        <section class="card group compact">
          <div class="field"><span>遭遇频率</span><div class="segments three"><button v-for="item in ENCOUNTER_FREQUENCIES" :key="item.value" :class="{ on: project.settings.encounterFrequency === item.value }" :title="item.desc" @click="patchProjectSettings({ encounterFrequency: item.value })">{{ item.label }}</button></div></div>
          <SwitchRow label="事件环境" desc="作为遭遇主轴或背景，提供可观察变化" :checked="project.settings.eventEnabled" @change="patchProjectSettings({ eventEnabled: $event })" />
          <SwitchRow label="人物参与者" desc="只在有合理因果时进入遭遇" :checked="project.settings.characterEnabled" @change="patchProjectSettings({ characterEnabled: $event })" />
          <div class="field"><span>人物性别</span><div class="segments"><button v-for="gender in GENDERS" :key="gender.value" :class="{ on: project.settings.characterGender === gender.value }" @click="patchProjectSettings({ characterGender: gender.value })">{{ gender.label }}</button></div></div>
          <SwitchRow label="悲剧方向" desc="必须有既有因果和铺垫，带来实际代价" :checked="project.settings.tragedyEnabled" @change="patchProjectSettings({ tragedyEnabled: $event })" />
          <SwitchRow label="爽点方向" desc="必须有前置铺垫，改变关系、资源或局势" :checked="project.settings.payoffEnabled" @change="patchProjectSettings({ payoffEnabled: $event })" />
        </section>
        <h2>遭遇词库</h2>
        <section class="item-list">
          <article v-for="kind in LIBRARY_KINDS" :key="kind" class="library-row">
            <div class="item-copy"><b>{{ KEYWORD_LIBRARY_LABELS[kind] }}</b><small>{{ libraries.stats(kind).source === 'custom' ? '当前项目自定义' : '内置通用' }} · {{ libraries.stats(kind).categories }} 类 · {{ libraries.stats(kind).keywords }} 词</small></div>
            <div class="text-actions"><button @click="openLibrary(kind)">查看编辑</button><button @click="startLibraryImport(kind)">导入</button><button @click="run(() => libraries.exportCurrent(kind), '正在导出')">导出</button><button :disabled="!libraries.custom[kind]" @click="askConfirm('恢复内置词库', '当前项目的自定义词库将被移除，改用内置通用版本。', () => libraries.restoreBuiltin(kind), '已恢复内置词库')">恢复内置</button></div>
          </article>
        </section>
        <input ref="fileInput" hidden type="file" accept=".json,application/json" @change="onBrowserImport" />
        </div>
      </template>

      <template v-if="activeTab === 'memory'">
        <div class="section-head"><div><h2>结构化记忆</h2><p>锁定事实不会被模型自动修改；章节改动会使关联事实过期。</p></div><button class="text-button" @click="project.addMemoryFact('新记忆事实')">新增事实</button></div>
        <p v-if="project.memoryPending" class="sync-state"><CoomiIcon name="alert" :size="15" />记忆待同步</p>
        <section class="memory-list">
          <article v-for="fact in project.memoryFacts" :key="fact.id" :class="{ stale: fact.stale }">
            <textarea v-if="editingFactId === fact.id" v-model="fact.text" rows="3" autofocus @change="saveFact(fact)" @blur="editingFactId = ''" />
            <button v-else class="memory-preview" :title="fact.text" @click="editingFactId = fact.id">{{ memoryPreview(fact.text) }}</button>
            <div><select v-model="fact.scope" @change="saveFact(fact)"><option value="objective">客观事实</option><option value="protagonist">主角已知</option></select><button :class="{ on: fact.locked }" @click="fact.locked = !fact.locked; saveFact(fact)">{{ fact.locked ? '已锁定' : '锁定' }}</button><span v-if="fact.stale">已过期</span><button class="danger" @click="deleteFact(fact)">删除</button></div>
          </article>
          <p v-if="project.memoryFacts.length === 0" class="empty">暂无结构化事实</p>
        </section>
        <div class="wide-actions"><button @click="requestRebuild(false)">局部重建</button><button @click="requestRebuild(true)">全部重建</button></div>
      </template>

      <template v-if="activeTab === 'time'">
        <div class="time-now"><span>当前故事时间</span><strong>{{ project.currentTimeLabel }}</strong><small>{{ project.time.locked ? '已锁定' : '随剧情推进' }}</small></div>
        <section class="card group compact">
          <label class="field"><span>历法</span><select v-model="project.time.calendar" @change="project.patchTime({ calendar: project.time.calendar })"><option value="relative">相对历</option><option value="gregorian">公历</option><option value="custom">自定义历法</option></select></label>
          <label v-if="project.time.calendar === 'custom'" class="field"><span>历法名称</span><input v-model="project.time.calendarName" @change="project.patchTime({ calendarName: project.time.calendarName })" /></label>
          <label class="field"><span>显示时间</span><span class="inline-input"><input v-model="timeDraft" /><button @click="saveTimeDisplay">保存</button></span></label>
          <div class="field"><span>时间精度</span><div class="segments"><button :class="{ on: project.time.precision === 'fuzzy' }" @click="project.patchTime({ precision: 'fuzzy' })">模糊</button><button :class="{ on: project.time.precision === 'day' }" @click="project.patchTime({ precision: 'day' })">天</button><button :class="{ on: project.time.precision === 'hour' }" @click="project.patchTime({ precision: 'hour' })">小时</button></div></div>
          <SwitchRow label="锁定当前时间" desc="禁止模型自动修改" :checked="project.time.locked" @change="project.patchTime({ locked: $event })" />
          <SwitchRow label="闪回状态" desc="第一版支持完整闪回，不创建并行时间线" :checked="!!project.time.flashback" @change="project.patchTime({ flashback: $event ? { active: true, at: project.currentTimeLabel, returnTo: project.currentTimeLabel } : null })" />
        </section>
        <h2>时间纠错</h2>
        <input v-model="correctionDraft" class="correction" placeholder="输入修正后的故事时间" />
        <div class="correction-actions"><button @click="correctTime('record')">仅修正时间记录</button><button @click="correctTime('rollback')">回滚受影响状态</button><button @click="correctTime('snapshot')">创建时间修订快照</button></div>
      </template>

      <template v-if="activeTab === 'theme'">
        <h2>主题外观</h2>
        <section class="card group">
          <button v-for="item in THEME_MODES" :key="item.mode" class="row" @click="config.setThemeMode(item.mode)"><span><b>{{ item.label }}</b><small>{{ item.desc }}</small></span><CoomiIcon v-if="config.themeMode === item.mode" name="check" class="selected" /></button>
        </section>
      </template>
    </main>

    <BottomSheet v-if="libraryEditor" :grip="false" @close="libraryEditor = null"><section class="sheet tall">
      <div class="sheet-head"><div><b>{{ KEYWORD_LIBRARY_LABELS[libraryEditor.kind] }}</b><small>分类与词条可自由增删</small></div><button @click="libraryEditor = null"><CoomiIcon name="close" /></button></div>
      <input v-model="keywordSearch" class="search" placeholder="搜索分类或词条" />
      <div class="category-list"><article v-for="[category, words] in visibleLibrary" :key="category"><div class="category-head"><input :value="category" readonly /><button @click="delete libraryEditor!.draft[category]">删除分类</button></div><div v-for="(_, index) in words" :key="index" class="word"><input v-model="words[index]" /><button title="删除词条" @click="removeKeyword(category, index)"><CoomiIcon name="close" :size="14" /></button></div><button class="add-word" @click="addKeyword(category)">+ 添加词条</button></article></div>
      <div class="new-category"><input v-model="newCategory" placeholder="新分类名称" /><button @click="addCategory">添加分类</button></div>
      <div class="sheet-actions"><button @click="libraryEditor = null">取消</button><button class="primary" @click="saveLibrary">保存为项目词库</button></div>
    </section></BottomSheet>

    <BottomSheet v-if="itemEditor" :grip="false" @close="itemEditor = null"><section class="sheet tall">
      <div class="sheet-head"><div><b>{{ itemEditor.readOnly ? '查看' : itemEditor.item ? '编辑与重命名' : '新增条目' }}</b><small>{{ itemEditor.kind === 'presets' ? '风格预设' : `剧情剧本 · ${SCRIPT_LEVEL_LABELS[itemEditor.scriptType]}` }}</small></div><button @click="itemEditor = null"><CoomiIcon name="close" /></button></div>
      <label>名称<input v-model="itemEditor.title" :readonly="itemEditor.readOnly" /></label>
      <div v-if="itemEditor.kind === 'scripts' && !itemEditor.item && !itemEditor.readOnly" class="field">
        <span>层级</span>
        <div class="segments three">
          <button v-for="level in SCRIPT_LEVEL_OPTIONS" :key="level.value" :class="{ on: itemEditor.scriptType === level.value }" :title="level.desc" @click="itemEditor.scriptType = level.value; itemEditor.parentId = ''">{{ level.label }}</button>
        </div>
      </div>
      <label v-if="itemEditor.kind === 'scripts' && itemEditor.scriptType !== 'stage'">
        {{ itemEditor.scriptType === 'major' ? '所属阶段' : '所属大剧情' }}
        <select v-model="itemEditor.parentId" :disabled="itemEditor.readOnly">
          <option value="">{{ itemEditor.scriptType === 'major' ? '未分组（不挂任何阶段）' : '未分组（不挂任何大剧情）' }}</option>
          <option v-for="parent in scriptParentOptions" :key="parent.id" :value="parent.id">{{ parent.title }}</option>
        </select>
      </label>
      <p v-if="itemEditor.kind === 'scripts' && itemEditor.scriptType === 'stage'" class="footnote">阶段只提供全局框架：不参与状态机、没有背景时钟，也不会被推选为主剧本。具体剧情内容请写在小剧情里。</p>
      <label v-if="itemEditor.kind === 'scripts'">{{ itemEditor.scriptType === 'stage' ? '阶段完成标志' : '完成条件' }}<input v-model="itemEditor.condition" :readonly="itemEditor.readOnly" :placeholder="itemEditor.scriptType === 'stage' ? '整个阶段达成什么后可以进入下一阶段' : '由模型据此判断完成'" /></label>
      <label v-if="itemEditor.kind === 'scripts'">{{ itemEditor.scriptType === 'stage' ? '阶段目标' : '默认路线' }}<input v-model="itemEditor.route" :readonly="itemEditor.readOnly" :placeholder="itemEditor.scriptType === 'stage' ? '这一阶段要走向哪里、边界在哪' : '未填写时遇到分叉将标记待处理'" /></label>
      <label class="grow">内容<textarea v-model="itemEditor.content" :readonly="itemEditor.readOnly" /></label>
      <div class="sheet-actions"><button @click="itemEditor = null">关闭</button><button v-if="!itemEditor.readOnly" class="primary" @click="saveItem">保存</button></div>
    </section></BottomSheet>

    <!-- 重构进行中不许点遮罩关闭：关掉不会取消已经发出的 Agent 任务。 -->
    <BottomSheet v-if="materialRefactor" :grip="false" :dismissible="!refactorLoading" @close="materialRefactor = null"><section class="sheet tall refactor-command-sheet">
      <div class="sheet-head"><div><b>{{ materialRefactor.source.kind === 'scripts' ? '剧本格式化重构' : '风格预设格式化重构' }}</b><small>{{ materialRefactor.source.mode === 'import' ? '新增导入' : '已有文件校正' }} · {{ materialRefactor.source.title }}</small></div><button class="execute-icon" :disabled="refactorLoading" title="执行格式化重构" @click="executeMaterialRefactor"><CoomiIcon :name="refactorLoading ? 'refresh' : 'send'" :size="17" /></button><button title="关闭" @click="materialRefactor = null"><CoomiIcon name="close" /></button></div>
      <p>提示词会保存到当前故事项目，下次打开同类格式化任务时继续使用。</p>
      <label class="grow">标准化详细化提示词<textarea v-model="materialRefactor.prompt" /></label>
      <div class="sheet-actions"><button @click="project.updateRefactorPrompt(materialRefactor!.source.kind, materialRefactor!.source.mode, materialRefactor!.prompt).then(() => notice = '格式化提示词已保存').catch(cause => error = cause instanceof Error ? cause.message : String(cause))">仅保存提示词</button><button class="primary" :disabled="refactorLoading" @click="executeMaterialRefactor">{{ refactorLoading ? '正在重构…' : '执行重构' }}</button></div>
    </section></BottomSheet>

    <BottomSheet v-if="scriptRefactor" :grip="false" @close="scriptRefactor = null; scriptRefactorSource = null"><section class="sheet tall refactor-sheet">
      <div class="sheet-head"><div><b>剧本格式化重构预览</b><small>原文件将备份，候选内容确认后写入标准目录</small></div><button @click="scriptRefactor = null; scriptRefactorSource = null"><CoomiIcon name="close" /></button></div>
      <div class="refactor-summary"><b>{{ scriptRefactor.majorTitle }}</b><span>{{ scriptRefactor.budget.totalTarget }} 个目标小剧情 · 识别 {{ scriptRefactor.minors.length }} 个</span><p>{{ scriptRefactor.premise }}</p></div>
      <p v-for="warning in scriptRefactor.warnings" :key="warning" class="validation-error">{{ warning }}</p>
      <div class="refactor-list"><article v-for="(minor, index) in scriptRefactor.minors" :key="minor.id"><span>{{ index + 1 }}</span><div><b>{{ minor.title }}</b><small>{{ MAJOR_PHASE_LABELS[minor.phase] }} · {{ MINOR_TYPE_LABELS[minor.minorType] }} · {{ minor.fragmentBudget.min }}–{{ minor.fragmentBudget.max }} 片段</small><p>{{ minor.majorContribution }}</p></div></article></div>
      <div class="sheet-actions"><button @click="scriptRefactor = null">取消</button><button class="primary" @click="commitScriptRefactor">确认重构并同步文件</button></div>
    </section></BottomSheet>

    <BottomSheet v-if="bulkRefactorSetup" :grip="false" @close="bulkRefactorSetup = null"><section class="sheet bulk-setup-sheet">
      <div class="sheet-head"><div><b>一键重构全部{{ bulkRefactorSetup.kind === 'scripts' ? '剧本' : '风格预设' }}</b><small>共 {{ bulkRefactorSetup.sources.length }} 个待处理条目</small></div><button title="关闭" @click="bulkRefactorSetup = null"><CoomiIcon name="close" /></button></div>
      <p>任务会逐条执行；原文件先备份，单个条目失败不会中断后续条目。</p>
      <div class="quantity-policy">
        <span>条目数量策略</span>
        <div class="quantity-options">
          <button :class="{ active: bulkRefactorSetup.quantityMode === 'preserve' }" @click="bulkRefactorSetup.quantityMode = 'preserve'"><b>保持一致</b><small>{{ bulkRefactorSetup.kind === 'scripts' ? '优先保持现有小剧情数量，内容冲突时由 Agent 调整' : '优先让每份原预设对应一份整理结果' }}</small></button>
          <button :class="{ active: bulkRefactorSetup.quantityMode === 'auto' }" @click="bulkRefactorSetup.quantityMode = 'auto'"><b>自动规划</b><small>{{ bulkRefactorSetup.kind === 'scripts' ? '按内容复杂度和剧情推进配置决定数量' : '复合预设可按独立风格体系拆分为多份' }}</small></button>
        </div>
      </div>
      <div class="sheet-actions"><button @click="bulkRefactorSetup = null">取消</button><button class="primary" @click="startBulkRefactor">开始重构</button></div>
    </section></BottomSheet>

    <!-- 批量重构进度：整个过程不可打断，只有跑完才出现「完成」。 -->
    <BottomSheet v-if="bulkRefactor" :grip="false" :dismissible="false" aria-label="批量重构进度"><section class="sheet tall bulk-progress-sheet">
      <div class="sheet-head"><div><b>{{ bulkRefactor.running ? 'Agent 正在批量重构' : '批量重构执行记录' }}</b><small>{{ bulkRefactor.completed }}/{{ bulkRefactor.total }} · 成功 {{ bulkRefactor.succeeded }} · 失败 {{ bulkRefactor.failed }}</small></div><CoomiIcon v-if="bulkRefactor.running" name="refresh" :size="19" /><button v-else title="关闭" @click="bulkRefactor = null"><CoomiIcon name="close" /></button></div>
      <p v-if="bulkRefactor.current">当前条目：{{ bulkRefactor.current }}</p>
      <p v-else>{{ bulkRefactor.quantityMode === 'preserve' ? '数量策略：优先保持现有条目数量' : '数量策略：由 Agent 自动规划' }}</p>
      <div class="bulk-progress"><i :style="{ width: `${bulkRefactor.total ? bulkRefactor.completed / bulkRefactor.total * 100 : 0}%` }" /></div>
      <div ref="bulkWaterfall" class="agent-waterfall">
        <article v-for="event in bulkRefactor.events" :key="event.id" :class="event.status">
          <span class="waterfall-node"><CoomiIcon v-if="event.status === 'done'" name="check" :size="12" /><CoomiIcon v-else-if="event.status === 'error'" name="alert" :size="12" /><i v-else /></span>
          <div><small>{{ event.sourceTitle }}</small><b>{{ event.label }}</b><p v-if="event.detail">{{ event.detail }}</p></div>
        </article>
      </div>
      <small v-if="bulkRefactor.running">Agent 分析阶段可能持续较长时间，请保持应用在前台。</small>
      <div v-else class="sheet-actions"><button class="primary" @click="bulkRefactor = null">完成</button></div>
    </section></BottomSheet>

    <BottomSheet v-if="confirmBox" :grip="false" role="alertdialog" :aria-label="confirmBox.title" @close="confirmBox = null"><section class="sheet"><div class="warning-icon"><CoomiIcon name="alert" /></div><b>{{ confirmBox.title }}</b><p>{{ confirmBox.message }}</p><div class="sheet-actions"><button @click="confirmBox = null">取消</button><button class="danger-fill" @click="acceptConfirm">确认</button></div></section></BottomSheet>
    <!-- 应用时机必须选一个，点遮罩不能跳过。 -->
    <BottomSheet v-if="mutation" :grip="false" role="alertdialog" :dismissible="false" aria-label="当前推理周期正在执行"><section class="sheet"><b>当前推理周期正在执行</b><p>修改项目约束后必须重建上下文。请选择应用时机。</p><div class="stack-actions"><button class="danger-fill" @click="resolveMutation('stop')">停止并应用</button><button @click="resolveMutation('after')">本轮结束后应用</button><button @click="resolveMutation('cancel')">取消修改</button></div></section></BottomSheet>
    <input ref="managedFileInput" hidden multiple type="file" accept=".txt,.md,.markdown,.json,.yaml,.yml,.csv,.tsv,.html,.htm,.xml,.toml,.rtf,.docx,.pdf,.log,text/plain,text/markdown,application/json,text/csv,text/tab-separated-values,text/html,application/xml,text/xml,application/rtf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/pdf" @change="onManagedFile" />
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
.encounter-options { transition:opacity .15s; }.encounter-options.disabled { opacity:.42; filter:grayscale(.65); }.encounter-options.disabled :is(button,input,select,textarea) { pointer-events:none; }
.row { display:flex; align-items:center; width:100%; min-height:58px; padding:10px 13px; text-align:left; color:var(--text); }
/* 分隔线只画在设置正文的分组列表里。这里必须写成 .group.compact 而不是裸 .compact：
   之前两个确认弹层挂的是 class="sheet compact"（想要的只是 max-height:none），
   却被这条裸选择器命中，在图标 / 标题 / 说明 / 按钮行之间画出了四条横线。 */
.row + .row,.group.compact > * + * { border-top:1px solid var(--border); }
.row > span { display:flex; flex:1; min-width:0; flex-direction:column; gap:3px; }
b { font-size:13.5px; font-weight:650; } small { color:var(--text-3); font-size:11.5px; line-height:1.45; }
.selected { color:var(--blue); }
.danger-row { color:var(--danger); }.danger-row:disabled { opacity:.45; }
.field { display:flex; align-items:center; justify-content:space-between; gap:12px; min-height:54px; padding:9px 13px; color:var(--text-2); font-size:13px; }
.field > span:first-child { flex:1; }
.field > b { max-width:58%; color:var(--text-1); font-size:13px; text-align:right; overflow-wrap:anywhere; }
input,select,textarea { border:1px solid var(--border-strong); border-radius:6px; background:var(--bg-input); color:var(--text); font:inherit; }
/* 右侧控件的宽度上限量的是「这一行还剩多少」，不是视口。原先写 min(218px,61vw)：
   vw 只认屏幕宽度，而分组两侧有内边距、弹层里这一行更窄，算出来一律偏大，
   窄屏上就把左边的标题挤成竖排。百分比落在 .field 的内容盒上，正好是要让的那个量——
   上面 .field > b 的 max-width:58% 用的就是同一个参照。 */
.field > input,.field > select { width:150px; max-width:45%; min-height:36px; padding:0 9px; }
.segments { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); width:218px; max-width:61%; padding:3px; border-radius:6px; background:var(--fill-strong); }
.segments.four { grid-template-columns:repeat(4,minmax(0,1fr)); width:286px; max-width:72%; }
.segments.five { grid-template-columns:repeat(5,minmax(0,1fr)); width:260px; max-width:68%; }
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
.stack-field { align-items:flex-start; flex-direction:column; }.stack-field > .segments { width:100%; }
.budget-list .field.disabled { opacity:.42; }.budget-list .field.disabled input { pointer-events:none; }
.percent-input { display:flex; align-items:center; gap:6px; }.percent-input input { width:72px; min-height:34px; padding:0 7px; }.percent-input i { color:var(--text-3); font-style:normal; }
.plot-estimate { display:grid; grid-template-columns:1fr auto; gap:3px 10px; margin-top:10px; padding:11px 13px; border:1px solid var(--border); border-radius:7px; background:var(--bg); }.plot-estimate span { align-self:center; color:var(--text-3); font-size:12px; }.plot-estimate b { color:var(--text); text-align:right; }.plot-estimate small { grid-column:1 / -1; text-align:right; }
.validation-error { margin:8px 2px 0!important; color:var(--danger)!important; font-size:11.5px!important; }
.footnote { margin:9px 3px; color:var(--text-3); font-size:11.5px; line-height:1.6; }
.notice { margin:0 0 10px; padding:9px 11px; border-radius:6px; font-size:12px; }.notice.ok { background:var(--ok-soft); color:var(--ok); }.notice.err { background:var(--danger-soft); color:var(--danger); }
.section-head { display:flex; align-items:center; gap:7px; margin-bottom:10px; }.section-head > div { flex:1; min-width:0; }.section-head h2 { margin:0; }.section-head p { margin:3px 0 0; color:var(--text-3); font-size:11.5px; line-height:1.45; }
.icon-action { display:grid; place-items:center; width:36px; height:36px; border-radius:6px; background:var(--fill-strong); color:var(--text-2); }.icon-action.primary,.icon-action.on { background:var(--blue-soft); color:var(--blue); }
.bulk-refactor-action { display:flex; align-items:center; gap:5px; min-height:36px; padding:0 9px; border-radius:6px; background:var(--blue-soft); color:var(--blue); font-size:11.5px; white-space:nowrap; }.bulk-refactor-action:disabled { opacity:.4; }
.item-list,.memory-list { display:flex; flex-direction:column; gap:7px; }.item-row,.library-row,.memory-list article { display:flex; align-items:center; flex-wrap:wrap; gap:9px; padding:11px; border:1px solid var(--border); border-radius:7px; background:var(--bg); }
.item-copy { display:flex; flex:1; min-width:140px; flex-direction:column; gap:3px; }.item-actions,.sort-actions { display:flex; gap:3px; }.item-actions button,.sort-actions button { display:grid; place-items:center; width:31px; height:31px; border-radius:5px; background:var(--fill); color:var(--text-2); }.danger { color:var(--danger)!important; }
.text-actions button,.wide-actions button,.text-button { min-height:32px; padding:0 10px; border-radius:5px; background:var(--fill-strong); color:var(--text-2); font-size:11.5px; }
/* ── 三级剧本树 ────────────────────────────────────────────────────────────
   缩进按 data-depth 算，不用嵌套容器：手机上每层嵌套都要再吃掉一截左右 padding，
   三层下来卡片就没地方放字了。列表顺序仍然是优先级顺序，缩进只表达归属。 */
.script-tree { display:flex; flex-direction:column; gap:7px; }
.script-card { position:relative; display:flex; flex-direction:column; gap:7px; padding:11px 12px 10px; border:1px solid var(--border); border-left:3px solid var(--border); border-radius:9px; background:var(--bg-card); }
.script-card[data-depth="1"] { margin-left:14px; }
.script-card[data-depth="2"] { margin-left:28px; }
/* 引导线画在左侧缩进的空档里，让「属于上面哪一条」在扫视时就能看出来。 */
.script-card[data-depth="1"]::before,
.script-card[data-depth="2"]::before { position:absolute; top:-8px; bottom:14px; left:-8px; width:1px; background:var(--border); content:''; }
.script-card[data-depth="1"]::after,
.script-card[data-depth="2"]::after { position:absolute; bottom:14px; left:-8px; width:6px; height:1px; background:var(--border); content:''; }
/* 层级用左侧色条区分，比只靠缩进更容易一眼定位。 */
.script-card.lv-stage { border-left-color:var(--blue); background:var(--bg-elevated); }
.script-card.lv-major { border-left-color:var(--orange); }
.script-card.lv-minor { padding:9px 11px 9px; }
.script-card.orphan { border-color:var(--orange); }
.script-card.off { opacity:.55; }
.script-card.backup { border-left-color:var(--border); background:var(--bg); }
.script-head { display:flex; align-items:flex-start; gap:8px; }
.script-chev { display:grid; flex-shrink:0; place-items:center; width:26px; height:26px; margin:-2px 0 0 -4px; border-radius:6px; color:var(--text-3); transition:transform .16s; }
.script-chev.open { transform:rotate(90deg); }
.script-chev.leaf { pointer-events:none; }
.script-chev.leaf > i { width:4px; height:4px; border-radius:50%; background:var(--border); }
.script-copy { display:flex; min-width:0; flex:1; flex-direction:column; gap:3px; }
.script-copy b { overflow-wrap:anywhere; font-size:14px; line-height:1.4; }
.lv-stage .script-copy b { font-size:15px; }
.lv-minor .script-copy b { font-size:13px; color:var(--text-2); }
.script-copy small { color:var(--text-3); font-size:11px; line-height:1.45; }
.script-tags { display:flex; flex-wrap:wrap; gap:5px; padding-left:22px; }
.script-tags .tag { padding:2px 7px; border-radius:4px; background:var(--fill); color:var(--text-3); font-size:10.5px; line-height:1.6; white-space:nowrap; }
.script-tags .tag.lv { background:var(--fill-strong); color:var(--text-2); }
.script-tags .tag.st-active { background:var(--blue-soft); color:var(--blue); }
.script-tags .tag.st-pending { background:var(--orange-soft); color:var(--orange); }
.script-tags .tag.st-completed { background:var(--ok-soft); color:var(--ok); }
.script-tags .tag.hot { background:var(--blue); color:var(--on-accent); }
.script-tags .tag.warn { background:var(--orange-soft); color:var(--orange); }
.script-bar { overflow:hidden; height:4px; margin-left:22px; border-radius:2px; background:var(--fill-strong); }
.script-bar i { display:block; height:100%; border-radius:inherit; background:var(--ok); transition:width .2s; }
.script-cond { margin:0 0 0 22px!important; display:-webkit-box; overflow:hidden; color:var(--text-3); font-size:11px!important; line-height:1.5; -webkit-box-orient:vertical; -webkit-line-clamp:2; }
.script-foot { display:flex; align-items:center; flex-wrap:wrap; gap:5px; margin-left:22px; padding-top:8px; border-top:1px solid var(--border); }
.script-gap { flex:1; min-width:0; }
.script-foot > button { display:grid; place-items:center; width:30px; height:30px; border-radius:6px; background:var(--fill); color:var(--text-2); }
.script-foot > button.ghost { width:auto; min-width:0; padding:0 9px; background:var(--fill-strong); font-size:11px; }
.script-foot > button.ghost.add { background:var(--blue-soft); color:var(--blue); }
.script-foot > button:disabled { opacity:.35; }
.script-backup-head { display:flex; align-items:center; gap:6px; min-height:36px; margin-top:5px; padding:0 4px; color:var(--text-3); font-size:11.5px; }
.script-backup-head > svg { transition:transform .16s; }
.script-backup-head.open > svg { transform:rotate(90deg); }
.empty { width:100%; padding:28px 0; color:var(--text-3); text-align:center; font-size:12.5px; }
.text-actions { display:flex; flex-wrap:wrap; width:100%; gap:6px; }.text-actions button:disabled { opacity:.4; }
.sync-state { display:flex; align-items:center; gap:6px; padding:8px 10px; border-radius:6px; background:var(--orange-soft); color:var(--orange); font-size:12px; }
.memory-list article { align-items:stretch; }.memory-list article.stale { border-color:var(--orange); }.memory-list textarea { width:100%; padding:8px; resize:vertical; }.memory-list .memory-preview { display:block; width:100%; overflow:hidden; padding:8px 4px; background:transparent; color:var(--text); font-size:14px; line-height:1.5; text-align:left; text-overflow:ellipsis; white-space:nowrap; }.memory-list article > div { display:flex; align-items:center; gap:7px; }.memory-list article > div select { min-height:28px; padding:0 6px; font-size:10.5px; }.memory-list article > div button { padding:5px 9px; border-radius:5px; background:var(--fill); font-size:11.5px; }.memory-list article button.on { color:var(--blue); background:var(--blue-soft); }.memory-list article span { flex:1; color:var(--orange); font-size:11px; }
.wide-actions,.correction-actions { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin-top:10px; }.wide-actions button { min-height:40px; }
.time-now { display:flex; flex-direction:column; gap:4px; padding:4px 2px 14px; }.time-now span { color:var(--text-3); font-size:12px; }.time-now strong { font-size:25px; letter-spacing:0; }.time-now small { color:var(--blue); }
.inline-input { display:flex; gap:6px; }.inline-input input { width:130px; padding:0 8px; }.inline-input button { padding:0 10px; border-radius:5px; background:var(--blue-soft); color:var(--blue); }
.correction { width:100%; min-height:42px; padding:0 10px; }.correction-actions { grid-template-columns:1fr; }.correction-actions button { min-height:39px; border-radius:6px; background:var(--fill-strong); color:var(--text-2); }
/* 遮罩、圆角、安全区内边距与升起动画都来自 components/ui/BottomSheet；
   .sheet 只剩「弹层内容的纵向栈」这一件事——11px 的统一行距、以及让
   .grow / .category-list / .refactor-list / .agent-waterfall 那些
   flex:1 + min-height:0 的滚动区仍有可撑开的父容器。 */
.sheet { display:flex; width:100%; min-height:0; flex-direction:column; gap:11px; }
/* 编辑器类弹层给一个固定高度，内容多少都不跳动。 */
.sheet.tall { height:min(88vh,760px); }
.sheet-head { display:flex; align-items:center; }.sheet-head > div { display:flex; flex:1; flex-direction:column; gap:2px; }.sheet-head > button { display:grid; place-items:center; width:36px; height:36px; }.sheet > label { display:flex; flex-direction:column; gap:5px; color:var(--text-3); font-size:11.5px; }.sheet > label input { min-height:40px; padding:0 9px; }.sheet label.grow { min-height:0; flex:1; }.sheet textarea { min-height:160px; flex:1; padding:9px; resize:none; line-height:1.6; }.sheet p { margin:0; color:var(--text-3); font-size:13px; line-height:1.65; }
.sheet-actions { display:flex; gap:8px; }.sheet-actions button { min-height:41px; flex:1; border-radius:6px; background:var(--fill-strong); color:var(--text-2); }.sheet-actions .primary { background:var(--blue); color:var(--on-accent); }.danger-fill { background:var(--danger)!important; color:var(--on-accent)!important; }
.sheet > label select { min-height:40px; padding:0 8px; }
.sheet > .field { min-height:0; padding:0; color:var(--text-3); font-size:11.5px; }
.refactor-sheet { overflow:hidden; }.refactor-summary { display:flex; flex-direction:column; gap:3px; padding:10px 0; border-top:1px solid var(--border); border-bottom:1px solid var(--border); }.refactor-summary span { color:var(--blue); font-size:11.5px; }.refactor-summary p { display:-webkit-box; overflow:hidden; -webkit-box-orient:vertical; -webkit-line-clamp:3; }
.refactor-command-sheet { overflow:hidden; }.refactor-command-sheet textarea { min-height:260px; }.execute-icon { color:var(--blue); }.execute-icon:disabled { opacity:.45; }.staged-list { margin-bottom:14px; }.staged-list .text-button { margin-left:auto; }
.refactor-list { min-height:0; flex:1; overflow-y:auto; }.refactor-list article { display:grid; grid-template-columns:24px minmax(0,1fr); gap:9px; padding:10px 2px; border-bottom:1px solid var(--border); }.refactor-list article > span { display:grid; place-items:center; width:22px; height:22px; border-radius:50%; background:var(--fill-strong); color:var(--text-3); font-size:10px; }.refactor-list article > div { display:flex; min-width:0; flex-direction:column; gap:3px; }.refactor-list article b,.refactor-list article small,.refactor-list article p { overflow-wrap:anywhere; }.refactor-list article p { display:-webkit-box; overflow:hidden; font-size:11.5px; -webkit-box-orient:vertical; -webkit-line-clamp:2; }
.bulk-setup-sheet { gap:14px; }.quantity-policy { display:flex; flex-direction:column; gap:7px; }.quantity-policy > span { color:var(--text-3); font-size:11.5px; }.quantity-options { display:grid; grid-template-columns:1fr 1fr; gap:8px; }.quantity-options button { display:flex; min-width:0; min-height:72px; flex-direction:column; align-items:flex-start; gap:4px; padding:10px; border:1px solid var(--border); border-radius:7px; color:var(--text-2); text-align:left; }.quantity-options button.active { border-color:var(--blue); background:var(--blue-soft); color:var(--blue); }.quantity-options small { color:var(--text-3); font-size:10.5px; line-height:1.45; }
.bulk-progress-sheet { overflow:hidden; gap:13px; }.bulk-progress-sheet .sheet-head > svg { color:var(--blue); animation:settings-spin .9s linear infinite; }.bulk-progress-sheet > p { overflow:hidden; color:var(--text); text-overflow:ellipsis; white-space:nowrap; }.bulk-progress { overflow:hidden; height:5px; flex-shrink:0; border-radius:3px; background:var(--fill-strong); }.bulk-progress i { display:block; height:100%; border-radius:inherit; background:var(--blue); transition:width .2s; }.agent-waterfall { min-height:0; flex:1; overflow-y:auto; padding:2px 1px; }.agent-waterfall article { position:relative; display:grid; grid-template-columns:24px minmax(0,1fr); gap:8px; padding:6px 0 10px; }.agent-waterfall article:not(:last-child)::before { position:absolute; top:25px; bottom:-4px; left:11px; width:1px; background:var(--border); content:''; }.waterfall-node { z-index:1; display:grid; place-items:center; width:23px; height:23px; border-radius:50%; background:var(--fill-strong); color:var(--text-3); }.agent-waterfall article.running .waterfall-node { background:var(--blue-soft); color:var(--blue); }.agent-waterfall article.error .waterfall-node { background:var(--danger-soft); color:var(--danger); }.waterfall-node > i { width:7px; height:7px; border-radius:50%; background:currentColor; animation:coomi-blink 1.2s ease-in-out infinite; }.agent-waterfall article > div { display:flex; min-width:0; flex-direction:column; gap:2px; }.agent-waterfall article small { overflow:hidden; color:var(--text-3); font-size:10.5px; text-overflow:ellipsis; white-space:nowrap; }.agent-waterfall article b { color:var(--text-2); font-size:12.5px; line-height:1.45; }.agent-waterfall article p { overflow-wrap:anywhere; font-size:11px; line-height:1.45; }
.search { min-height:40px; padding:0 10px; }.category-list { min-height:0; flex:1; overflow-y:auto; }.category-list article { padding:9px 0; border-bottom:1px solid var(--border); }.category-head,.word,.new-category { display:flex; gap:6px; margin-bottom:6px; }.category-head input,.word input,.new-category input { min-width:0; flex:1; min-height:36px; padding:0 8px; }.category-head button,.word button,.new-category button,.add-word { padding:0 9px; border-radius:5px; background:var(--fill); color:var(--text-3); font-size:11px; }.add-word { min-height:31px; }.warning-icon { display:grid; place-items:center; width:42px; height:42px; border-radius:50%; background:var(--danger-soft); color:var(--danger); }.stack-actions { display:grid; gap:8px; }.stack-actions button { min-height:42px; border-radius:6px; background:var(--fill-strong); color:var(--text-2); }
@keyframes settings-spin { to { transform:rotate(360deg); } }
@media (max-width:520px) {
  .material-section-head { flex-wrap:wrap; }
  .material-section-head > div { flex-basis:100%; }
  .material-section-head .bulk-refactor-action { margin-left:auto; }
}
</style>
