import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { authedFetch } from '@/bridge/http'
import { clampInt, pickBool, pickEnum, pickText } from '@/utils/validate'
import {
  createId, currentProjectRoot, deleteProjectFile, exportProjectContent, readProjectJson, readProjectText,
  safeFilename, writeProjectJson, writeProjectText,
} from '@/utils/projectFiles'
import {
  auditStoryTurn, createDefaultDirectorState, evaluateDirectorTurn, normalizeDirectorState,
  evidenceAppearsInContent,
  type DirectorDelta, type DirectorEvaluation, type DirectorPlan, type DirectorState, type StoryPace,
} from '@/story/directorMechanics'
import {
  createMajorBudgetSnapshot, DEFAULT_PLOT_MECHANICS, MAJOR_PHASE_LABELS, MAJOR_PHASES, minorTypeForTurn,
  normalizePlotMechanics, validatePlotMechanics,
  type CountRange, type MajorStoryPhase, type MinorStoryType, type PlotMechanicsSettings,
} from '@/story/plotMechanics'

export interface ProjectSettings {
  schemaVersion: 2
  recentFragments: number
  memoryCheckpoint: 5 | 10 | 15 | 20 | 30
  inferenceCycle: 10
  fortuneEnabled: boolean
  encounterEnabled: boolean
  encounterFrequency: 'restrained' | 'balanced' | 'active'
  eventEnabled: boolean
  characterEnabled: boolean
  characterGender: 'random' | 'male' | 'female'
  tragedyEnabled: boolean
  payoffEnabled: boolean
  directorEnabled: boolean
  storyPace: StoryPace
  majorHookEnabled: boolean
  /** Consecutive non-mainline turns before the next turn becomes a hard push. */
  stagnationWarningThreshold: number
  plotMechanics: PlotMechanicsSettings
}

export interface ManagedItem {
  id: string
  title: string
  filename: string
  enabled: boolean
  status?: 'active' | 'pending' | 'completed'
  completionCondition?: string
  defaultRoute?: string
  clock?: number
  clockMax?: number
  deadlineTurns?: number
  consequence?: string
  lastTickTurn?: number
  updatedAt: string
  formatVersion?: 1 | 2
  /**
   * 剧本层级：阶段 → 大剧情 → 小剧情。
   * 'stage' 只承载框架指导（目标写在 defaultRoute，阶段完成标志写在 completionCondition），
   * **不参与状态机**——推进逻辑仍然只认 major 的 5 阶段与 minor 的片段预算。
   */
  scriptType?: 'stage' | 'major' | 'minor'
  /** 两级父链：major.parentId → 所属 stage.id；minor.parentId → 所属 major.id。 */
  parentId?: string
  majorPhase?: MajorStoryPhase
  minorType?: MinorStoryType
  fragmentBudget?: CountRange
  proposed?: boolean
  path?: string
  refactoredTo?: string
}

export type MaterialKind = 'scripts' | 'presets'
export type MaterialRefactorMode = 'import' | 'existing'
export type MaterialRefactorQuantityMode = 'preserve' | 'auto'

export type MaterialRefactorProgressStage =
  | 'prepare'
  | 'agent'
  | 'validate'
  | 'backup'
  | 'write'
  | 'complete'

export interface MaterialRefactorProgress {
  stage: MaterialRefactorProgressStage
  label: string
  status: 'running' | 'done' | 'error'
  detail?: string
}

export interface MaterialRefactorOptions {
  quantityMode?: MaterialRefactorQuantityMode
  sourceItemCount?: number
  onProgress?: (progress: MaterialRefactorProgress) => void
}

export interface MaterialRefactorSource {
  kind: MaterialKind
  mode: MaterialRefactorMode
  title: string
  filename: string
  path: string
  itemId?: string
}

export interface RefactorPrompts {
  scriptImport: string
  scriptExisting: string
  presetImport: string
  presetExisting: string
}

const SCRIPT_IMPORT_PROMPT = `你是 Storydex 项目 Agent。请完整理解导入文本，在不擅自续写剧情、不改变核心人物和因果的前提下，把它整理为可供统一剧情控制系统使用的大剧情与小剧情。
请主动识别核心目标、持续阻力、完成条件和因果顺序，并根据原文内容安排合适的剧情阶段与节奏。无法从原文确认的信息可以保守概括，不要为了满足格式或数量而虚构空条目。`

const SCRIPT_EXISTING_PROMPT = `你是 Storydex 项目 Agent。请理解现有剧本并保留其核心设定、人物、因果、未完成承诺和可执行内容，修复阶段混乱、目标模糊、重复和无法收束的问题。
请按实际内容规划大剧情与小剧情，不得引入原文没有依据的重大事实，也不要为了满足格式或数量而制造内容。`

const PRESET_IMPORT_PROMPT = `你是 Storydex 项目 Agent。请理解导入内容，把其中有效的写作偏好整理成可直接约束小说正文的风格预设。风格只能控制表达，不得改写事实、剧情计划或玩家决定。
请保留原始审美意图，消除矛盾和越权内容；不必为了补齐固定栏目而编造要求。`

const PRESET_EXISTING_PROMPT = `你是 Storydex 项目 Agent。请理解并保留原预设的审美方向，整理其中有效的叙事视角、语言、句段密度、对话、节奏、描写重点和禁止项，消除矛盾、空泛描述及越权控制剧情的要求。
以实际内容为准，不必为了补齐固定栏目而制造约束。`

const DEFAULT_REFACTOR_PROMPTS: RefactorPrompts = {
  scriptImport: SCRIPT_IMPORT_PROMPT,
  scriptExisting: SCRIPT_EXISTING_PROMPT,
  presetImport: PRESET_IMPORT_PROMPT,
  presetExisting: PRESET_EXISTING_PROMPT,
}

function migrateRefactorPrompt(stored: string | undefined, fallback: string): string {
  const value = stored?.trim()
  if (!value || value.includes('只输出约定 JSON') || value.includes('不要输出解释或 Markdown')) return fallback
  return value
}

export interface ScriptRefactorMinorPreview {
  id: string
  title: string
  phase: MajorStoryPhase
  minorType: MinorStoryType
  fragmentBudget: CountRange
  objective: string
  opposition: string
  majorContribution: string
  content: string
}

export interface ScriptRefactorPreview {
  sourceId: string
  sourceTitle: string
  majorId: string
  majorTitle: string
  premise: string
  objective: string
  opposition: string
  completionCondition: string
  budget: ReturnType<typeof createMajorBudgetSnapshot>
  minors: ScriptRefactorMinorPreview[]
  warnings: string[]
}

interface ModelScriptRefactor {
  major?: {
    title?: string
    premise?: string
    objective?: string
    opposition?: string
    completionCondition?: string
  }
  minors?: Array<{
    title?: string
    majorPhase?: MajorStoryPhase
    minorType?: MinorStoryType
    objective?: string
    opposition?: string
    majorContribution?: string
    content?: string
  }>
}

interface ModelPresetItem {
  title?: string
  content?: string
}

interface ModelPresetRefactor extends ModelPresetItem {
  items?: ModelPresetItem[]
}

type ManagedItemDocument = {
  schemaVersion?: number
  items?: Array<Record<string, unknown>>
  entries?: Array<Record<string, unknown>>
}

export interface MemoryFact {
  id: string
  text: string
  locked: boolean
  stale: boolean
  sources: string[]
  scope: 'objective' | 'protagonist'
}

export interface TimeState {
  schemaVersion: 1
  calendar: 'gregorian' | 'relative' | 'custom'
  calendarName: string
  current: string
  display: string
  precision: 'fuzzy' | 'day' | 'hour'
  locked: boolean
  flashback: { active: boolean; at: string; returnTo: string } | null
  revisionSnapshots: Array<{ id: string; createdAt: string; from: string; to: string }>
}

export interface StoryStateDelta {
  advanced?: boolean
  timeDisplay?: string
  timeEvidence?: string
  memoryFacts?: Array<{ text: string; evidence?: string; sources?: string[]; scope?: 'objective' | 'protagonist' }>
  memoryOperations?: Array<{
    action: 'add' | 'update' | 'invalidate'
    id?: string
    text?: string
    evidence: string
    scope?: 'objective' | 'protagonist'
  }>
  scriptUpdates?: Array<{ id?: string; title?: string; status: 'active' | 'pending' | 'completed'; evidence?: string }>
  director?: DirectorDelta
}

const DEFAULT_SETTINGS: ProjectSettings = {
  schemaVersion: 2, recentFragments: 3, memoryCheckpoint: 10, inferenceCycle: 10,
  fortuneEnabled: true, encounterEnabled: false, encounterFrequency: 'balanced',
  eventEnabled: false, characterEnabled: false, characterGender: 'random',
  tragedyEnabled: false, payoffEnabled: false,
  directorEnabled: true, storyPace: 'balanced', majorHookEnabled: true,
  stagnationWarningThreshold: 3,
  plotMechanics: normalizePlotMechanics(DEFAULT_PLOT_MECHANICS),
}

/** 枚举字段的合法取值。写在一处，归一化与配置工具共用，不各留一份会漂移的清单。 */
const ENCOUNTER_FREQUENCIES: readonly ProjectSettings['encounterFrequency'][] = ['restrained', 'balanced', 'active']
const CHARACTER_GENDERS: readonly ProjectSettings['characterGender'][] = ['random', 'male', 'female']
const STORY_PACES: readonly StoryPace[] = ['deliberate', 'balanced', 'urgent']
const MEMORY_CHECKPOINTS: readonly ProjectSettings['memoryCheckpoint'][] = [5, 10, 15, 20, 30]
const MEMORY_SCOPES: readonly MemoryFact['scope'][] = ['objective', 'protagonist']
const SCRIPT_STATUSES: readonly NonNullable<ManagedItem['status']>[] = ['active', 'pending', 'completed']

/**
 * 把任意来源的设置收敛成合法值：磁盘上的旧文件、界面补丁、Agent 的配置工具。
 *
 * 这些夹取原先内联在 runInitialize 里，只覆盖「加载项目」一条路径；patchSettings
 * 只校验 plotMechanics，于是 17 个字段里有 15 个能被写进任意值——枚举写歪不会报错，
 * 只会让下游 `=== 'balanced'` 之类的判断全部落到 else 分支，表现为"设置没生效"。
 * 抽出来之后读盘与写入共用同一套规则。
 *
 * `migrateLegacy` 只在读盘时为真：encounterEnabled 是后加的字段，老文件里没有，
 * 要从 eventEnabled/characterEnabled 推出来。补丁路径上基线已经是正确值，再推一次
 * 会让"改别的字段"顺手把遭遇开关也改掉。
 */
function normalizeProjectSettings(
  incoming: Partial<ProjectSettings> | null | undefined,
  base: ProjectSettings,
  migrateLegacy = false,
): ProjectSettings {
  const source = incoming ?? {}
  const merged = { ...base, ...source }
  return {
    ...merged,
    schemaVersion: 2,
    inferenceCycle: 10,
    recentFragments: clampInt(merged.recentFragments, 1, 20, DEFAULT_SETTINGS.recentFragments),
    memoryCheckpoint: pickEnum(merged.memoryCheckpoint, MEMORY_CHECKPOINTS, DEFAULT_SETTINGS.memoryCheckpoint),
    fortuneEnabled: pickBool(merged.fortuneEnabled, DEFAULT_SETTINGS.fortuneEnabled),
    encounterEnabled: typeof source.encounterEnabled === 'boolean'
      ? source.encounterEnabled
      : migrateLegacy
        ? Boolean(source.eventEnabled || source.characterEnabled)
        : pickBool(merged.encounterEnabled, DEFAULT_SETTINGS.encounterEnabled),
    encounterFrequency: pickEnum(merged.encounterFrequency, ENCOUNTER_FREQUENCIES, DEFAULT_SETTINGS.encounterFrequency),
    eventEnabled: pickBool(merged.eventEnabled, DEFAULT_SETTINGS.eventEnabled),
    characterEnabled: pickBool(merged.characterEnabled, DEFAULT_SETTINGS.characterEnabled),
    characterGender: pickEnum(merged.characterGender, CHARACTER_GENDERS, DEFAULT_SETTINGS.characterGender),
    tragedyEnabled: pickBool(merged.tragedyEnabled, DEFAULT_SETTINGS.tragedyEnabled),
    payoffEnabled: pickBool(merged.payoffEnabled, DEFAULT_SETTINGS.payoffEnabled),
    directorEnabled: pickBool(merged.directorEnabled, DEFAULT_SETTINGS.directorEnabled),
    storyPace: pickEnum(merged.storyPace, STORY_PACES, DEFAULT_SETTINGS.storyPace),
    majorHookEnabled: pickBool(merged.majorHookEnabled, DEFAULT_SETTINGS.majorHookEnabled),
    stagnationWarningThreshold: clampInt(
      merged.stagnationWarningThreshold, 1, 20, DEFAULT_SETTINGS.stagnationWarningThreshold,
    ),
    plotMechanics: normalizePlotMechanics(merged.plotMechanics),
  }
}

export interface StoryConsistencyReason {
  type: 'chapter-edited' | 'manual-rebuild'
  fragmentPath?: string
  createdAt: string
}

export interface StoryConsistencyState {
  required: boolean
  updating: boolean
  reasons: StoryConsistencyReason[]
  affectedFrom: string
  lastUpdatedAt: string
  lastError: string
}

const DEFAULT_CONSISTENCY: StoryConsistencyState = {
  required: false,
  updating: false,
  reasons: [],
  affectedFrom: '',
  lastUpdatedAt: '',
  lastError: '',
}

type CommitPhase = 'prepared' | 'chapter_written' | 'director_written' | 'delta_written'

interface PendingCommit {
  schemaVersion: 2
  phase: CommitPhase
  preparedAt: string
  fragmentPath: string
  sourceMessageId: string
  planId: string
  nextState: DirectorState
  storyDelta?: StoryStateDelta
  evaluation: DirectorEvaluation
  plan: DirectorPlan
}

function semanticTerms(value: string): Set<string> {
  const compact = value.toLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]/g, '')
  const result = new Set<string>()
  for (const size of [4, 3, 2]) {
    for (let index = 0; index + size <= compact.length; index += 1) result.add(compact.slice(index, index + size))
  }
  return result
}

export function claimSupportedByEvidence(claim: string, evidence: string, content: string): boolean {
  const grounded = evidence.trim()
  if (!grounded || !evidenceAppearsInContent(content, grounded)) return false
  const claimTerms = semanticTerms(claim)
  const evidenceTerms = semanticTerms(grounded)
  if (claimTerms.size === 0) return false
  let overlap = 0
  for (const term of claimTerms) if (evidenceTerms.has(term)) overlap += 1
  return overlap >= Math.max(1, Math.ceil(Math.min(claimTerms.size, 12) * 0.2))
}

function conflictsWithLockedFact(claim: string, lockedFact: string): boolean {
  const left = semanticTerms(claim)
  const right = semanticTerms(lockedFact)
  let overlap = 0
  for (const term of left) if (right.has(term)) overlap += 1
  const negation = (value: string) => /(?:不|未|无|没有|并非|不是|从未)/.test(value)
  return overlap >= 2 && negation(claim) !== negation(lockedFact)
}

function protagonistKnowledgeGrounded(evidence: string): boolean {
  return /(?:看见|看到|听见|听到|得知|发现|收到|读到|告诉|告知|亲眼|注意到|认出|意识到|获悉|察觉)/.test(evidence)
}
const DEFAULT_TIME: TimeState = {
  schemaVersion: 1, calendar: 'relative', calendarName: '相对历', current: '1', display: '第1日',
  precision: 'day', locked: false, flashback: null, revisionSnapshots: [],
}

const CALENDARS: readonly TimeState['calendar'][] = ['gregorian', 'relative', 'custom']
const TIME_PRECISIONS: readonly TimeState['precision'][] = ['fuzzy', 'day', 'hour']

/**
 * 收敛时间状态。原先 patchTime 是无校验的直接展开，calendar / precision 写歪之后
 * 界面的分段控件会一个都不高亮（每个都 `=== ` 不中），看上去像"设置丢了"。
 *
 * 这里只做结构校验，不碰 locked 语义——锁的含义是界面上写明的"禁止模型自动修改"，
 * 由 applyStoryDelta 在自动落盘那一步把关；patchTime 是显式意图路径（用户点开关、
 * Agent 执行用户交代的配置），若在这里也拦一道，连"解锁"本身都会被自己拦住。
 */
function normalizeTimeState(
  incoming: Partial<TimeState> | null | undefined,
  base: TimeState,
): TimeState {
  const merged = { ...base, ...(incoming ?? {}) }
  const flashback = merged.flashback
  return {
    ...merged,
    schemaVersion: 1,
    calendar: pickEnum(merged.calendar, CALENDARS, DEFAULT_TIME.calendar),
    // calendarName 允许为空：界面是 v-model 直接改再 patch，若在这里兜回默认值，
    // 用户清空自定义历法名的瞬间会被弹回"相对历"。空历法名不影响任何结构。
    calendarName: typeof merged.calendarName === 'string' ? merged.calendarName.trim() : DEFAULT_TIME.calendarName,
    // current / display 是时间锚点：display 上界面标签，current 进提示词，空了就是坏的。
    current: pickText(merged.current, DEFAULT_TIME.current),
    display: pickText(merged.display, DEFAULT_TIME.display),
    precision: pickEnum(merged.precision, TIME_PRECISIONS, DEFAULT_TIME.precision),
    locked: pickBool(merged.locked, DEFAULT_TIME.locked),
    // 半个 flashback（active 为真但没有回归点）会让"返回主线"按钮无处可回。
    flashback: flashback && typeof flashback === 'object'
      ? { active: pickBool(flashback.active, false), at: pickText(flashback.at, ''), returnTo: pickText(flashback.returnTo, '') }
      : null,
    revisionSnapshots: Array.isArray(merged.revisionSnapshots)
      ? merged.revisionSnapshots.filter(item => item && typeof item === 'object' && typeof item.id === 'string')
      : [],
  }
}

type CollectionKind = 'presets' | 'scripts'

export function canApplyScriptStatus(
  current: ManagedItem['status'],
  next: NonNullable<ManagedItem['status']>,
  directorEnabled: boolean,
  controlAccepted: boolean,
  isPrimaryScript: boolean,
  planSatisfied: boolean,
  progressScore: number,
  completionEvidenceVerified: boolean,
): boolean {
  if (directorEnabled && !controlAccepted) return false
  if (current === next) return true
  if (current === 'pending' && next === 'active') return true
  return current === 'active'
    && next === 'completed'
    && isPrimaryScript
    && planSatisfied
    && progressScore >= 4
    && completionEvidenceVerified
}

/**
 * 剧本层级归一化。未知/缺失值一律落到 'major'，这样旧项目（没有 scriptType 字段）
 * 加载后行为与改动前完全一致，无需迁移脚本。
 */
export function normalizeScriptType(value: unknown): NonNullable<ManagedItem['scriptType']> {
  return value === 'minor' ? 'minor' : value === 'stage' ? 'stage' : 'major'
}

/**
 * 阶段条目故意不在此列：它只提供框架文本，没有 5 阶段状态机也没有 budgetSnapshot，
 * 因此不能交给导演做生命周期流转。
 */
export function scriptLifecycleManagedByDirector(item: Pick<ManagedItem, 'formatVersion' | 'scriptType'>): boolean {
  return item.formatVersion === 2 && (item.scriptType === 'major' || item.scriptType === 'minor')
}

export function materialCanBulkRefactor(kind: MaterialKind, item: ManagedItem): boolean {
  if (kind === 'presets') return true
  // 只有大剧情能被批量重构成小剧情；阶段是纯框架文本，小剧情已是叶子。
  return item.scriptType === 'major' && !item.refactoredTo
}

export function scriptMinorCountForRefactor(source: MaterialRefactorSource, items: ManagedItem[]): number | undefined {
  if (source.kind !== 'scripts' || !source.itemId) return undefined
  const count = items.filter(item => item.scriptType === 'minor' && item.parentId === source.itemId).length
  return count > 0 ? count : undefined
}

export const useProjectStore = defineStore('story-project', () => {
  const ready = ref(false)
  const error = ref('')
  const settings = ref<ProjectSettings>({ ...DEFAULT_SETTINGS })
  const presets = ref<ManagedItem[]>([])
  const scripts = ref<ManagedItem[]>([])
  const refactorPrompts = ref<RefactorPrompts>({ ...DEFAULT_REFACTOR_PROMPTS })
  const stagedMaterials = ref<MaterialRefactorSource[]>([])
  const memoryFacts = ref<MemoryFact[]>([])
  const memoryPending = ref(false)
  const consistency = ref<StoryConsistencyState>({ ...DEFAULT_CONSISTENCY })
  const time = ref<TimeState>({ ...DEFAULT_TIME })
  const directorState = ref<DirectorState>(createDefaultDirectorState())
  const pendingCommit = ref<PendingCommit | null>(null)
  let initializedProjectRoot = ''
  let initializePromise: Promise<void> | null = null
  const currentTimeLabel = computed(() => time.value.display || '第1日')
  const primaryScriptFocus = computed(() => {
    const boundId = directorState.value.activeArc?.majorScriptId
    const item = scripts.value.find(candidate => candidate.id === boundId && candidate.enabled
      && candidate.status !== 'completed' && normalizeScriptType(candidate.scriptType) !== 'stage')
      ?? scripts.value.find(candidate => candidate.enabled && candidate.status === 'active'
        && (candidate.scriptType ?? 'major') === 'major')
    const phase = directorState.value.activeArc?.phase ?? (settings.value.majorHookEnabled ? 'hook' : 'beginning')
    const activeMinorId = directorState.value.subArcs[0]?.minorScriptId
    const minor = item
      ? scripts.value.find(candidate => candidate.id === activeMinorId && candidate.parentId === item.id)
        ?? scripts.value.find(candidate => candidate.enabled && candidate.status === 'pending'
          && candidate.scriptType === 'minor' && candidate.parentId === item.id && candidate.majorPhase === phase)
      : undefined
    // 沿父链向上取所属阶段。阶段是可选的：parentId 为空的大剧情属于「未分组」，照常运行。
    const stage = item?.parentId
      ? scripts.value.find(candidate => candidate.id === item.parentId && candidate.scriptType === 'stage')
      : undefined
    return item ? {
      id: item.id,
      title: item.title,
      completionCondition: item.completionCondition ?? '',
      defaultRoute: item.defaultRoute ?? '',
      lifecycleManagedByDirector: scriptLifecycleManagedByDirector(item),
      path: item.path,
      ...(stage ? {
        stageScript: {
          id: stage.id,
          title: stage.title,
          objective: stage.defaultRoute ?? '',
          completionCondition: stage.completionCondition ?? '',
          path: stage.path,
        },
      } : {}),
      ...(minor ? {
        minorScript: {
          id: minor.id,
          title: minor.title,
          parentId: minor.parentId ?? item.id,
          majorPhase: minor.majorPhase ?? phase,
          minorType: minor.minorType ?? 'standard',
          objective: minor.defaultRoute ?? '',
          completionCondition: minor.completionCondition ?? '',
          fragmentBudget: { ...(minor.fragmentBudget ?? settings.value.plotMechanics.minorFragments[minor.minorType ?? 'standard']) },
          path: minor.path,
        },
      } : {}),
    } : undefined
  })

  async function runInitialize(projectRoot: string) {
    error.value = ''
    try {
      const firstOpenForProject = initializedProjectRoot !== projectRoot
      if (firstOpenForProject) {
        await Promise.all([
          deleteProjectFile('.storydex/temp/temp_scripts').catch(() => {}),
          deleteProjectFile('.storydex/temp/temp_presets').catch(() => {}),
        ])
        stagedMaterials.value = []
      }
      const storedSettings = await readProjectJson<Partial<ProjectSettings>>('.storydex/settings.json') ?? {}
      settings.value = normalizeProjectSettings(storedSettings, DEFAULT_SETTINGS, true)
      presets.value = await loadCollection('presets')
      scripts.value = await loadCollection('scripts')
      const storedRefactorPrompts = await readProjectJson<Partial<RefactorPrompts>>('.storydex/refactor-prompts.json') ?? {}
      refactorPrompts.value = {
        scriptImport: migrateRefactorPrompt(storedRefactorPrompts.scriptImport, SCRIPT_IMPORT_PROMPT),
        scriptExisting: migrateRefactorPrompt(storedRefactorPrompts.scriptExisting, SCRIPT_EXISTING_PROMPT),
        presetImport: migrateRefactorPrompt(storedRefactorPrompts.presetImport, PRESET_IMPORT_PROMPT),
        presetExisting: migrateRefactorPrompt(storedRefactorPrompts.presetExisting, PRESET_EXISTING_PROMPT),
      }
      const memory = await readProjectJson<{
        facts?: MemoryFact[]
        pendingSync?: boolean
        consistency?: Partial<StoryConsistencyState>
      }>('.storydex/memory/state.json')
      memoryFacts.value = (memory?.facts ?? []).map(fact => ({ ...fact, scope: fact.scope ?? 'objective' }))
      memoryPending.value = memory?.pendingSync ?? false
      consistency.value = {
        ...DEFAULT_CONSISTENCY,
        ...(memory?.consistency ?? {}),
        updating: false,
        reasons: Array.isArray(memory?.consistency?.reasons) ? memory.consistency.reasons.slice(-50) : [],
      }
      if (memoryPending.value && !consistency.value.required) {
        consistency.value.required = true
        consistency.value.reasons = [{ type: 'manual-rebuild', createdAt: new Date().toISOString() }]
      }
      time.value = normalizeTimeState(await readProjectJson<Partial<TimeState>>('.storydex/time/state.json'), DEFAULT_TIME)
      let loadedDirector = normalizeDirectorState(
        await readProjectJson<Partial<DirectorState>>('.storydex/director/state.json'),
      )
      const pendingDirector = await readProjectJson<PendingCommit>('.storydex/director/pending-commit.json').catch(() => null)
      pendingCommit.value = pendingDirector?.nextState && pendingDirector.evaluation && pendingDirector.plan
        ? { ...pendingDirector, schemaVersion: 2, phase: pendingDirector.phase ?? 'prepared' }
        : null
      const pendingFragmentPath = typeof pendingDirector?.fragmentPath === 'string'
        && pendingDirector.fragmentPath.startsWith('chapters/')
        ? pendingDirector.fragmentPath : ''
      let recoveredFragment = ''
      if (pendingDirector?.nextState && pendingFragmentPath) {
        const pendingState = normalizeDirectorState(pendingDirector.nextState)
        const fragmentExists = await readProjectText(pendingFragmentPath).catch(() => null)
        if (fragmentExists != null) {
          recoveredFragment = fragmentExists
          if (pendingState.revision > loadedDirector.revision) loadedDirector = pendingState
        }
      }
      directorState.value = loadedDirector
      if (recoveredFragment && pendingDirector?.storyDelta) {
        await applyStoryDelta(
          pendingDirector.storyDelta,
          pendingDirector.evaluation,
          pendingDirector.plan,
          recoveredFragment,
          pendingFragmentPath,
        )
      }
      if (pendingDirector) await deleteProjectFile('.storydex/director/pending-commit.json').catch(() => {})
      pendingCommit.value = null
      await Promise.all([
        writeProjectJson('.storydex/project.json', { schemaVersion: 1, updatedAt: new Date().toISOString() }),
        saveSettings(), saveCollection('presets'), saveCollection('scripts'), saveRefactorPrompts(), saveMemory(), saveTime(), saveDirector(),
      ])
      initializedProjectRoot = projectRoot
      ready.value = true
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
    }
  }

  async function saveSettings() { await writeProjectJson('.storydex/settings.json', settings.value) }
  async function saveRefactorPrompts() { await writeProjectJson('.storydex/refactor-prompts.json', refactorPrompts.value) }
  async function patchSettings(patch: Partial<ProjectSettings>) {
    const next = normalizeProjectSettings(patch, settings.value)
    // 归一化只保证"值合法"，plotMechanics 还有跨字段约束（配额之间、与 majorHookEnabled
    // 之间）。这类冲突不能静默夹取——夹完就不是用户要的配置了——所以照旧抛给调用方。
    if (patch.plotMechanics) {
      const errors = validatePlotMechanics(next.plotMechanics, next.majorHookEnabled)
      if (errors.length) throw new Error(errors[0])
    }
    settings.value = next
    await saveSettings()
  }

  async function applyPlotSettingsToCurrent() {
    if (!directorState.value.activeArc) return false
    const active = directorState.value.activeArc
    const completed = active.phaseMinorCompleted ?? { hook: 0, beginning: 0, development: 0, climax: 0, ending: 0 }
    const snapshot = createMajorBudgetSnapshot(
      settings.value.plotMechanics,
      settings.value.majorHookEnabled,
      directorState.value.revision + directorState.value.turnIndex + 1,
    )
    for (const phase of Object.keys(completed) as MajorStoryPhase[]) {
      snapshot.phaseTargets[phase] = Math.max(snapshot.phaseTargets[phase], completed[phase])
      snapshot.phaseRanges[phase].max = Math.max(snapshot.phaseRanges[phase].max, completed[phase])
    }
    if (directorState.value.subArcs.length === 0
      && snapshot.phaseTargets[active.phase] <= completed[active.phase]) {
      snapshot.phaseTargets[active.phase] = completed[active.phase] + 1
      snapshot.phaseRanges[active.phase].max = Math.max(
        snapshot.phaseRanges[active.phase].max,
        snapshot.phaseTargets[active.phase],
      )
    }
    snapshot.totalTarget = Math.max(snapshot.totalTarget, Object.values(completed).reduce((sum, value) => sum + value, 0))
    snapshot.totalTarget = Math.max(snapshot.totalTarget, Object.values(snapshot.phaseTargets).reduce((sum, value) => sum + value, 0))
    active.budgetSnapshot = snapshot
    await saveDirector()
    return true
  }

  function collection(kind: CollectionKind) { return kind === 'presets' ? presets : scripts }
  function stringField(raw: Record<string, unknown>, ...keys: string[]): string {
    for (const key of keys) {
      const value = raw[key]
      if (typeof value === 'string' && value.trim()) return value.trim()
    }
    return ''
  }
  function leafFilename(value: string): string {
    const normalized = value.replace(/\\/g, '/')
    const leaf = normalized.split('/').pop() ?? ''
    return leaf && leaf !== '.' && leaf !== '..' ? safeFilename(leaf) : ''
  }
  async function loadCollection(kind: CollectionKind): Promise<ManagedItem[]> {
    const document = await readProjectJson<ManagedItemDocument>(`.storydex/${kind}/index.json`)
    const records = document?.items?.length ? document.items : (document?.entries ?? document?.items ?? [])
    const result: ManagedItem[] = []
    for (const [index, raw] of records.entries()) {
      if (!raw || typeof raw !== 'object') continue
      const id = stringField(raw, 'id', 'key') || createId(kind === 'presets' ? 'preset' : 'script')
      const inlineContent = stringField(raw, 'content', 'prompt', 'body', 'text', 'instructions', 'description')
      const rawFilename = stringField(raw, 'filename', 'file', 'path', 'relativePath', 'contentFile', 'content_file')
      let filename = leafFilename(rawFilename)
      const storedPath = stringField(raw, 'path', 'relativePath')
      let title = stringField(raw, 'title', 'name', 'label', 'presetName', 'scriptName')
      if (!title && filename) title = filename.replace(/\.(?:md|markdown|txt)$/i, '')
      title ||= kind === 'presets' ? `未命名预设 ${index + 1}` : `未命名剧本 ${index + 1}`
      if (!filename) filename = `${safeFilename(title)}-${id.slice(-8)}.md`

      const relativePath = storedPath.startsWith(`.storydex/${kind}/`) ? storedPath : `.storydex/${kind}/${filename}`
      const existing = await readProjectText(relativePath)
      if (existing == null && inlineContent) {
        await writeProjectText(`.storydex/${kind}/${filename}`, `${inlineContent}\n`)
      }
      const enabledValue = raw.enabled ?? raw.active ?? (typeof raw.disabled === 'boolean' ? !raw.disabled : undefined)
      const statusValue = stringField(raw, 'status')
      const status = ['active', 'pending', 'completed'].includes(statusValue)
        ? statusValue as ManagedItem['status']
        : (kind === 'scripts' ? 'active' : undefined)
      result.push({
        id,
        title,
        filename,
        enabled: typeof enabledValue === 'boolean' ? enabledValue : true,
        status,
        completionCondition: kind === 'scripts'
          ? stringField(raw, 'completionCondition', 'completion_condition', 'condition', 'goal')
          : undefined,
        defaultRoute: kind === 'scripts'
          ? stringField(raw, 'defaultRoute', 'default_route', 'route')
          : undefined,
        clock: kind === 'scripts' ? Math.max(0, Number(raw.clock) || 0) : undefined,
        clockMax: kind === 'scripts' ? Math.min(20, Math.max(2, Number(raw.clockMax) || 4)) : undefined,
        deadlineTurns: kind === 'scripts' ? Math.min(100, Math.max(1, Number(raw.deadlineTurns) || 4)) : undefined,
        consequence: kind === 'scripts' ? stringField(raw, 'consequence', 'deadlineConsequence') : undefined,
        lastTickTurn: kind === 'scripts' ? Math.max(0, Number(raw.lastTickTurn) || 0) : undefined,
        updatedAt: stringField(raw, 'updatedAt', 'updated_at') || new Date().toISOString(),
        formatVersion: Number(raw.formatVersion) === 2 ? 2 : 1,
        scriptType: kind === 'scripts' ? normalizeScriptType(raw.scriptType) : undefined,
        parentId: kind === 'scripts' ? stringField(raw, 'parentId') || undefined : undefined,
        majorPhase: kind === 'scripts' && ['hook', 'beginning', 'development', 'climax', 'ending'].includes(String(raw.majorPhase))
          ? raw.majorPhase as MajorStoryPhase : undefined,
        minorType: kind === 'scripts' && ['quick', 'standard', 'focus'].includes(String(raw.minorType))
          ? raw.minorType as MinorStoryType : undefined,
        fragmentBudget: kind === 'scripts' && raw.fragmentBudget && typeof raw.fragmentBudget === 'object'
          ? raw.fragmentBudget as CountRange : undefined,
        proposed: raw.proposed === true,
        path: relativePath,
        refactoredTo: kind === 'scripts' ? stringField(raw, 'refactoredTo') || undefined : undefined,
      })
    }
    return result
  }
  async function saveCollection(kind: CollectionKind) {
    await writeProjectJson(`.storydex/${kind}/index.json`, { schemaVersion: 2, items: collection(kind).value })
  }
  /**
   * 校验并归一化三级归属：大剧情只能挂阶段，小剧情只能挂大剧情，阶段是最上层。
   *
   * 新建与改挂共用一条规则。此前只有 setScriptParent 做校验，addItem 直接把
   * options.parentId 写进条目，于是"新建"能造出"改挂"会拒绝的结构（小剧情挂到阶段上、
   * parentId 指向不存在的条目），加载时又只归一化 scriptType 不校验父链，坏结构会一直留着。
   *
   * 返回值 undefined 表示"未分组"——允许没有阶段就先写大剧情，这是迁移期的正常状态。
   */
  function resolveScriptParent(
    type: NonNullable<ManagedItem['scriptType']>,
    parentId: string | undefined,
    selfId?: string,
  ): string | undefined {
    const nextId = (parentId ?? '').trim()
    if (type === 'stage') {
      if (nextId) throw new Error('阶段是最上层条目，不能挂到其他条目下')
      return undefined
    }
    if (!nextId) return undefined
    if (selfId && nextId === selfId) throw new Error('条目不能挂到自己下面')
    const parent = scripts.value.find(candidate => candidate.id === nextId)
    if (!parent) throw new Error('未找到指定的父条目')
    const parentType = normalizeScriptType(parent.scriptType)
    if (type === 'major' && parentType !== 'stage') throw new Error('大剧情只能挂在阶段下')
    if (type === 'minor' && parentType !== 'major') throw new Error('小剧情只能挂在大剧情下')
    return nextId
  }
  async function addItem(
    kind: CollectionKind,
    title: string,
    content: string,
    completionCondition = '',
    defaultRoute = '',
    options: { scriptType?: NonNullable<ManagedItem['scriptType']>, parentId?: string } = {},
  ) {
    const id = createId(kind === 'presets' ? 'preset' : 'script')
    const filename = `${safeFilename(title)}-${id.slice(-8)}.md`
    const scriptType = kind === 'scripts' ? normalizeScriptType(options.scriptType ?? 'major') : undefined
    // 先校验再落盘：父链非法时不该留下一个孤儿 md 文件。
    const parentId = scriptType ? resolveScriptParent(scriptType, options.parentId) : undefined
    const item: ManagedItem = {
      id, title: title.trim() || (kind === 'presets' ? '未命名预设' : '未命名剧本'), filename,
      enabled: true, status: kind === 'scripts' ? 'active' : undefined,
      completionCondition: kind === 'scripts' ? completionCondition.trim() : undefined,
      defaultRoute: kind === 'scripts' ? defaultRoute.trim() : undefined,
      clock: kind === 'scripts' ? 0 : undefined,
      clockMax: kind === 'scripts' ? 4 : undefined,
      deadlineTurns: kind === 'scripts' ? 4 : undefined,
      consequence: kind === 'scripts' ? `${title.trim() || '该背景剧本'}未及时处理，将主动产生后果` : undefined,
      lastTickTurn: kind === 'scripts' ? 0 : undefined,
      formatVersion: 1,
      scriptType,
      parentId,
      path: `.storydex/${kind}/${filename}`,
      updatedAt: new Date().toISOString(),
    }
    await writeProjectText(`.storydex/${kind}/${filename}`, content.trim() + '\n')
    collection(kind).value.push(item)
    await saveCollection(kind)
    return item
  }
  async function updateItem(kind: CollectionKind, item: ManagedItem, title: string, content: string, completionCondition = '', defaultRoute = '') {
    item.title = title.trim() || item.title
    item.completionCondition = kind === 'scripts' ? completionCondition.trim() : undefined
    item.defaultRoute = kind === 'scripts' ? defaultRoute.trim() : undefined
    if (kind === 'presets') item.formatVersion = 1
    item.updatedAt = new Date().toISOString()
    await writeProjectText(item.path ?? `.storydex/${kind}/${item.filename}`, content.trim() + '\n')
    await saveCollection(kind)
  }
  async function readItem(kind: CollectionKind, item: ManagedItem) {
    return await readProjectText(item.path ?? `.storydex/${kind}/${item.filename}`) ?? ''
  }
  async function toggleItem(kind: CollectionKind, item: ManagedItem) {
    if (kind === 'scripts' && item.refactoredTo) return
    item.enabled = !item.enabled
    item.updatedAt = new Date().toISOString()
    await saveCollection(kind)
  }
  async function renameItem(kind: CollectionKind, item: ManagedItem, title: string) {
    item.title = title.trim() || item.title
    item.updatedAt = new Date().toISOString()
    await saveCollection(kind)
  }
  async function removeItem(kind: CollectionKind, item: ManagedItem) {
    await deleteProjectFile(item.path ?? `.storydex/${kind}/${item.filename}`)
    collection(kind).value = collection(kind).value.filter(candidate => candidate.id !== item.id)
    // 删掉阶段后，它下面的大剧情回落到「未分组」，而不是留下指向已删条目的悬空 parentId。
    if (kind === 'scripts' && item.scriptType === 'stage') {
      for (const child of scripts.value) {
        if (child.parentId === item.id) {
          child.parentId = undefined
          child.updatedAt = new Date().toISOString()
        }
      }
    }
    await saveCollection(kind)
  }
  async function moveItem(kind: CollectionKind, item: ManagedItem, direction: -1 | 1) {
    const list = collection(kind).value
    const from = list.findIndex(candidate => candidate.id === item.id)
    const to = from + direction
    if (from < 0 || to < 0 || to >= list.length) return
    list.splice(to, 0, list.splice(from, 1)[0])
    await saveCollection(kind)
  }
  /**
   * 在**同级兄弟**之间上移 / 下移剧本条目。
   *
   * 扁平数组的顺序就是优先级顺序（`primaryScriptFocus` 取第一个进行中的大剧情，其后最多
   * 两个降级成背景时钟），树形界面并不重排它，只表达归属。但树里视觉相邻的两个兄弟在扁平
   * 数组里往往并不相邻——中间夹着别的条目——所以不能用 moveItem 那种「与前一个元素交换」：
   * 那会把某个大剧情插进别人的子树，或者在界面上看起来一动不动（换到的位置不是兄弟）。
   *
   * 这里先沿数组找到同级同父的前 / 后一个兄弟，再交换两者位置：同级相对顺序按预期改变，
   * 其余所有条目的相对顺序保持不变。
   */
  async function moveScriptSibling(item: ManagedItem, direction: -1 | 1) {
    const list = scripts.value
    const type = normalizeScriptType(item.scriptType)
    const parentId = item.parentId || undefined
    const from = list.findIndex(candidate => candidate.id === item.id)
    if (from < 0) return
    for (let cursor = from + direction; cursor >= 0 && cursor < list.length; cursor += direction) {
      const sibling = list[cursor]
      if (sibling.refactoredTo) continue
      if (normalizeScriptType(sibling.scriptType) !== type) continue
      if ((sibling.parentId || undefined) !== parentId) continue
      list[from] = sibling
      list[cursor] = item
      item.updatedAt = new Date().toISOString()
      await saveCollection('scripts')
      return
    }
  }
  /**
   * 把当前所有「未分组」的大剧情一次收进一个新建阶段。
   *
   * 三级结构之前的项目只有大剧情和小剧情，parentId 全为空。手工补齐要先建阶段再逐个改挂，
   * 在 8 个大剧情的项目上是 9 次交互，中途失败还会留下一半分组的状态。这里做成一步：
   * 阶段建成后统一改挂，最后只落一次盘。
   *
   * 只收 major：小剧情的父级是大剧情，本来就不该直接挂到阶段上（resolveScriptParent 会拒）。
   */
  async function groupUngroupedMajorsIntoStage(title: string) {
    const orphans = scripts.value.filter(item => !item.refactoredTo
      && normalizeScriptType(item.scriptType) === 'major' && !item.parentId)
    if (!orphans.length) throw new Error('当前没有未分组的大剧情')
    const stageTitle = title.trim() || '第一阶段'
    const body = [
      `# ${stageTitle}`,
      '',
      '## 阶段目标',
      '（这一阶段整体要走向哪里、边界在哪）',
      '',
      '## 阶段完成标志',
      '（达成什么之后可以进入下一阶段）',
      '',
      '## 包含的大剧情',
      ...orphans.map(item => `- ${item.title}`),
    ].join('\n')
    const stage = await addItem('scripts', stageTitle, body, '', '', { scriptType: 'stage' })
    const now = new Date().toISOString()
    for (const orphan of orphans) {
      orphan.parentId = stage.id
      orphan.updatedAt = now
    }
    await saveCollection('scripts')
    return stage
  }
  async function exportItem(kind: CollectionKind, item: ManagedItem) {
    const content = await readItem(kind, item)
    await exportProjectContent(item.path ?? `.storydex/${kind}/${item.filename}`, content, item.filename)
  }
  /**
   * 手动改剧本状态（界面上的「标记完成 / 撤销完成 / 待处理」）。
   *
   * 这里刻意**不**走 canApplyScriptStatus：那道闸门是给模型自动落盘用的（见 :985 的注释
   * "A model cannot reopen a completed route"），completed → active 本就被它拒绝，
   * 而"撤销完成"正是这条边。人工与受托于人工的配置调用属于显式意图，只做结构校验。
   */
  async function markScript(item: ManagedItem, status: 'active' | 'pending' | 'completed') {
    if (!SCRIPT_STATUSES.includes(status)) throw new Error(`未知的剧本状态：${status}`)
    // 已重构走的条目由 refactoredTo 指向新条目，改它的状态只会让两边不一致。
    // 界面用 v-if 挡掉了按钮，能走到这里的只有外部调用，需要明确报错而不是静默返回。
    if (item.refactoredTo) throw new Error('该剧本已被重构替换，请改动接替它的新条目')
    item.status = status
    item.updatedAt = new Date().toISOString()
    await saveCollection('scripts')
  }
  /**
   * 改挂剧本层级归属：大剧情 → 阶段，小剧情 → 大剧情。
   *
   * 层级规则本身在 resolveScriptParent 里，与新建共用；这里只多一条改挂特有的约束
   * （已完成的小剧情不能换爹）。传空字符串表示回落到「未分组」。
   */
  async function setScriptParent(item: ManagedItem, parentId: string) {
    const type = normalizeScriptType(item.scriptType)
    const nextId = resolveScriptParent(type, parentId, item.id)
    // 已完成的小剧情已经计入原大剧情的阶段完成数，改挂会让两边计数都失真。
    if (type === 'minor' && item.status === 'completed' && nextId !== item.parentId) {
      throw new Error('已完成的小剧情不能改挂到别的大剧情下')
    }
    if (item.parentId === nextId) return
    item.parentId = nextId
    item.updatedAt = new Date().toISOString()
    await saveCollection('scripts')
  }

  async function saveMemory() {
    await writeProjectJson('.storydex/memory/state.json', {
      schemaVersion: 2,
      pendingSync: memoryPending.value,
      consistency: consistency.value,
      facts: memoryFacts.value,
      updatedAt: new Date().toISOString(),
    })
  }
  async function addMemoryFact(text: string, scope: MemoryFact['scope'] = 'objective') {
    const trimmed = text.trim()
    // 空事实会被原样拼进每一轮的系统提示词，只占预算不带信息。
    if (!trimmed) throw new Error('记忆事实内容不能为空')
    const fact: MemoryFact = {
      id: createId('fact'),
      text: trimmed,
      locked: false,
      stale: false,
      sources: [],
      scope: pickEnum(scope, MEMORY_SCOPES, 'objective'),
    }
    memoryFacts.value.push(fact)
    await saveMemory()
    // 返回新事实：界面不需要（它只看列表），但外部调用方要拿到 id 才能接着补来源或回报结果。
    return fact
  }
  /**
   * 按 id 改一条记忆事实。界面上是直接改 fact 对象再调 saveMemory（模板里的 select /
   * 锁定开关只能产出合法值，改不出脏数据），外部调用方没有那条通路，需要一个带校验的入口。
   *
   * 不拦 locked：锁的含义是界面写明的"禁止模型自动修改"，由 applyStoryDelta 在自动
   * 落盘那一步把关（见 :905 与 :916）。这里是显式意图路径，若在此也拦一道，用户/Agent
   * 连"解锁"这个动作本身都做不到。
   */
  async function updateMemoryFact(id: string, patch: Partial<Pick<MemoryFact, 'text' | 'scope' | 'locked' | 'stale'>>) {
    const fact = memoryFacts.value.find(candidate => candidate.id === id)
    if (!fact) throw new Error('未找到指定的记忆事实')
    if (patch.text !== undefined) {
      const trimmed = patch.text.trim()
      if (!trimmed) throw new Error('记忆事实内容不能为空')
      fact.text = trimmed
    }
    if (patch.scope !== undefined) fact.scope = pickEnum(patch.scope, MEMORY_SCOPES, fact.scope)
    if (patch.locked !== undefined) fact.locked = pickBool(patch.locked, fact.locked)
    if (patch.stale !== undefined) fact.stale = pickBool(patch.stale, fact.stale)
    await saveMemory()
  }
  async function removeMemoryFact(fact: MemoryFact) {
    memoryFacts.value = memoryFacts.value.filter(candidate => candidate.id !== fact.id)
    await saveMemory()
  }
  async function saveTime() { await writeProjectJson('.storydex/time/state.json', time.value) }
  async function saveDirector() { await writeProjectJson('.storydex/director/state.json', directorState.value) }
  async function patchTime(patch: Partial<TimeState>) {
    time.value = normalizeTimeState(patch, time.value)
    await saveTime()
  }
  async function createTimeRevision(next: string) {
    const target = next.trim()
    // 空显示值会让界面的时间标签整块消失，而修订快照又记下了一次"改成空"的历史。
    if (!target) throw new Error('时间显示值不能为空')
    if (target === time.value.display) return
    time.value.revisionSnapshots.push({ id: createId('time-revision'), createdAt: new Date().toISOString(), from: time.value.display, to: target })
    time.value.display = target
    await saveTime()
  }
  async function applyStoryDelta(
    delta: StoryStateDelta,
    directorEvaluation?: DirectorEvaluation | null,
    directorPlan?: DirectorPlan | null,
    content = '',
    fragmentPath = '',
  ) {
    if (!delta.advanced || (settings.value.directorEnabled && (!directorEvaluation?.accepted || !directorEvaluation.planSatisfied))) return
    const memoryOperations: NonNullable<StoryStateDelta['memoryOperations']> = [
      ...(delta.memoryFacts ?? []).map(incoming => ({
        action: 'add' as const, id: undefined, text: incoming.text, evidence: incoming.evidence ?? '', scope: incoming.scope,
      })),
      ...(delta.memoryOperations ?? []),
    ]
    for (const operation of memoryOperations) {
      const evidence = operation.evidence?.trim() ?? ''
      const existing = operation.id ? memoryFacts.value.find(fact => fact.id === operation.id) : undefined
      if (operation.action === 'invalidate') {
        if (!existing || existing.locked || !claimSupportedByEvidence(existing.text, evidence, content)) continue
        existing.stale = true
        if (fragmentPath && !existing.sources.includes(fragmentPath)) existing.sources.push(fragmentPath)
        continue
      }
      const text = operation.text?.trim() ?? ''
      const scope = operation.scope ?? existing?.scope ?? 'objective'
      if (!text || !claimSupportedByEvidence(text, evidence, content)) continue
      if (scope === 'protagonist' && !protagonistKnowledgeGrounded(evidence)) continue
      if (memoryFacts.value.some(fact => fact.locked && !fact.stale && fact.id !== existing?.id && conflictsWithLockedFact(text, fact.text))) continue
      if (operation.action === 'update') {
        if (!existing || existing.locked) continue
        existing.text = text
        existing.scope = scope
        existing.stale = false
        if (fragmentPath && !existing.sources.includes(fragmentPath)) existing.sources.push(fragmentPath)
        continue
      }
      if (memoryFacts.value.some(fact => fact.text === text && !fact.stale)) continue
      memoryFacts.value.push({
        id: createId('fact'), text, locked: false, stale: false,
        sources: fragmentPath ? [fragmentPath] : [], scope,
      })
    }
    for (const update of delta.scriptUpdates ?? []) {
      const item = scripts.value.find(candidate => candidate.id === update.id || candidate.title === update.title)
      if (!item) continue
      // Standard v2 scripts are a projection of the validated director state.
      // The model may report evidence, but cannot directly change their lifecycle.
      if (scriptLifecycleManagedByDirector(item)) continue
      // Script lifecycle is monotonic. A model cannot reopen a completed route
      // or jump a pending route directly to completed without the director
      // validator having accepted a milestone/resolution in the same turn.
      const allowed = canApplyScriptStatus(
        item.status,
        update.status,
        settings.value.directorEnabled,
        directorEvaluation?.accepted === true,
        directorPlan?.scriptFocus?.id === item.id,
        directorEvaluation?.planSatisfied === true,
        directorEvaluation?.progressScore ?? 0,
        typeof update.evidence === 'string'
          && claimSupportedByEvidence(item.completionCondition || item.title, update.evidence, content),
      )
      if (allowed) { item.status = update.status; item.updatedAt = new Date().toISOString() }
    }
    const timeEvidence = delta.timeEvidence?.trim() ?? ''
    if (delta.timeDisplay?.trim() && !time.value.locked
      && evidenceAppearsInContent(content, timeEvidence)
      && /(?:日|天|时|刻|分|秒|晨|午|夜|翌|次日|当晚|月|年|季)/.test(timeEvidence)) {
      time.value.display = delta.timeDisplay.trim()
    }
    // A normal story turn may update incremental memory, but it must never
    // unlock a project whose historical chapters were edited. Only the
    // explicit consistency rebuild may clear that lock.
    if (!consistency.value.required) memoryPending.value = false
    syncScriptLifecycle(directorEvaluation?.nextState ?? directorState.value)
    await Promise.all([saveMemory(), saveCollection('scripts'), saveTime()])
  }

  function promptKey(kind: MaterialKind, mode: MaterialRefactorMode): keyof RefactorPrompts {
    if (kind === 'scripts') return mode === 'import' ? 'scriptImport' : 'scriptExisting'
    return mode === 'import' ? 'presetImport' : 'presetExisting'
  }

  async function updateRefactorPrompt(kind: MaterialKind, mode: MaterialRefactorMode, prompt: string) {
    const normalized = prompt.trim()
    if (normalized.length < 40) throw new Error('Agent 任务说明过短，无法准确理解整理目标')
    refactorPrompts.value[promptKey(kind, mode)] = normalized
    await saveRefactorPrompts()
  }

  async function stageImportedMaterial(kind: MaterialKind, filename: string, content: string): Promise<MaterialRefactorSource> {
    if (!content.trim()) throw new Error('导入文件没有可读取的文本内容')
    if (content.length > 500_000) throw new Error('单个导入文件不得超过 500000 个字符')
    const id = createId(kind === 'scripts' ? 'temp-script' : 'temp-preset')
    const title = filename.replace(/\.[^.]+$/, '').trim() || (kind === 'scripts' ? '导入剧本' : '导入风格')
    const storedName = `${safeFilename(title)}-${id.slice(-8)}.txt`
    const directory = kind === 'scripts' ? 'temp_scripts' : 'temp_presets'
    const path = `.storydex/temp/${directory}/${storedName}`
    await writeProjectText(path, content)
    const source: MaterialRefactorSource = { kind, mode: 'import', title, filename, path }
    stagedMaterials.value.push(source)
    return source
  }

  function existingMaterialSource(kind: MaterialKind, item: ManagedItem): MaterialRefactorSource {
    return {
      kind,
      mode: 'existing',
      title: item.title,
      filename: item.filename,
      path: item.path ?? `.storydex/${kind}/${item.filename}`,
      itemId: item.id,
    }
  }

  function scriptPreviewFromModel(source: MaterialRefactorSource, result: ModelScriptRefactor): ScriptRefactorPreview {
    const major = result.major && typeof result.major === 'object' ? result.major : {}
    const sourceItem = source.itemId ? scripts.value.find(item => item.id === source.itemId) : undefined
    const majorTitle = major.title?.trim() || sourceItem?.title?.trim() || source.title.trim() || '未命名剧本'
    const premise = major.premise?.trim() || sourceItem?.defaultRoute?.trim() || `围绕“${majorTitle}”展开的既有剧情`
    const objective = major.objective?.trim() || sourceItem?.defaultRoute?.trim() || `推动并完成“${majorTitle}”的核心剧情`
    const opposition = major.opposition?.trim() || sourceItem?.consequence?.trim() || `延续“${majorTitle}”中已经建立的核心阻力`
    const completionCondition = major.completionCondition?.trim() || sourceItem?.completionCondition?.trim()
      || `“${majorTitle}”的核心矛盾得到明确且可验证的解决`
    const modelCandidates = Array.isArray(result.minors)
      ? (result.minors as unknown[]).filter((candidate): candidate is NonNullable<ModelScriptRefactor['minors']>[number] => Boolean(candidate) && typeof candidate === 'object')
      : []
    const candidates = modelCandidates.length > 0
      ? modelCandidates.slice(0, 100)
      : [{ title: `${majorTitle}的核心进程`, objective, opposition, majorContribution: objective, content: premise }]
    const seed = stableMaterialSeed(`${source.path}:${majorTitle}:${candidates.length}`)
    const budget = createMajorBudgetSnapshot(settings.value.plotMechanics, settings.value.majorHookEnabled, seed)
    const allowedPhases = new Set<MajorStoryPhase>(settings.value.majorHookEnabled
      ? MAJOR_PHASES : MAJOR_PHASES.filter(phase => phase !== 'hook'))
    const minors = candidates.map((candidate, index): ScriptRefactorMinorPreview => {
      const phase = allowedPhases.has(candidate.majorPhase as MajorStoryPhase)
        ? candidate.majorPhase as MajorStoryPhase
        : (settings.value.majorHookEnabled ? MAJOR_PHASES : MAJOR_PHASES.filter(item => item !== 'hook'))[
            Math.min(index, (settings.value.majorHookEnabled ? MAJOR_PHASES.length : MAJOR_PHASES.length - 1) - 1)
          ]
      const minorType = ['quick', 'standard', 'focus'].includes(String(candidate.minorType))
        ? candidate.minorType as MinorStoryType
        : minorTypeForTurn(seed + index + 1, budget.minorTypeMix, phase)
      const title = candidate.title?.trim() || `小剧情 ${index + 1}`
      const content = candidate.content?.trim() || candidate.objective?.trim() || premise
      return {
        id: createId('minor'), title, phase, minorType,
        fragmentBudget: { ...budget.minorFragments[minorType] },
        objective: candidate.objective?.trim() || `解决“${title}”的局部目标`,
        opposition: candidate.opposition?.trim() || '沿用原剧本已经建立的阻力',
        majorContribution: candidate.majorContribution?.trim() || `推动“${majorTitle}”的${phase}阶段`,
        content,
      }
    })
    const phaseCounts = Object.fromEntries(MAJOR_PHASES.map(phase => [
      phase, minors.filter(item => item.phase === phase).length,
    ])) as Record<MajorStoryPhase, number>
    const requiredPhases = settings.value.majorHookEnabled ? MAJOR_PHASES : MAJOR_PHASES.filter(phase => phase !== 'hook')
    const missingPhases = requiredPhases.filter(phase => phaseCounts[phase] === 0)
    for (const phase of MAJOR_PHASES) {
      budget.phaseTargets[phase] = phaseCounts[phase]
      budget.phaseRanges[phase] = { min: phaseCounts[phase], max: phaseCounts[phase] }
    }
    budget.totalTarget = minors.length
    budget.totalRange = { min: minors.length, max: minors.length }
    const warnings: string[] = []
    if (missingPhases.length) {
      warnings.push(`Agent 未单独拆分${missingPhases.map(phase => `“${MAJOR_PHASE_LABELS[phase]}”`).join('、')}阶段；已保留有效内容，后续由剧情控制系统动态规划。`)
    }
    if (minors.length < settings.value.plotMechanics.totalMinorPlots.min
      || minors.length > settings.value.plotMechanics.totalMinorPlots.max) {
      warnings.push(`模型输出 ${minors.length} 个小剧情，超出当前配置 ${settings.value.plotMechanics.totalMinorPlots.min}-${settings.value.plotMechanics.totalMinorPlots.max}；运行预算已按实际结果冻结。`)
    }
    return {
      sourceId: sourceItem?.id ?? source.path,
      sourceTitle: source.title,
      majorId: createId('major'),
      majorTitle,
      premise,
      objective,
      opposition,
      completionCondition,
      budget, minors, warnings,
    }
  }

  function stableMaterialSeed(value: string): number {
    let hash = 2166136261
    for (let index = 0; index < value.length; index += 1) hash = Math.imul(hash ^ value.charCodeAt(index), 16777619)
    return hash >>> 0
  }

  async function executeMaterialRefactor(
    source: MaterialRefactorSource,
    prompt: string,
    providerId: string,
    reasoningEffort: string,
    options: MaterialRefactorOptions = {},
  ): Promise<ScriptRefactorPreview | null> {
    const emit = (progress: MaterialRefactorProgress) => options.onProgress?.(progress)
    try {
      emit({ stage: 'prepare', label: '读取资料与项目上下文', status: 'running', detail: source.path })
      await updateRefactorPrompt(source.kind, source.mode, prompt)
      emit({ stage: 'prepare', label: '读取资料与项目上下文', status: 'done' })
      emit({
        stage: 'agent', label: 'Agent 理解内容并规划重构方案', status: 'running',
        detail: options.quantityMode === 'preserve' ? '优先保持现有条目数量' : '自动规划合适数量',
      })
      const response = await authedFetch('/api/storydex/refactor-material', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          path: currentProjectRoot(),
          sourcePath: source.path,
          kind: source.kind,
          mode: source.mode,
          prompt: prompt.trim(),
          providerId,
          reasoningEffort,
          plotMechanics: settings.value.plotMechanics,
          majorHookEnabled: settings.value.majorHookEnabled,
          preserveItemCount: options.quantityMode === 'preserve',
          allowItemCountChange: options.quantityMode === 'auto',
          sourceItemCount: options.sourceItemCount,
        }),
      })
      if (!response.ok) {
        let detail = `HTTP ${response.status}`
        try { detail = (await response.json())?.error ?? detail } catch { /* plain response */ }
        throw new Error(`格式化重构失败：${detail}`)
      }
      emit({ stage: 'agent', label: 'Agent 理解内容并规划重构方案', status: 'done' })
      emit({ stage: 'validate', label: '整理 Agent 结果', status: 'running' })
      const payload = await response.json() as { result?: ModelScriptRefactor | ModelPresetRefactor }
      if (!payload.result || typeof payload.result !== 'object') throw new Error('格式化重构没有返回有效结果')
      if (source.kind === 'scripts') {
        const preview = scriptPreviewFromModel(source, payload.result as ModelScriptRefactor)
        if (options.quantityMode === 'preserve' && options.sourceItemCount
          && preview.minors.length !== options.sourceItemCount) {
          preview.warnings.push(`原有 ${options.sourceItemCount} 个小剧情，Agent 根据有效内容整理为 ${preview.minors.length} 个。`)
        }
        emit({ stage: 'validate', label: '整理 Agent 结果', status: 'done', detail: `${preview.minors.length} 个小剧情${preview.warnings.length ? ` · ${preview.warnings.length} 项提示` : ''}` })
        return preview
      }
      emit({ stage: 'validate', label: '整理 Agent 结果', status: 'done' })
      await commitPresetRefactor(source, payload.result as ModelPresetRefactor, emit)
      emit({ stage: 'complete', label: '条目重构完成', status: 'done' })
      return null
    } catch (cause) {
      const detail = cause instanceof Error ? cause.message : String(cause)
      emit({ stage: 'complete', label: '条目重构失败', status: 'error', detail })
      throw cause
    }
  }

  async function commitPresetRefactor(
    source: MaterialRefactorSource,
    result: ModelPresetRefactor,
    emit?: (progress: MaterialRefactorProgress) => void,
  ) {
    const sourceContent = await readProjectText(source.path) ?? ''
    const modelCandidates = Array.isArray(result.items)
      ? (result.items as unknown[]).filter((candidate): candidate is ModelPresetItem => Boolean(candidate) && typeof candidate === 'object')
      : []
    const candidates = modelCandidates.length ? modelCandidates.slice(0, 20) : [result]
    const items = candidates.map((candidate, index) => ({
      title: candidate.title?.trim() || (candidates.length > 1 ? `${source.title} ${index + 1}` : source.title) || '未命名风格预设',
      content: candidate.content?.trim() || sourceContent.trim(),
    }))
    if (!items.some(item => item.content)) throw new Error('Agent 未返回可使用的风格内容')
    const existing = source.itemId ? presets.value.find(item => item.id === source.itemId) : undefined
    const stamp = new Date().toISOString().replace(/[:.]/g, '-')
    if (existing) {
      emit?.({ stage: 'backup', label: '备份原始风格预设', status: 'running' })
      const original = sourceContent
      if (!original.trim()) throw new Error('原始风格预设内容已经不存在')
      await writeProjectText(
        `.storydex/presets/imports/${stamp}-${safeFilename(existing.filename)}`,
        original,
      )
      emit?.({ stage: 'backup', label: '备份原始风格预设', status: 'done' })
      emit?.({ stage: 'write', label: '写入标准预设并同步索引', status: 'running' })
      await updateItem('presets', existing, items[0].title, items[0].content)
      existing.formatVersion = 2
      await saveCollection('presets')
      for (const candidate of items.slice(1)) {
        const created = await addItem('presets', candidate.title, candidate.content)
        created.formatVersion = 2
      }
      if (items.length > 1) await saveCollection('presets')
    } else {
      emit?.({ stage: 'write', label: '写入标准预设并同步索引', status: 'running' })
      for (const candidate of items) {
        const created = await addItem('presets', candidate.title, candidate.content)
        created.formatVersion = 2
      }
      await saveCollection('presets')
    }
    if (source.mode === 'import') {
      await deleteProjectFile(source.path).catch(() => {})
      stagedMaterials.value = stagedMaterials.value.filter(item => item.path !== source.path)
    }
    emit?.({ stage: 'write', label: '写入标准预设并同步索引', status: 'done', detail: `${items.length} 个风格预设` })
  }

  async function initialize(force = false): Promise<void> {
    const projectRoot = currentProjectRoot()
    if (!force && ready.value && initializedProjectRoot === projectRoot) return
    if (initializePromise) {
      await initializePromise
      if (!force && ready.value && initializedProjectRoot === projectRoot) return
    }
    const task = runInitialize(projectRoot)
    initializePromise = task
    try {
      await task
    } finally {
      if (initializePromise === task) initializePromise = null
    }
  }

  async function commitScriptRefactor(
    preview: ScriptRefactorPreview,
    materialSource?: MaterialRefactorSource,
    onProgress?: (progress: MaterialRefactorProgress) => void,
  ): Promise<void> {
    const source = scripts.value.find(item => item.id === (materialSource?.itemId ?? preview.sourceId))
    if (!source && !materialSource) throw new Error('原始剧本已不存在')
    if (source?.refactoredTo) throw new Error('该原始剧本已经完成过格式化重构')
    if (scripts.value.some(item => item.id === preview.majorId)) throw new Error('该重构预览已经提交，请重新执行格式化')
    const sourcePath = materialSource?.path ?? (source?.path ?? `.storydex/scripts/${source?.filename ?? ''}`)
    const sourceContent = await readProjectText(sourcePath) ?? ''
    if (!sourceContent.trim()) throw new Error('原始剧本内容已经不存在')
    const stamp = new Date().toISOString().replace(/[:.]/g, '-')
    onProgress?.({ stage: 'backup', label: '备份原始剧本及关联结构', status: 'running' })
    await writeProjectText(`.storydex/scripts/imports/${stamp}-${safeFilename(source?.filename ?? materialSource?.filename ?? 'source.txt')}`, sourceContent)
    onProgress?.({ stage: 'backup', label: '备份原始剧本及关联结构', status: 'done' })
    const majorFilename = `${safeFilename(preview.majorTitle)}-${preview.majorId.slice(-8)}.md`
    const majorPath = `.storydex/scripts/major/${majorFilename}`
    const shouldActivate = source?.status === 'active'
      || !scripts.value.some(item => item.enabled && item.status === 'active' && (item.scriptType ?? 'major') === 'major' && item.id !== source?.id)
    const majorStatus: NonNullable<ManagedItem['status']> = shouldActivate ? 'active' : 'pending'
    const majorBody = `---\nschemaVersion: 2\ntype: major\nid: ${preview.majorId}\ntitle: ${JSON.stringify(preview.majorTitle)}\nstatus: ${majorStatus}\n---\n\n## 前提\n${preview.premise}\n\n## 目标\n${preview.objective}\n\n## 核心阻力\n${preview.opposition}\n\n## 完成条件\n${preview.completionCondition}\n\n## 动态预算\n${JSON.stringify(preview.budget, null, 2)}\n`
    const createdPaths: string[] = []
    const previousScripts = [...scripts.value]
    const previousDirector = normalizeDirectorState(JSON.parse(JSON.stringify(directorState.value)))
    const previousSource = source ? {
      enabled: source.enabled,
      status: source.status,
      updatedAt: source.updatedAt,
      refactoredTo: source.refactoredTo,
    } : null
    const previousChildren = source
      ? scripts.value.filter(item => item.scriptType === 'minor' && item.parentId === source.id)
      : []
    try {
      onProgress?.({ stage: 'write', label: '写入大剧本、小剧情和项目索引', status: 'running' })
      await writeProjectText(majorPath, majorBody)
      createdPaths.push(majorPath)
    const now = new Date().toISOString()
      const major: ManagedItem = {
        id: preview.majorId, title: preview.majorTitle, filename: majorFilename, path: majorPath,
        enabled: true, status: majorStatus, completionCondition: preview.completionCondition,
      defaultRoute: preview.objective, updatedAt: now, formatVersion: 2, scriptType: 'major', proposed: false,
      clock: 0, clockMax: 4, deadlineTurns: 4, consequence: `${preview.majorTitle}长期未处理将主动恶化`, lastTickTurn: 0,
    }
    const minors: ManagedItem[] = []
    for (const candidate of preview.minors) {
      const filename = `${safeFilename(candidate.title)}-${candidate.id.slice(-8)}.md`
      const path = `.storydex/scripts/minor/${preview.majorId}/${filename}`
      const body = `---\nschemaVersion: 2\ntype: minor\nid: ${candidate.id}\nparentId: ${preview.majorId}\ntitle: ${JSON.stringify(candidate.title)}\nmajorPhase: ${candidate.phase}\nminorType: ${candidate.minorType}\nfragmentBudget: ${JSON.stringify(candidate.fragmentBudget)}\nstatus: pending\nproposed: true\n---\n\n## 局部目标\n${candidate.objective}\n\n## 阻力\n${candidate.opposition}\n\n## 主线贡献\n${candidate.majorContribution}\n\n## 原剧本内容\n${candidate.content}\n`
      await writeProjectText(path, body)
      createdPaths.push(path)
      minors.push({
        id: candidate.id, title: candidate.title, filename, path, enabled: true, status: 'pending',
        completionCondition: candidate.majorContribution, defaultRoute: candidate.objective,
        updatedAt: now, formatVersion: 2, scriptType: 'minor', parentId: preview.majorId,
        majorPhase: candidate.phase, minorType: candidate.minorType,
        fragmentBudget: { ...candidate.fragmentBudget }, proposed: true,
      })
    }
    if (source) {
      source.enabled = false
      source.status = 'pending'
      source.updatedAt = now
      source.refactoredTo = preview.majorId
    }
    scripts.value = [major, ...minors, ...scripts.value.filter(item => !previousChildren.includes(item))]
    await saveCollection('scripts')
    if (source && directorState.value.activeArc?.majorScriptId === source.id) {
      directorState.value.activeArc.majorScriptId = preview.majorId
      directorState.value.subArcs = []
      await saveDirector()
      await markMemoryPending().catch(() => {})
    }
    for (const child of previousChildren) {
      await deleteProjectFile(child.path ?? `.storydex/scripts/${child.filename}`).catch(() => {})
    }
    if (materialSource?.mode === 'import') {
      await deleteProjectFile(materialSource.path).catch(() => {})
      stagedMaterials.value = stagedMaterials.value.filter(item => item.path !== materialSource.path)
    }
    onProgress?.({ stage: 'write', label: '写入大剧本、小剧情和项目索引', status: 'done' })
    onProgress?.({ stage: 'complete', label: '条目重构完成', status: 'done' })
    } catch (cause) {
      scripts.value = previousScripts
      directorState.value = previousDirector
      if (source && previousSource) {
        source.enabled = previousSource.enabled
        source.status = previousSource.status
        source.updatedAt = previousSource.updatedAt
        source.refactoredTo = previousSource.refactoredTo
      }
      await Promise.all([
        saveCollection('scripts').catch(() => {}),
        saveDirector().catch(() => {}),
      ])
      for (const path of createdPaths.reverse()) await deleteProjectFile(path).catch(() => {})
      throw cause
    }
  }
  async function prepareDirectorTurn(
    content: string,
    delta: DirectorDelta | undefined,
    plan: DirectorPlan,
    sourceMessageId: string,
    fragmentPath: string,
    storyDelta?: StoryStateDelta,
  ): Promise<DirectorEvaluation> {
    if (sourceMessageId && directorState.value.lastCommittedMessageId === sourceMessageId) {
      return evaluateDirectorTurn(directorState.value, plan, delta, content, sourceMessageId)
    }
    const evaluation = evaluateDirectorTurn(directorState.value, plan, delta, content, sourceMessageId)
    const outputAudit = auditStoryTurn(content, plan, evaluation)
    evaluation.outputAudit = outputAudit
    if (!outputAudit.accepted) {
      evaluation.accepted = false
      evaluation.planSatisfied = false
      evaluation.rejectedReasons.push(...outputAudit.violations)
      evaluation.nextState = normalizeDirectorState(directorState.value)
      return evaluation
    }
    const attachSource = (arc: DirectorState['activeArc']) => {
      if (!arc || !fragmentPath) return
      arc.sourceFragments = [...new Set([...(arc.sourceFragments ?? []), fragmentPath])].slice(-200)
    }
    if (evaluation.mainlineChanged) attachSource(evaluation.nextState.activeArc)
    if (evaluation.minorUpdated) {
      evaluation.nextState.subArcs.forEach(attachSource)
      const previousCompleted = new Set(directorState.value.completedArcs.map(arc => arc.id))
      evaluation.nextState.completedArcs.filter(arc => !previousCompleted.has(arc.id)).forEach(attachSource)
    }
    tickBackgroundScripts(evaluation, plan)
    const envelope: PendingCommit = {
      schemaVersion: 2,
      phase: 'prepared',
      preparedAt: new Date().toISOString(),
      fragmentPath,
      sourceMessageId,
      planId: plan.id,
      nextState: evaluation.nextState,
      storyDelta,
      evaluation,
      plan,
    }
    pendingCommit.value = envelope
    await writeProjectJson('.storydex/director/pending-commit.json', envelope)
    return evaluation
  }

  function tickBackgroundScripts(evaluation: DirectorEvaluation, plan: DirectorPlan) {
    const background = scripts.value
      // 阶段不参与状态机。addItem 给它写的也是 enabled:true / status:'active'，不排除的话
      // 它会占掉一个背景时钟名额，走满 clockMax 后再以阶段标题塞一条 severity 4 的后果——
      // 而阶段只是框架指导，本身没有「未及时处理」这回事。Rust 侧同样不看阶段的 status。
      .filter(item => normalizeScriptType(item.scriptType) !== 'stage'
        && item.enabled && item.status === 'active' && item.id !== plan.scriptFocus?.id)
      .slice(0, 2)
    for (const item of background) {
      if (item.lastTickTurn === evaluation.nextState.turnIndex) continue
      item.clockMax = Math.min(20, Math.max(2, item.clockMax ?? item.deadlineTurns ?? 4))
      item.clock = Math.min(item.clockMax, (item.clock ?? 0) + 1)
      item.lastTickTurn = evaluation.nextState.turnIndex
      item.updatedAt = new Date().toISOString()
      if (item.clock < item.clockMax) continue
      const source = item.consequence?.trim() || `${item.title}因长期未处理而主动恶化`
      if (!evaluation.nextState.unresolvedConsequences.some(entry => entry.source === source && entry.status === 'pending')) {
        evaluation.nextState.unresolvedConsequences.push({
          id: createId('consequence'), source, status: 'pending', severity: 4,
          dueAfterTurns: 0, evidence: `背景时钟 ${item.clock}/${item.clockMax}`,
          updatedAt: new Date().toISOString(),
        })
      }
    }
  }

  async function updatePendingPhase(phase: CommitPhase) {
    if (!pendingCommit.value) return
    pendingCommit.value = { ...pendingCommit.value, phase }
    await writeProjectJson('.storydex/director/pending-commit.json', pendingCommit.value)
  }
  async function commitDirectorTurn(
    evaluation: DirectorEvaluation,
    plan: DirectorPlan,
    sourceMessageId: string,
    fragmentPath: string,
  ): Promise<void> {
    if (sourceMessageId && directorState.value.lastCommittedMessageId === sourceMessageId) {
      return
    }
    const previousDirector = directorState.value
    await writeProjectJson('.storydex/director/state.json', evaluation.nextState)
    directorState.value = evaluation.nextState
    syncScriptLifecycle(evaluation.nextState)
    const logPath = '.storydex/director/event-log.jsonl'
    const existing = await readProjectText(logPath).catch(() => null)
    const entries = (existing ?? '').split(/\r?\n/).filter(Boolean).slice(-499)
    entries.push(JSON.stringify({
      schemaVersion: 1,
      committedAt: new Date().toISOString(),
      turnIndex: evaluation.nextState.turnIndex,
      planId: plan.id,
      action: plan.action,
      fragmentPath,
      sourceMessageId,
      score: evaluation.progressScore,
      planSatisfied: evaluation.planSatisfied,
      mainlineChanged: evaluation.mainlineChanged,
      phaseTransitioned: evaluation.phaseTransitioned,
      arcEstablished: evaluation.arcEstablished,
      arcCompleted: evaluation.arcCompleted,
      acceptedEvidence: evaluation.acceptedEvidence,
      rejectedReasons: evaluation.rejectedReasons,
      control: plan.control,
      strictProgressWarning: plan.strictProgressWarning,
      primaryScriptId: plan.scriptFocus?.id ?? null,
      majorScriptIdBefore: previousDirector.activeArc?.majorScriptId ?? plan.scriptFocus?.id ?? null,
      majorScriptIdAfter: evaluation.nextState.activeArc?.majorScriptId
        ?? [...evaluation.nextState.completedArcs].reverse().find(arc => arc.scope === 'major')?.majorScriptId ?? null,
      majorPhaseBefore: plan.phase,
      majorPhaseAfter: evaluation.nextState.activeArc?.phase ?? null,
      activeMinorScriptId: evaluation.nextState.subArcs[0]?.minorScriptId ?? null,
      completedMinorScriptIds: evaluation.nextState.completedArcs
        .filter(arc => arc.scope === 'minor' && arc.sourceFragments?.includes(fragmentPath))
        .map(arc => arc.minorScriptId).filter(Boolean),
      minorCompleted: evaluation.minorCompleted,
      encounter: plan.encounterKind ? { kind: plan.encounterKind, intensity: plan.encounterIntensity ?? 1 } : null,
    }))
    await writeProjectText(logPath, `${entries.join('\n')}\n`).catch(() => {})
    await saveCollection('scripts')
    await updatePendingPhase('director_written')
  }

  function syncScriptLifecycle(state: DirectorState) {
    const now = new Date().toISOString()
    const activeMajorId = state.activeArc?.majorScriptId
    const completedMajorIds = new Set(state.completedArcs
      .filter(arc => arc.scope === 'major').map(arc => arc.majorScriptId).filter((id): id is string => Boolean(id)))
    const activeMinorIds = new Set(state.subArcs.map(arc => arc.minorScriptId).filter((id): id is string => Boolean(id)))
    const completedMinorIds = new Set(state.completedArcs
      .filter(arc => arc.scope === 'minor').map(arc => arc.minorScriptId).filter((id): id is string => Boolean(id)))
    for (const item of scripts.value) {
      if (!scriptLifecycleManagedByDirector(item)) continue
      let status = item.status
      if ((item.scriptType ?? 'major') === 'major') {
        if (completedMajorIds.has(item.id)) status = 'completed'
        else if (item.id === activeMajorId) status = 'active'
        else status = 'pending'
      } else {
        if (completedMinorIds.has(item.id)) status = 'completed'
        else if (activeMinorIds.has(item.id)) status = 'active'
        else status = 'pending'
      }
      if (status !== item.status) {
        item.status = status
        item.updatedAt = now
      }
    }
  }
  async function finalizeStoryTurn() {
    await updatePendingPhase('delta_written')
    await deleteProjectFile('.storydex/director/pending-commit.json').catch(() => {})
    pendingCommit.value = null
  }
  async function markMemoryPending() {
    memoryPending.value = true
    consistency.value.required = true
    consistency.value.lastError = ''
    consistency.value.reasons = [
      ...consistency.value.reasons,
      { type: 'manual-rebuild' as const, createdAt: new Date().toISOString() },
    ].slice(-50)
    await saveMemory()
  }
  async function markMemoryStale(source: string) {
    for (const fact of memoryFacts.value) {
      if (!fact.locked && (fact.sources.length === 0 || fact.sources.includes(source))) fact.stale = true
    }
    const now = new Date().toISOString()
    memoryPending.value = true
    consistency.value.required = true
    consistency.value.updating = false
    consistency.value.lastError = ''
    consistency.value.affectedFrom = !consistency.value.affectedFrom || source < consistency.value.affectedFrom
      ? source : consistency.value.affectedFrom
    consistency.value.reasons = [
      ...consistency.value.reasons.filter(reason => reason.fragmentPath !== source),
      { type: 'chapter-edited' as const, fragmentPath: source, createdAt: now },
    ].slice(-50)
    await saveMemory()
  }

  async function beginConsistencyRebuild() {
    consistency.value.updating = true
    consistency.value.lastError = ''
    await saveMemory()
  }

  async function failConsistencyRebuild(message: string) {
    consistency.value.updating = false
    consistency.value.required = true
    consistency.value.lastError = message
    memoryPending.value = true
    await saveMemory()
  }

  return {
    ready, error, settings, presets, scripts, refactorPrompts, stagedMaterials,
    memoryFacts, memoryPending, consistency, time, currentTimeLabel, directorState, primaryScriptFocus, pendingCommit,
    initialize, patchSettings, applyPlotSettingsToCurrent, addItem, updateItem, readItem, toggleItem, renameItem, removeItem,
    moveItem, moveScriptSibling, groupUngroupedMajorsIntoStage,
    exportItem, markScript, setScriptParent, commitScriptRefactor,
    updateRefactorPrompt, stageImportedMaterial, existingMaterialSource, executeMaterialRefactor,
    saveMemory, addMemoryFact, updateMemoryFact, removeMemoryFact, patchTime, createTimeRevision,
    applyStoryDelta, prepareDirectorTurn, updatePendingPhase, commitDirectorTurn, finalizeStoryTurn,
    markMemoryPending, markMemoryStale, beginConsistencyRebuild, failConsistencyRebuild,
  }
})
