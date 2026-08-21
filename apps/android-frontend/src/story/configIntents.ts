/**
 * Agent 配置意图的前端执行器。
 *
 * 引擎只做传输：它把「哪个工具 + 原样参数」送过来，真正的执行走这里，而这里调的是界面按钮
 * 调的**同一批** store action。两个好处，也正是这层存在的理由：
 *  1. 改动即时出现在界面上——store 是响应式的真相源，不存在「盘上改了界面不知道」。
 *     反过来若让引擎自己写盘：store 每个项目根只加载一次，界面看不见那份改动，
 *     而它下一次保存又是拿内存整份覆盖同一个文件，Agent 的改动就没了；
 *  2. 校验只有一份——枚举收敛、比例求和、剧本三级层级、状态机、锁定事实保护，
 *     全部继承 store action 已有的规则，引擎侧不再抄第二份。
 *
 * 不可逆操作（删除条目、删除事实、覆盖或还原词库）在引擎侧已经过一道审批
 * （`storydex_intent_approval_reason`），走到这里的都是用户点过同意的。
 */
import { KEYWORD_LIBRARY_KINDS, KEYWORD_LIBRARY_LABELS, useKeywordLibraryStore, type KeywordLibrary, type KeywordLibraryKind } from '@/stores/keywordLibraries'
import { THEME_MODES, useConfigStore, type ThemeMode } from '@/stores/config'
import { normalizeScriptType, useProjectStore, type ManagedItem, type ProjectSettings, type TimeState } from '@/stores/project'
import { MAJOR_PHASE_LABELS, MINOR_TYPE_LABELS, normalizePlotMechanics, plotSettingsForScale, type PlotMechanicsSettings } from '@/story/plotMechanics'

export interface ConfigIntentResult {
  ok: boolean
  detail: string
}

type Args = Record<string, unknown>

const CONFIG_SECTIONS = ['settings', 'plot', 'time', 'director', 'presets', 'scripts', 'memory', 'keywords', 'appearance'] as const
type ConfigSection = typeof CONFIG_SECTIONS[number]

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}
function record(value: unknown): Args | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Args : null
}
function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

/** 剧本 / 预设条目的对外形状：只给元信息和路径，正文让模型用 read_file 按需读。 */
function itemView(item: ManagedItem, includeLevel: boolean) {
  const view: Record<string, unknown> = {
    id: item.id,
    title: item.title,
    enabled: item.enabled,
    path: item.path ?? '',
    updatedAt: item.updatedAt,
  }
  if (!includeLevel) return view
  view.level = normalizeScriptType(item.scriptType)
  view.parentId = item.parentId ?? ''
  view.status = item.status ?? 'active'
  if (item.completionCondition) view.completionCondition = item.completionCondition
  if (item.defaultRoute) view.defaultRoute = item.defaultRoute
  if (item.majorPhase) view.phase = MAJOR_PHASE_LABELS[item.majorPhase]
  if (item.minorType) view.minorType = MINOR_TYPE_LABELS[item.minorType]
  if (item.refactoredTo) view.refactoredTo = item.refactoredTo
  return view
}

function readSections(sections: ConfigSection[]): Record<string, unknown> {
  const project = useProjectStore()
  const config = useConfigStore()
  const libraries = useKeywordLibraryStore()
  const out: Record<string, unknown> = {}
  for (const section of sections) {
    switch (section) {
      case 'settings': {
        // plotMechanics 单独作为 plot 段返回，避免同一份数据在两个段里各出现一次。
        const { plotMechanics: _plot, ...rest } = project.settings
        out.settings = rest
        break
      }
      case 'plot': out.plot = project.settings.plotMechanics; break
      case 'time': out.time = project.time; break
      case 'director': out.director = project.directorState; break
      case 'presets': out.presets = project.presets.map(item => itemView(item, false)); break
      case 'scripts':
        out.scripts = project.scripts.map(item => itemView(item, true))
        // 扁平数组的顺序就是优先级顺序，模型改动前必须知道这点，否则会把 reorder 当成纯装饰。
        out.scriptsNote = 'Array order is priority order: the first active non-stage entry is the primary script, the next two are background clocks.'
        break
      case 'memory': out.memory = project.memoryFacts; break
      case 'keywords':
        out.keywords = Object.fromEntries(KEYWORD_LIBRARY_KINDS.map(kind => [kind, {
          label: KEYWORD_LIBRARY_LABELS[kind],
          ...libraries.stats(kind),
        }]))
        break
      case 'appearance':
        out.appearance = {
          theme: config.themeMode,
          available: THEME_MODES.map(item => ({ mode: item.mode, label: item.label })),
        }
        break
    }
  }
  return out
}

async function applySettings(args: Args): Promise<string[]> {
  const project = useProjectStore()
  const config = useConfigStore()
  const changed: string[] = []

  const settings = record(args.settings)
  const plot = record(args.plot)
  if (settings || plot) {
    const patch: Record<string, unknown> = { ...(settings ?? {}) }
    if (plot) patch.plotMechanics = mergePlotPatch(project.settings.plotMechanics, plot)
    await project.patchSettings(patch as Partial<ProjectSettings>)
    if (settings) changed.push('机制设置')
    if (plot) changed.push('剧情推进配置')
  }

  const time = record(args.time)
  if (time) {
    await project.patchTime(time as Partial<TimeState>)
    changed.push('时间系统')
  }

  const appearance = record(args.appearance)
  if (appearance) {
    const theme = text(appearance.theme)
    if (!THEME_MODES.some(item => item.mode === theme)) {
      throw new Error(`未知的主题档位「${theme}」；可用档位：${THEME_MODES.map(item => item.mode).join('、')}`)
    }
    config.setThemeMode(theme as ThemeMode)
    changed.push('主题外观')
  }
  return changed
}

/**
 * 把局部 plot 补丁并到完整配置上。
 *
 * `normalizeProjectSettings` 做的是浅合并，嵌套对象整体替换——只发一个字段就会让其余字段
 * 静默回落到 `balanced` 预设。所以补丁必须在这里先补全。
 *
 * 底座按界面 `selectMajorScale` 的语义选：显式指定了非自定义档位就用该档位的预设（选档位
 * 本来就是「把数量整套换掉」），否则以当前配置为底并落到自定义档——手工改过任何范围之后，
 * 界面上显示的也不再是原来那个档位。
 */
function mergePlotPatch(current: PlotMechanicsSettings, patch: Args): PlotMechanicsSettings {
  const scale = text(patch.scale)
  const presetScale = scale === 'fast' || scale === 'balanced' || scale === 'detailed' ? scale : null
  const base = presetScale ? plotSettingsForScale(presetScale) : { ...current, scale: 'custom' as const }
  return normalizePlotMechanics({ ...base, ...patch })
}

/** 剧本与预设共用一套条目 action，差别只在剧本多了层级 / 归属 / 状态。 */
async function manageItem(kind: 'presets' | 'scripts', args: Args): Promise<string> {
  const project = useProjectStore()
  const action = text(args.action)
  const list = kind === 'scripts' ? project.scripts : project.presets
  const label = kind === 'scripts' ? '剧本' : '风格预设'

  if (action === 'create') {
    const title = text(args.title).trim()
    if (!title) throw new Error(`新建${label}必须有标题`)
    const level = kind === 'scripts' ? text(args.level) : ''
    if (kind === 'scripts' && !['stage', 'major', 'minor'].includes(level)) {
      throw new Error('新建剧本必须指明层级：stage、major 或 minor')
    }
    // parentId 一并交给 addItem：它在落盘前校验父链，父级非法时不会先留下一个孤儿 md。
    const created = await project.addItem(kind, title, text(args.content),
      text(args.completionCondition), text(args.defaultRoute),
      kind === 'scripts'
        ? { scriptType: level as NonNullable<ManagedItem['scriptType']>, parentId: text(args.parentId).trim() }
        : {})
    return `已新建${label}「${created.title}」（id=${created.id}）`
  }

  const id = text(args.id).trim()
  if (!id) throw new Error(`${action} 需要指明条目 id`)
  const item = list.find(candidate => candidate.id === id)
  if (!item) throw new Error(`未找到 id 为 ${id} 的${label}条目`)

  switch (action) {
    case 'update': {
      // 只发标题就只改标题：读回当前正文再写，避免把内容清空。
      const content = args.content === undefined ? await project.readItem(kind, item) : text(args.content)
      await project.updateItem(kind, item, text(args.title) || item.title, content,
        args.completionCondition === undefined ? item.completionCondition ?? '' : text(args.completionCondition),
        args.defaultRoute === undefined ? item.defaultRoute ?? '' : text(args.defaultRoute))
      return `已更新${label}「${item.title}」`
    }
    case 'delete':
      await project.removeItem(kind, item)
      return `已删除${label}「${item.title}」`
    case 'enable':
    case 'disable': {
      const want = action === 'enable'
      if (item.enabled === want) return `${label}「${item.title}」已经是${want ? '启用' : '停用'}状态`
      await project.toggleItem(kind, item)
      return `已${want ? '启用' : '停用'}${label}「${item.title}」`
    }
    case 'set_status': {
      const status = text(args.status)
      if (!['active', 'pending', 'completed'].includes(status)) throw new Error('status 必须是 active、pending 或 completed')
      await project.markScript(item, status as 'active' | 'pending' | 'completed')
      return `已把剧本「${item.title}」标记为 ${status}`
    }
    case 'set_parent':
      await project.setScriptParent(item, text(args.parentId))
      return text(args.parentId).trim()
        ? `已把剧本「${item.title}」挂到 ${text(args.parentId)} 下`
        : `已把剧本「${item.title}」移出所属层级`
    case 'reorder': {
      const direction = text(args.direction)
      if (direction !== 'up' && direction !== 'down') throw new Error('direction 必须是 up 或 down')
      const step: -1 | 1 = direction === 'up' ? -1 : 1
      if (kind === 'scripts') await project.moveScriptSibling(item, step)
      else await project.moveItem(kind, item, step)
      return `已把${label}「${item.title}」${direction === 'up' ? '上移' : '下移'}一位`
    }
    default:
      throw new Error(`未知的操作：${action}`)
  }
}

async function manageMemory(args: Args): Promise<string> {
  const project = useProjectStore()
  const action = text(args.action)
  if (action === 'add') {
    const scope = text(args.scope)
    const fact = await project.addMemoryFact(text(args.text),
      scope === 'protagonist' ? 'protagonist' : 'objective')
    const sources = stringList(args.sources)
    if (sources.length) {
      fact.sources = sources
      await project.saveMemory()
    }
    return `已新增记忆事实（id=${fact.id}）`
  }
  const id = text(args.id).trim()
  if (!id) throw new Error(`${action} 需要指明事实 id`)
  const fact = project.memoryFacts.find(candidate => candidate.id === id)
  if (!fact) throw new Error(`未找到 id 为 ${id} 的记忆事实`)
  // 锁定事实是用户手工钉住的：内容与有效性一律不动，要改先显式解锁。
  if (fact.locked && ['update', 'invalidate', 'restore', 'delete'].includes(action)) {
    throw new Error('该事实已被用户锁定；如确需改动，请先执行 unlock 并说明理由')
  }
  switch (action) {
    case 'update': {
      const patch: Parameters<typeof project.updateMemoryFact>[1] = {}
      if (args.text !== undefined) patch.text = text(args.text)
      if (args.scope !== undefined) patch.scope = text(args.scope) === 'protagonist' ? 'protagonist' : 'objective'
      await project.updateMemoryFact(id, patch)
      const sources = stringList(args.sources)
      if (sources.length) { fact.sources = sources; await project.saveMemory() }
      return '已更新记忆事实'
    }
    case 'invalidate':
      await project.updateMemoryFact(id, { stale: true })
      return '已把该事实标记为不再成立（内容保留）'
    case 'restore':
      await project.updateMemoryFact(id, { stale: false })
      return '已恢复该事实的有效状态'
    case 'lock':
    case 'unlock':
      await project.updateMemoryFact(id, { locked: action === 'lock' })
      return action === 'lock' ? '已锁定该事实' : '已解锁该事实'
    case 'delete':
      await project.removeMemoryFact(fact)
      return '已永久删除该记忆事实'
    default:
      throw new Error(`未知的操作：${action}`)
  }
}

async function manageKeywordLibrary(args: Args): Promise<string> {
  const libraries = useKeywordLibraryStore()
  const kind = text(args.kind) as KeywordLibraryKind
  if (!KEYWORD_LIBRARY_KINDS.includes(kind)) {
    throw new Error(`未知的词库类型：${text(args.kind)}`)
  }
  const label = KEYWORD_LIBRARY_LABELS[kind]
  switch (text(args.action)) {
    case 'get':
      return JSON.stringify({ kind, label, ...libraries.stats(kind), library: libraries.active(kind) })
    case 'replace': {
      const library = record(args.library)
      if (!library) throw new Error('replace 需要提供 library（分类名 → 词条数组）')
      const result = await libraries.replaceLibrary(kind, library as KeywordLibrary)
      return `已把「${label}」替换为项目词库：${result.categories} 个分类 / ${result.keywords} 个词条${result.warning ? `（${result.warning}）` : ''}。内置词库仍可用 restore_builtin 恢复。`
    }
    case 'restore_builtin':
      await libraries.restoreBuiltin(kind)
      return `已把「${label}」恢复为内置词库`
    default:
      throw new Error(`未知的操作：${text(args.action)}`)
  }
}

/**
 * 执行一条配置意图。
 *
 * 一律返回结果而不抛出：调用方要把成败原样回给模型，让它自己纠正参数，而不是让一次
 * 参数写错就把整轮执行打断。
 */
export async function executeConfigIntent(tool: string, args: Args): Promise<ConfigIntentResult> {
  try {
    switch (tool) {
      case 'storydex_config_get': {
        const requested = stringList(args.sections).filter((item): item is ConfigSection =>
          (CONFIG_SECTIONS as readonly string[]).includes(item))
        const sections = requested.length ? requested : [...CONFIG_SECTIONS]
        return { ok: true, detail: JSON.stringify(readSections(sections)) }
      }
      case 'storydex_config_set': {
        const changed = await applySettings(args)
        if (!changed.length) return { ok: false, detail: '没有提供任何要修改的字段' }
        return { ok: true, detail: `已更新：${changed.join('、')}。界面已同步显示。` }
      }
      case 'storydex_script_manage':
        return { ok: true, detail: await manageItem('scripts', args) }
      case 'storydex_preset_manage':
        return { ok: true, detail: await manageItem('presets', args) }
      case 'storydex_memory_manage':
        return { ok: true, detail: await manageMemory(args) }
      case 'storydex_keyword_library':
        return { ok: true, detail: await manageKeywordLibrary(args) }
      default:
        return { ok: false, detail: `未知的配置工具：${tool}` }
    }
  } catch (cause) {
    return { ok: false, detail: cause instanceof Error ? cause.message : String(cause) }
  }
}

/** 只读意图不改任何状态，回合结束后不必重装上下文。 */
export function configIntentMutates(tool: string): boolean {
  if (tool === 'storydex_config_get') return false
  return true
}
