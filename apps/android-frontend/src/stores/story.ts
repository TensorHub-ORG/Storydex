import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { apiGet, authedFetch } from '@/bridge/http'
import { pickEnum } from '@/utils/validate'
import type { Timelineitem } from './viewModel'
import { buildStoryPrompt } from '@/story/prompt'
import type { CharacterGenderMode, EncounterFrequency } from '@/story/randomMechanics'
import {
  directorPlanPrompt, type DirectorPlan,
} from '@/story/directorMechanics'
import { prepareUnifiedTurn } from '@/story/unifiedTurnController'
import { useKeywordLibraryStore } from './keywordLibraries'
import { useProjectStore } from './project'

export type AgentMode = 'story' | 'narrator' | 'agent'
export type NarrativeMode = 'immersive' | 'narrative' | 'free'
export type ReasoningEffort = 'auto' | 'low' | 'medium' | 'high' | 'xhigh'

export interface StoryFragment {
  id: string
  group: string
  filename: string
  path: string
  createdAt: number
  summary: string
  content: string
  suggestions: string[]
  sourceMessageId?: string
  synced?: boolean
  suggestionsPersisted?: boolean
}

const LEGACY_STORAGE_KEY = 'storydex.mobile.story.v1'
const PROJECT_STORAGE_PREFIX = 'storydex.mobile.story.project.v2:'
const LEGACY_PREFERENCE_STORAGE_KEY = 'storydex.mobile.story.preferences.v2'
const PREFERENCE_STORAGE_PREFIX = 'storydex.mobile.story.preferences.v3:'
const ACTIONS_MARKER = '[STORYDEX_ACTIONS]'
const STATE_MARKER = '[STORYDEX_STATE_DELTA]'

function projectStorageKey(projectPath: string): string {
  return PROJECT_STORAGE_PREFIX + encodeURIComponent(projectPath || '__default__')
}

function preferenceStorageKey(projectPath: string): string {
  return PREFERENCE_STORAGE_PREFIX + encodeURIComponent(projectPath || '__default__')
}

interface StoryState {
  fragments: StoryFragment[]
  agentMode: AgentMode
  narrativeMode: NarrativeMode
  fragmentMin: number
  fragmentMax: number
  reasoningEffort: ReasoningEffort
  fortuneEnabled: boolean
  encounterEnabled: boolean
  encounterFrequency: EncounterFrequency
  eventEnabled: boolean
  characterEnabled: boolean
  characterGender: CharacterGenderMode
  tragedyEnabled: boolean
  payoffEnabled: boolean
}

function normalizedLength(value: unknown, fallback: number): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.min(8000, Math.max(200, Math.round(parsed))) : fallback
}

/**
 * 枚举字段的合法取值。读盘（readState）与写入（各 setter）共用同一份清单。
 *
 * 此前这些清单只内联在 readState 里：读的时候校验，写的时候不校验。story.ts 又是
 * 提示词组装时的读取权威，所以一个非法值写进 ref 后，落盘的 project.settings 是
 * 合法的、内存里的却不是，两边静默分叉，表现为"设置显示对了但不起作用"。
 */
const AGENT_MODES: readonly AgentMode[] = ['story', 'narrator', 'agent']
const NARRATIVE_MODES: readonly NarrativeMode[] = ['immersive', 'narrative', 'free']
const REASONING_EFFORTS: readonly ReasoningEffort[] = ['auto', 'low', 'medium', 'high', 'xhigh']
const ENCOUNTER_FREQUENCIES: readonly EncounterFrequency[] = ['restrained', 'balanced', 'active']
const CHARACTER_GENDERS: readonly CharacterGenderMode[] = ['random', 'male', 'female']

function readState(projectPath: string): StoryState {
  try {
    const projectKey = projectStorageKey(projectPath)
    const projectValue = localStorage.getItem(projectKey)
    const legacy = JSON.parse(localStorage.getItem(LEGACY_STORAGE_KEY) ?? '{}')
    const value = projectValue ? JSON.parse(projectValue) : legacy
    const preferences = JSON.parse(
      localStorage.getItem(preferenceStorageKey(projectPath))
      ?? localStorage.getItem(LEGACY_PREFERENCE_STORAGE_KEY)
      ?? '{}',
    )
    const fragmentMin = normalizedLength(preferences.fragmentMin, 1000)
    const fragmentMax = Math.max(fragmentMin, normalizedLength(preferences.fragmentMax, 2000))
    const reasoningEffort = preferences.reasoningEffort === 'max'
      ? 'xhigh'
      : pickEnum(preferences.reasoningEffort, REASONING_EFFORTS, 'high')
    const fortuneEnabled = preferences.fortuneEnabled !== false
    const eventEnabled = preferences.eventEnabled === true
    const characterEnabled = (preferences.characterEnabled ?? preferences.romanceEnabled) === true
    const tragedyEnabled = preferences.tragedyEnabled === true
    const payoffEnabled = preferences.payoffEnabled === true
    const encounterEnabled = typeof preferences.encounterEnabled === 'boolean'
      ? preferences.encounterEnabled
      : eventEnabled || characterEnabled
    const encounterFrequency = pickEnum(preferences.encounterFrequency, ENCOUNTER_FREQUENCIES, 'balanced')
    const characterGender = pickEnum(preferences.characterGender, CHARACTER_GENDERS, 'random')
    return {
      fragments: Array.isArray(value.fragments) ? value.fragments : [],
      agentMode: pickEnum(preferences.agentMode ?? value.agentMode, AGENT_MODES, 'story'),
      narrativeMode: pickEnum(preferences.narrativeMode ?? value.narrativeMode, NARRATIVE_MODES, 'immersive'),
      fragmentMin,
      fragmentMax,
      reasoningEffort,
      fortuneEnabled,
      encounterEnabled,
      encounterFrequency,
      eventEnabled,
      characterEnabled,
      characterGender,
      tragedyEnabled,
      payoffEnabled,
    }
  } catch {
    return {
      fragments: [], agentMode: 'story', narrativeMode: 'immersive',
      fragmentMin: 1000, fragmentMax: 2000, reasoningEffort: 'high',
      fortuneEnabled: true, encounterEnabled: false, encounterFrequency: 'balanced',
      eventEnabled: false, characterEnabled: false, characterGender: 'random',
      tragedyEnabled: false, payoffEnabled: false,
    }
  }
}

function timestamp(date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}${p(date.getMonth() + 1)}${p(date.getDate())}${p(date.getHours())}${p(date.getMinutes())}`
}

function shortSummary(content: string): string {
  return content.replace(/[#*_>`\[\]()]/g, '').replace(/\s+/g, ' ').trim().slice(0, 50)
}

function containsLeakedPlanning(content: string): boolean {
  const head = content.slice(0, 8_000)
  const signals = [
    /\bI now have (?:strong|enough|the) context\b/i,
    /\bLet me (?:reconsider|refine|finalize|write|draft|construct|proceed)\b/i,
    /\bNow for the state JSON\b/i,
    /\bMy handling:\s/i,
    /\bThe player wants\b/i,
    /\bI(?:'ll| will) proceed\b/i,
  ]
  return signals.reduce((count, pattern) => count + Number(pattern.test(head)), 0) >= 2
}

export function parseStoryResponse(raw: string): { content: string; suggestions: string[]; delta: import('./project').StoryStateDelta } | null {
  const markerIndex = raw.lastIndexOf(ACTIONS_MARKER)
  if (markerIndex < 0) return null
  const stateIndex = raw.lastIndexOf(STATE_MARKER)
  if (stateIndex < markerIndex) return null
  const content = raw.slice(0, markerIndex).trim()
  // Some providers may incorrectly stream hidden planning as visible text.
  // Never archive or apply state from such a response: it pollutes chapters,
  // memory and director scoring at the same time.
  if (containsLeakedPlanning(content)) return null
  const suggestions = raw.slice(markerIndex + ACTIONS_MARKER.length, stateIndex)
    .split(/\r?\n/)
    .map(line => line.replace(/^\s*(?:[-*•]|\d+[.)、])\s*/, '').trim())
    .filter(Boolean)
    .slice(0, 4)
  let delta: import('./project').StoryStateDelta
  try { delta = JSON.parse(raw.slice(stateIndex + STATE_MARKER.length).trim()) } catch { return null }
  return content && suggestions.length === 4 && delta && typeof delta === 'object' ? { content, suggestions, delta } : null
}

function nextGroupTimestamp(existingGroups: Set<string>, latestGroup?: string): string {
  let date = new Date()
  let candidate = timestamp(date)
  while (candidate === latestGroup || existingGroups.has(candidate)) {
    date = new Date(date.getTime() + 60_000)
    candidate = timestamp(date)
  }
  return candidate
}

/** 一个章节分组容纳的片段数。满了就滚到下一个时间戳分组。 */
const FRAGMENTS_PER_GROUP = 5

/** 组内已有的最大序号；文件名形如 {group}-{三位序号}.md，无法解析的按 0 计。 */
function maxSequence(groupFragments: StoryFragment[]): number {
  return groupFragments.reduce((max, item) => {
    const parsed = Number(item.filename.replace(/\.md$/i, '').split('-').pop())
    return Number.isFinite(parsed) ? Math.max(max, parsed) : max
  }, 0)
}

interface FsEntry { name: string; is_dir: boolean; size: number; modified: number }
interface FsListData { path: string; entries: FsEntry[] }

/** frontmatter 值去引号：优先 JSON.parse（处理 \" 转义），失败时剥掉首尾成对引号。 */
function unquoteFrontmatterValue(value: string): string {
  const v = value.trim()
  if (v.length >= 2 && v.startsWith('"') && v.endsWith('"')) {
    try { return JSON.parse(v) } catch { return v.slice(1, -1) }
  }
  if (v.length >= 2 && v.startsWith("'") && v.endsWith("'")) return v.slice(1, -1)
  return v
}

/** 解析剧情片段文件的 frontmatter（---\nsummary: …\ncreatedAt: …\n---\n\n正文）。 */
function parseFragmentFile(raw: string): { summary: string; createdAt: number; suggestions: string[]; content: string } | null {
  const match = raw.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/)
  if (!match) return null
  const meta: Record<string, string> = {}
  for (const line of match[1].split(/\r?\n/)) {
    const idx = line.indexOf(':')
    if (idx > 0) meta[line.slice(0, idx).trim().toLowerCase()] = unquoteFrontmatterValue(line.slice(idx + 1))
  }
  const createdAt = Date.parse(meta.createdat ?? meta.created_at ?? '')
  return {
    summary: meta.summary ?? '',
    createdAt: Number.isFinite(createdAt) ? createdAt : 0,
    suggestions: (() => {
      try {
        const value = JSON.parse(meta.suggestions ?? '[]')
        return Array.isArray(value) ? value.filter(item => typeof item === 'string').slice(0, 8) : []
      } catch { return [] }
    })(),
    content: (match[2] ?? '').trim(),
  }
}

/** 递归收集某目录下的 .md 文件（fs/list 单层，需要逐层展开）。 */
async function collectMarkdownFiles(
  absDir: string,
  relPrefix: string,
  out: { rel: string; abs: string }[],
): Promise<void> {
  let data: FsListData
  try {
    data = await apiGet(`/api/fs/list?path=${encodeURIComponent(absDir)}`)
  } catch {
    return
  }
  for (const e of data.entries ?? []) {
    if (e.name === '.' || e.name === '..') continue
    const rel = relPrefix ? `${relPrefix}/${e.name}` : e.name
    if (e.is_dir) await collectMarkdownFiles(`${absDir}/${e.name}`, rel, out)
    else if (/\.md$/i.test(e.name)) out.push({ rel, abs: `${absDir}/${e.name}` })
  }
}

export const useStoryStore = defineStore('story', () => {
  const keywordLibraries = useKeywordLibraryStore()
  const project = useProjectStore()
  const projectPath = ref(window.CoomiAndroid?.getStoryProjectPath?.() ?? '')
  const initial = readState(projectPath.value)
  const fragments = ref<StoryFragment[]>(initial.fragments)
  const agentMode = ref<AgentMode>(initial.agentMode)
  const narrativeMode = ref<NarrativeMode>(initial.narrativeMode)
  const fragmentMin = ref(initial.fragmentMin)
  const fragmentMax = ref(initial.fragmentMax)
  const reasoningEffort = ref<ReasoningEffort>(initial.reasoningEffort)
  const fortuneEnabled = ref(initial.fortuneEnabled)
  const encounterEnabled = ref(initial.encounterEnabled)
  const encounterFrequency = ref<EncounterFrequency>(initial.encounterFrequency)
  const eventEnabled = ref(initial.eventEnabled)
  const characterEnabled = ref(initial.characterEnabled)
  const characterGender = ref<CharacterGenderMode>(initial.characterGender)
  const tragedyEnabled = ref(initial.tragedyEnabled)
  const payoffEnabled = ref(initial.payoffEnabled)
  const olderExpanded = ref(false)
  /** null means the live/latest story surface; a value means a read-only historical fragment. */
  const viewedFragmentId = ref<string | null>(null)
  /** Increments when restored engine history has been replaced by the canonical latest-fragment projection. */
  const liveViewRevision = ref(0)
  const pendingDirectorPlans = new Map<string, DirectorPlan>()

  const latest = computed(() => fragments.value[fragments.value.length - 1] ?? null)
  const turnCommitPending = computed(() => Boolean(project.pendingCommit))
  const latestFive = computed(() => fragments.value.slice(-5).reverse())
  const older = computed(() => fragments.value.slice(0, -5).reverse())
  const viewedFragment = computed(() => viewedFragmentId.value
    ? fragments.value.find(fragment => fragment.id === viewedFragmentId.value) ?? null
    : null)
  const viewingHistory = computed(() => Boolean(viewedFragment.value && viewedFragment.value.id !== latest.value?.id))

  function persist() {
    localStorage.setItem(projectStorageKey(projectPath.value), JSON.stringify({ fragments: fragments.value }))
    localStorage.setItem(preferenceStorageKey(projectPath.value), JSON.stringify({
      agentMode: agentMode.value,
      narrativeMode: narrativeMode.value,
      fragmentMin: fragmentMin.value,
      fragmentMax: fragmentMax.value,
      reasoningEffort: reasoningEffort.value,
      fortuneEnabled: fortuneEnabled.value,
      encounterEnabled: encounterEnabled.value,
      encounterFrequency: encounterFrequency.value,
      eventEnabled: eventEnabled.value,
      characterEnabled: characterEnabled.value,
      characterGender: characterGender.value,
      tragedyEnabled: tragedyEnabled.value,
      payoffEnabled: payoffEnabled.value,
    }))
  }

  function setAgentMode(mode: AgentMode) {
    const next = pickEnum(mode, AGENT_MODES, agentMode.value)
    agentMode.value = next; persist(); if (next !== 'agent') void loadFragmentsFromProject()
  }
  function viewFragment(id: string) {
    viewedFragmentId.value = id === latest.value?.id ? null : id
  }
  function showLatestFragment() {
    viewedFragmentId.value = null
    liveViewRevision.value += 1
  }
  function setNarrativeMode(mode: NarrativeMode) { narrativeMode.value = pickEnum(mode, NARRATIVE_MODES, narrativeMode.value); persist() }
  function setFragmentLength(min: number, max: number) {
    const nextMin = normalizedLength(min, fragmentMin.value)
    const nextMax = normalizedLength(max, fragmentMax.value)
    fragmentMin.value = Math.min(nextMin, nextMax)
    fragmentMax.value = Math.max(nextMin, nextMax)
    persist()
  }
  function setReasoningEffort(effort: ReasoningEffort) { reasoningEffort.value = pickEnum(effort, REASONING_EFFORTS, reasoningEffort.value); persist() }
  function setFortuneEnabled(enabled: boolean) { fortuneEnabled.value = enabled; persist(); void project.patchSettings({ fortuneEnabled: enabled }) }
  function setEncounterEnabled(enabled: boolean) { encounterEnabled.value = enabled; persist(); void project.patchSettings({ encounterEnabled: enabled }) }
  function setEncounterFrequency(frequency: EncounterFrequency) { const next = pickEnum(frequency, ENCOUNTER_FREQUENCIES, encounterFrequency.value); encounterFrequency.value = next; persist(); void project.patchSettings({ encounterFrequency: next }) }
  function setEventEnabled(enabled: boolean) { eventEnabled.value = enabled; persist(); void project.patchSettings({ eventEnabled: enabled }) }
  function setCharacterEnabled(enabled: boolean) { characterEnabled.value = enabled; persist(); void project.patchSettings({ characterEnabled: enabled }) }
  function setCharacterGender(gender: CharacterGenderMode) { const next = pickEnum(gender, CHARACTER_GENDERS, characterGender.value); characterGender.value = next; persist(); void project.patchSettings({ characterGender: next }) }
  function setTragedyEnabled(enabled: boolean) { tragedyEnabled.value = enabled; persist(); void project.patchSettings({ tragedyEnabled: enabled }) }
  function setPayoffEnabled(enabled: boolean) { payoffEnabled.value = enabled; persist(); void project.patchSettings({ payoffEnabled: enabled }) }

  async function hydrateProjectSettings() {
    await project.initialize()
    fortuneEnabled.value = project.settings.fortuneEnabled
    encounterEnabled.value = project.settings.encounterEnabled
    encounterFrequency.value = project.settings.encounterFrequency
    eventEnabled.value = project.settings.eventEnabled
    characterEnabled.value = project.settings.characterEnabled
    characterGender.value = project.settings.characterGender
    tragedyEnabled.value = project.settings.tragedyEnabled
    payoffEnabled.value = project.settings.payoffEnabled
    await keywordLibraries.initialize()
    persist()
  }

  function promptFor(text: string): string {
    const preparedTurn = prepareUnifiedTurn({
      agentMode: agentMode.value,
      directorState: project.directorState,
      settings: project.settings,
      primaryScriptFocus: project.primaryScriptFocus,
      fortuneEnabled: fortuneEnabled.value,
      encounterEnabled: encounterEnabled.value,
      encounterFrequency: encounterFrequency.value,
      eventEnabled: eventEnabled.value,
      characterEnabled: characterEnabled.value,
      characterGender: characterGender.value,
      tragedyEnabled: tragedyEnabled.value,
      payoffEnabled: payoffEnabled.value,
      libraries: {
        event: keywordLibraries.eventLibrary,
        male: keywordLibraries.maleLibrary,
        female: keywordLibraries.femaleLibrary,
        tragedy: keywordLibraries.tragedyLibrary,
        payoff: keywordLibraries.payoffLibrary,
      },
    })
    const { directorPlan, mechanics } = preparedTurn
    if (directorPlan) {
      pendingDirectorPlans.set(directorPlan.id, directorPlan)
      while (pendingDirectorPlans.size > 16) pendingDirectorPlans.delete(pendingDirectorPlans.keys().next().value as string)
    }
    return buildStoryPrompt({
      agentMode: agentMode.value,
      narrativeMode: narrativeMode.value,
      fragmentMin: fragmentMin.value,
      fragmentMax: fragmentMax.value,
      playerText: text,
      actionsMarker: ACTIONS_MARKER,
      stateMarker: STATE_MARKER,
      director: directorPlan ? directorPlanPrompt(directorPlan, project.directorState) : undefined,
      mechanics: mechanics.block || undefined,
    })
  }

  async function writeStoryFragment(sessionId: string, path: string, content: string): Promise<void> {
    const response = await authedFetch(`/api/sessions/${encodeURIComponent(sessionId)}/story-fragment`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, content }),
    })
    if (!response.ok) {
      let detail = `HTTP ${response.status}`
      try { detail = (await response.json())?.error ?? detail } catch { /* response is not JSON */ }
      throw new Error(`剧情片段写入失败：${detail}`)
    }
  }

  async function captureTurn(items: Timelineitem[], sessionId: string): Promise<StoryFragment | null> {
    if (agentMode.value !== 'story') return null
    const assistant = [...items].reverse().find(item => item.kind === 'assistant')
    if (!assistant || assistant.kind !== 'assistant' || !assistant.content.trim()) return null
    if (latest.value?.sourceMessageId === assistant.id) return null
    const parsed = parseStoryResponse(assistant.content)
    // 只有完整的正文、行动建议与状态增量都通过解析才算剧情推进。
    // OOC 拒绝不带标记，因此绝不会归档或推进时间/剧本/记忆。
    if (!parsed) return null
    const content = parsed.content
    assistant.content = content
    if (latest.value?.content === content) return null
    const now = Date.now()
    // 是否开新分组只看「当前最后一组装了几个」，不看片段总数。用总数取模的话，
    // 历史上任何一次增删（外部导入、手工整理、解析失败被跳过）都会让余数永久错位，
    // 之后每组都装不满 5 个且不会自己回正——真机上实测 51 组里 32 组不是 5 个。
    const latestGroup = latest.value?.group
    const groupFragments = latestGroup ? fragments.value.filter(item => item.group === latestGroup) : []
    const startsNewGroup = !latestGroup || groupFragments.length >= FRAGMENTS_PER_GROUP
    const group = startsNewGroup
      ? nextGroupTimestamp(new Set(fragments.value.map(item => item.group)), latestGroup)
      : latestGroup
    // 组内序号取「已有最大序号 + 1」而非「个数 + 1」：组内序号有缺口时（例如 001、002、004）
    // 按个数算会得出 004 并静默覆盖已有文件。
    const sequence = (startsNewGroup ? 1 : maxSequence(groupFragments) + 1).toString().padStart(3, '0')
    const filename = `${group}-${sequence}.md`
    const relativePath = `chapters/${group}/${filename}`
    const summary = shortSummary(content)
    const fragment: StoryFragment = {
      id: `${group}-${sequence}`,
      group,
      filename,
      path: relativePath,
      createdAt: now,
      summary,
      content,
      suggestions: parsed.suggestions,
      sourceMessageId: assistant.id,
      synced: true,
      suggestionsPersisted: true,
    }
    const body = `---\nsummary: ${JSON.stringify(summary)}\ncreatedAt: ${new Date(now).toISOString()}\nsuggestions: ${JSON.stringify(parsed.suggestions)}\n---\n\n${content}\n`
    const directorPlanId = parsed.delta.director?.planId
    const queuedDirectorPlans = [...pendingDirectorPlans.values()]
    const reconstructedDirectorPlan = project.settings.directorEnabled
      ? prepareUnifiedTurn({
        agentMode: 'story', directorState: project.directorState, settings: project.settings,
        primaryScriptFocus: project.primaryScriptFocus,
        fortuneEnabled: false, encounterEnabled: false, encounterFrequency: encounterFrequency.value,
        eventEnabled: false, characterEnabled: false, characterGender: characterGender.value,
        tragedyEnabled: false, payoffEnabled: false,
        libraries: {
          event: keywordLibraries.eventLibrary, male: keywordLibraries.maleLibrary,
          female: keywordLibraries.femaleLibrary, tragedy: keywordLibraries.tragedyLibrary,
          payoff: keywordLibraries.payoffLibrary,
        },
      }).directorPlan
      : null
    const directorPlan = (directorPlanId ? pendingDirectorPlans.get(directorPlanId) : undefined)
      ?? queuedDirectorPlans[queuedDirectorPlans.length - 1]
      ?? (reconstructedDirectorPlan?.id === directorPlanId ? reconstructedDirectorPlan : null)
    const directorEvaluation = directorPlan && project.settings.directorEnabled
      ? await project.prepareDirectorTurn(
        content, parsed.delta.director, directorPlan, assistant.id, relativePath, parsed.delta,
      )
      : null
    if (project.settings.directorEnabled && (!directorPlan || !directorEvaluation?.accepted || !directorEvaluation.planSatisfied)) {
      if (directorPlan) pendingDirectorPlans.delete(directorPlan.id)
      const reasons = directorEvaluation?.rejectedReasons?.slice(-3) ?? ['缺少与当前状态匹配的统一导演计划']
      assistant.content = `本轮剧情未通过统一剧情控制校验，已阻止归档和状态推进。\n\n${reasons.map(item => `- ${item}`).join('\n')}\n\n请重新执行本轮；下一次生成仍使用未推进的主线状态。`
      return null
    }
    if (projectPath.value) await writeStoryFragment(sessionId, relativePath, body)
    if (directorPlan && directorEvaluation) await project.updatePendingPhase('chapter_written')
    fragments.value.push(fragment)
    showLatestFragment()
    if (directorPlan && directorEvaluation) {
      await project.commitDirectorTurn(directorEvaluation, directorPlan, assistant.id, relativePath)
      pendingDirectorPlans.delete(directorPlan.id)
    }
    try {
      await project.applyStoryDelta(parsed.delta, directorEvaluation, directorPlan, content, relativePath)
      if (directorPlan && directorEvaluation) await project.finalizeStoryTurn()
    } catch { await project.markMemoryPending().catch(() => {}) }
    persist()
    return fragment
  }

  async function updateFragment(id: string, content: string, sessionId: string): Promise<boolean> {
    const fragment = fragments.value.find(item => item.id === id)
    if (!fragment) return false
    const trimmed = content.trim()
    const summary = shortSummary(trimmed)
    const body = `---\nsummary: ${JSON.stringify(summary)}\ncreatedAt: ${new Date(fragment.createdAt).toISOString()}\nsuggestions: ${JSON.stringify(fragment.suggestions)}\n---\n\n${trimmed}\n`
    if (projectPath.value) await writeStoryFragment(sessionId, fragment.path, body)
    fragment.content = trimmed
    fragment.summary = summary
    fragment.synced = true
    fragment.suggestionsPersisted = true
    await project.markMemoryStale(fragment.path).catch(() => {})
    persist()
    return true
  }

  async function syncFragments(sessionId: string): Promise<number> {
    let synced = 0
    for (const fragment of fragments.value.filter(item => !item.synced || !item.suggestionsPersisted)) {
      const body = `---\nsummary: ${JSON.stringify(fragment.summary)}\ncreatedAt: ${new Date(fragment.createdAt).toISOString()}\nsuggestions: ${JSON.stringify(fragment.suggestions)}\n---\n\n${fragment.content.trim()}\n`
      await writeStoryFragment(sessionId, fragment.path, body)
      fragment.synced = true
      fragment.suggestionsPersisted = true
      synced++
    }
    if (synced > 0) persist()
    return synced
  }

  /**
   * 从当前故事项目目录扫描已有剧情片段（chapters/**​/*.md）并格式化加载到侧边栏。
   * 适用「从外部导入的已有数据项目」：本 app 生成的片段都在 localStorage 里，
   * 已有本地数据时跳过（不覆盖本地编辑与行动建议）；本地为空时以项目目录为权威重建。
   */
  async function loadFragmentsFromProject(): Promise<boolean> {
    const current = window.CoomiAndroid?.getStoryProjectPath?.() ?? projectPath.value
    if (!current) return false
    // 项目目录切换（同一页面实例内）：先重置本地片段，避免旧项目数据串到新项目。
    if (current !== projectPath.value) {
      projectPath.value = current
      fragments.value = []
      showLatestFragment()
    }
    if (fragments.value.length > 0) return false
    const chaptersDir = current.replace(/\/+$/, '') + '/chapters'
    try {
      const files: { rel: string; abs: string }[] = []
      await collectMarkdownFiles(chaptersDir, 'chapters', files)
      if (files.length === 0) return false
      const candidates: Array<StoryFragment & { sortKey: number }> = []
      const seenIds = new Set<string>()
      for (const f of files) {
        const res = await authedFetch(`/api/fs/raw?path=${encodeURIComponent(f.abs)}`)
        if (!res.ok) continue
        const parsed = parseFragmentFile(await res.text())
        if (!parsed || !parsed.content) continue
        const parts = f.rel.split('/')
        const group = parts.length > 1 ? parts[1] : 'imported'
        const filename = parts[parts.length - 1]
        // 与 captureTurn 的 id 约定一致：group-组内序号（文件名形如 {group}-{序号}.md）。
        const sequence = (filename.replace(/\.md$/i, '').split('-').pop() || '000')
        const id = `${group}-${sequence}`
        if (seenIds.has(id)) continue // 嵌套子目录可能产生相同 id，跳过重复
        seenIds.add(id)
        candidates.push({
          id,
          group,
          filename,
          path: f.rel,
          createdAt: parsed.createdAt,
          summary: parsed.summary || shortSummary(parsed.content),
          content: parsed.content,
          suggestions: parsed.suggestions,
          synced: true,
          suggestionsPersisted: true,
          sortKey: parsed.createdAt,
        })
      }
      if (candidates.length === 0) return false
      // 按 createdAt 升序（外部命名 chapter2/chapter10 用字典序会错乱）。
      candidates.sort((a, b) => a.sortKey - b.sortKey)
      // 竞态保护：等待 fs/raw 期间若已有新片段生成（captureTurn），放弃覆盖。
      if (fragments.value.length > 0) return false
      fragments.value = candidates.map(({ sortKey, ...fragment }) => fragment)
      persist()
      return true
    } catch {
      return false
    }
  }

  void hydrateProjectSettings()
  return {
    fragments, agentMode, narrativeMode, fragmentMin, fragmentMax, reasoningEffort, projectPath, olderExpanded, liveViewRevision,
    fortuneEnabled, encounterEnabled, encounterFrequency,
    eventEnabled, characterEnabled, characterGender, tragedyEnabled, payoffEnabled,
    latest, latestFive, older, viewedFragment, viewingHistory, turnCommitPending,
    setAgentMode, viewFragment, showLatestFragment, setNarrativeMode, setFragmentLength, setReasoningEffort,
    setFortuneEnabled, setEncounterEnabled, setEncounterFrequency,
    setEventEnabled, setCharacterEnabled, setCharacterGender, setTragedyEnabled, setPayoffEnabled,
    promptFor, captureTurn, updateFragment, syncFragments, loadFragmentsFromProject, hydrateProjectSettings,
    parseStoryResponse,
  }
})
