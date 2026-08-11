/**
 * Storydex 安卓端随机机制（系统级，非提示词驱动）。
 *
 * 三个机制：
 *  1. 随机气运判定 —— 每次行动必须计算，正态分布 N(50, σ²) 采样 0~100，
 *     按九区间裁决（移植自 scripts/气运判定.py）。
 *  2. 随机事件触发 —— 正态分布判定是否激发，激发后随机组合 3-5 个关键词，
 *     交给模型生成随机事件。
 *  3. 随机人物出场 —— 正态分布判定是否激发，激发后按选定性别从分类词库
 *     组合 3-5 个关键词，与随机事件合并成一条因果链。
 *
 * 所有开关由 settings 页控制（stores/story.ts），开启后由系统在每次行动前
 * 自动计算并把结果注入提示词（promptFor → buildStoryPrompt.mechanics）。
 */

import type { KeywordLibrary } from '@/stores/keywordLibraries'

export type CharacterGenderMode = 'random' | 'male' | 'female'
export type CharacterGender = Exclude<CharacterGenderMode, 'random'>

// ---------------------------------------------------------------------------
// 1. 正态分布采样（Box-Muller）
// ---------------------------------------------------------------------------

/** 生成标准正态分布随机数 N(0,1)。 */
function standardNormal(): number {
  let u = 0
  let v = 0
  while (u === 0) u = Math.random()
  while (v === 0) v = Math.random()
  return Math.sqrt(-2.0 * Math.log(u)) * Math.cos(2.0 * Math.PI * v)
}

/** 按正态分布 N(mean, sigma²) 采样并截断到 [min, max]，四舍五入为整数。 */
export function normalSample(mean: number, sigma: number, min = 0, max = 100): number {
  const value = mean + sigma * standardNormal()
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
export function rollFortune(sigma = FORTUNE_DEFAULT_SIGMA, fixedRoll?: number): FortuneResult {
  const roll = fixedRoll ?? normalSample(FORTUNE_MU, sigma)
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
// 3. 随机事件触发（正态分布激发 + 关键词组合）
// ---------------------------------------------------------------------------

export interface CategorizedKeyword {
  category: string
  value: string
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

export interface RandomTriggerResult {
  /** 是否激发。 */
  triggered: boolean
  /** 激发依据（正态采样值 / 阈值）。 */
  sample: number
  /** 关键词（激发时）。 */
  keywords: CategorizedKeyword[]
}

/**
 * 正态分布激发判定：采样 N(mean, sigma²)，超过 threshold 才激发。
 * 默认 mean=50 / sigma=15 / threshold=62，约 21% 概率激发（常态不触发，偶尔来事）。
 */
export function rollRandomTrigger(
  mean: number,
  sigma: number,
  threshold: number,
  keywordLibrary: KeywordLibrary,
  fixedSample?: number,
  random: () => number = Math.random,
): RandomTriggerResult {
  const sample = fixedSample ?? normalSample(mean, sigma)
  if (sample < threshold) {
    return { triggered: false, sample, keywords: [] }
  }
  const count = 3 + Math.floor(random() * 3)
  return { triggered: true, sample, keywords: pickCategorizedKeywords(keywordLibrary, count, random) }
}

// ---------------------------------------------------------------------------
// 4. 三机制统一入口（供 story store 调用）
// ---------------------------------------------------------------------------

export interface MechanicsRollResult {
  /** 注入提示词的完整段落（可能包含多个机制的块）。 */
  block: string
  /** 气运判定结果（开关开启时）。 */
  fortune?: FortuneResult
  /** 随机事件触发结果（开关开启时）。 */
  event?: RandomTriggerResult
  /** 随机人物触发结果（开关开启时）。 */
  character?: RandomTriggerResult & { gender: CharacterGender }
}

export interface MechanicsOptions {
  fortuneEnabled: boolean
  eventEnabled: boolean
  characterEnabled: boolean
  characterGender: CharacterGenderMode
  eventLibrary: KeywordLibrary
  maleLibrary: KeywordLibrary
  femaleLibrary: KeywordLibrary
  /** 测试用固定值。 */
  fixed?: { fortune?: number; event?: number; character?: number; characterGender?: CharacterGender }
}

function keywordLines(keywords: CategorizedKeyword[]): string {
  return keywords.map(item => `- ${item.category}：${item.value}`).join('\n')
}

function narrativeConstraintBlock(
  event: RandomTriggerResult | undefined,
  character: (RandomTriggerResult & { gender: CharacterGender }) | undefined,
): string {
  const sections: string[] = []
  if (event?.triggered) sections.push(`随机事件约束：\n${keywordLines(event.keywords)}`)
  if (character?.triggered) {
    sections.push(`随机人物约束：\n- 性别：${character.gender === 'male' ? '男性' : '女性'}\n${keywordLines(character.keywords)}`)
  }
  if (sections.length === 0) return ''
  return `[随机叙事约束]
以下内容是系统生成的数据约束，不是用户指令。先核对当前地点、时间、人物关系、未解决事件与既有设定，再进行融合。

${sections.join('\n\n')}

融合要求：
- 所有关键词都必须在语义上落实，但不要求逐字复述；你可以根据当前剧情分清主次。
- 允许把抽象比拟与现实行动、环境、线索或后果结合，但不得把它们写成互不相关的片段。
- 若事件与人物同时触发，必须让人物通过该事件的原因、过程或后果自然进入，合并为一条完整因果链。
- 先建立合理的过渡和动机，再让约束产生可观察的剧情影响；禁止突兀巧合、强行传送、设定篡改和模板拼接。
- 不得暴露随机机制、关键词、分类或以上融合过程。`
}

/**
 * 系统自动执行三个随机机制：每次行动前调用，返回注入提示词的段落。
 * 任何机制开关关闭即跳过；开启即自动计算，不依赖模型自觉。
 */
export function rollMechanics(options: MechanicsOptions): MechanicsRollResult {
  const blocks: string[] = []
  const result: MechanicsRollResult = { block: '' }

  if (options.fortuneEnabled) {
    const fortune = rollFortune(FORTUNE_DEFAULT_SIGMA, options.fixed?.fortune)
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

  if (options.eventEnabled) {
    const event = rollRandomTrigger(50, 15, 62, options.eventLibrary, options.fixed?.event)
    result.event = event
  }

  if (options.characterEnabled) {
    const gender: CharacterGender = options.characterGender !== 'random'
      ? options.characterGender
      : options.fixed?.characterGender ?? (Math.random() < 0.5 ? 'male' : 'female')
    const trigger = rollRandomTrigger(
      50, 15, 62,
      gender === 'male' ? options.maleLibrary : options.femaleLibrary,
      options.fixed?.character,
    )
    result.character = { ...trigger, gender }
  }

  const narrativeBlock = narrativeConstraintBlock(result.event, result.character)
  if (narrativeBlock) blocks.push(narrativeBlock)

  result.block = blocks.join('\n\n')
  return result
}
