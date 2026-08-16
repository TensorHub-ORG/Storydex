import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  createId, deleteProjectFile, exportProjectContent, readProjectJson, readProjectText,
  safeFilename, writeProjectJson, writeProjectText,
} from '@/utils/projectFiles'

export interface ProjectSettings {
  schemaVersion: 1
  recentFragments: number
  memoryCheckpoint: 5 | 10 | 15 | 20 | 30
  inferenceCycle: 10
  fortuneEnabled: boolean
  eventEnabled: boolean
  characterEnabled: boolean
  characterGender: 'random' | 'male' | 'female'
}

export interface ManagedItem {
  id: string
  title: string
  filename: string
  enabled: boolean
  status?: 'active' | 'pending' | 'completed'
  completionCondition?: string
  defaultRoute?: string
  updatedAt: string
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
  memoryFacts?: Array<{ text: string; sources?: string[]; scope?: 'objective' | 'protagonist' }>
  scriptUpdates?: Array<{ id?: string; title?: string; status: 'active' | 'pending' | 'completed' }>
}

const DEFAULT_SETTINGS: ProjectSettings = {
  schemaVersion: 1, recentFragments: 3, memoryCheckpoint: 10, inferenceCycle: 10,
  fortuneEnabled: true, eventEnabled: false, characterEnabled: false, characterGender: 'random',
}
const DEFAULT_TIME: TimeState = {
  schemaVersion: 1, calendar: 'relative', calendarName: '相对历', current: '1', display: '第1日',
  precision: 'day', locked: false, flashback: null, revisionSnapshots: [],
}

type CollectionKind = 'presets' | 'scripts'

export const useProjectStore = defineStore('story-project', () => {
  const ready = ref(false)
  const error = ref('')
  const settings = ref<ProjectSettings>({ ...DEFAULT_SETTINGS })
  const presets = ref<ManagedItem[]>([])
  const scripts = ref<ManagedItem[]>([])
  const memoryFacts = ref<MemoryFact[]>([])
  const memoryPending = ref(false)
  const time = ref<TimeState>({ ...DEFAULT_TIME })
  const currentTimeLabel = computed(() => time.value.display || '第1日')

  async function initialize() {
    error.value = ''
    try {
      settings.value = { ...DEFAULT_SETTINGS, ...(await readProjectJson<Partial<ProjectSettings>>('.storydex/settings.json') ?? {}) }
      presets.value = await loadCollection('presets')
      scripts.value = await loadCollection('scripts')
      const memory = await readProjectJson<{ facts?: MemoryFact[]; pendingSync?: boolean }>('.storydex/memory/state.json')
      memoryFacts.value = (memory?.facts ?? []).map(fact => ({ ...fact, scope: fact.scope ?? 'objective' }))
      memoryPending.value = memory?.pendingSync ?? false
      time.value = { ...DEFAULT_TIME, ...(await readProjectJson<Partial<TimeState>>('.storydex/time/state.json') ?? {}) }
      await Promise.all([
        writeProjectJson('.storydex/project.json', { schemaVersion: 1, updatedAt: new Date().toISOString() }),
        saveSettings(), saveCollection('presets'), saveCollection('scripts'), saveMemory(), saveTime(),
      ])
      ready.value = true
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
    }
  }

  async function saveSettings() { await writeProjectJson('.storydex/settings.json', settings.value) }
  async function patchSettings(patch: Partial<ProjectSettings>) {
    settings.value = { ...settings.value, ...patch, schemaVersion: 1 }
    await saveSettings()
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
      let title = stringField(raw, 'title', 'name', 'label', 'presetName', 'scriptName')
      if (!title && filename) title = filename.replace(/\.(?:md|markdown|txt)$/i, '')
      title ||= kind === 'presets' ? `未命名预设 ${index + 1}` : `未命名剧本 ${index + 1}`
      if (!filename) filename = `${safeFilename(title)}-${id.slice(-8)}.md`

      const existing = await readProjectText(`.storydex/${kind}/${filename}`)
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
        updatedAt: stringField(raw, 'updatedAt', 'updated_at') || new Date().toISOString(),
      })
    }
    return result
  }
  async function saveCollection(kind: CollectionKind) {
    await writeProjectJson(`.storydex/${kind}/index.json`, { schemaVersion: 1, items: collection(kind).value })
  }
  async function addItem(kind: CollectionKind, title: string, content: string, completionCondition = '', defaultRoute = '') {
    const id = createId(kind === 'presets' ? 'preset' : 'script')
    const filename = `${safeFilename(title)}-${id.slice(-8)}.md`
    const item: ManagedItem = {
      id, title: title.trim() || (kind === 'presets' ? '未命名预设' : '未命名剧本'), filename,
      enabled: true, status: kind === 'scripts' ? 'active' : undefined,
      completionCondition: kind === 'scripts' ? completionCondition.trim() : undefined,
      defaultRoute: kind === 'scripts' ? defaultRoute.trim() : undefined,
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
    item.updatedAt = new Date().toISOString()
    await writeProjectText(`.storydex/${kind}/${item.filename}`, content.trim() + '\n')
    await saveCollection(kind)
  }
  async function readItem(kind: CollectionKind, item: ManagedItem) {
    return await readProjectText(`.storydex/${kind}/${item.filename}`) ?? ''
  }
  async function toggleItem(kind: CollectionKind, item: ManagedItem) {
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
    await deleteProjectFile(`.storydex/${kind}/${item.filename}`)
    collection(kind).value = collection(kind).value.filter(candidate => candidate.id !== item.id)
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
  async function exportItem(kind: CollectionKind, item: ManagedItem) {
    const content = await readItem(kind, item)
    await exportProjectContent(`.storydex/${kind}/${item.filename}`, content, item.filename)
  }
  async function markScript(item: ManagedItem, status: 'active' | 'pending' | 'completed') {
    item.status = status
    item.updatedAt = new Date().toISOString()
    await saveCollection('scripts')
  }

  async function saveMemory() {
    await writeProjectJson('.storydex/memory/state.json', {
      schemaVersion: 1, pendingSync: memoryPending.value, facts: memoryFacts.value, updatedAt: new Date().toISOString(),
    })
  }
  async function addMemoryFact(text: string) {
    memoryFacts.value.push({ id: createId('fact'), text: text.trim(), locked: false, stale: false, sources: [], scope: 'objective' })
    await saveMemory()
  }
  async function removeMemoryFact(fact: MemoryFact) {
    memoryFacts.value = memoryFacts.value.filter(candidate => candidate.id !== fact.id)
    await saveMemory()
  }
  async function saveTime() { await writeProjectJson('.storydex/time/state.json', time.value) }
  async function patchTime(patch: Partial<TimeState>) {
    time.value = { ...time.value, ...patch, schemaVersion: 1 }
    await saveTime()
  }
  async function createTimeRevision(next: string) {
    time.value.revisionSnapshots.push({ id: createId('time-revision'), createdAt: new Date().toISOString(), from: time.value.display, to: next })
    time.value.display = next
    await saveTime()
  }
  async function applyStoryDelta(delta: StoryStateDelta) {
    if (!delta.advanced) return
    for (const incoming of delta.memoryFacts ?? []) {
      const text = incoming.text?.trim()
      if (!text || memoryFacts.value.some(fact => fact.text === text)) continue
      memoryFacts.value.push({ id: createId('fact'), text, locked: false, stale: false, sources: incoming.sources ?? [], scope: incoming.scope ?? 'objective' })
    }
    for (const update of delta.scriptUpdates ?? []) {
      const item = scripts.value.find(candidate => candidate.id === update.id || candidate.title === update.title)
      if (item) { item.status = update.status; item.updatedAt = new Date().toISOString() }
    }
    if (delta.timeDisplay?.trim() && !time.value.locked) time.value.display = delta.timeDisplay.trim()
    memoryPending.value = false
    await Promise.all([saveMemory(), saveCollection('scripts'), saveTime()])
  }
  async function markMemoryPending() { memoryPending.value = true; await saveMemory() }
  async function markMemoryStale(source: string) {
    let matched = false
    for (const fact of memoryFacts.value) {
      if (!fact.locked && (fact.sources.length === 0 || fact.sources.includes(source))) { fact.stale = true; matched = true }
    }
    if (matched) { memoryPending.value = true; await saveMemory() }
  }

  return {
    ready, error, settings, presets, scripts, memoryFacts, memoryPending, time, currentTimeLabel,
    initialize, patchSettings, addItem, updateItem, readItem, toggleItem, renameItem, removeItem,
    moveItem, exportItem, markScript, saveMemory, addMemoryFact, removeMemoryFact, patchTime, createTimeRevision,
    applyStoryDelta, markMemoryPending, markMemoryStale,
  }
})
