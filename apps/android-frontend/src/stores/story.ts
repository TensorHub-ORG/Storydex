import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { authedFetch } from '@/bridge/http'
import type { Timelineitem } from './viewModel'
import { buildStoryPrompt } from '@/story/prompt'

export type AgentMode = 'story' | 'narrator' | 'agent'
export type NarrativeMode = 'immersive' | 'narrative' | 'free'
export type ReasoningEffort = 'auto' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'

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
}

const LEGACY_STORAGE_KEY = 'storydex.mobile.story.v1'
const PROJECT_STORAGE_PREFIX = 'storydex.mobile.story.project.v2:'
const PREFERENCE_STORAGE_KEY = 'storydex.mobile.story.preferences.v2'
const ACTIONS_MARKER = '[STORYDEX_ACTIONS]'

function projectStorageKey(projectPath: string): string {
  return PROJECT_STORAGE_PREFIX + encodeURIComponent(projectPath || '__default__')
}

interface StoryState {
  fragments: StoryFragment[]
  agentMode: AgentMode
  narrativeMode: NarrativeMode
  fragmentMin: number
  fragmentMax: number
  reasoningEffort: ReasoningEffort
}

function normalizedLength(value: unknown, fallback: number): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.min(8000, Math.max(200, Math.round(parsed))) : fallback
}

function readState(projectPath: string): StoryState {
  try {
    const projectKey = projectStorageKey(projectPath)
    const projectValue = localStorage.getItem(projectKey)
    const legacy = JSON.parse(localStorage.getItem(LEGACY_STORAGE_KEY) ?? '{}')
    const value = projectValue ? JSON.parse(projectValue) : legacy
    const preferences = JSON.parse(localStorage.getItem(PREFERENCE_STORAGE_KEY) ?? '{}')
    const fragmentMin = normalizedLength(preferences.fragmentMin, 1000)
    const fragmentMax = Math.max(fragmentMin, normalizedLength(preferences.fragmentMax, 2000))
    const reasoningEffort = ['auto', 'low', 'medium', 'high', 'xhigh', 'max'].includes(preferences.reasoningEffort)
      ? preferences.reasoningEffort as ReasoningEffort : 'high'
    return {
      fragments: Array.isArray(value.fragments) ? value.fragments : [],
      agentMode: ['story', 'narrator', 'agent'].includes(preferences.agentMode ?? value.agentMode)
        ? (preferences.agentMode ?? value.agentMode) : 'story',
      narrativeMode: ['immersive', 'narrative', 'free'].includes(preferences.narrativeMode ?? value.narrativeMode)
        ? (preferences.narrativeMode ?? value.narrativeMode) : 'immersive',
      fragmentMin,
      fragmentMax,
      reasoningEffort,
    }
  } catch {
    return {
      fragments: [], agentMode: 'story', narrativeMode: 'immersive',
      fragmentMin: 1000, fragmentMax: 2000, reasoningEffort: 'high',
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

function parseStoryResponse(raw: string): { content: string; suggestions: string[] } | null {
  const markerIndex = raw.lastIndexOf(ACTIONS_MARKER)
  if (markerIndex < 0) return null
  const content = raw.slice(0, markerIndex).trim()
  const suggestions = raw.slice(markerIndex + ACTIONS_MARKER.length)
    .split(/\r?\n/)
    .map(line => line.replace(/^\s*(?:[-*•]|\d+[.)、])\s*/, '').trim())
    .filter(Boolean)
    .slice(0, 4)
  return content && suggestions.length === 4 ? { content, suggestions } : null
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

export const useStoryStore = defineStore('story', () => {
  const projectPath = ref(window.CoomiAndroid?.getStoryProjectPath?.() ?? '')
  const initial = readState(projectPath.value)
  const fragments = ref<StoryFragment[]>(initial.fragments)
  const agentMode = ref<AgentMode>(initial.agentMode)
  const narrativeMode = ref<NarrativeMode>(initial.narrativeMode)
  const fragmentMin = ref(initial.fragmentMin)
  const fragmentMax = ref(initial.fragmentMax)
  const reasoningEffort = ref<ReasoningEffort>(initial.reasoningEffort)
  const olderExpanded = ref(false)

  const latest = computed(() => fragments.value[fragments.value.length - 1] ?? null)
  const latestFive = computed(() => fragments.value.slice(-5).reverse())
  const older = computed(() => fragments.value.slice(0, -5).reverse())

  function persist() {
    localStorage.setItem(projectStorageKey(projectPath.value), JSON.stringify({ fragments: fragments.value }))
    localStorage.setItem(PREFERENCE_STORAGE_KEY, JSON.stringify({
      agentMode: agentMode.value,
      narrativeMode: narrativeMode.value,
      fragmentMin: fragmentMin.value,
      fragmentMax: fragmentMax.value,
      reasoningEffort: reasoningEffort.value,
    }))
  }

  function setAgentMode(mode: AgentMode) { agentMode.value = mode; persist() }
  function setNarrativeMode(mode: NarrativeMode) { narrativeMode.value = mode; persist() }
  function setFragmentLength(min: number, max: number) {
    const nextMin = normalizedLength(min, fragmentMin.value)
    const nextMax = normalizedLength(max, fragmentMax.value)
    fragmentMin.value = Math.min(nextMin, nextMax)
    fragmentMax.value = Math.max(nextMin, nextMax)
    persist()
  }
  function setReasoningEffort(effort: ReasoningEffort) { reasoningEffort.value = effort; persist() }

  function promptFor(text: string): string {
    return buildStoryPrompt({
      agentMode: agentMode.value,
      narrativeMode: narrativeMode.value,
      fragmentMin: fragmentMin.value,
      fragmentMax: fragmentMax.value,
      playerText: text,
      actionsMarker: ACTIONS_MARKER,
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
    if (!parsed) return null
    assistant.content = parsed.content
    if (latest.value?.content === parsed.content) return null
    const now = Date.now()
    const startsNewGroup = fragments.value.length % 5 === 0
    const group = startsNewGroup
      ? nextGroupTimestamp(new Set(fragments.value.map(item => item.group)), latest.value?.group)
      : (latest.value?.group ?? nextGroupTimestamp(new Set()))
    const sequence = (fragments.value.filter(item => item.group === group).length + 1).toString().padStart(3, '0')
    const filename = `${group}-${sequence}.md`
    const relativePath = `chapters/${group}/${filename}`
    const content = parsed.content
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
    }
    const body = `---\nsummary: ${JSON.stringify(summary)}\ncreatedAt: ${new Date(now).toISOString()}\n---\n\n${content}\n`
    if (projectPath.value) await writeStoryFragment(sessionId, relativePath, body)
    fragments.value.push(fragment)
    persist()
    return fragment
  }

  async function updateFragment(id: string, content: string, sessionId: string): Promise<boolean> {
    const fragment = fragments.value.find(item => item.id === id)
    if (!fragment) return false
    const trimmed = content.trim()
    const summary = shortSummary(trimmed)
    const body = `---\nsummary: ${JSON.stringify(summary)}\ncreatedAt: ${new Date(fragment.createdAt).toISOString()}\n---\n\n${trimmed}\n`
    if (projectPath.value) await writeStoryFragment(sessionId, fragment.path, body)
    fragment.content = trimmed
    fragment.summary = summary
    fragment.synced = true
    persist()
    return true
  }

  async function syncFragments(sessionId: string): Promise<number> {
    let synced = 0
    for (const fragment of fragments.value.filter(item => !item.synced)) {
      const body = `---\nsummary: ${JSON.stringify(fragment.summary)}\ncreatedAt: ${new Date(fragment.createdAt).toISOString()}\n---\n\n${fragment.content.trim()}\n`
      await writeStoryFragment(sessionId, fragment.path, body)
      fragment.synced = true
      synced++
    }
    if (synced > 0) persist()
    return synced
  }

  return {
    fragments, agentMode, narrativeMode, fragmentMin, fragmentMax, reasoningEffort, projectPath, olderExpanded,
    latest, latestFive, older, setAgentMode, setNarrativeMode, setFragmentLength, setReasoningEffort,
    promptFor, captureTurn, updateFragment, syncFragments,
    parseStoryResponse,
  }
})
