/**
 * Storydex 安卓端随机机制（系统级，非提示词驱动）。
 *
 * 两层机制：
 *  1. 随机气运判定 —— 每次行动必须计算，正态分布 N(50, σ²) 采样 0~100，
 *     按九区间裁决（移植自 scripts/气运判定.py）。
 *  2. 随机遭遇调度 —— 每轮只做一次触发判定，再从事件、人物、悲剧和爽点中
 *     选择一个主轴并按需添加事件/人物参与者，最终形成一条结构化因果链。
 *
 * 所有开关由 settings 页控制（stores/story.ts），开启后由系统在每次行动前
 * 自动计算并把结果注入提示词（promptFor → buildStoryPrompt.mechanics）。
 */

import type { KeywordLibrary } from '@/stores/keywordLibraries'

export type CharacterGenderMode = 'random' | 'male' | 'female'
export type CharacterGender = Exclude<CharacterGenderMode, 'random'>
export type EncounterFrequency = 'restrained' | 'balanced' | 'active'
export type EncounterKind = 'event' | 'character' | 'tragedy' | 'payoff'
export type EncounterIntensity = 1 | 2 | 3 | 4 | 5

/** Deterministic PRNG used for a prepared turn; retries must replay the same draw. */
export function seededRandom(seed: number): () => number {
  let value = seed >>> 0
  return () => {
    value = (value + 0x6D2B79F5) >>> 0
    let mixed = value
    mixed = Math.imul(mixed ^ (mixed >>> 15), mixed | 1)
    mixed ^= mixed + Math.imul(mixed ^ (mixed >>> 7), mixed | 61)
    return ((mixed ^ (mixed >>> 14)) >>> 0) / 4294967296
  }
}

// ---------------------------------------------------------------------------
// 1. 正态分布采样（Box-Muller）
// ---------------------------------------------------------------------------

/** 生成标准正态分布随机数 N(0,1)。 */
function standardNormal(random: () => number = Math.random): number {
  let u = 0
  let v = 0
  while (u === 0) u = random()
  while (v === 0) v = random()
  return Math.sqrt(-2.0 * Math.log(u)) * Math.cos(2.0 * Math.PI * v)
}

/** 按正态分布 N(mean, sigma²) 采样并截断到 [min, max]，四舍五入为整数。 */
export function normalSample(
  mean: number,
  sigma: number,
  min = 0,
  max = 100,
  random: () => number = Math.random,
): number {
  const value = mean + sigma * standardNormal(random)
  return Math.round(Math.min(max, Math.max(min, value)))
}

// ---------------------------------------------------------------------------
// 2. 随机气运判定（移植 scripts/气运判定.py）
// ---------------------------------------------------------------------------

export const FORTUNE_MU = 50.0
export const FORTUNE_DEFAULT_SIGMA = 12.0

/** 九区间分位点（z 值，μ ± z·σ 为边界），与 py 一致。 */
const Z_EDGES = [-2.5, -1.96, -1.28, -0.67, 0.67, 1.28, 1.96, 2.5]

/** 行动分量表（标签 → 数值）。行动分量由 Agent 依据行动内容自行定夺，系统不代为推断。 */
export const FORTUNE_SCALE_LABELS: Record<string, number> = {
  '琐碎': 0.1, '轻微': 0.3, '一般': 0.6, '普通': 1.0,
  '重要': 1.5, '重大': 2.0, '决定性': 2.5,
}

/** 九区间：名称、严重度、行动效果描述（与 py 一致的精简版）。 */
const FORTUNE_INTERVALS = [
  {
    name: '大凶', severity: -5,
    desc: '行动几乎必然走向最坏的可能：陷阱被触发、误会加深、伤势恶化、最坏的人在最坏的时候出现。损失远超预期，过程毫无转圜，先保命再言其他。',
  },
  {
    name: '凶', severity: -4,
    desc: '行动大概率得到负面结果：失败、受挫、破财、受伤。原本稳妥的事也会横生枝节，帮手可能变卦，退路可能被断；没有立即致命的危险，但每一步都在悄悄积累劣势。',
  },
  {
    name: '小凶', severity: -3,
    desc: '行动结果偏向负面但留有余地：小挫、小损、小误会。能办成的事办得难看，能躲开的麻烦擦肩而过也要沾一身灰；损失不大，却让人心头不畅。',
  },
  {
    name: '偏逆', severity: -2,
    desc: '行动结果略偏不利：事情能推进，但处处别扭。本可顺利的部分出现小波折，时间与心力被额外消耗；不致命，却需要多费一番手脚。',
  },
  {
    name: '平', severity: 0,
    desc: '行动结果不偏不倚：成事与否全看行动本身的扎实程度。没有意外之喜，也没有无妄之灾；付出多少，便收获多少。',
  },
  {
    name: '偏顺', severity: 2,
    desc: '行动结果略偏有利：事情推进顺畅，偶有顺手之便。原本要绕的路忽然通了，原本要等的时机恰好出现；虽无大惊喜，却处处顺手。',
  },
  {
    name: '小吉', severity: 3,
    desc: '行动结果明显有利：付出获得超出预期的回报。关键处有人搭手，为难处恰好留有余地；小有所得，足以让局面向前一步。',
  },
  {
    name: '吉', severity: 4,
    desc: '行动结果大获裨益：关键转折恰逢其时，阻力化为助力。原本要硬闯的关隘忽然敞开，原本要失去的恰好保住；好运显而易见，局面为之一新。',
  },
  {
    name: '大吉', severity: 5,
    desc: '行动结果近乎心想事成：所有条件在最恰当的时候聚齐，天时地利人和齐备。不仅所求达成，还可能有意外的丰厚收获；这是命运难得的垂青。',
  },
]

export interface FortuneResult {
  /** 气运值 0~100。 */
  roll: number
  /** 区间名：大凶/凶/小凶/偏逆/平/偏顺/小吉/吉/大吉。 */
  interval: string
  /** 严重度 -5~+5。 */
  severity: number
  /** 区间效果描述。 */
  desc: string
  /** 系统判定说明。 */
  note: string
}

/**
 * 气运判定：N(50, σ²) 采样 → 九区间。
 * 每次行动都必须调用（开关开启时），系统自动执行。
 * 行动分量不由系统推断，由 Agent 依据行动内容自行定夺（见 FORTUNE_SCALE_LABELS）。
 */
export function rollFortune(
  sigma = FORTUNE_DEFAULT_SIGMA,
  fixedRoll?: number,
  random: () => number = Math.random,
): FortuneResult {
  const roll = fixedRoll ?? normalSample(FORTUNE_MU, sigma, 0, 100, random)
  // 按 z 分位点计算九区间边界并定位（与 py 一致）。
  const z = (roll - FORTUNE_MU) / sigma
  let index = 0
  for (let i = 0; i < Z_EDGES.length; i++) {
    if (z >= Z_EDGES[i]) index = i + 1
  }
  const interval = FORTUNE_INTERVALS[index]
  return {
    roll,
    interval: interval.name,
    severity: interval.severity,
    desc: interval.desc,
    note: '系统自动气运判定（正态分布 N(50,12)，非提示词驱动）；行动分量由 Agent 依据行动内容自行定夺',
  }
}

// ---------------------------------------------------------------------------
// 3. 随机遭遇调度
// ---------------------------------------------------------------------------

export interface CategorizedKeyword {
  category: string
  value: string
}

export interface RandomTriggerResult {
  triggered: boolean
  sample: number
  keywords: CategorizedKeyword[]
}

export interface EncounterComponent {
  kind: EncounterKind
  role: 'primary' | 'context' | 'participant'
  keywords: CategorizedKeyword[]
  gender?: CharacterGender
}

export interface EncounterPlan {
  triggered: boolean
  sample: number
  threshold: number
  intensity: EncounterIntensity
  primary?: EncounterKind
  components: EncounterComponent[]
}

export const ENCOUNTER_FREQUENCY_THRESHOLDS: Record<EncounterFrequency, number> = {
  restrained: 70,
  balanced: 62,
  active: 55,
}

const ENCOUNTER_KIND_WEIGHTS: Record<EncounterKind, number> = {
  event: 5,
  character: 3,
  tragedy: 2,
  payoff: 2,
}

/** 优先跨分类抽取；分类用尽后再从剩余词条补足。 */
export function pickCategorizedKeywords(
  library: KeywordLibrary,
  count: number,
  random: () => number = Math.random,
): CategorizedKeyword[] {
  const categories = Object.entries(library)
    .map(([category, values]) => ({ category, values: [...values] }))
    .filter(item => item.values.length > 0)
  const categoryBag = [...categories]
  const picked: CategorizedKeyword[] = []
  while (picked.length < count && categoryBag.length > 0) {
    const categoryIndex = Math.floor(random() * categoryBag.length)
    const item = categoryBag.splice(categoryIndex, 1)[0]
    const valueIndex = Math.floor(random() * item.values.length)
    picked.push({ category: item.category, value: item.values.splice(valueIndex, 1)[0] })
  }
  const remainder = categories.flatMap(item =>
    item.values.map(value => ({ category: item.category, value })),
  )
  while (picked.length < count && remainder.length > 0) {
    const index = Math.floor(random() * remainder.length)
    picked.push(remainder.splice(index, 1)[0])
  }
  return picked
}

/** 保留给独立判定和兼容测试使用；主流程只进行一次统一遭遇判定。 */
export function rollRandomTrigger(
  mean: number,
  sigma: number,
  threshold: number,
  keywordLibrary: KeywordLibrary,
  fixedSample?: number,
  random: () => number = Math.random,
): RandomTriggerResult {
  const sample = fixedSample ?? normalSample(mean, sigma, 0, 100, random)
  if (sample < threshold) return { triggered: false, sample, keywords: [] }
  const count = 3 + Math.floor(random() * 3)
  return { triggered: true, sample, keywords: pickCategorizedKeywords(keywordLibrary, count, random) }
}

function intensityFrom(sample: number, threshold: number): EncounterIntensity {
  const excess = sample - threshold
  if (excess < 4) return 1
  if (excess < 8) return 2
  if (excess < 13) return 3
  if (excess < 19) return 4
  return 5
}

function weightedKind(kinds: EncounterKind[], random: () => number): EncounterKind {
  const total = kinds.reduce((sum, kind) => sum + ENCOUNTER_KIND_WEIGHTS[kind], 0)
  let cursor = random() * total
  for (const kind of kinds) {
    cursor -= ENCOUNTER_KIND_WEIGHTS[kind]
    if (cursor < 0) return kind
  }
  return kinds[kinds.length - 1]
}

function keywordCountFor(kind: EncounterKind, intensity: EncounterIntensity): number {
  if (kind === 'tragedy' || kind === 'payoff') return 1
  if (kind === 'event') return Math.min(4, 2 + Math.floor(intensity / 2))
  return Math.min(5, 3 + Math.floor(intensity / 2))
}

export interface EncounterOptions {
  enabled: boolean
  frequency: EncounterFrequency
  eventEnabled: boolean
  characterEnabled: boolean
  characterGender: CharacterGenderMode
  tragedyEnabled: boolean
  payoffEnabled: boolean
  eventLibrary: KeywordLibrary
  maleLibrary: KeywordLibrary
  femaleLibrary: KeywordLibrary
  tragedyLibrary: KeywordLibrary
  payoffLibrary: KeywordLibrary
  allowedKinds?: EncounterKind[]
  /** Stable seed from the unified turn control contract. */
  randomSeed?: number
  random?: () => number
  fixed?: {
    sample?: number
    primary?: EncounterKind
    characterGender?: CharacterGender
    includeEvent?: boolean
    includeCharacter?: boolean
  }
}

/**
 * 一轮只产生一条随机遭遇。悲剧和爽点只可作为互斥主轴；事件与人物可作为
 * 上下文和参与者加入同一条因果链，避免多个独立判定互相打架。
 */
export function rollEncounter(options: EncounterOptions): EncounterPlan {
  const threshold = ENCOUNTER_FREQUENCY_THRESHOLDS[options.frequency]
  const random = options.random ?? (options.randomSeed == null ? Math.random : seededRandom(options.randomSeed))
  const enabledKinds: EncounterKind[] = []
  if (options.eventEnabled) enabledKinds.push('event')
  if (options.characterEnabled) enabledKinds.push('character')
  if (options.tragedyEnabled) enabledKinds.push('tragedy')
  if (options.payoffEnabled) enabledKinds.push('payoff')
  const allowedKinds = options.allowedKinds?.length ? new Set(options.allowedKinds) : null
  const eligibleKinds = allowedKinds ? enabledKinds.filter(kind => allowedKinds.has(kind)) : enabledKinds

  if (!options.enabled || eligibleKinds.length === 0) {
    return { triggered: false, sample: 0, threshold, intensity: 1, components: [] }
  }

  const sample = options.fixed?.sample ?? normalSample(50, 15, 0, 100, random)
  if (sample < threshold) {
    return { triggered: false, sample, threshold, intensity: 1, components: [] }
  }

  const intensity = intensityFrom(sample, threshold)
  const primary = options.fixed?.primary && eligibleKinds.includes(options.fixed.primary)
    ? options.fixed.primary
    : weightedKind(eligibleKinds, random)
  const gender: CharacterGender = options.characterGender !== 'random'
    ? options.characterGender
    : options.fixed?.characterGender ?? (random() < 0.5 ? 'male' : 'female')
  const libraryFor = (kind: EncounterKind): KeywordLibrary => {
    if (kind === 'event') return options.eventLibrary
    if (kind === 'character') return gender === 'male' ? options.maleLibrary : options.femaleLibrary
    if (kind === 'tragedy') return options.tragedyLibrary
    return options.payoffLibrary
  }
  const component = (kind: EncounterKind, role: EncounterComponent['role']): EncounterComponent => ({
    kind,
    role,
    keywords: pickCategorizedKeywords(libraryFor(kind), keywordCountFor(kind, intensity), random),
    ...(kind === 'character' ? { gender } : {}),
  })

  const components: EncounterComponent[] = [component(primary, 'primary')]
  const includeEvent = primary !== 'event' && eligibleKinds.includes('event')
    && (options.fixed?.includeEvent ?? random() < (primary === 'character' ? 0.55 : 0.4))
  const includeCharacter = primary !== 'character' && eligibleKinds.includes('character')
    && (options.fixed?.includeCharacter ?? random() < (primary === 'event' ? 0.55 : 0.3))
  if (includeEvent) components.unshift(component('event', 'context'))
  if (includeCharacter) components.push(component('character', 'participant'))

  return { triggered: true, sample, threshold, intensity, primary, components }
}

// ---------------------------------------------------------------------------
// 4. 气运与随机遭遇统一入口（供 story store 调用）
// ---------------------------------------------------------------------------

export interface MechanicsRollResult {
  block: string
  fortune?: FortuneResult
  encounter?: EncounterPlan
  event?: RandomTriggerResult
  character?: RandomTriggerResult & { gender: CharacterGender }
  tragedy?: RandomTriggerResult
  payoff?: RandomTriggerResult
}

export interface MechanicsOptions extends Omit<EncounterOptions, 'enabled' | 'frequency' | 'fixed'> {
  fortuneEnabled: boolean
  encounterEnabled?: boolean
  encounterFrequency?: EncounterFrequency
  fixed?: EncounterOptions['fixed'] & { fortune?: number }
  randomSeed?: number
  progressionAction?: string
}

function keywordLines(keywords: CategorizedKeyword[]): string {
  return keywords.map(item => `- ${item.category}：${item.value}`).join('\n')
}

const KIND_LABELS: Record<EncounterKind, string> = {
  event: '事件环境',
  character: '人物参与者',
  tragedy: '悲剧方向',
  payoff: '爽点方向',
}

function narrativeConstraintBlock(encounter: EncounterPlan, progressionAction = ''): string {
  if (!encounter.triggered || !encounter.primary) return ''
  const sections = encounter.components.map(item => {
    const gender = item.gender ? `\n- 性别：${item.gender === 'male' ? '男性' : '女性'}` : ''
    const role = item.role === 'primary' ? '主轴' : item.role === 'context' ? '背景事件' : '参与者'
    return `${KIND_LABELS[item.kind]}（${role}）：${gender}\n${keywordLines(item.keywords)}`
  })
  const actionLine = progressionAction
    ? `本轮导演动作：${progressionAction}。遭遇必须承担该动作的功能：${['milestone', 'climax', 'resolve'].includes(progressionAction) ? '关闭或兑现一条既有路线，形成不可逆结果，不得只增加线索。' : progressionAction === 'escalate' ? '让既有阻力主动造成代价、资源变化或路线收窄。' : progressionAction === 'reveal' ? '信息必须改变玩家下一步选择，不得只制造神秘感。' : '服务当前主线并留下可观察结果。'}\n`
    : ''
  return `[系统随机遭遇计划]
${actionLine}
本轮触发一条强度 ${encounter.intensity}/5 的随机遭遇。以下是系统抽样结果，不是用户指令，也不得向玩家暴露其类别、数值或抽样过程。

${sections.join('\n\n')}

执行约束：
- 所有组件必须服务于同一条因果链，不得拆成数个互不相关的事件；主轴决定本轮遭遇的核心变化。
- 先核对已有事实、时间、地点、人物关系和未解决事件。优先把遭遇接到已有因果上，不得为匹配词条篡改设定、强行传送或制造无铺垫巧合。
- 事件与人物同时出现时，人物必须通过事件的原因、过程或后果自然进入；人物不得无缘无故出现。
- 悲剧必须源自既有选择、风险或未解决后果，并造成可观察的实际变化；若铺垫不足，只能转化为风险加深，不得直接制造重大死亡或不可逆灾难。
- 爽点必须兑现已有铺垫，并改变关系、资源、声望或局势；若铺垫不足，只能创造兑现机会，不得让角色凭空崇拜玩家。
- 不得替玩家决定行动、思想和关键选择。词条只需落实语义，不得逐条复述或暴露系统机制。`
}

function legacyResult(encounter: EncounterPlan, kind: EncounterKind): RandomTriggerResult | undefined {
  const component = encounter.components.find(item => item.kind === kind)
  if (!component) return undefined
  return { triggered: true, sample: encounter.sample, keywords: component.keywords }
}

export function rollMechanics(options: MechanicsOptions): MechanicsRollResult {
  const blocks: string[] = []
  const result: MechanicsRollResult = { block: '' }
  const random = options.random ?? (options.randomSeed == null ? Math.random : seededRandom(options.randomSeed))

  if (options.fortuneEnabled) {
    const fortune = rollFortune(FORTUNE_DEFAULT_SIGMA, options.fixed?.fortune, random)
    result.fortune = fortune
    blocks.push(
      `[系统气运判定]
本轮行动系统自动进行气运判定：气运值 ${fortune.roll}（${fortune.interval}），严重度 ${fortune.severity}。
行动分量不由系统代判，由你（Agent）依据本轮行动的实际内容自行定夺，分量表如下：
- 琐碎(0.1)：无关紧要的日常小动作；
- 轻微(0.3)：无伤大雅的常规举动；
- 一般(0.6)：有一定目的性的行动；
- 普通(1.0)：值得一写的常规行动；
- 重要(1.5)：影响局面的关键行动；
- 重大(2.0)：攸关生死的重大抉择；
- 决定性(2.5)：赌上一切的终极行动。
气运对结果的影响力度 = 严重度 × 行动分量：分量越高，好气运的加成与坏气运的挫伤都越显著；分量越低，影响越轻微。请在心中完成该计算并在正文中体现。
${fortune.desc}
你必须在剧情正文中如实体现该气运对行动结果的影响：好气运让行动顺遂有意外之喜，坏气运让行动受挫有代价，不得无视或篡改系统判定结果。`,
    )
  }

  const encounter = rollEncounter({
    ...options,
    random,
    enabled: options.encounterEnabled ?? Boolean(options.eventEnabled || options.characterEnabled || options.tragedyEnabled || options.payoffEnabled),
    frequency: options.encounterFrequency ?? 'balanced',
  })
  result.encounter = encounter
  result.event = legacyResult(encounter, 'event')
  const character = encounter.components.find(item => item.kind === 'character')
  if (character?.gender) {
    result.character = {
      triggered: true, sample: encounter.sample, keywords: character.keywords, gender: character.gender,
    }
  }
  result.tragedy = legacyResult(encounter, 'tragedy')
  result.payoff = legacyResult(encounter, 'payoff')

  const narrativeBlock = narrativeConstraintBlock(encounter, options.progressionAction)
  if (narrativeBlock) blocks.push(narrativeBlock)
  result.block = blocks.join('\n\n')
  return result
}
