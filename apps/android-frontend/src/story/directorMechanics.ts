import type { EncounterKind } from './randomMechanics'
import {
  createMajorBudgetSnapshot, DEFAULT_PLOT_MECHANICS, minorPhaseForFragment, minorTypeForTurn,
  normalizePlotMechanics,
  type CountRange, type MajorBudgetSnapshot, type MinorStoryType, type PlotMechanicsSettings,
} from './plotMechanics'

export type StoryPhase = 'hook' | 'beginning' | 'development' | 'climax' | 'ending'
export type StoryArcScope = 'major' | 'minor'
export type StoryPace = 'deliberate' | 'balanced' | 'urgent'
export type DirectorAction = 'establish' | 'hold' | 'reveal' | 'escalate' | 'milestone' | 'climax' | 'resolve'
export type ProgressChangeKind =
  | 'clue' | 'relationship' | 'resource' | 'identity' | 'reputation'
  | 'risk' | 'route' | 'irreversible' | 'milestone' | 'resolution'

export interface StoryArcState {
  id: string
  scope: StoryArcScope
  /** Formal script identities are the authority for the arc lifecycle. */
  majorScriptId?: string
  minorScriptId?: string
  majorPhase?: StoryPhase
  sourceFragments?: string[]
  title: string
  phase: StoryPhase
  objective: string
  opposition: string
  stakes: string[]
  phaseGoal: string
  exitCriteria: string[]
  plannedMilestones: string[]
  completedMilestones: string[]
  phaseTurnCount: number
  phaseEffectiveTurns: number
  stagnationCount: number
  completedPhases: StoryPhase[]
  totalTurnCount: number
  /** Major arcs freeze their configured scale; minor arcs freeze their own fragment budget. */
  budgetSnapshot?: MajorBudgetSnapshot
  phaseMinorCompleted?: Record<StoryPhase, number>
  minorType?: MinorStoryType
  fragmentBudget?: CountRange
  fragmentCount?: number
  effectiveFragmentCount?: number
  majorContribution?: string
  minorTypeChanged?: boolean
  createdAt: string
  updatedAt: string
}

export interface StoryThread {
  id: string
  title: string
  status: 'active' | 'resolved' | 'abandoned'
  importance: 1 | 2 | 3 | 4 | 5
  sourceEvidence: string
  updatedAt: string
}

export interface StoryConsequence {
  id: string
  source: string
  status: 'pending' | 'resolved'
  severity: 1 | 2 | 3 | 4 | 5
  dueAfterTurns?: number
  evidence: string
  updatedAt: string
}

export interface DirectorState {
  schemaVersion: 1
  revision: number
  turnIndex: number
  activeArc: StoryArcState | null
  subArcs: StoryArcState[]
  completedArcs: StoryArcState[]
  activeThreads: StoryThread[]
  unresolvedConsequences: StoryConsequence[]
  pacing: {
    stagnationCount: number
    progressDebt: number
    climaxDebt: number
    lastMajorChangeTurn: number
    recentScores: number[]
    recentActions: DirectorAction[]
  }
  cooldowns: { tragedy: number; payoff: number }
  lastCommittedMessageId: string
  updatedAt: string
}

/**
 * The control contract for one story turn.  It is deliberately kept on the
 * plan rather than inferred from the model response so retries can replay the
 * same decision and random draw.
 */
export interface TurnControlContract {
  turnId: string
  stateRevision: number
  turnIndex: number
  primaryScriptId: string | null
  randomSeed: number
  strictProgressWarning: boolean
  warningThreshold: number
}

export interface DirectorPlan {
  id: string
  stateRevision: number
  turnIndex: number
  action: DirectorAction
  targetArcId: string | null
  phase: StoryPhase | null
  requiredChange: string
  requiredBeats: string[]
  forbiddenOutcomes: string[]
  expectedProgressScore: number
  allowPhaseTransition: boolean
  transitionTarget: StoryPhase | null
  allowedEncounterKinds: EncounterKind[]
  encounterKind?: EncounterKind
  encounterIntensity?: number
  coordination: {
    primaryArc: string | null
    encounterRole: 'background' | 'advance' | 'pressure' | 'payoff' | 'resolve'
    styleMayNotOverride: boolean
  }
  scriptFocus?: {
    id: string
    title: string
    completionCondition: string
    defaultRoute: string
    lifecycleManagedByDirector?: boolean
    path?: string
    /**
     * 当前大剧情所属的阶段（三级结构的最上层）。只作为顶层框架指导注入，
     * 不参与状态机流转；大剧情没有归入阶段时缺省不存在。
     */
    stageScript?: {
      id: string
      title: string
      objective: string
      completionCondition: string
      path?: string
    }
    minorScript?: {
      id: string
      title: string
      parentId: string
      majorPhase: StoryPhase
      minorType: MinorStoryType
      objective: string
      completionCondition: string
      fragmentBudget: CountRange
      path?: string
    }
  }
  control: TurnControlContract
  strictProgressWarning: boolean
  warningThreshold: number
  randomSeed: number
  renderMode: 'compressed' | 'standard' | 'setpiece'
  plotControl: {
    budgetSnapshot: MajorBudgetSnapshot
    phaseCompleted: number
    phaseTarget: number
    phaseMaximum: number
    phaseClosureWindow: boolean
    transitionRequired: boolean
    suggestedMinorType: MinorStoryType
    currentMinorId: string | null
    currentMinorBudget: CountRange
    minorClosureRequired: boolean
  }
}

export interface DirectorArcInitialization {
  title: string
  scope: StoryArcScope
  phase: 'hook' | 'beginning'
  objective: string
  opposition: string
  stakes: string[]
  phaseGoal: string
  exitCriteria: string[]
  plannedMilestones: string[]
}

export interface DirectorProgressChange {
  kind: ProgressChangeKind
  relevance: 'mainline' | 'local'
  description: string
  evidence: string
}

export interface DirectorDelta {
  planId?: string
  turnId?: string
  encounterOutcome?: {
    kind: 'tragedy' | 'payoff'
    evidence: string
  }
  arcInitialization?: DirectorArcInitialization
  changes?: DirectorProgressChange[]
  completedMilestones?: string[]
  phaseTransition?: { from: StoryPhase; to: StoryPhase }
  nextPhaseSetup?: { phaseGoal: string; exitCriteria: string[]; plannedMilestones: string[] }
  completeArc?: boolean
  threadUpdates?: Array<{
    id?: string
    title: string
    status: StoryThread['status']
    importance?: StoryThread['importance']
    evidence: string
  }>
  consequenceUpdates?: Array<{
    id?: string
    source: string
    status: StoryConsequence['status']
    severity?: StoryConsequence['severity']
    dueAfterTurns?: number
    evidence: string
  }>
  subArcUpdates?: Array<{
    id?: string
    action: 'create' | 'createResolved' | 'progress' | 'advance' | 'resolve' | 'abandon'
    title: string
    phase: Exclude<StoryPhase, 'hook'>
    minorType?: MinorStoryType
    majorContribution?: string
    objective?: string
    opposition?: string
    stakes?: string[]
    phaseGoal?: string
    exitCriteria?: string[]
    plannedMilestones?: string[]
    evidence: string
  }>
}

export interface DirectorEvaluation {
  accepted: boolean
  planMatched: boolean
  planSatisfied: boolean
  progressScore: number
  mainlineChanged: boolean
  phaseTransitioned: boolean
  arcEstablished: boolean
  arcCompleted: boolean
  minorUpdated: boolean
  minorCompleted: boolean
  acceptedEvidence: string[]
  rejectedReasons: string[]
  nextState: DirectorState
  outputAudit?: StoryTurnAudit
}

export interface StoryTurnAudit {
  accepted: boolean
  mainlineCollisionVerified: boolean
  verifiedEvidence: string[]
  violations: string[]
}

const PHASE_ORDER: StoryPhase[] = ['hook', 'beginning', 'development', 'climax', 'ending']
const CHANGE_SCORES: Record<ProgressChangeKind, number> = {
  clue: 2,
  relationship: 2,
  resource: 3,
  identity: 3,
  reputation: 3,
  risk: 3,
  route: 4,
  irreversible: 4,
  milestone: 5,
  resolution: 5,
}
const PHASE_BUDGETS: Record<StoryPace, Record<StoryPhase, { min: number; max: number }>> = {
  deliberate: {
    hook: { min: 1, max: 2 }, beginning: { min: 2, max: 4 }, development: { min: 4, max: 10 },
    climax: { min: 1, max: 3 }, ending: { min: 1, max: 2 },
  },
  balanced: {
    hook: { min: 1, max: 2 }, beginning: { min: 2, max: 3 }, development: { min: 3, max: 7 },
    climax: { min: 1, max: 2 }, ending: { min: 1, max: 1 },
  },
  urgent: {
    hook: { min: 1, max: 1 }, beginning: { min: 1, max: 2 }, development: { min: 2, max: 5 },
    climax: { min: 1, max: 1 }, ending: { min: 1, max: 1 },
  },
}

function nowIso(): string {
  return new Date().toISOString()
}

function directorId(prefix: string): string {
  const random = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `${prefix}-${random}`
}

function strings(value: unknown, max = 12): string[] {
  return Array.isArray(value)
    ? value.filter(item => typeof item === 'string').map(item => item.trim()).filter(Boolean).slice(0, max)
    : []
}

function boundedInt(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.min(max, Math.max(min, Math.round(parsed))) : fallback
}

/** Stable, platform-independent seed for a prepared turn. */
export function stableTurnSeed(stateRevision: number, turnIndex: number, scriptId = ''): number {
  const value = `${stateRevision}:${turnIndex}:${scriptId}`
  let hash = 2166136261
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}

export function createDefaultDirectorState(): DirectorState {
  return {
    schemaVersion: 1,
    revision: 0,
    turnIndex: 0,
    activeArc: null,
    subArcs: [],
    completedArcs: [],
    activeThreads: [],
    unresolvedConsequences: [],
    pacing: {
      stagnationCount: 0,
      progressDebt: 0,
      climaxDebt: 0,
      lastMajorChangeTurn: 0,
      recentScores: [],
      recentActions: [],
    },
    cooldowns: { tragedy: 0, payoff: 0 },
    lastCommittedMessageId: '',
    updatedAt: nowIso(),
  }
}

function normalizeArc(value: unknown): StoryArcState | null {
  if (!value || typeof value !== 'object') return null
  const raw = value as Partial<StoryArcState>
  const title = typeof raw.title === 'string' ? raw.title.trim() : ''
  const objective = typeof raw.objective === 'string' ? raw.objective.trim() : ''
  const opposition = typeof raw.opposition === 'string' ? raw.opposition.trim() : ''
  if (!title || !objective || !opposition) return null
  const phase = PHASE_ORDER.includes(raw.phase as StoryPhase) ? raw.phase as StoryPhase : 'beginning'
  const phaseMinorCompleted = Object.fromEntries(PHASE_ORDER.map(item => [
    item, boundedInt(raw.phaseMinorCompleted?.[item], 0, 0, 10_000),
  ])) as Record<StoryPhase, number>
  const minorType = ['quick', 'standard', 'focus'].includes(String(raw.minorType))
    ? raw.minorType as MinorStoryType : undefined
  const fragmentBudget = raw.fragmentBudget && typeof raw.fragmentBudget === 'object'
    ? {
        min: boundedInt(raw.fragmentBudget.min, 1, 1, 50),
        max: boundedInt(raw.fragmentBudget.max, 3, 1, 50),
      }
    : undefined
  if (fragmentBudget && fragmentBudget.max < fragmentBudget.min) fragmentBudget.max = fragmentBudget.min
  return {
    id: typeof raw.id === 'string' && raw.id ? raw.id : directorId('arc'),
    scope: raw.scope === 'minor' ? 'minor' : 'major',
    title,
    phase,
    objective,
    opposition,
    stakes: strings(raw.stakes, 8),
    phaseGoal: typeof raw.phaseGoal === 'string' ? raw.phaseGoal.trim() : '',
    exitCriteria: strings(raw.exitCriteria, 8),
    plannedMilestones: strings(raw.plannedMilestones, 12),
    completedMilestones: strings(raw.completedMilestones, 24),
    phaseTurnCount: boundedInt(raw.phaseTurnCount, 0, 0, 10_000),
    phaseEffectiveTurns: boundedInt(raw.phaseEffectiveTurns, 0, 0, 10_000),
    stagnationCount: boundedInt(raw.stagnationCount, 0, 0, 10_000),
    completedPhases: strings(raw.completedPhases, 5)
      .filter((phase): phase is StoryPhase => PHASE_ORDER.includes(phase as StoryPhase)),
    totalTurnCount: boundedInt(raw.totalTurnCount, 0, 0, 100_000),
    ...(raw.budgetSnapshot ? { budgetSnapshot: raw.budgetSnapshot as MajorBudgetSnapshot } : {}),
    majorScriptId: typeof raw.majorScriptId === 'string' ? raw.majorScriptId : undefined,
    minorScriptId: typeof raw.minorScriptId === 'string' ? raw.minorScriptId : undefined,
    majorPhase: PHASE_ORDER.includes(raw.majorPhase as StoryPhase) ? raw.majorPhase as StoryPhase : undefined,
    sourceFragments: strings(raw.sourceFragments, 200),
    phaseMinorCompleted,
    ...(minorType ? { minorType } : {}),
    ...(fragmentBudget ? { fragmentBudget } : {}),
    fragmentCount: boundedInt(raw.fragmentCount, 0, 0, 10_000),
    effectiveFragmentCount: boundedInt(raw.effectiveFragmentCount, 0, 0, 10_000),
    majorContribution: typeof raw.majorContribution === 'string' ? raw.majorContribution.trim() : '',
    minorTypeChanged: raw.minorTypeChanged === true,
    createdAt: typeof raw.createdAt === 'string' ? raw.createdAt : nowIso(),
    updatedAt: typeof raw.updatedAt === 'string' ? raw.updatedAt : nowIso(),
  }
}

export function normalizeDirectorState(value: unknown): DirectorState {
  const fallback = createDefaultDirectorState()
  if (!value || typeof value !== 'object') return fallback
  const raw = value as Partial<DirectorState>
  const pacing = raw.pacing && typeof raw.pacing === 'object' ? raw.pacing : fallback.pacing
  const normalizedActiveArc = normalizeArc(raw.activeArc)
  return {
    ...fallback,
    revision: boundedInt(raw.revision, 0, 0, 1_000_000),
    turnIndex: boundedInt(raw.turnIndex, 0, 0, 1_000_000),
    activeArc: normalizedActiveArc ? { ...normalizedActiveArc, scope: 'major' } : null,
    subArcs: Array.isArray(raw.subArcs)
      ? raw.subArcs.map(normalizeArc).filter((arc): arc is StoryArcState => arc != null && arc.scope === 'minor' && arc.phase !== 'hook').slice(-12)
      : [],
    completedArcs: Array.isArray(raw.completedArcs)
      ? raw.completedArcs.map(normalizeArc).filter((arc): arc is StoryArcState => arc != null).slice(-20)
      : [],
    activeThreads: Array.isArray(raw.activeThreads) ? raw.activeThreads.filter(item => item && typeof item === 'object').map(item => {
      const thread = item as Partial<StoryThread>
      return {
        id: typeof thread.id === 'string' && thread.id ? thread.id : directorId('thread'),
        title: typeof thread.title === 'string' ? thread.title.trim() : '',
        status: ['resolved', 'abandoned'].includes(String(thread.status)) ? thread.status as StoryThread['status'] : 'active',
        importance: boundedInt(thread.importance, 3, 1, 5) as StoryThread['importance'],
        sourceEvidence: typeof thread.sourceEvidence === 'string' ? thread.sourceEvidence.trim() : '',
        updatedAt: typeof thread.updatedAt === 'string' ? thread.updatedAt : nowIso(),
      }
    }).filter(item => item.title).slice(-50) : [],
    unresolvedConsequences: Array.isArray(raw.unresolvedConsequences)
      ? raw.unresolvedConsequences.filter(item => item && typeof item === 'object').map(item => {
        const consequence = item as Partial<StoryConsequence>
        return {
          id: typeof consequence.id === 'string' && consequence.id ? consequence.id : directorId('consequence'),
          source: typeof consequence.source === 'string' ? consequence.source.trim() : '',
          status: consequence.status === 'resolved' ? 'resolved' as const : 'pending' as const,
          severity: boundedInt(consequence.severity, 3, 1, 5) as StoryConsequence['severity'],
          ...(typeof consequence.dueAfterTurns === 'number'
            ? { dueAfterTurns: boundedInt(consequence.dueAfterTurns, 1, 0, 100) }
            : {}),
          evidence: typeof consequence.evidence === 'string' ? consequence.evidence.trim() : '',
          updatedAt: typeof consequence.updatedAt === 'string' ? consequence.updatedAt : nowIso(),
        }
      }).filter(item => item.source).slice(-50) : [],
    pacing: {
      stagnationCount: boundedInt(pacing.stagnationCount, 0, 0, 100),
      progressDebt: boundedInt(pacing.progressDebt, 0, 0, 100),
      climaxDebt: boundedInt(pacing.climaxDebt, 0, 0, 100),
      lastMajorChangeTurn: boundedInt(pacing.lastMajorChangeTurn, 0, 0, 1_000_000),
      recentScores: Array.isArray(pacing.recentScores)
        ? pacing.recentScores.map(score => boundedInt(score, 0, 0, 5)).slice(-8) : [],
      recentActions: Array.isArray(pacing.recentActions)
        ? pacing.recentActions.filter(action => ['establish', 'hold', 'reveal', 'escalate', 'milestone', 'climax', 'resolve'].includes(action)).slice(-8)
        : [],
    },
    cooldowns: {
      tragedy: boundedInt(raw.cooldowns?.tragedy, 0, 0, 100),
      payoff: boundedInt(raw.cooldowns?.payoff, 0, 0, 100),
    },
    lastCommittedMessageId: typeof raw.lastCommittedMessageId === 'string' ? raw.lastCommittedMessageId : '',
    updatedAt: typeof raw.updatedAt === 'string' ? raw.updatedAt : fallback.updatedAt,
  }
}

function nextPhase(phase: StoryPhase): StoryPhase | null {
  const index = PHASE_ORDER.indexOf(phase)
  return index >= 0 && index < PHASE_ORDER.length - 1 ? PHASE_ORDER[index + 1] : null
}

function encounterKindsFor(action: DirectorAction, state: DirectorState): EncounterKind[] {
  const kinds: EncounterKind[] = ['event', 'character']
  const hasTragedySource = state.unresolvedConsequences.some(item => item.status === 'pending')
  const hasPayoffSetup = Boolean(state.activeArc?.completedMilestones.length)
    || state.pacing.recentScores.some(score => score >= 3)
  if (['escalate', 'milestone', 'climax', 'resolve'].includes(action)
    && hasTragedySource && state.cooldowns.tragedy === 0) kinds.push('tragedy')
  if (['milestone', 'climax', 'resolve'].includes(action)
    && hasPayoffSetup && state.cooldowns.payoff === 0) kinds.push('payoff')
  return kinds
}

export function buildDirectorPlan(
  state: DirectorState,
  pace: StoryPace,
  majorHookEnabled: boolean,
  scriptFocus?: DirectorPlan['scriptFocus'],
  stagnationWarningThreshold = 3,
  plotSettings: PlotMechanicsSettings = DEFAULT_PLOT_MECHANICS,
): DirectorPlan {
  const turnIndex = state.turnIndex + 1
  const warningThreshold = boundedInt(stagnationWarningThreshold, 3, 1, 20)
  const randomSeed = stableTurnSeed(state.revision, turnIndex, scriptFocus?.id ?? '')
  const strictProgressWarning = state.pacing.stagnationCount >= warningThreshold
  const normalizedPlotSettings = normalizePlotMechanics(plotSettings)
  const budgetSnapshot = state.activeArc?.budgetSnapshot
    ?? createMajorBudgetSnapshot(normalizedPlotSettings, majorHookEnabled, randomSeed)
  const currentPhase = state.activeArc?.phase ?? (majorHookEnabled ? 'hook' : 'beginning')
  const phaseCompleted = state.activeArc?.phaseMinorCompleted?.[currentPhase] ?? 0
  const phaseTarget = budgetSnapshot.phaseTargets[currentPhase]
  const phaseMaximum = budgetSnapshot.phaseRanges[currentPhase].max
  const currentMinor = state.subArcs[0] ?? null
  const selectedMinorScript = currentMinor?.minorScriptId
    ? scriptFocus?.minorScript?.id === currentMinor.minorScriptId ? scriptFocus.minorScript : undefined
    : scriptFocus?.minorScript
  const suggestedMinorType = currentMinor?.minorType ?? selectedMinorScript?.minorType
    ?? minorTypeForTurn(randomSeed, budgetSnapshot.minorTypeMix, currentPhase)
  const currentMinorBudget = currentMinor?.fragmentBudget
    ?? selectedMinorScript?.fragmentBudget
    ?? budgetSnapshot.minorFragments[suggestedMinorType]
  const currentMinorCanResolve = Boolean(currentMinor
    && (currentMinor.fragmentCount ?? currentMinor.totalTurnCount) >= currentMinorBudget.min - 1)
  const plotControl: DirectorPlan['plotControl'] = {
    budgetSnapshot,
    phaseCompleted,
    phaseTarget,
    phaseMaximum,
    phaseClosureWindow: phaseTarget - phaseCompleted <= budgetSnapshot.phaseClosureReserve,
    transitionRequired: phaseCompleted >= phaseMaximum || phaseCompleted >= phaseTarget,
    suggestedMinorType,
    currentMinorId: currentMinor?.id ?? null,
    currentMinorBudget,
    minorClosureRequired: Boolean(currentMinor && (currentMinor.fragmentCount ?? currentMinor.phaseTurnCount) >= currentMinorBudget.max - 1),
  }
  const control: TurnControlContract = {
    turnId: `turn-${state.revision}-${turnIndex}`,
    stateRevision: state.revision,
    turnIndex,
    primaryScriptId: scriptFocus?.id ?? null,
    randomSeed,
    strictProgressWarning,
    warningThreshold,
  }
  const base = {
    id: `director-${state.revision}-${turnIndex}`,
    stateRevision: state.revision,
    turnIndex,
    targetArcId: state.activeArc?.id ?? null,
    phase: state.activeArc?.phase ?? null,
    forbiddenOutcomes: [
      '用换场景、重复解释或空泛气氛冒充推进',
      '替玩家决定思想、行动或关键选择',
      '暴露导演阶段、进度、计划、候选路线或未发生结局',
      '制造没有因果来源的重大死亡、背叛、胜利或崇拜',
    ],
    control,
    strictProgressWarning,
    warningThreshold,
    randomSeed,
    renderMode: 'standard' as DirectorPlan['renderMode'],
    plotControl,
  }
  if (!state.activeArc) {
    const initialPhase: StoryPhase = majorHookEnabled ? 'hook' : 'beginning'
    return {
      ...base,
      action: 'establish',
      phase: initialPhase,
      requiredChange: '建立一条可持续发展的主要剧情：明确异常或目标、现实阻力、风险和玩家可选择的介入点。',
      requiredBeats: ['形成具体目标或重大异常', '让阻力以可观察方式进入', '留下玩家能够采取的下一步行动'],
      expectedProgressScore: 3,
      allowPhaseTransition: false,
      transitionTarget: null,
      allowedEncounterKinds: encounterKindsFor('establish', state),
      coordination: { primaryArc: null, encounterRole: 'advance', styleMayNotOverride: true },
      ...(scriptFocus ? { scriptFocus } : {}),
    }
  }

  const arc = state.activeArc
  const target = nextPhase(arc.phase)
  const legacyBudget = PHASE_BUDGETS[pace][arc.phase]
  const dueForTransition = plotControl.transitionRequired
    || (phaseCompleted >= budgetSnapshot.phaseRanges[arc.phase].min && state.pacing.progressDebt >= 6)
    || (phaseCompleted === 0 && arc.phaseTurnCount >= legacyBudget.max * 2)
  let action: DirectorAction = 'hold'
  let expectedProgressScore = 1
  let requiredChange = '允许玩家处理当前情境，但必须留下至少一个具体、可观察的局部结果。'
  let requiredBeats = ['完成玩家本轮行动的直接反馈', '保持当前主线目标和阻力可感知']

  if (!currentMinor) {
    action = dueForTransition ? 'milestone' : 'establish'
    expectedProgressScore = dueForTransition ? 4 : 3
      requiredChange = dueForTransition
        ? `建立并完成一个${arc.phase === 'ending' ? '结局' : '阶段收束'}小剧情，兑现当前阶段已有冲突，不得再开启无关线索。`
      : selectedMinorScript
        ? `执行当前阶段的标准小剧本“${selectedMinorScript.title}”；不得另建无关小剧情，正文必须兑现其目标并推动主剧本。`
        : `建立一个${suggestedMinorType === 'quick' ? '快速' : suggestedMinorType === 'focus' ? '重点' : '标准'}小剧情；它必须有局部目标、阻力、四要素和对主线的明确贡献。`
    requiredBeats = ['在隐藏状态中创建当前小剧情', '明确它对当前大剧情阶段的贡献', '正文必须产生可定位的局势变化']
  } else if (plotControl.minorClosureRequired) {
    action = arc.phase === 'ending' ? 'resolve' : 'milestone'
    expectedProgressScore = 4
    requiredChange = `小剧情“${currentMinor.title}”已经达到片段上限，本轮必须形成成功、失败或代价结果并完成它；禁止继续铺垫。`
    requiredBeats = ['完成当前小剧情而不是创建新支线', '结果必须回写当前大剧情', '用正文证据证明结果已经发生']
  }

  else if (currentMinor && arc.phase === 'climax') {
    action = 'climax'
    expectedProgressScore = 4
    requiredChange = '集中兑现核心冲突，迫使局势产生高代价选择、公开揭露、胜负变化或不可逆结果。'
    requiredBeats = ['让核心阻力直接作用于当前场景', '产生不可逆变化或高代价选择', '保留玩家对关键选择的决定权']
  } else if (currentMinor && arc.phase === 'ending') {
    action = 'resolve'
    expectedProgressScore = 5
    requiredChange = '让当前剧情的结果正式落地，说明目标、代价、关系和局势的实际结果，不再制造同一冲突的新引子。'
    requiredBeats = ['落实当前剧情胜负或结果', '兑现主要代价和关系变化', '收束旧冲突而非重新包装']
  } else if (currentMinor
    && (strictProgressWarning || dueForTransition
      || (plotControl.phaseClosureWindow && currentMinorCanResolve)
      || state.pacing.stagnationCount >= 4)) {
    action = 'milestone'
    expectedProgressScore = 4
    requiredChange = strictProgressWarning
      ? `严厉推进警告：已经连续 ${state.pacing.stagnationCount} 个剧情片段没有主线实质变化。本轮必须完成“${arc.phaseGoal || '当前阶段目标'}”的可验证里程碑，关闭或兑现既有路线，并产生不可逆结果；不得继续写日常、讨论、换场景或新引子。`
      : `完成“${arc.phaseGoal || '当前阶段目标'}”的关键里程碑，并为进入下一阶段制造不可逆条件。`
    requiredBeats = strictProgressWarning
      ? ['必须兑现既有里程碑或产生不可逆变化', '必须让主线目标主动作用于玩家当前处境', '不得用新线索、新人物或气氛描写替代结果']
      : ['完成或封闭至少一条现有路线', '产生不可逆事实、代价或立场变化', '不得用新的无关悬念替代旧冲突']
  } else if (currentMinor && (state.pacing.stagnationCount >= 3 || state.pacing.progressDebt >= 5)) {
    action = 'escalate'
    expectedProgressScore = 3
    requiredChange = '让对手、环境或未解决后果主动施压，使风险、资源、关系或可行路线发生实质变化。'
    requiredBeats = ['压力必须来自已有因果', '至少改变风险、资源、关系或路线之一', '变化必须给玩家新的可行动局面']
  } else if (currentMinor && (state.pacing.stagnationCount >= 2 || state.pacing.progressDebt >= 3)) {
    action = 'reveal'
    expectedProgressScore = 2
    requiredChange = '提供一条能够改变玩家下一步选择的可行动信息，或让重要关系发生明确变化。'
    requiredBeats = ['新信息必须可被玩家观察', '信息要指向具体行动而非只增加神秘感']
  }

  return {
    ...base,
    action,
    requiredChange,
    requiredBeats,
    expectedProgressScore,
    allowPhaseTransition: dueForTransition && target != null,
    transitionTarget: action === 'resolve' ? null : target,
    allowedEncounterKinds: encounterKindsFor(action, state),
    coordination: {
      primaryArc: arc.id,
      encounterRole: action === 'resolve' ? 'resolve'
        : action === 'climax' ? 'payoff'
          : action === 'milestone' ? 'advance'
            : action === 'escalate' ? 'pressure' : 'background',
      styleMayNotOverride: true,
    },
    renderMode: action === 'hold' || action === 'resolve'
      ? 'compressed'
      : action === 'climax' ? 'setpiece' : 'standard',
    ...(scriptFocus ? { scriptFocus } : {}),
  }
}

function normalizedEvidence(value: string): string {
  return value.toLocaleLowerCase().replace(/[\s\p{P}\p{S}]+/gu, '')
}

export function evidenceAppearsInContent(content: string, evidence: string): boolean {
  const needle = normalizedEvidence(evidence)
  return needle.length >= 4 && normalizedEvidence(content).includes(needle)
}

function cleanText(value: unknown, max = 240): string {
  return typeof value === 'string' ? value.trim().slice(0, max) : ''
}

function validInitialization(value: unknown, expectedPhase: StoryPhase | null): DirectorArcInitialization | null {
  if (!value || typeof value !== 'object') return null
  const raw = value as Partial<DirectorArcInitialization>
  const title = cleanText(raw.title, 80)
  const objective = cleanText(raw.objective)
  const opposition = cleanText(raw.opposition)
  const phaseGoal = cleanText(raw.phaseGoal)
  const phase = raw.phase === 'hook' || raw.phase === 'beginning' ? raw.phase : null
  if (!title || !objective || !opposition || !phaseGoal || !phase || phase !== expectedPhase) return null
  if (raw.scope !== 'major') return null
  const exitCriteria = strings(raw.exitCriteria, 8)
  const plannedMilestones = strings(raw.plannedMilestones, 12)
  if (exitCriteria.length === 0 || plannedMilestones.length === 0) return null
  return {
    title,
    scope: 'major',
    phase,
    objective,
    opposition,
    stakes: strings(raw.stakes, 8),
    phaseGoal,
    exitCriteria,
    plannedMilestones,
  }
}

function updateThreads(
  current: StoryThread[],
  updates: DirectorDelta['threadUpdates'],
  content: string,
  rejected: string[],
): StoryThread[] {
  const next = current.map(item => ({ ...item }))
  for (const update of Array.isArray(updates) ? updates : []) {
    if (!update || typeof update !== 'object') continue
    const title = cleanText(update.title, 120)
    const evidence = cleanText(update.evidence, 160)
    if (!title || !evidenceAppearsInContent(content, evidence)) {
      rejected.push('未接受缺少正文证据的剧情线程更新')
      continue
    }
    const existing = next.find(item => item.id === update.id || item.title === title)
    if (existing) {
      existing.status = ['resolved', 'abandoned'].includes(update.status) ? update.status : 'active'
      existing.importance = boundedInt(update.importance, existing.importance, 1, 5) as StoryThread['importance']
      existing.sourceEvidence = evidence
      existing.updatedAt = nowIso()
    } else if (update.status === 'active') {
      next.push({
        id: directorId('thread'), title, status: 'active',
        importance: boundedInt(update.importance, 3, 1, 5) as StoryThread['importance'],
        sourceEvidence: evidence, updatedAt: nowIso(),
      })
    }
  }
  return next.slice(-50)
}

function updateConsequences(
  current: StoryConsequence[],
  updates: DirectorDelta['consequenceUpdates'],
  content: string,
  rejected: string[],
): StoryConsequence[] {
  const next = current.map(item => ({ ...item }))
  for (const update of Array.isArray(updates) ? updates : []) {
    if (!update || typeof update !== 'object') continue
    const source = cleanText(update.source, 180)
    const evidence = cleanText(update.evidence, 160)
    if (!source || !evidenceAppearsInContent(content, evidence)) {
      rejected.push('未接受缺少正文证据的后果更新')
      continue
    }
    const existing = next.find(item => item.id === update.id || item.source === source)
    if (existing) {
      existing.status = update.status === 'resolved' ? 'resolved' : 'pending'
      existing.severity = boundedInt(update.severity, existing.severity, 1, 5) as StoryConsequence['severity']
      existing.evidence = evidence
      existing.updatedAt = nowIso()
      if (typeof update.dueAfterTurns === 'number') existing.dueAfterTurns = boundedInt(update.dueAfterTurns, 1, 0, 100)
    } else if (update.status === 'pending') {
      next.push({
        id: directorId('consequence'), source, status: 'pending',
        severity: boundedInt(update.severity, 3, 1, 5) as StoryConsequence['severity'],
        ...(typeof update.dueAfterTurns === 'number'
          ? { dueAfterTurns: boundedInt(update.dueAfterTurns, 1, 0, 100) }
          : {}),
        evidence, updatedAt: nowIso(),
      })
    }
  }
  return next.slice(-50)
}

function updateSubArcs(
  current: StoryArcState[],
  updates: DirectorDelta['subArcUpdates'],
  content: string,
  rejected: string[],
  plan: DirectorPlan,
): {
  active: StoryArcState[]
  completed: StoryArcState[]
  evidence: string[]
  updated: boolean
  created: boolean
  scoreFloor: number
} {
  const active = current.map(arc => ({
    ...arc,
    stakes: [...arc.stakes],
    exitCriteria: [...arc.exitCriteria],
    plannedMilestones: [...arc.plannedMilestones],
    completedMilestones: [...arc.completedMilestones],
  }))
  const completed: StoryArcState[] = []
  const acceptedEvidence: string[] = []
  let updated = false
  let created = false
  let scoreFloor = 0
  const requestedUpdates = Array.isArray(updates) ? updates.filter(Boolean) : []
  if (requestedUpdates.length > 1) {
    rejected.push('每个剧情片段只能更新唯一的当前小剧情')
    return { active: active.slice(0, 1), completed, evidence: acceptedEvidence, updated, created, scoreFloor }
  }
  for (const update of requestedUpdates) {
    if (!update || typeof update !== 'object') continue
    const title = cleanText(update.title, 80)
    const evidence = cleanText(update.evidence, 160)
    if (!title || !evidenceAppearsInContent(content, evidence)) {
      rejected.push('未接受缺少正文证据的局部剧情更新')
      continue
    }
    const existingIndex = active.findIndex(arc => arc.id === update.id || arc.title === title)
    const existing = existingIndex >= 0 ? active[existingIndex] : null
    if (update.action === 'create' || update.action === 'createResolved') {
      if (existing || active.length > 0 || update.phase !== 'beginning') {
        rejected.push('同一时间只能执行一个小剧情，且必须从开端创建')
        continue
      }
      const boundScript = plan.scriptFocus?.minorScript
      const expectedMajorPhase = plan.phase ?? 'beginning'
      if (boundScript && boundScript.majorPhase !== expectedMajorPhase) {
        rejected.push('候选小剧本不属于当前大剧情阶段')
        continue
      }
      if (boundScript && title !== boundScript.title) {
        rejected.push(`本轮必须建立已绑定的小剧本“${boundScript.title}”`)
        continue
      }
      const objective = boundScript?.objective || cleanText(update.objective)
      const opposition = cleanText(update.opposition)
      const phaseGoal = cleanText(update.phaseGoal)
      const exitCriteria = strings(update.exitCriteria, 8)
      const plannedMilestones = strings(update.plannedMilestones, 12)
      const majorContribution = boundScript?.completionCondition || cleanText(update.majorContribution)
      if (!objective || !opposition || !phaseGoal || !exitCriteria.length || !plannedMilestones.length || !majorContribution) {
        rejected.push('小剧情缺少目标、阻力、四要素结构、退出条件或大剧情贡献')
        continue
      }
      const minorType = boundScript?.minorType ?? (['quick', 'standard', 'focus'].includes(String(update.minorType))
        ? update.minorType as MinorStoryType : plan.plotControl.suggestedMinorType
      )
      const fragmentBudget = boundScript?.fragmentBudget ?? plan.plotControl.budgetSnapshot.minorFragments[minorType]
      const stamp = nowIso()
      const createdArc: StoryArcState = {
        id: directorId('subarc'), scope: 'minor', title, phase: 'beginning', objective, opposition,
        majorScriptId: plan.scriptFocus?.id,
        minorScriptId: boundScript?.id,
        majorPhase: expectedMajorPhase,
        sourceFragments: [],
        stakes: strings(update.stakes, 8), phaseGoal, exitCriteria, plannedMilestones,
        completedMilestones: [], phaseTurnCount: 1, phaseEffectiveTurns: 1, stagnationCount: 0,
        completedPhases: ['beginning'], totalTurnCount: 1,
        minorType, fragmentBudget: { ...fragmentBudget }, fragmentCount: 1,
        effectiveFragmentCount: 1, majorContribution, minorTypeChanged: false,
        createdAt: stamp, updatedAt: stamp,
      }
      if (update.action === 'createResolved') {
        if (fragmentBudget.min > 1) {
          rejected.push('只有片段下限为 1 的快速小剧情允许在创建片段内直接完成')
          continue
        }
        createdArc.phase = 'ending'
        createdArc.completedPhases = ['beginning', 'development', 'climax', 'ending']
        completed.push(createdArc)
        scoreFloor = Math.max(scoreFloor, 4)
      } else {
        active.push(createdArc)
        scoreFloor = Math.max(scoreFloor, 3)
      }
      acceptedEvidence.push(evidence)
      updated = true
      created = true
      continue
    }
    if (!existing) {
      rejected.push('找不到要更新的局部剧情')
      continue
    }
    if (update.action === 'abandon') {
      active.splice(existingIndex, 1)
      acceptedEvidence.push(evidence)
      updated = true
      scoreFloor = Math.max(scoreFloor, 1)
      continue
    }
    let minorType = existing.minorType ?? plan.plotControl.suggestedMinorType
    let fragmentBudget = existing.fragmentBudget ?? plan.plotControl.currentMinorBudget
    let minorTypeChanged = existing.minorTypeChanged === true
    if (update.minorType && update.minorType !== minorType) {
      if (!plan.plotControl.budgetSnapshot.allowDynamicTypeChange || minorTypeChanged) {
        rejected.push('当前小剧情不允许再次动态调整类型')
        continue
      }
      const changedBudget = plan.plotControl.budgetSnapshot.minorFragments[update.minorType]
      const prospectiveCount = (existing.fragmentCount ?? existing.totalTurnCount) + 1
      if (changedBudget.max < prospectiveCount) {
        rejected.push('目标小剧情类型的片段上限低于当前进度')
        continue
      }
      minorType = update.minorType
      fragmentBudget = changedBudget
      minorTypeChanged = true
    }
    const currentFragmentCount = existing.fragmentCount ?? existing.totalTurnCount
    const nextFragmentCount = update.action === 'resolve' && currentFragmentCount >= fragmentBudget.max
      ? currentFragmentCount : currentFragmentCount + 1
    if (update.action === 'resolve') {
      const majorContribution = cleanText(update.majorContribution) || existing.majorContribution || ''
      const mayFinish = nextFragmentCount >= fragmentBudget.min
        && (plan.plotControl.budgetSnapshot.allowEarlyCompletion || nextFragmentCount >= fragmentBudget.max)
      if (!mayFinish || !majorContribution) {
        rejected.push('小剧情尚未达到完成下限，或缺少对大剧情的明确贡献')
        continue
      }
      const resolved: StoryArcState = {
        ...existing,
        minorType,
        minorTypeChanged,
        fragmentBudget: { ...fragmentBudget },
        fragmentCount: nextFragmentCount,
        effectiveFragmentCount: (existing.effectiveFragmentCount ?? existing.phaseEffectiveTurns) + 1,
        totalTurnCount: existing.totalTurnCount + 1,
        phaseTurnCount: existing.phaseTurnCount + 1,
        phaseEffectiveTurns: existing.phaseEffectiveTurns + 1,
        stagnationCount: 0,
        phase: 'ending',
        majorContribution,
        completedPhases: [...new Set<StoryPhase>([...existing.completedPhases, 'ending'])],
        updatedAt: nowIso(),
      }
      completed.push(resolved)
      active.splice(existingIndex, 1)
      acceptedEvidence.push(evidence)
      updated = true
      scoreFloor = Math.max(scoreFloor, 4)
      continue
    }
    if (!['progress', 'advance'].includes(update.action)) continue
    if (nextFragmentCount >= fragmentBudget.max) {
      rejected.push('小剧情已达到片段硬上限，本轮必须提交明确结局')
      continue
    }
    const nextMinorPhase = minorPhaseForFragment(nextFragmentCount, fragmentBudget)
    active[existingIndex] = {
      ...existing,
      minorType,
      minorTypeChanged,
      fragmentBudget: { ...fragmentBudget },
      fragmentCount: nextFragmentCount,
      effectiveFragmentCount: (existing.effectiveFragmentCount ?? existing.phaseEffectiveTurns) + 1,
      totalTurnCount: existing.totalTurnCount + 1,
      phaseTurnCount: existing.phaseTurnCount + 1,
      phaseEffectiveTurns: existing.phaseEffectiveTurns + 1,
      stagnationCount: 0,
      phase: nextMinorPhase,
      completedPhases: [...new Set<StoryPhase>([...existing.completedPhases, nextMinorPhase])],
      updatedAt: nowIso(),
    }
    acceptedEvidence.push(evidence)
    updated = true
    scoreFloor = Math.max(scoreFloor, 1)
  }
  return { active: active.slice(0, 1), completed, evidence: acceptedEvidence, updated, created, scoreFloor }
}

export function evaluateDirectorTurn(
  stateValue: DirectorState,
  plan: DirectorPlan,
  deltaValue: DirectorDelta | undefined,
  content: string,
  sourceMessageId: string,
): DirectorEvaluation {
  const state = normalizeDirectorState(stateValue)
  if (sourceMessageId && state.lastCommittedMessageId === sourceMessageId) {
    return {
      accepted: false, planMatched: false, planSatisfied: false, progressScore: 0, mainlineChanged: false,
      phaseTransitioned: false, arcEstablished: false, arcCompleted: false,
      minorUpdated: false, minorCompleted: false,
      acceptedEvidence: [], rejectedReasons: ['该消息已经提交过导演状态'], nextState: state,
    }
  }
  const delta = deltaValue && typeof deltaValue === 'object' ? deltaValue : {}
  const rejectedReasons: string[] = []
  const planMatched = delta.planId === plan.id
    && (delta.turnId == null || delta.turnId === plan.control.turnId)
    && plan.stateRevision === state.revision
    && plan.turnIndex === state.turnIndex + 1
  if (!planMatched) rejectedReasons.push('导演计划编号或状态版本不匹配')

  let activeArc = state.activeArc ? {
    ...state.activeArc,
    phaseMinorCompleted: { ...(state.activeArc.phaseMinorCompleted ?? {}) } as Record<StoryPhase, number>,
  } : null
  let arcEstablished = false
  let arcCompleted = false
  let phaseTransitioned = false
  let score = 0
  const acceptedKinds = new Set<ProgressChangeKind>()
  let completedMilestonesThisTurn: string[] = []
  const acceptedEvidence: string[] = []
  if (activeArc) {
    activeArc.phaseTurnCount += 1
    activeArc.totalTurnCount += 1
    activeArc.stagnationCount += 1
    activeArc.updatedAt = nowIso()
  }

  if (planMatched && !activeArc && plan.action === 'establish') {
    const initialization = validInitialization(delta.arcInitialization, plan.phase)
    const changes = Array.isArray(delta.changes) ? delta.changes : []
    const groundedChange = changes.find(change =>
      change?.relevance === 'mainline' && evidenceAppearsInContent(content, cleanText(change.evidence, 160)),
    )
    if (initialization && groundedChange) {
      const stamp = nowIso()
      activeArc = {
        id: directorId('arc'),
        ...initialization,
        majorScriptId: plan.scriptFocus?.id,
        sourceFragments: [],
        completedMilestones: [], phaseTurnCount: 1, phaseEffectiveTurns: 1, stagnationCount: 0,
        completedPhases: [initialization.phase], totalTurnCount: 1,
        budgetSnapshot: plan.plotControl.budgetSnapshot,
        phaseMinorCompleted: { hook: 0, beginning: 0, development: 0, climax: 0, ending: 0 },
        createdAt: stamp, updatedAt: stamp,
      }
      if (plan.scriptFocus) {
        activeArc.title = plan.scriptFocus.title
        activeArc.objective = plan.scriptFocus.defaultRoute || activeArc.objective
        if (plan.scriptFocus.completionCondition
          && !activeArc.exitCriteria.includes(plan.scriptFocus.completionCondition)) {
          activeArc.exitCriteria.push(plan.scriptFocus.completionCondition)
        }
      }
      arcEstablished = true
      score = 3
      acceptedEvidence.push(cleanText(groundedChange.evidence, 160))
    } else {
      rejectedReasons.push('主线初始化缺少必要字段或正文证据')
    }
  } else if (planMatched && activeArc) {
    if (activeArc.majorScriptId && plan.scriptFocus?.id && activeArc.majorScriptId !== plan.scriptFocus.id) {
      rejectedReasons.push('当前主线绑定的主剧本与本轮主剧本不一致')
    } else if (!activeArc.majorScriptId && plan.scriptFocus?.id) {
      activeArc.majorScriptId = plan.scriptFocus.id
    }
    const changes = Array.isArray(delta.changes) ? delta.changes : []
    for (const change of changes.slice(0, 12)) {
      if (!change || !Object.prototype.hasOwnProperty.call(CHANGE_SCORES, change.kind)) continue
      const evidence = cleanText(change.evidence, 160)
      if (!evidenceAppearsInContent(content, evidence)) {
        rejectedReasons.push(`未在正文中找到“${cleanText(change.description, 40)}”的证据`)
        continue
      }
      acceptedEvidence.push(evidence)
      if (change.relevance === 'mainline') {
        score = Math.max(score, CHANGE_SCORES[change.kind])
        acceptedKinds.add(change.kind)
      }
    }
    if (score >= 2) activeArc.phaseEffectiveTurns += 1

    const completed = strings(delta.completedMilestones, 12)
      .filter(item => activeArc!.plannedMilestones.includes(item) && !activeArc!.completedMilestones.includes(item))
    completedMilestonesThisTurn = completed
    if (completed.length && score >= 4) activeArc.completedMilestones.push(...completed)

    // The model may propose the next phase setup, but the program owns the
    // transition decision after a complete minor plot has been verified.
  }

  const acceptedDelta = planMatched ? delta : {}
  let nextThreads = updateThreads(state.activeThreads, acceptedDelta.threadUpdates, content, rejectedReasons)
  let nextConsequences = updateConsequences(
    state.unresolvedConsequences, acceptedDelta.consequenceUpdates, content, rejectedReasons,
  )
  nextConsequences = nextConsequences.map(item => item.status === 'pending' && typeof item.dueAfterTurns === 'number'
    ? { ...item, dueAfterTurns: Math.max(0, item.dueAfterTurns - 1) }
    : item)
  const subArcResult = updateSubArcs(
    state.subArcs, acceptedDelta.subArcUpdates, content, rejectedReasons, plan,
  )
  acceptedEvidence.push(...subArcResult.evidence)
  score = Math.max(score, subArcResult.scoreFloor)

  if (activeArc && subArcResult.completed.length > 0) {
    const completedPhase = activeArc.phase
    const counts = activeArc.phaseMinorCompleted
      ?? { hook: 0, beginning: 0, development: 0, climax: 0, ending: 0 }
    counts[completedPhase] = (counts[completedPhase] ?? 0) + subArcResult.completed.length
    activeArc.phaseMinorCompleted = counts
    const snapshot = activeArc.budgetSnapshot ?? plan.plotControl.budgetSnapshot
    activeArc.budgetSnapshot = snapshot
    const proposed = delta.phaseTransition
    const explicitSequentialRequest = proposed?.from === completedPhase && proposed.to === nextPhase(completedPhase)
    const reachedTarget = counts[completedPhase] >= snapshot.phaseTargets[completedPhase]
    const reachedMaximum = counts[completedPhase] >= snapshot.phaseRanges[completedPhase].max
    const reachedMinimum = counts[completedPhase] >= snapshot.phaseRanges[completedPhase].min
    const mayLeaveEarly = snapshot.allowEarlyCompletion && reachedMinimum && explicitSequentialRequest && score >= 4
    if (reachedTarget || reachedMaximum || mayLeaveEarly) {
      const targetPhase = nextPhase(completedPhase)
      if (!targetPhase) {
        arcCompleted = score >= 4
        if (!arcCompleted) rejectedReasons.push('大剧情结局缺少足以落地胜负、代价或后果的主线变化')
      } else {
        const setup = delta.nextPhaseSetup
        activeArc.phase = targetPhase
        activeArc.phaseGoal = cleanText(setup?.phaseGoal)
          || `${targetPhase === 'beginning' ? '明确主线目标、阻力与首条行动路线' : targetPhase === 'development' ? '通过连续小剧情改变局势并兑现主线里程碑' : targetPhase === 'climax' ? '让核心阻力正面介入并形成高代价选择' : '落实胜负、代价、关系和世界后果'}`
        activeArc.exitCriteria = strings(setup?.exitCriteria, 8)
        if (!activeArc.exitCriteria.length) activeArc.exitCriteria = [`完成${snapshot.phaseTargets[targetPhase]}个对主线有明确贡献的小剧情`]
        activeArc.plannedMilestones = strings(setup?.plannedMilestones, 12)
        if (!activeArc.plannedMilestones.length) activeArc.plannedMilestones = [`完成${targetPhase}阶段的决定性状态变化`]
        activeArc.completedMilestones = []
        activeArc.phaseTurnCount = 0
        activeArc.phaseEffectiveTurns = 0
        activeArc.stagnationCount = 0
        activeArc.completedPhases = [...new Set<StoryPhase>([...activeArc.completedPhases, targetPhase])]
        phaseTransitioned = true
      }
    }
  }

  // A clue or relationship beat is useful evidence, but does not reset
  // stagnation. Only a concrete state change counts as mainline movement.
  const mainlineChanged = arcEstablished || score >= 3 || subArcResult.completed.length > 0
  const completedMinorThisTurn = subArcResult.completed.length > 0
  const createdMinorThisTurn = subArcResult.created
  const actionHasProof = plan.plotControl.transitionRequired
    ? completedMinorThisTurn && (phaseTransitioned || arcCompleted)
    : plan.action === 'establish'
      ? arcEstablished || createdMinorThisTurn
      : plan.action === 'milestone'
    ? completedMinorThisTurn || completedMilestonesThisTurn.length > 0 || ['irreversible', 'milestone', 'resolution'].some(kind => acceptedKinds.has(kind as ProgressChangeKind))
    : plan.action === 'climax'
      ? ['irreversible', 'milestone', 'resolution'].some(kind => acceptedKinds.has(kind as ProgressChangeKind))
      : plan.action === 'resolve'
        ? (completedMinorThisTurn || arcCompleted) && ['irreversible', 'milestone', 'resolution'].some(kind => acceptedKinds.has(kind as ProgressChangeKind))
        : true
  const planSatisfied = planMatched && actionHasProof && (
    arcEstablished
    || score >= plan.expectedProgressScore
    || (plan.action === 'hold' && acceptedEvidence.length > 0)
  )
  if (activeArc && mainlineChanged && planSatisfied) activeArc.stagnationCount = 0
  let realizedSpecialEncounter: 'tragedy' | 'payoff' | null = null
  if (planMatched && planSatisfied && (plan.encounterKind === 'tragedy' || plan.encounterKind === 'payoff')) {
    const outcome = delta.encounterOutcome
    const outcomeEvidence = cleanText(outcome?.evidence, 160)
    if (outcome?.kind === plan.encounterKind && evidenceAppearsInContent(content, outcomeEvidence)) {
      realizedSpecialEncounter = outcome.kind
      acceptedEvidence.push(outcomeEvidence)
    } else {
      rejectedReasons.push(`${plan.encounterKind === 'tragedy' ? '悲剧' : '爽点'}遭遇缺少正文证据，未启动冷却`)
    }
  }
  const nextTurnIndex = state.turnIndex + 1
  const nextPacing = {
    stagnationCount: mainlineChanged && planSatisfied ? 0 : Math.min(100, state.pacing.stagnationCount + 1),
    progressDebt: mainlineChanged && planSatisfied
      ? Math.max(0, state.pacing.progressDebt - score)
      : Math.min(100, state.pacing.progressDebt + Math.max(
        plan.strictProgressWarning ? 2 : 1,
        plan.expectedProgressScore - score,
      )),
    climaxDebt: activeArc?.phase === 'climax' && !arcCompleted
      ? Math.min(100, state.pacing.climaxDebt + (score >= 4 ? 0 : 2))
      : 0,
    lastMajorChangeTurn: arcEstablished || (planSatisfied && score >= 3) ? nextTurnIndex : state.pacing.lastMajorChangeTurn,
    recentScores: [...state.pacing.recentScores, score].slice(-8),
    recentActions: [...state.pacing.recentActions, plan.action].slice(-8),
  }
  const completedArcs = [...state.completedArcs, ...subArcResult.completed]
  if (arcCompleted && activeArc) completedArcs.push(activeArc)
  const nextState: DirectorState = {
    ...state,
    revision: state.revision + 1,
    turnIndex: nextTurnIndex,
    activeArc: arcCompleted ? null : activeArc,
    subArcs: subArcResult.active,
    completedArcs: completedArcs.slice(-20),
    activeThreads: nextThreads,
    unresolvedConsequences: nextConsequences,
    pacing: nextPacing,
    cooldowns: {
      tragedy: realizedSpecialEncounter === 'tragedy'
        ? 5 : Math.max(0, state.cooldowns.tragedy - 1),
      payoff: realizedSpecialEncounter === 'payoff'
        ? 4 : Math.max(0, state.cooldowns.payoff - 1),
    },
    lastCommittedMessageId: sourceMessageId,
    updatedAt: nowIso(),
  }
  return {
    accepted: planMatched,
    planMatched,
    planSatisfied,
    progressScore: score,
    mainlineChanged,
    phaseTransitioned,
    arcEstablished,
    arcCompleted,
    minorUpdated: subArcResult.updated,
    minorCompleted: completedMinorThisTurn,
    acceptedEvidence: [...new Set(acceptedEvidence)],
    rejectedReasons,
    nextState,
  }
}

/**
 * A second, model-independent gate over the parsed response. It does not infer
 * progress from prose alone: it verifies the frozen plan, accepted evidence and
 * concrete mainline collision before any project file may be committed.
 */
export function auditStoryTurn(
  content: string,
  plan: DirectorPlan,
  evaluation: DirectorEvaluation,
): StoryTurnAudit {
  const violations: string[] = []
  const prose = content.trim()
  const verifiedEvidence = evaluation.acceptedEvidence
    .map(item => cleanText(item, 160))
    .filter(item => evidenceAppearsInContent(prose, item))
  if (!evaluation.accepted || !evaluation.planMatched) violations.push('统一回合编号或导演状态版本不匹配')
  if (!evaluation.planSatisfied) violations.push('正文没有完成本轮导演计划要求的实质变化')
  if (evaluation.rejectedReasons.length > 0) violations.push(...evaluation.rejectedReasons)
  if (!prose || prose.length < 24) violations.push('剧情正文过短，无法形成可验证事件')
  if (evaluation.acceptedEvidence.length !== verifiedEvidence.length) violations.push('状态增量包含无法在正文定位的证据')

  const mainlineCollisionVerified = evaluation.mainlineChanged
    && verifiedEvidence.length > 0
    && !/(只是|仅仅|仍然|继续)(?:等待|讨论|观察|整理|思考)/.test(prose.slice(-120))
  const requiresMainlineCollision = plan.strictProgressWarning
    || ['escalate', 'milestone', 'climax', 'resolve'].includes(plan.action)
  if (requiresMainlineCollision && !mainlineCollisionVerified) {
    violations.push('严厉推进轮没有让主线以可观察结果主动影响当前局面')
  }
  if (plan.targetArcId && !evaluation.minorUpdated) {
    violations.push('有效剧情片段必须创建、推进、完成或明确放弃唯一的当前小剧情')
  }
  if (plan.plotControl.minorClosureRequired && !evaluation.minorCompleted) {
    violations.push('当前小剧情已到硬上限，本轮必须完成并回写大剧情')
  }
  if (plan.plotControl.transitionRequired && !(evaluation.phaseTransitioned || evaluation.arcCompleted)) {
    violations.push('当前大剧情阶段已到硬上限，本轮必须由程序顺序切换阶段')
  }
  if (plan.action !== 'hold' && verifiedEvidence.length === 0) violations.push('缺少独立可定位的正文证据')
  if (/\b(planId|turnId|progressScore|director plan)\b/i.test(prose)) violations.push('正文泄露了隐藏控制字段')

  return {
    accepted: violations.length === 0,
    mainlineCollisionVerified,
    verifiedEvidence: [...new Set(verifiedEvidence)],
    violations: [...new Set(violations)],
  }
}

export function directorPlanPrompt(plan: DirectorPlan, state: DirectorState): string {
  const arc = state.activeArc
  const currentMinor = state.subArcs[0] ?? null
  const arcLines = arc
    ? `当前主线：${arc.title}\n当前目标：${arc.objective}\n核心阻力：${arc.opposition}\n当前阶段内部目标：${arc.phaseGoal}\n阶段退出条件：${arc.exitCriteria.join('；') || '尚未明确'}`
    : '当前没有已建立的主线；本轮必须建立主线骨架。'
  const subArcLines = state.subArcs.length
    ? `当前局部剧情：\n${state.subArcs.map(item => `- ${item.id}｜${item.title}｜${item.phase}｜${item.objective}｜局部停滞 ${item.stagnationCount}${item.stagnationCount >= 3 ? '（本轮必须推进、收束或明确放弃，禁止继续悬置）' : ''}`).join('\n')}`
    : '当前没有结构化局部剧情。局部剧情只能从 beginning 开始，不使用引子。'
  const typeLabel = { quick: '快速', standard: '标准', focus: '重点' }[plan.plotControl.suggestedMinorType]
  const plotBudgetLines = `大剧情规模：${plan.plotControl.budgetSnapshot.scale}｜当前阶段已完成小剧情 ${plan.plotControl.phaseCompleted}/${plan.plotControl.phaseTarget}（硬上限 ${plan.plotControl.phaseMaximum}）
当前小剧情：${currentMinor ? `${currentMinor.title}｜${currentMinor.minorType ?? typeLabel}｜片段 ${currentMinor.fragmentCount ?? currentMinor.totalTurnCount}/${currentMinor.fragmentBudget?.max ?? plan.plotControl.currentMinorBudget.max}` : `尚未建立；本轮应建立${typeLabel}小剧情，片段范围 ${plan.plotControl.currentMinorBudget.min}-${plan.plotControl.currentMinorBudget.max}`}
${plan.scriptFocus ? `权威主剧本：${plan.scriptFocus.id}｜${plan.scriptFocus.title}｜完成条件：${plan.scriptFocus.completionCondition || '按正文结果判断'}
${plan.scriptFocus.minorScript ? `当前阶段权威小剧本：${plan.scriptFocus.minorScript.id}｜${plan.scriptFocus.minorScript.title}｜目标：${plan.scriptFocus.minorScript.objective}｜完成贡献：${plan.scriptFocus.minorScript.completionCondition}` : '当前阶段没有标准小剧本，允许按主剧本动态建立一个小剧情。'}` : '当前没有正式主剧本；允许从现有事实建立动态主线。'}
${plan.plotControl.phaseClosureWindow ? `阶段收束窗口：剩余目标不超过 ${plan.plotControl.budgetSnapshot.phaseClosureReserve} 个小剧情；只允许兑现或关闭既有矛盾，不再开启无关支线。` : ''}
${plan.plotControl.transitionRequired ? '阶段硬约束：当前阶段预算已经到达目标；必须先完成当前/收束小剧情，程序将在验收后顺序切换阶段。' : ''}
${plan.plotControl.minorClosureRequired ? '小剧情硬约束：本轮必须 resolve 当前小剧情，提供明确结果和 majorContribution；不得继续 progress。' : ''}`
  const specialEncounterLine = plan.encounterKind === 'tragedy' || plan.encounterKind === 'payoff'
    ? `本轮随机主轴为${plan.encounterKind === 'tragedy' ? '悲剧' : '爽点'}。只有正文真实兑现该主轴时，才在隐藏状态增量 encounterOutcome 中填写 kind 和正文连续证据短句；未兑现则不要填写。`
    : ''
  const scriptLines = plan.scriptFocus
    ? `本轮唯一主剧本：${plan.scriptFocus.id}｜${plan.scriptFocus.title}\n完成条件：${plan.scriptFocus.completionCondition || '尚未填写；必须围绕该剧本当前冲突形成可验证里程碑'}\n默认路线：${plan.scriptFocus.defaultRoute || '未设置；不得因此另开无关引子'}\n${plan.scriptFocus.lifecycleManagedByDirector ? '该标准剧本的 pending/active/completed 由程序根据导演与小剧情验收结果同步；不得通过 scriptUpdates 直接改写。' : '本轮状态增量可报告该旧版剧本的证据化状态变化；其他剧本只作为背景时钟。'}`
    : '当前没有可用主剧本；导演主线优先，禁止同时展开多个新剧本。'
  return `[Storydex 隐藏剧情导演计划]
计划编号：${plan.id}
统一回合控制：${plan.control.turnId}｜状态版本 ${plan.control.stateRevision}｜随机种子 ${plan.randomSeed}
导演动作：${plan.action}
叙事速度：${plan.renderMode === 'compressed' ? '压缩叙事；跳过无关过程，直接写行动结果、代价和下一处有效局面，不得把赶路、盘点、等待或重复讨论扩成完整片段' : plan.renderMode === 'setpiece' ? '重点场景；集中篇幅写核心冲突、选择和不可逆结果，禁止在高潮前继续绕行' : '标准场景；只保留推动当前目标的动作、对话和细节'}
${plan.strictProgressWarning ? `严厉推进警告：已连续 ${state.pacing.stagnationCount} 个剧情片段没有主线实质变化。下一轮不得继续温水推进；必须形成主线可观察的不可逆变化、既有里程碑兑现或路线收束，否则本轮状态不视为有效推进。\n` : ''}
主线协同：${plan.coordination.primaryArc ?? '尚未建立'}｜遭遇职责：${plan.coordination.encounterRole}｜风格预设不得覆盖本轮实质变化要求
${scriptLines}
${arcLines}
${plotBudgetLines}
${subArcLines}
本轮必要变化：${plan.requiredChange}
必须覆盖：
${plan.requiredBeats.map(item => `- ${item}`).join('\n')}
${specialEncounterLine}
禁止结果：
${plan.forbiddenOutcomes.map(item => `- ${item}`).join('\n')}
${plan.allowPhaseTransition && plan.transitionTarget ? `程序验收小剧情完成后将顺序进入：${plan.transitionTarget}` : '本轮默认不切换大剧情阶段。'}

小剧情状态规则：每个可归档剧情片段都必须且只能提交一条 subArcUpdates。没有当前小剧情时必须 create，并填写 minorType、majorContribution、目标、阻力、阶段目标、退出条件和里程碑；若快速小剧情的片段下限为 1 且正文已完整兑现四要素，可使用 createResolved 在同一片段建立并完成。已有小剧情时用 progress 表示继续，用 resolve 表示以成功、失败或代价形成结局。有权威小剧本时 title 必须逐字照抄小剧本标题，禁止另建无关小剧情。允许动态调整类型时，可通过 minorType 调整一次，程序会冻结新预算。随机事件本身不计入小剧情数量，除非正式创建为小剧情。
本区块是隐藏执行约束，不得在正文或行动建议中暴露计划编号、阶段名、进度分、导演动作、预算和未来路线。随机遭遇只能作为完成本计划的载体；不适配时弱化为背景，不能抢占主线。`
}
