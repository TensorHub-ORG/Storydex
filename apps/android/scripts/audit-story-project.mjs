/**
 * 故事项目结构审计：把一个项目目录对照代码里的真实契约逐条检查。
 *
 *   node audit-story-project.mjs <项目目录>
 *
 * 契约来源（不是文档，是代码——文档和实现有出入时以实现为准）：
 *   - 片段命名与分组      stores/story.ts captureTurn / timestamp / nextGroupTimestamp
 *   - frontmatter 解析     stores/story.ts parseFragmentFile
 *   - 加载与排序          stores/story.ts loadFragmentsFromProject
 *   - 索引与层级          stores/project.ts loadCollection / normalizeScriptType
 *
 * error 级 = 会让数据对 app 不可见或加载错乱；warn 级 = 不规范但仍能工作。
 * 有 error 时退出码 1，便于回写后当作回归检查跑。
 */
import fs from 'node:fs'
import path from 'node:path'

const root = process.argv[2]
if (!root) {
  console.error('用法：node audit-story-project.mjs <项目目录>')
  process.exit(2)
}
if (!fs.existsSync(root)) {
  console.error(`目录不存在：${root}`)
  process.exit(2)
}

const errors = []
const warns = []
const notes = []
const err = (msg) => errors.push(msg)
const warn = (msg) => warns.push(msg)
const note = (msg) => notes.push(msg)

const readJson = (rel) => {
  const abs = path.join(root, rel)
  if (!fs.existsSync(abs)) return null
  try { return JSON.parse(fs.readFileSync(abs, 'utf8')) } catch (e) {
    err(`${rel} 不是合法 JSON：${e.message}`)
    return null
  }
}

// ── frontmatter：与 parseFragmentFile 完全同构 ──
const FRONTMATTER = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/
function unquote(value) {
  const v = value.trim()
  if (v.length >= 2 && v.startsWith('"') && v.endsWith('"')) {
    try { return JSON.parse(v) } catch { return v.slice(1, -1) }
  }
  if (v.length >= 2 && v.startsWith("'") && v.endsWith("'")) return v.slice(1, -1)
  return v
}
function parseFragment(raw) {
  const match = raw.match(FRONTMATTER)
  if (!match) return null
  const meta = {}
  for (const line of match[1].split(/\r?\n/)) {
    const idx = line.indexOf(':')
    if (idx > 0) meta[line.slice(0, idx).trim().toLowerCase()] = unquote(line.slice(idx + 1))
  }
  const createdAt = Date.parse(meta.createdat ?? meta.created_at ?? '')
  let suggestions = null
  if (meta.suggestions !== undefined) {
    try {
      const value = JSON.parse(meta.suggestions)
      suggestions = Array.isArray(value) ? value.filter(i => typeof i === 'string') : null
    } catch { suggestions = null }
  }
  return {
    summary: meta.summary ?? '',
    createdAt: Number.isFinite(createdAt) ? createdAt : 0,
    hasCreatedAt: meta.createdat !== undefined || meta.created_at !== undefined,
    suggestions,
    content: (match[2] ?? '').trim(),
  }
}

// ══════════ 1. chapters/ ══════════
const chaptersDir = path.join(root, 'chapters')
const fragments = []
let groups = []
if (!fs.existsSync(chaptersDir)) {
  err('缺少 chapters/ 目录')
} else {
  groups = fs.readdirSync(chaptersDir, { withFileTypes: true })
    .filter(e => e.isDirectory()).map(e => e.name).sort()
  const loose = fs.readdirSync(chaptersDir, { withFileTypes: true })
    .filter(e => e.isFile() && /\.md$/i.test(e.name)).map(e => e.name)
  // loadFragmentsFromProject 用 rel.split('/')[1] 当分组，直接放在 chapters/ 下的文件
  // 会把文件名本身当成分组名，后续 id 和排序都会错。
  if (loose.length) err(`chapters/ 下有 ${loose.length} 个散落 md（应在分组子目录内）：${loose.slice(0, 5).join('、')}`)

  const seenIds = new Map()
  for (const group of groups) {
    const files = fs.readdirSync(path.join(chaptersDir, group), { withFileTypes: true })
      .filter(e => e.isFile() && /\.md$/i.test(e.name)).map(e => e.name).sort()
    if (!/^\d{12}$/.test(group)) {
      if (/^\d{14}$/.test(group)) warn(`分组 ${group} 是 14 位旧格式（当前 timestamp() 产出 12 位 YYYYMMDDHHMM）`)
      else warn(`分组 ${group} 不是 12 位时间戳`)
    }
    const seqs = []
    for (const name of files) {
      const stem = name.replace(/\.md$/i, '')
      const rel = `chapters/${group}/${name}`
      if (stem !== `${group}-${String(stem.split('-').pop()).padStart(3, '0')}`) {
        err(`${rel} 文件名不符合 {分组}-{三位序号}.md`)
      }
      const seqRaw = stem.split('-').pop()
      if (!/^\d{3}$/.test(seqRaw)) err(`${rel} 序号不是三位数字`)
      seqs.push(Number(seqRaw))

      const id = `${group}-${seqRaw}`
      if (seenIds.has(id)) err(`片段 id 重复：${rel} 与 ${seenIds.get(id)}（加载时后者会被丢弃）`)
      else seenIds.set(id, rel)

      const parsed = parseFragment(fs.readFileSync(path.join(chaptersDir, group, name), 'utf8'))
      if (!parsed) {
        err(`${rel} 没有 frontmatter —— parseFragmentFile 返回 null，该片段对 app 完全不可见`)
        continue
      }
      if (!parsed.content) err(`${rel} frontmatter 之后没有正文`)
      if (!parsed.hasCreatedAt) err(`${rel} 缺 createdAt —— 排序键落到 0，会被排到最前面`)
      else if (parsed.createdAt === 0) err(`${rel} 的 createdAt 无法解析为时间`)
      if (!parsed.summary) warn(`${rel} 缺 summary（加载时会用正文前 50 字兜底）`)
      if (parsed.suggestions === null) warn(`${rel} 缺 suggestions（行动建议为空，界面无快捷选项）`)
      else if (parsed.suggestions.length !== 4) warn(`${rel} 的 suggestions 有 ${parsed.suggestions.length} 条（约定 4 条）`)
      fragments.push({ rel, group, seq: Number(seqRaw), createdAt: parsed.createdAt })
    }
    const expected = Array.from({ length: files.length }, (_, i) => i + 1)
    if (files.length && seqs.slice().sort((a, b) => a - b).join(',') !== expected.join(',')) {
      err(`分组 ${group} 的序号不是从 001 连续排列：${seqs.join('、')}`)
    }
    if (files.length !== 5) note(`分组 ${group} 有 ${files.length} 个片段`)
  }

  // captureTurn 装满当前分组才滚到下一组，所以除最后一组外每组都该恰好 5 个。
  const bySize = groups.map(g => ({
    g, n: fs.readdirSync(path.join(chaptersDir, g)).filter(f => /\.md$/i.test(f)).length,
  }))
  const offenders = bySize.slice(0, -1).filter(x => x.n !== 5)
  if (offenders.length) {
    warn(`${offenders.length}/${groups.length} 个非末尾分组不是 5 个片段（历史遗留；`
      + `captureTurn 现在按「当前分组已装满」滚组，会自行回正，不再持续错位）`)
  }

  // 排序键与实际叙事顺序是否一致：createdAt 升序应当和 (分组, 序号) 升序一致。
  const byName = [...fragments].sort((a, b) => a.group.localeCompare(b.group) || a.seq - b.seq)
  const byTime = [...fragments].sort((a, b) => a.createdAt - b.createdAt)
  const firstDivergence = byName.findIndex((f, i) => byTime[i]?.rel !== f.rel)
  if (firstDivergence >= 0) {
    warn(`createdAt 排序与文件名顺序不一致，最早分歧在第 ${firstDivergence + 1} 位：`
      + `文件名序为 ${byName[firstDivergence].rel}，时间序为 ${byTime[firstDivergence].rel}`)
  }
}

// ══════════ 2. .storydex/ ══════════
const dotDir = path.join(root, '.storydex')
if (!fs.existsSync(dotDir)) {
  err('缺少 .storydex/ 目录')
} else {
  const project = readJson('.storydex/project.json')
  if (!project) err('缺少 .storydex/project.json')
  else if (project.schemaVersion !== 1) warn(`project.json schemaVersion=${project.schemaVersion}（约定 1）`)

  for (const rel of ['.storydex/director/state.json', '.storydex/memory/state.json', '.storydex/time/state.json']) {
    if (!fs.existsSync(path.join(root, rel))) warn(`缺少 ${rel}`)
  }
  if (fs.existsSync(path.join(root, '.storydex/director/pending-commit.json'))) {
    warn('存在 director/pending-commit.json —— 上一轮提交没有正常收尾，回写前先确认这一轮的归属')
  }

  const VALID_TYPES = new Set(['stage', 'major', 'minor'])
  for (const kind of ['presets', 'scripts']) {
    const index = readJson(`.storydex/${kind}/index.json`)
    if (!index) { err(`缺少 .storydex/${kind}/index.json`); continue }
    if (index.schemaVersion !== 2) warn(`${kind}/index.json schemaVersion=${index.schemaVersion}（约定 2）`)
    const items = Array.isArray(index.items) ? index.items : []
    if (!Array.isArray(index.items)) { err(`${kind}/index.json 的 items 不是数组`); continue }

    const byId = new Map(items.map(i => [i.id, i]))
    if (byId.size !== items.length) err(`${kind}/index.json 存在重复 id`)
    for (const item of items) {
      if (!item.id || !item.title) { err(`${kind}/index.json 有条目缺 id 或 title`); continue }
      if (!item.path) { err(`${kind} 条目「${item.title}」缺 path`); continue }
      if (!fs.existsSync(path.join(root, item.path))) err(`${kind} 条目「${item.title}」指向的文件不存在：${item.path}`)
      if (kind !== 'scripts') continue

      // loadCollection 会把未知 scriptType 归一化成 major，所以非法值不是致命错，
      // 但它会静默改变层级，属于必须报出来的静默降级。
      const type = item.scriptType ?? 'major'
      if (!VALID_TYPES.has(type)) warn(`剧本「${item.title}」的 scriptType="${type}" 非法，加载时会被当作 major`)
      if (type === 'stage' && item.parentId) err(`阶段「${item.title}」不该有 parentId（阶段是最上层）`)
      if (type === 'minor' && !item.parentId) err(`小剧情「${item.title}」没有 parentId，无法归属到大剧情`)
      if (item.parentId) {
        const parent = byId.get(item.parentId)
        if (!parent) err(`剧本「${item.title}」的 parentId 指向不存在的条目 ${item.parentId}`)
        else {
          const parentType = parent.scriptType ?? 'major'
          if (type === 'major' && parentType !== 'stage') err(`大剧情「${item.title}」挂在了 ${parentType}「${parent.title}」下（只能挂阶段）`)
          if (type === 'minor' && parentType !== 'major') err(`小剧情「${item.title}」挂在了 ${parentType}「${parent.title}」下（只能挂大剧情）`)
        }
      }
    }

    // 盘上有文件但索引里没有 → 界面看不见，等于孤儿。
    const indexed = new Set(items.map(i => i.path))
    const walk = (dir) => {
      if (!fs.existsSync(dir)) return []
      return fs.readdirSync(dir, { withFileTypes: true }).flatMap(e => {
        const abs = path.join(dir, e.name)
        return e.isDirectory() ? walk(abs) : /\.md$/i.test(e.name) ? [abs] : []
      })
    }
    for (const sub of ['stage', 'major', 'minor']) {
      for (const abs of walk(path.join(dotDir, kind, sub))) {
        const rel = path.relative(root, abs).split(path.sep).join('/')
        if (!indexed.has(rel)) warn(`${kind} 目录里有未登记到 index.json 的文件：${rel}`)
      }
    }
    if (kind === 'scripts') {
      const counts = { stage: 0, major: 0, minor: 0 }
      for (const item of items) counts[VALID_TYPES.has(item.scriptType ?? 'major') ? (item.scriptType ?? 'major') : 'major']++
      note(`剧本层级分布：阶段 ${counts.stage} / 大剧情 ${counts.major} / 小剧情 ${counts.minor}`)
      if (counts.stage === 0 && counts.major > 0) warn(`没有任何阶段条目，${counts.major} 个大剧情全部处于「未分组」`)
    }
  }
}

// ══════════ 3. 顶层裸目录 ══════════
// other/ 也在标准布局里（prompt.ts 的 PROJECT_STRUCTURE 明确列了「其他杂项文件」），
// 只是内容不结构化。把它算进已知项，免得每次审计都报一条假阳性。
const KNOWN_TOP = new Set(['.storydex', 'chapters', 'other'])
for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
  if (KNOWN_TOP.has(entry.name)) continue
  note(`顶层还有 ${entry.isDirectory() ? '目录' : '文件'} ${entry.name}（不在标准布局内，检索时不会被当作结构化内容）`)
}

// ══════════ 报告 ══════════
console.log(`审计 ${root}`)
console.log(`  片段 ${fragments.length} 个，分布在 ${groups.length} 个分组`)
for (const [label, list, color] of [['错误', errors, 31], ['警告', warns, 33], ['提示', notes, 90]]) {
  if (!list.length) continue
  console.log(`\n[${color}m${label}（${list.length}）[0m`)
  for (const msg of list) console.log(`  · ${msg}`)
}
if (!errors.length && !warns.length) console.log('\n✓ 完全符合契约')
else console.log(`\n合计：${errors.length} 错误、${warns.length} 警告、${notes.length} 提示`)
process.exit(errors.length ? 1 : 0)
