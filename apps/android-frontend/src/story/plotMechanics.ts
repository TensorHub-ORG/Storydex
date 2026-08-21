export type MajorStoryScale = 'fast' | 'balanced' | 'detailed' | 'custom'
export type MinorStoryType = 'quick' | 'standard' | 'focus'
export type MajorStoryPhase = 'hook' | 'beginning' | 'development' | 'climax' | 'ending'

export interface CountRange {
  min: number
  max: number
}

export type PhaseCountRanges = Record<MajorStoryPhase, CountRange>
export type MinorFragmentRanges = Record<MinorStoryType, CountRange>
export type MinorTypeMix = Record<MinorStoryType, number>

export interface PlotMechanicsSettings {
  scale: MajorStoryScale
  totalMinorPlots: CountRange
  phaseMinorPlots: PhaseCountRanges
  minorFragments: MinorFragmentRanges
  minorTypeMix: MinorTypeMix
  allowEarlyCompletion: boolean
  allowDynamicTypeChange: boolean
  phaseClosureReserve: number
}

export interface MajorBudgetSnapshot {
  schemaVersion: 1
  scale: MajorStoryScale
  totalRange: CountRange
  totalTarget: number
  phaseRanges: PhaseCountRanges
  phaseTargets: Record<MajorStoryPhase, number>
  minorFragments: MinorFragmentRanges
  minorTypeMix: MinorTypeMix
  allowEarlyCompletion: boolean
  allowDynamicTypeChange: boolean
  phaseClosureReserve: number
  seed: number
  createdAt: string
}

export interface PlotEstimate {
  minorMin: number
  minorMax: number
  fragmentMin: number
  fragmentMax: number
}

export const MAJOR_PHASES: MajorStoryPhase[] = ['hook', 'beginning', 'development', 'climax', 'ending']
export const MINOR_STORY_TYPES: MinorStoryType[] = ['quick', 'standard', 'focus']

/**
 * 阶段与小剧情类型的中文标签。
 *
 * 此前有三份：`project.ts` 重构告警里一份、`SettingsView.vue` 里 `STORY_PHASE_LABELS`
 * 和 `MINOR_TYPE_LABELS` 各一份。同一个词在不同界面漂移过（'快速' / '快节奏'），
 * 所以收成一处。`directorMechanics.StoryPhase` 与 `MajorStoryPhase` 是结构相同的联合类型，
 * 两边都能用这一份索引。
 */
export const MAJOR_PHASE_LABELS: Record<MajorStoryPhase, string> = {
  hook: '引子', beginning: '开端', development: '发展', climax: '高潮', ending: '结局',
}
export const MINOR_TYPE_LABELS: Record<MinorStoryType, string> = {
  quick: '快速', standard: '标准', focus: '重点',
}

const SCALE_PRESETS: Record<Exclude<MajorStoryScale, 'custom'>, Pick<PlotMechanicsSettings,
  'totalMinorPlots' | 'phaseMinorPlots' | 'minorTypeMix'>> = {
  fast: {
    totalMinorPlots: { min: 5, max: 10 },
    phaseMinorPlots: {
      hook: { min: 1, max: 1 }, beginning: { min: 1, max: 2 }, development: { min: 1, max: 4 },
      climax: { min: 1, max: 2 }, ending: { min: 1, max: 1 },
    },
    minorTypeMix: { quick: 60, standard: 35, focus: 5 },
  },
  balanced: {
    totalMinorPlots: { min: 15, max: 20 },
    phaseMinorPlots: {
      hook: { min: 1, max: 2 }, beginning: { min: 3, max: 4 }, development: { min: 6, max: 8 },
      climax: { min: 3, max: 4 }, ending: { min: 2, max: 2 },
    },
    minorTypeMix: { quick: 25, standard: 55, focus: 20 },
  },
  detailed: {
    totalMinorPlots: { min: 25, max: 30 },
    phaseMinorPlots: {
      hook: { min: 1, max: 2 }, beginning: { min: 4, max: 5 }, development: { min: 11, max: 13 },
      climax: { min: 5, max: 6 }, ending: { min: 4, max: 4 },
    },
    minorTypeMix: { quick: 15, standard: 50, focus: 35 },
  },
}

export const DEFAULT_MINOR_FRAGMENT_RANGES: MinorFragmentRanges = {
  quick: { min: 1, max: 2 },
  standard: { min: 3, max: 5 },
  focus: { min: 6, max: 9 },
}

function cloneRange(value: CountRange): CountRange { return { min: value.min, max: value.max } }
function clonePhaseRanges(value: PhaseCountRanges): PhaseCountRanges {
  return Object.fromEntries(MAJOR_PHASES.map(phase => [phase, cloneRange(value[phase])])) as PhaseCountRanges
}
function cloneFragmentRanges(value: MinorFragmentRanges): MinorFragmentRanges {
  return Object.fromEntries(MINOR_STORY_TYPES.map(type => [type, cloneRange(value[type])])) as MinorFragmentRanges
}

export function plotSettingsForScale(scale: Exclude<MajorStoryScale, 'custom'>): PlotMechanicsSettings {
  const preset = SCALE_PRESETS[scale]
  return {
    scale,
    totalMinorPlots: cloneRange(preset.totalMinorPlots),
    phaseMinorPlots: clonePhaseRanges(preset.phaseMinorPlots),
    minorFragments: cloneFragmentRanges(DEFAULT_MINOR_FRAGMENT_RANGES),
    minorTypeMix: { ...preset.minorTypeMix },
    allowEarlyCompletion: true,
    allowDynamicTypeChange: true,
    phaseClosureReserve: 1,
  }
}

export const DEFAULT_PLOT_MECHANICS = plotSettingsForScale('balanced')

function integer(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.min(max, Math.max(min, Math.round(parsed))) : fallback
}

function normalizeRange(value: unknown, fallback: CountRange, limit = 200): CountRange {
  const raw = value && typeof value === 'object' ? value as Partial<CountRange> : {}
  const min = integer(raw.min, fallback.min, 0, limit)
  const max = integer(raw.max, fallback.max, min, limit)
  return { min, max }
}

export function normalizePlotMechanics(value: unknown): PlotMechanicsSettings {
  const raw = value && typeof value === 'object' ? value as Partial<PlotMechanicsSettings> : {}
  const scale = ['fast', 'balanced', 'detailed', 'custom'].includes(String(raw.scale))
    ? raw.scale as MajorStoryScale : 'balanced'
  const base = scale === 'custom' ? DEFAULT_PLOT_MECHANICS : plotSettingsForScale(scale)
  const rawPhases = raw.phaseMinorPlots && typeof raw.phaseMinorPlots === 'object' ? raw.phaseMinorPlots : base.phaseMinorPlots
  const rawFragments = raw.minorFragments && typeof raw.minorFragments === 'object' ? raw.minorFragments : base.minorFragments
  const rawMix = raw.minorTypeMix && typeof raw.minorTypeMix === 'object' ? raw.minorTypeMix : base.minorTypeMix
  const phaseMinorPlots = Object.fromEntries(MAJOR_PHASES.map(phase => [
    phase, normalizeRange((rawPhases as Partial<PhaseCountRanges>)[phase], base.phaseMinorPlots[phase], 100),
  ])) as PhaseCountRanges
  const minorFragments = Object.fromEntries(MINOR_STORY_TYPES.map(type => [
    type, normalizeRange((rawFragments as Partial<MinorFragmentRanges>)[type], base.minorFragments[type], 50),
  ])) as MinorFragmentRanges
  const mixValues = MINOR_STORY_TYPES.map(type => integer((rawMix as Partial<MinorTypeMix>)[type], base.minorTypeMix[type], 0, 100))
  const mixTotal = mixValues.reduce((sum, current) => sum + current, 0)
  const minorTypeMix: MinorTypeMix = mixTotal === 100
    ? { quick: mixValues[0], standard: mixValues[1], focus: mixValues[2] }
    : { ...base.minorTypeMix }
  return {
    scale,
    totalMinorPlots: normalizeRange(raw.totalMinorPlots, base.totalMinorPlots, 100),
    phaseMinorPlots,
    minorFragments,
    minorTypeMix,
    allowEarlyCompletion: raw.allowEarlyCompletion !== false,
    allowDynamicTypeChange: raw.allowDynamicTypeChange !== false,
    phaseClosureReserve: integer(raw.phaseClosureReserve, base.phaseClosureReserve, 0, 5),
  }
}

export function validatePlotMechanics(value: PlotMechanicsSettings, majorHookEnabled = true): string[] {
  const errors: string[] = []
  const phases = majorHookEnabled ? MAJOR_PHASES : MAJOR_PHASES.filter(phase => phase !== 'hook')
  const minimum = phases.reduce((sum, phase) => sum + value.phaseMinorPlots[phase].min, 0)
  const maximum = phases.reduce((sum, phase) => sum + value.phaseMinorPlots[phase].max, 0)
  if (minimum > value.totalMinorPlots.max) errors.push('各阶段最小值之和不能超过大剧情总数最大值')
  if (maximum < value.totalMinorPlots.min) errors.push('各阶段最大值之和不能小于大剧情总数最小值')
  for (const phase of phases) {
    if (value.phaseMinorPlots[phase].min < 1) errors.push('启用的每个大剧情阶段至少需要 1 个小剧情')
  }
  if (MINOR_STORY_TYPES.reduce((sum, type) => sum + value.minorTypeMix[type], 0) !== 100) {
    errors.push('三类小剧情比例之和必须为 100%')
  }
  for (const type of MINOR_STORY_TYPES) {
    if (value.minorFragments[type].min < 1) errors.push(`${type} 小剧情至少需要 1 个剧情片段`)
  }
  return errors
}

function randomFromSeed(seed: number): () => number {
  let value = seed >>> 0 || 0x9e3779b9
  return () => {
    value += 0x6d2b79f5
    let result = value
    result = Math.imul(result ^ result >>> 15, result | 1)
    result ^= result + Math.imul(result ^ result >>> 7, result | 61)
    return ((result ^ result >>> 14) >>> 0) / 4294967296
  }
}

function sampledInteger(range: CountRange, random: () => number): number {
  if (range.min >= range.max) return range.min
  const bell = (random() + random() + random()) / 3
  return range.min + Math.round(bell * (range.max - range.min))
}

function effectivePhaseRanges(settings: PlotMechanicsSettings, majorHookEnabled: boolean): PhaseCountRanges {
  const ranges = clonePhaseRanges(settings.phaseMinorPlots)
  if (majorHookEnabled) return ranges
  const released = ranges.hook
  ranges.hook = { min: 0, max: 0 }
  ranges.beginning.max += Math.ceil(released.max / 2)
  ranges.development.max += Math.floor(released.max / 2)
  ranges.beginning.min += Math.ceil(released.min / 2)
  ranges.development.min += Math.floor(released.min / 2)
  return ranges
}

export function createMajorBudgetSnapshot(
  value: PlotMechanicsSettings,
  majorHookEnabled: boolean,
  seed: number,
): MajorBudgetSnapshot {
  const settings = normalizePlotMechanics(value)
  const random = randomFromSeed(seed)
  const phaseRanges = effectivePhaseRanges(settings, majorHookEnabled)
  const minSum = MAJOR_PHASES.reduce((sum, phase) => sum + phaseRanges[phase].min, 0)
  const maxSum = MAJOR_PHASES.reduce((sum, phase) => sum + phaseRanges[phase].max, 0)
  const allowedTotal = {
    min: Math.max(settings.totalMinorPlots.min, minSum),
    max: Math.min(settings.totalMinorPlots.max, maxSum),
  }
  if (allowedTotal.max < allowedTotal.min) allowedTotal.max = allowedTotal.min
  const totalTarget = sampledInteger(allowedTotal, random)
  const phaseTargets = Object.fromEntries(MAJOR_PHASES.map(phase => [phase, phaseRanges[phase].min])) as Record<MajorStoryPhase, number>
  let remaining = totalTarget - minSum
  const priority: MajorStoryPhase[] = ['development', 'climax', 'beginning', 'ending', 'hook']
  while (remaining > 0) {
    const candidates = priority.filter(phase => phaseTargets[phase] < phaseRanges[phase].max)
    if (!candidates.length) break
    const weighted: MajorStoryPhase[] = candidates.flatMap(phase =>
      Array<MajorStoryPhase>(Math.max(1, phaseRanges[phase].max - phaseTargets[phase])).fill(phase),
    )
    const phase = weighted[Math.floor(random() * weighted.length)] ?? candidates[0]
    phaseTargets[phase] += 1
    remaining -= 1
  }
  return {
    schemaVersion: 1,
    scale: settings.scale,
    totalRange: cloneRange(settings.totalMinorPlots),
    totalTarget,
    phaseRanges,
    phaseTargets,
    minorFragments: cloneFragmentRanges(settings.minorFragments),
    minorTypeMix: { ...settings.minorTypeMix },
    allowEarlyCompletion: settings.allowEarlyCompletion,
    allowDynamicTypeChange: settings.allowDynamicTypeChange,
    phaseClosureReserve: settings.phaseClosureReserve,
    seed: seed >>> 0,
    createdAt: new Date().toISOString(),
  }
}

export function estimatePlotSize(value: PlotMechanicsSettings): PlotEstimate {
  const settings = normalizePlotMechanics(value)
  const weighted = (field: 'min' | 'max') => MINOR_STORY_TYPES.reduce(
    (sum, type) => sum + settings.minorFragments[type][field] * settings.minorTypeMix[type] / 100,
    0,
  )
  return {
    minorMin: settings.totalMinorPlots.min,
    minorMax: settings.totalMinorPlots.max,
    fragmentMin: Math.max(1, Math.floor(settings.totalMinorPlots.min * weighted('min'))),
    fragmentMax: Math.max(1, Math.ceil(settings.totalMinorPlots.max * weighted('max'))),
  }
}

export function minorTypeForTurn(seed: number, mix: MinorTypeMix, phase: MajorStoryPhase): MinorStoryType {
  if (phase === 'climax' && mix.focus > 0) return 'focus'
  if (phase === 'ending' && mix.standard > 0) return 'standard'
  const draw = randomFromSeed(seed)() * 100
  if (draw < mix.quick) return 'quick'
  return draw < mix.quick + mix.standard ? 'standard' : 'focus'
}

export function minorPhaseForFragment(fragmentCount: number, budget: CountRange): Exclude<MajorStoryPhase, 'hook'> {
  if (budget.max <= 2) return fragmentCount >= budget.min ? 'ending' : 'beginning'
  const ratio = fragmentCount / Math.max(1, budget.max)
  if (ratio <= .25) return 'beginning'
  if (ratio <= .65) return 'development'
  if (fragmentCount < budget.max) return 'climax'
  return 'ending'
}
