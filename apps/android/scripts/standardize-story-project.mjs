#!/usr/bin/env node
/**
 * 把 pull-story-project.ps1 拉下来的工作副本整理成标准布局。
 *
 * 默认只打印计划（dry run），加 --apply 才真正改 work/ 下的文件；无论如何都不碰设备，
 * 也不碰同级的 original/（那是只读对照）。回写设备是另一步，由人看过 diff 再决定。
 *
 * 只做「结构」层面的整理——移动放错位置的文件、补齐解析器要求的 frontmatter、建立缺失的
 * 层级。不改写任何正文、不编造 summary 之外的内容、不重命名历史分组（见下）。
 *
 *   node standardize-story-project.mjs <work 目录> [--apply]
 */
import fs from 'node:fs'
import path from 'node:path'

const workDir = process.argv[2]
const apply = process.argv.includes('--apply')
if (!workDir || !fs.existsSync(path.join(workDir, '.storydex'))) {
  console.error('用法：node standardize-story-project.mjs <work 目录> [--apply]')
  console.error('（该目录下必须有 .storydex/，即 pull-story-project.ps1 产出的 work 副本）')
  process.exit(2)
}

const actions = []
/** 记录一步改动。fn 在 --apply 时执行；dry run 只打印 desc。 */
function step(desc, fn) { actions.push({ desc, fn }) }

const rel = p => path.relative(workDir, p).replace(/\\/g, '/')
const readJson = p => JSON.parse(fs.readFileSync(p, 'utf8'))
// 不写 BOM：Rust 侧和 JSON.parse 都不接受 BOM 开头。
const writeJson = (p, value) => fs.writeFileSync(p, JSON.stringify(value, null, 2) + '\n', 'utf8')

const move = (from, to) => step(`移动  ${rel(from)}\n        → ${rel(to)}`, () => {
  fs.mkdirSync(path.dirname(to), { recursive: true })
  fs.renameSync(from, to)
})

// ══════════ 1. chapters/20260809213100 里三个放错位置的文件 ══════════
//
// 这个分组是项目第一天留下的，序号 000/001/900 本身就说明它不是正常的片段分组：
// captureTurn 只会产出 -001 起连续编号。逐个看过内容后的归位依据写在各条里。

const badGroup = path.join(workDir, 'chapters', '20260809213100')
const other = path.join(workDir, 'other')

const openingCard = path.join(badGroup, '20260809213100-000.md')
if (fs.existsSync(openingCard)) {
  // 角色卡，不是剧情片段。但卡上的数值（炼气二层 / 灵石 3 / 修炼进度 35% / 友人：无）是
  // 开局那天的快照，与 memory 里已确立的事实（炼气五层 / 灵石 142 / 已卷入泰昌行与州衙）
  // 直接矛盾。characters/ 会被注入每一轮提示词，放进去等于让模型读到一份过期数值，
  // 所以归档到 other/ 保留，不进 characters/。
  // 当前主角卡确实是缺的——但那需要依据既有事实重写内容，属于创作决定，留给用户或 app 内的 Agent。
  move(openingCard, path.join(other, '开局角色卡_沈砚_20260809.md'))
}

const demoFragment = path.join(badGroup, '20260809213100-900.md')
if (fs.existsSync(demoFragment)) {
  // 文件自述「本文件为演示『如何记录一次冒险』的样例」，且设定与正传不符
  // （清河村猎户之子 / 十九岁，正传是青石镇药农之子 / 十七岁）。属示例文档。
  move(demoFragment, path.join(other, '示例_如何记录一次冒险.md'))
}

// ══════════ 2. 真正的开局正文补 frontmatter ══════════
//
// parseFragmentFile 没有 frontmatter 就返回 null，整个片段对界面不可见——这个项目的
// 第一幕现在是看不到的。补上 summary 与 createdAt 即可恢复，正文一字不改。

const opening = path.join(badGroup, '20260809213100-001.md')
if (fs.existsSync(opening)) {
  const body = fs.readFileSync(opening, 'utf8')
  if (/^---\r?\n/.test(body)) {
    console.log(`  跳过：${rel(opening)} 已有 frontmatter`)
  } else {
    // summary 取正文首段前 50 字：与 captureTurn 的做法一致，不新增任何内容。
    // 跳过 markdown 标题和引言块，取第一段叙述。
    const prose = body.split(/\r?\n/)
      .filter(line => line.trim() && !line.startsWith('#') && !line.startsWith('>') && line.trim() !== '---')
    const summary = (prose[0] ?? '').trim().slice(0, 50)
    // createdAt 由分组名推出：20260809213100 = 2026-08-09 21:31:00 本机时区（UTC+8）。
    // 这是唯一有据可依的时间来源，不去猜别的。
    const createdAt = new Date('2026-08-09T21:31:00+08:00').toISOString()
    const frontmatter = `---\nsummary: ${JSON.stringify(summary)}\ncreatedAt: ${createdAt}\n---\n\n`
    step(`补 frontmatter  ${rel(opening)}\n        summary=${JSON.stringify(summary)}\n        createdAt=${createdAt}`,
      () => fs.writeFileSync(opening, frontmatter + body, 'utf8'))
  }
}

// ══════════ 3. 顶层的世界观总览归入 worldbook/ ══════════
//
// 它是全书最高层的世界观文档（玩法内核、基调、参考气质），和 worldbook/001_世界总纲.md
// 不重复。放在顶层不会被当作结构化世界观检索到，编号 000 让它排在 README 之前。

const overview = path.join(workDir, '世界观总览.md')
if (fs.existsSync(overview)) {
  move(overview, path.join(workDir, '.storydex', 'worldbook', '000_世界观总览.md'))
}

// ══════════ 4. 建立阶段层，把 8 个大剧情归入其下 ══════════
//
// 项目现在是「8 个大剧情全部未分组」。这里逐字复刻 app 内 groupUngroupedMajorsIntoStage
// + addItem 的产物（project.ts:877 / :760），好处是回写后 app 加载它与自己新建的毫无区别：
//   · md 平铺在 .storydex/scripts/ 下，不进 stage/ 子目录（只有重构产物才分子目录）；
//   · filename = <安全标题>-<uuid 末 8 位>.md；
//   · formatVersion 是 1（不是 2）——2 会被导演流转当作可推进条目；
//   · clock / clockMax / deadlineTurns / consequence / lastTickTurn 照写，addItem 就是这么写的；
//   · 不写 parentId：resolveScriptParent 对阶段传非空父级会直接抛错；
//   · 条目追加到数组末尾（addItem 用的是 push），不插到最前面。
// 阶段的 md 没有 frontmatter，completionCondition / defaultRoute 只存在 index.json 一处。

const scriptsIndexPath = path.join(workDir, '.storydex', 'scripts', 'index.json')
const scriptsIndex = readJson(scriptsIndexPath)
const items = scriptsIndex.items ?? []
const majors = items.filter(item => (item.scriptType ?? 'major') === 'major' && !item.refactoredTo)
const orphans = majors.filter(item => !item.parentId)
const hasStage = items.some(item => item.scriptType === 'stage')

if (hasStage) {
  console.log('  跳过：已存在阶段条目，不重复创建')
} else if (orphans.length) {
  const stageTitle = '第一阶段·东岸求生与泰昌行暗线'
  const uuid = 'a1c4f0d2-3e6b-4a58-9f21-7d0b5e83c6f4'
  const stageId = `script-${uuid}`
  const filename = `${stageTitle}-${uuid.slice(-8)}.md`
  const stagePath = `.storydex/scripts/${filename}`
  const now = new Date().toISOString()

  const completionCondition = '主角在东岸站稳落脚点，且泰昌行与州衙两条线的立场都已明确（成为可依靠的一方或彻底敌对），不再是两边都在观望的悬置状态。'
  const defaultRoute = '本阶段只界定方向与边界：主角处在洪水断路、泰昌行与州衙双向注视之下的求生期，一切大剧情都应服务于「先活下来并选定站位」，不引入超出散修—坊市—州衙这一层级的势力。'

  const stageItem = {
    id: stageId,
    title: stageTitle,
    filename,
    enabled: true,
    status: 'active',
    completionCondition,
    defaultRoute,
    clock: 0,
    clockMax: 4,
    deadlineTurns: 4,
    consequence: `${stageTitle}未及时处理，将主动产生后果`,
    lastTickTurn: 0,
    formatVersion: 1,
    scriptType: 'stage',
    path: stagePath,
    updatedAt: now,
  }

  // 正文沿用 groupUngroupedMajorsIntoStage 的三段式，但把占位提示换成实际内容。
  const stageBody = [
    `# ${stageTitle}`,
    '',
    '## 阶段目标',
    defaultRoute,
    '',
    '## 阶段完成标志',
    completionCondition,
    '',
    '## 本阶段边界',
    '- 势力层级止于散修、坊市商行（泰昌行、永和栈）、州衙差役与猎兽队，不引入宗门与筑基以上的正面冲突。',
    '- 主角境界在本阶段内保持凡人流的缓慢积累，不安排跨越式突破。',
    '- 洪水与断路是本阶段的物理约束，路线选择应始终受水情牵制。',
    '',
    '## 包含的大剧情',
    ...orphans.map(item => `- ${item.title}`),
  ].join('\n')

  step(`新建阶段  ${stagePath}\n        标题：${stageTitle}\n        formatVersion=1、scriptType=stage、追加到 index.items 末尾`, () => {
    fs.writeFileSync(path.join(workDir, stagePath), stageBody + '\n', 'utf8')
  })

  step(`归属      ${orphans.length} 个未分组大剧情的 parentId → ${stageId}\n        ${orphans.map(m => m.title).join('\n        ')}`, () => {
    const fresh = readJson(scriptsIndexPath)
    const orphanIds = new Set(orphans.map(item => item.id))
    fresh.items = [
      ...(fresh.items ?? []).map(item =>
        orphanIds.has(item.id) ? { ...item, parentId: stageId, updatedAt: now } : item),
      stageItem,
    ]
    writeJson(scriptsIndexPath, fresh)
  })
}

// ══════════ 5. 绑定 director.activeArc.majorScriptId ══════════
//
// 现在这个字段是缺的，前端和 Rust 各自回落到「首个 enabled + status==='active' + 大剧情」。
// 回落本身是一致的，但项目里的大剧情是 formatVersion 2，而 syncScriptLifecycle 会把
// 「不等于 activeMajorId」的 v2 大剧情统统刷成 pending——字段缺失时它一条都匹配不上，
// 于是全部变 pending，之后「status==='active'」的回落就再也找不到大剧情，主剧本和阶段
// 会一起从上下文里消失。所以补上比留空安全。
//
// 补的值就取那条回落目标本身（数组里首个 enabled+active 的大剧情），与两侧现有行为完全一致，
// 也正是 directorMechanics 在下一个通过审计的回合会自愈成的值——绝不能凭 activeArc.title
// 猜一个别的 id：写错会让一致性闸门每轮拒绝，导演状态从此不再推进。

const directorPath = path.join(workDir, '.storydex', 'director', 'state.json')
const director = readJson(directorPath)
const fallbackMajor = items.find(item =>
  (item.scriptType ?? 'major') === 'major' && item.enabled !== false
  && (item.status ?? 'active') === 'active' && !item.refactoredTo)

if (!director.activeArc) {
  console.log('  跳过：没有进行中的主线，无需绑定 majorScriptId')
} else if (director.activeArc.majorScriptId) {
  console.log('  跳过：activeArc.majorScriptId 已有值')
} else if (!fallbackMajor) {
  console.log('  跳过：找不到可绑定的大剧情（没有 enabled+active 的条目）')
} else {
  step(`绑定      director.activeArc.majorScriptId → ${fallbackMajor.id}\n        「${fallbackMajor.title}」（即两侧当前已在使用的回落目标）\n        activeArc.title「${director.activeArc.title}」保持不动——它不参与任何校验`, () => {
    const fresh = readJson(directorPath)
    fresh.activeArc.majorScriptId = fallbackMajor.id
    // revision 递增：这份状态是给 app 读的，改过内容就该体现在版本号上。
    if (typeof fresh.revision === 'number') fresh.revision += 1
    fresh.updatedAt = new Date().toISOString()
    writeJson(directorPath, fresh)
  })
}

// ══════════ 报告 ══════════

console.log(`\n标准化计划（${apply ? '执行' : '仅预览，加 --apply 才写入'}）：${workDir}\n`)
actions.forEach((action, index) => console.log(`  ${index + 1}. ${action.desc}\n`))

if (!actions.length) {
  console.log('  没有需要改动的地方。\n')
} else if (apply) {
  for (const action of actions) action.fn()
  console.log(`✓ 已应用 ${actions.length} 步改动到 work 副本。original/ 未被触碰。`)
  console.log('  下一步：重新审计确认无错误，再决定是否回写设备。\n')
} else {
  console.log('以上均未执行。确认后加 --apply 重跑。\n')
}

// ══════════ 明确不做的事 ══════════
console.log('以下问题经核对后决定不动，理由：')
console.log('  · 32 个 14 位旧格式分组不重命名——.storydex/ 下有 2246 处引用这些路径')
console.log('    （会话记录、事件日志、用量账本），重命名要连带重写全部引用；且 captureTurn')
console.log('    现在按「当前分组已装满」滚组，新分组本就是 12 位，会自行回正。')
console.log('  · 历史片段缺 suggestions 不补——行动建议必须与当轮情境相关，编造等于往用户的')
console.log('    故事里塞假内容。只影响这些旧片段没有快捷选项按钮。')
console.log('  · 非 5 片段分组、createdAt 与文件名排序分歧：同属历史遗留，滚组机制会自行回正。')
console.log('  · .storydex/random/ 不存在是正常的——该目录只在项目自定义了随机词库后才出现。')
console.log('  · 主角当前角色卡缺失（characters/ 只有模板和 NPC）：补它需要依据已确立事实重写')
console.log('    人物数值，属创作决定，留给用户或 app 内 Agent，脚本不代劳。\n')
