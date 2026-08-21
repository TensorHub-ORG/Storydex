import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { EVENT_KEYWORD_LIBRARY } from '@/story/eventKeywords'
import { FEMALE_CHARACTER_KEYWORD_LIBRARY } from '@/story/femaleCharacterKeywords'
import { MALE_CHARACTER_KEYWORD_LIBRARY } from '@/story/maleCharacterKeywords'
import { PAYOFF_KEYWORD_LIBRARY } from '@/story/payoffKeywords'
import { TRAGEDY_KEYWORD_LIBRARY } from '@/story/tragedyKeywords'
import { currentProjectRoot, deleteProjectFile, exportProjectContent, readProjectJson, writeProjectJson } from '@/utils/projectFiles'

export type KeywordLibrary = Record<string, string[]>
export type KeywordLibraryKind = 'event' | 'male' | 'female' | 'tragedy' | 'payoff'

export const KEYWORD_LIBRARY_KINDS: KeywordLibraryKind[] = ['event', 'male', 'female', 'tragedy', 'payoff']

const STORAGE_PREFIX = 'storydex.mobile.keyword-library.v1:'
const MIGRATION_PREFIX = 'storydex.mobile.keyword-library.migrated.v2:'
const MAX_JSON_LENGTH = 2 * 1024 * 1024
const MAX_KEYWORDS = 10_000
const MAX_KEYWORD_LENGTH = 80
const INJECTION_PATTERN = /(?:ignore\s+(?:all\s+)?(?:previous|above)\s+instructions|system\s+prompt|忽略.{0,8}(?:指令|要求)|系统提示词|越过.{0,4}(?:规则|限制))/i

const BUILTIN_LIBRARIES: Record<KeywordLibraryKind, KeywordLibrary> = {
  event: EVENT_KEYWORD_LIBRARY,
  male: MALE_CHARACTER_KEYWORD_LIBRARY,
  female: FEMALE_CHARACTER_KEYWORD_LIBRARY,
  tragedy: TRAGEDY_KEYWORD_LIBRARY,
  payoff: PAYOFF_KEYWORD_LIBRARY,
}

export function builtinKeywordLibrary(kind: KeywordLibraryKind): KeywordLibrary {
  return BUILTIN_LIBRARIES[kind]
}

export const KEYWORD_LIBRARY_LABELS: Record<KeywordLibraryKind, string> = {
  event: '随机事件',
  male: '男性人物',
  female: '女性人物',
  tragedy: '悲剧情节',
  payoff: '爽点情节',
}

export const KEYWORD_LIBRARY_FILENAMES: Record<KeywordLibraryKind, string> = {
  event: 'storydex-random-events.json',
  male: 'storydex-random-characters-male.json',
  female: 'storydex-random-characters-female.json',
  tragedy: 'storydex-random-tragedies.json',
  payoff: 'storydex-random-payoffs.json',
}

const PROJECT_LIBRARY_FILES: Record<KeywordLibraryKind, string> = {
  event: '.storydex/random/events.json',
  male: '.storydex/random/characters-male.json',
  female: '.storydex/random/characters-female.json',
  tragedy: '.storydex/random/tragedies.json',
  payoff: '.storydex/random/payoffs.json',
}

export interface KeywordLibraryStats {
  categories: number
  keywords: number
  source: 'builtin' | 'custom'
  warning: string
}

export interface KeywordImportResult {
  library: KeywordLibrary
  categories: number
  keywords: number
  warning: string
}

export function keywordCount(library: KeywordLibrary): number {
  return Object.values(library).reduce((total, values) => total + values.length, 0)
}

export function normalizeKeywordLibrary(value: unknown): KeywordImportResult {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('JSON 顶层必须是“分类名: 字符串数组”的对象')
  }
  const normalized: KeywordLibrary = {}
  const seen = new Set<string>()
  let total = 0
  for (const [rawCategory, rawValues] of Object.entries(value as Record<string, unknown>)) {
    const category = rawCategory.trim()
    if (!category) continue
    if (!Array.isArray(rawValues)) throw new Error(`分类“${category}”必须是字符串数组`)
    const values: string[] = []
    for (const rawValue of rawValues) {
      if (typeof rawValue !== 'string') throw new Error(`分类“${category}”包含非字符串词条`)
      const keyword = rawValue.trim()
      if (!keyword) continue
      if (keyword.length > MAX_KEYWORD_LENGTH) throw new Error(`词条“${keyword.slice(0, 12)}…”超过 ${MAX_KEYWORD_LENGTH} 个字符`)
      if (/[\u0000-\u001f\u007f\r\n]/.test(keyword) || INJECTION_PATTERN.test(keyword)) {
        throw new Error(`词条“${keyword.slice(0, 16)}”包含控制字符或指令式内容`)
      }
      if (seen.has(keyword)) continue
      seen.add(keyword)
      values.push(keyword)
      total += 1
      if (total > MAX_KEYWORDS) throw new Error(`词条总数不能超过 ${MAX_KEYWORDS}`)
    }
    if (values.length) normalized[category] = values
  }
  const categories = Object.keys(normalized).length
  if (total === 0) throw new Error('词汇表没有可用词条')
  return {
    library: normalized,
    categories,
    keywords: total,
    warning: total < 3 ? '词库过小：触发时只能使用全部可用词条' : '',
  }
}

export function parseKeywordLibraryJson(raw: string): KeywordImportResult {
  if (raw.length > MAX_JSON_LENGTH) throw new Error('JSON 文件不能超过 2 MB')
  let value: unknown
  try { value = JSON.parse(raw) } catch { throw new Error('JSON 格式无效') }
  return normalizeKeywordLibrary(value)
}

function readCustom(kind: KeywordLibraryKind): KeywordLibrary | null {
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + kind)
    return raw ? parseKeywordLibraryJson(raw).library : null
  } catch {
    return null
  }
}

export const useKeywordLibraryStore = defineStore('keyword-libraries', () => {
  const custom = ref<Record<KeywordLibraryKind, KeywordLibrary | null>>({
    event: null,
    male: null,
    female: null,
    tragedy: null,
    payoff: null,
  })
  const loadedProject = ref('')

  const eventLibrary = computed(() => custom.value.event ?? BUILTIN_LIBRARIES.event)
  const maleLibrary = computed(() => custom.value.male ?? BUILTIN_LIBRARIES.male)
  const femaleLibrary = computed(() => custom.value.female ?? BUILTIN_LIBRARIES.female)
  const tragedyLibrary = computed(() => custom.value.tragedy ?? BUILTIN_LIBRARIES.tragedy)
  const payoffLibrary = computed(() => custom.value.payoff ?? BUILTIN_LIBRARIES.payoff)

  function active(kind: KeywordLibraryKind): KeywordLibrary {
    return custom.value[kind] ?? BUILTIN_LIBRARIES[kind]
  }

  function stats(kind: KeywordLibraryKind): KeywordLibraryStats {
    const library = active(kind)
    const keywords = keywordCount(library)
    return {
      categories: Object.keys(library).length,
      keywords,
      source: custom.value[kind] ? 'custom' : 'builtin',
      warning: keywords < 3 ? '词库过小：触发时只能使用全部可用词条' : '',
    }
  }

  async function initialize() {
    const root = currentProjectRoot()
    if (!root || loadedProject.value === root) return
    const next: Record<KeywordLibraryKind, KeywordLibrary | null> = {
      event: null, male: null, female: null, tragedy: null, payoff: null,
    }
    for (const kind of KEYWORD_LIBRARY_KINDS) {
      const projectValue = await readProjectJson<unknown>(PROJECT_LIBRARY_FILES[kind])
      if (projectValue) next[kind] = normalizeKeywordLibrary(projectValue).library
    }
    const migrationKey = MIGRATION_PREFIX + encodeURIComponent(root)
    if (!localStorage.getItem(migrationKey)) {
      for (const kind of KEYWORD_LIBRARY_KINDS) {
        if (!next[kind]) {
          const legacy = readCustom(kind)
          if (legacy) {
            next[kind] = legacy
            await writeProjectJson(PROJECT_LIBRARY_FILES[kind], legacy)
          }
        }
      }
      localStorage.setItem(migrationKey, new Date().toISOString())
    }
    custom.value = next
    loadedProject.value = root
  }

  async function importJson(kind: KeywordLibraryKind, raw: string): Promise<KeywordImportResult> {
    const result = parseKeywordLibraryJson(raw)
    await writeProjectJson(PROJECT_LIBRARY_FILES[kind], result.library)
    custom.value = { ...custom.value, [kind]: result.library }
    return result
  }

  async function restoreBuiltin(kind: KeywordLibraryKind) {
    await deleteProjectFile(PROJECT_LIBRARY_FILES[kind])
    custom.value = { ...custom.value, [kind]: null }
  }

  async function replaceLibrary(kind: KeywordLibraryKind, library: KeywordLibrary) {
    const result = normalizeKeywordLibrary(library)
    await writeProjectJson(PROJECT_LIBRARY_FILES[kind], result.library)
    custom.value = { ...custom.value, [kind]: result.library }
    return result
  }

  function exportJson(kind: KeywordLibraryKind): string {
    return JSON.stringify(active(kind), null, 2) + '\n'
  }

  async function exportCurrent(kind: KeywordLibraryKind) {
    await exportProjectContent(
      PROJECT_LIBRARY_FILES[kind],
      exportJson(kind),
      KEYWORD_LIBRARY_FILENAMES[kind],
    )
  }

  void initialize()
  return {
    custom, eventLibrary, maleLibrary, femaleLibrary, tragedyLibrary, payoffLibrary,
    active, stats, initialize,
    importJson, replaceLibrary, restoreBuiltin, exportJson, exportCurrent,
  }
})
