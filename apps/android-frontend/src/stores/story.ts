import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { apiGet, authedFetch } from '@/bridge/http'
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
function parseFragmentFile(raw: string): { summary: string; createdAt: number; content: string } | null {
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

  function setAgentMode(mode: AgentMode) { agentMode.value = mode; persist(); if (mode !== 'agent') void loadFragmentsFromProject() }
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
          suggestions: [],
          synced: true,
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

  return {
    fragments, agentMode, narrativeMode, fragmentMin, fragmentMax, reasoningEffort, projectPath, olderExpanded,
    latest, latestFive, older, setAgentMode, setNarrativeMode, setFragmentLength, setReasoningEffort,
    promptFor, captureTurn, updateFragment, syncFragments, loadFragmentsFromProject,
    parseStoryResponse,
  }
})
