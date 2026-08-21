<script setup lang="ts">
/**
 * SKILL / MCP 管理（控制台入口）。
 * 数据来自引擎 /api/catalog；安装走 /api/catalog/{mcp,skills}/install。
 * 交互：点击「安装」→ 弹出确认（名称/描述/来源/生效方式）→ MCP 再填参数，Skill 直接安装。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import PageHead from '@/components/PageHead.vue'
import CoomiIcon from '@/components/CoomiIcon.vue'
import BottomSheet from '@/components/ui/BottomSheet.vue'
import Segments from '@/components/ui/Segments.vue'
import { authedFetch } from '@/bridge/http'

const router = useRouter()
const CORE_ABILITIES = [
  ['项目检索', '按推理强度检索章节、角色、世界观与来源'],
  ['连续性与溯源', '核对事实、变量来源、矛盾与过期记忆'],
  ['剧本与时间', '维护故事内时间、剧本路线、完成条件与闪回'],
  ['角色塑造', '人物动机、关系、成长弧与非模板化命名'],
  ['世界构建', '制度、地理、文化、规则与因果约束'],
  ['情节设计', '冲突、伏笔、节奏、转折与回收'],
  ['对白写作', '区分声线、潜台词、身份与情绪变化'],
  ['悬疑推理', '线索公平性、误导、证据链与揭晓节奏'],
  ['动作场景', '空间关系、能力限制、节奏与可追踪后果'],
  ['文字润色', '按激活预设改善表达并保持事实不变'],
  ['互动叙事', '保留玩家选择权，拒绝越权控制并提供替代行动'],
] as const

/** 解析引擎响应：兼容空 body（旧引擎进程对未知路由返回 404 空体），错误带可读信息。 */
async function parseRes(res: Response): Promise<any> {
  const text = await res.text()
  let data: any = {}
  try { data = text ? JSON.parse(text) : {} } catch { data = {} }
  if (!res.ok) {
    const detail = data.error ?? data.message ?? ''
    throw new Error(detail || `HTTP ${res.status}${text ? '' : '（引擎响应为空，可能版本过旧，请重启应用）'}`)
  }
  return data
}

type Tab = 'mcp' | 'skills'
const tab = ref<Tab>('mcp')
/** 页签内的视图：已安装（本机实际配置，含自建/导入）｜仓库（内置目录）。 */
type Scope = 'installed' | 'catalog'
const scope = ref<Scope>('catalog')
const SCOPES: { key: Scope; label: string; icon: string }[] = [
  { key: 'installed', label: '已安装', icon: 'check' },
  { key: 'catalog', label: '仓库', icon: 'globe' },
]

interface RequiredParam { key: string; label: string; secret?: boolean }
interface McpItem {
  id: string; name: string; description: string; transport: string
  required_parameters: RequiredParam[]; installed: boolean; enabled: boolean
  path?: string
}
interface SkillItem { id: string; name: string; description: string; repository: string; installed: boolean; enabled: boolean; path?: string }

const mcp = ref<McpItem[]>([])
const skills = ref<SkillItem[]>([])
const installedMcp = ref<McpItem[]>([])
const installedSkills = ref<SkillItem[]>([])
const loading = ref(true)
const error = ref('')
const busy = ref<string | null>(null)
const notice = ref('')
/** 当前展开详情的卡片 id（卡片默认折叠，只显示名称）。 */
const expanded = ref<string | null>(null)
function toggleExpanded(id: string) {
  expanded.value = expanded.value === id ? null : id
}

// ── 安装确认（所有安装必须先确认，不能点击即装）──
const askMcp = ref<McpItem | null>(null)
const askSkill = ref<SkillItem | null>(null)

// ── MCP 安装参数表单（按目录的 required_parameters 动态生成）──
const installingMcp = ref<McpItem | null>(null)
const installValues = ref<Record<string, string>>({})

/** 必填参数是否都已填写（未填完不允许安装，避免 500）。 */
const installReady = computed(() => {
  const item = installingMcp.value
  if (!item) return false
  return item.required_parameters.every(
    p => (installValues.value[p.key] ?? '').trim().length > 0,
  )
})

function confirmMcpInstall(item: McpItem) {
  askMcp.value = item
}

function proceedMcp() {
  const item = askMcp.value
  askMcp.value = null
  if (!item) return
  installingMcp.value = item
  installValues.value = {}
  notice.value = ''
}

function confirmSkillInstall(item: SkillItem) {
  askSkill.value = item
}

// ── 停用 / 启用（管理页卸载 = 停用可恢复；删除 = 彻底删除）──
const askDelete = ref<{ kind: 'mcp' | 'skill'; item: McpItem | SkillItem } | null>(null)

async function setEnabled(kind: 'mcp' | 'skill', item: McpItem | SkillItem, enabled: boolean) {
  busy.value = item.id
  notice.value = ''
  try {
    // 注意：引擎路由为 /api/catalog/{mcp,skills}/...（skills 是复数）
    const resource = kind === 'mcp' ? 'mcp' : 'skills'
    const res = await authedFetch(`/api/catalog/${resource}/${item.id}/enabled`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    })
    await parseRes(res)
    // 本地乐观更新：立即反映按钮/徽标状态（不依赖后续 load 是否成功）。
    item.enabled = enabled
    notice.value = enabled
      ? `已启用${kind === 'mcp' ? '通用工具' : '创作能力'}「${item.name}」，新开会话后生效`
      : `已停用${kind === 'mcp' ? '通用工具' : '创作能力'}「${item.name}」，文件与配置已保留，可随时重新启用`
    await load()
  } catch (e) {
    notice.value = `${enabled ? '启用' : '停用'}失败：${e instanceof Error ? e.message : String(e)}`
  } finally {
    busy.value = null
  }
}

function confirmDelete(kind: 'mcp' | 'skill', item: McpItem | SkillItem) {
  askDelete.value = { kind, item }
}

async function deleteItem() {
  const target = askDelete.value
  if (!target) return
  busy.value = target.item.id
  notice.value = ''
  try {
    // 注意：引擎路由为 /api/catalog/{mcp,skills}/...（skills 是复数）
    const resource = target.kind === 'mcp' ? 'mcp' : 'skills'
    const res = await authedFetch(`/api/catalog/${resource}/${target.item.id}`, { method: 'DELETE' })
    await parseRes(res)
    notice.value = `已彻底删除${target.kind === 'mcp' ? '通用工具' : '创作能力'}「${target.item.name}」`
    askDelete.value = null
    await load()
  } catch (e) {
    notice.value = `删除失败：${e instanceof Error ? e.message : String(e)}`
  } finally {
    busy.value = null
  }
}

function proceedSkill() {
  const item = askSkill.value
  askSkill.value = null
  if (!item) return
  installSkill(item)
}

function closeInstallForm() {
  installingMcp.value = null
  installValues.value = {}
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await authedFetch('/api/catalog')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await parseRes(res)
    mcp.value = data.mcp ?? []
    skills.value = data.skills ?? []
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

/** 加载本机已安装列表（含目录之外自建/导入的）：/api/runtime/installed。 */
async function loadInstalled() {
  loading.value = true
  error.value = ''
  try {
    const res = await authedFetch('/api/runtime/installed')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await parseRes(res)
    installedMcp.value = (data.mcp ?? []).map((m: any) => ({
      id: m.id, name: m.name, description: '', transport: m.transport ?? '',
      required_parameters: [], installed: true, enabled: m.enabled, path: m.path,
    }))
    installedSkills.value = (data.skills ?? []).map((s: any) => ({
      id: s.id, name: s.name, description: '', repository: '',
      installed: true, enabled: s.enabled, path: s.path,
    }))
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

/** 切换视图（已安装/仓库）：已安装首次进入时拉取一次。 */
function switchScope(next: Scope) {
  if (scope.value === next) return
  scope.value = next
  error.value = ''
  if (next === 'installed') void loadInstalled()
}

/** 当前视图下要渲染的列表（已安装｜仓库）。 */
const STORY_TOOL_COPY: Record<string, { name: string; description: string }> = {
  filesystem: { name: '外部资料目录', description: '授权一个参考资料目录，用于检索设定集、文献和素材。' },
  fetch: { name: '网页资料读取', description: '联网读取历史、地理、专业知识和公开网页，作为故事研究来源。' },
  memory: { name: '资料关系图谱', description: '辅助维护复杂人物、地点、组织、事件与关系。' },
}
const visibleMcp = computed(() => scope.value === 'installed'
  ? installedMcp.value
  : mcp.value.filter(item => STORY_TOOL_COPY[item.id]).map(item => ({ ...item, ...STORY_TOOL_COPY[item.id] })))
const visibleSkills = computed(() => (scope.value === 'installed' ? installedSkills.value : skills.value))

async function installMcp() {
  const item = installingMcp.value
  if (!item) return
  busy.value = item.id
  notice.value = ''
  try {
    const res = await authedFetch('/api/catalog/mcp/install', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: item.id, values: installValues.value }),
    })
    await parseRes(res)
    notice.value = `已安装通用工具「${item.name}」，重启引擎或新开会话后生效`
    closeInstallForm()
    await load()
  } catch (e) {
    notice.value = `安装失败：${e instanceof Error ? e.message : String(e)}`
  } finally {
    busy.value = null
  }
}

async function installSkill(item: SkillItem) {
  busy.value = item.id
  notice.value = ''
  try {
    const res = await authedFetch('/api/catalog/skills/install', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: item.id }),
    })
    await parseRes(res)
    notice.value = `已安装创作能力「${item.name}」，重启引擎或新开会话后生效`
    await load()
  } catch (e) {
    notice.value = `安装失败：${e instanceof Error ? e.message : String(e)}`
  } finally {
    busy.value = null
  }
}

onMounted(load)
// 从控制台进入：返回统一回控制台（浏览器环境回聊天主页）
function goDashboard() {
  if (window.CoomiAndroid?.openDashboard) window.CoomiAndroid.openDashboard()
  else router.push('/')
}
</script>

<template>
  <div class="page">
    <PageHead title="拓展管理" @back="goDashboard" />
    <main class="body">
      <!-- 一级：已安装 | 仓库 -->
      <Segments class="seg" :items="SCOPES" :value="scope" size="sm" @pick="switchScope" />

      <!-- 能力分类 -->
      <div class="tabs">
        <button class="tab" :class="{ on: tab === 'skills' }" @click="tab = 'skills'">
          <CoomiIcon name="sparkle" :size="15" />故事创作
          <span class="cnt">{{ CORE_ABILITIES.length }}</span>
        </button>
        <button class="tab" :class="{ on: tab === 'mcp' }" @click="tab = 'mcp'">
          <CoomiIcon name="globe" :size="15" />通用工具
          <span class="cnt">{{ scope === 'installed' ? installedMcp.length : mcp.length }}</span>
        </button>
      </div>

      <p v-if="notice" class="notice" :class="{ err: notice.startsWith('安装失败') }">{{ notice }}</p>
      <p v-if="error" class="notice err">加载失败：{{ error }}</p>
      <p v-if="loading" class="hint">加载中…</p>

      <!-- MCP -->
      <template v-if="tab === 'mcp'">
        <p v-if="!loading && visibleMcp.length === 0" class="hint">
          {{ scope === 'installed' ? '本机还没有已安装的通用工具。' : '目录为空，暂时没有可安装的通用工具。' }}
        </p>
        <div v-else class="cards">
          <div v-for="item in visibleMcp" :key="item.id" class="card">
            <button class="card-head" @click.stop="toggleExpanded(item.id)">
              <span class="tile" :class="{ on: item.installed }">
                <CoomiIcon name="plug" :size="18" />
              </span>
              <span class="cname">{{ item.name }}</span>
              <span v-if="item.installed" class="badge" :class="item.enabled ? 'ok' : 'off'">
                {{ item.enabled ? '已启用' : '已停用' }}
              </span>
              <span v-else class="badge plain">未安装</span>
              <CoomiIcon name="chevronRight" :size="14" class="chev" :class="{ open: expanded === item.id }" />
            </button>
            <div v-if="expanded === item.id" class="detail">
              <p class="cdesc">{{ item.description || '本机配置的通用工具' }}</p>
              <span v-if="item.transport" class="cmeta"><CoomiIcon name="link" :size="12" />{{ item.transport }}</span>
              <span v-if="item.path" class="cmeta path"><CoomiIcon name="folder" :size="12" />{{ item.path }}</span>
              <div class="dops">
                <template v-if="item.installed">
                  <button class="act" :disabled="busy !== null" @click.stop="setEnabled('mcp', item, !item.enabled)">
                    {{ item.enabled ? '停用' : '启用' }}
                  </button>
                  <button class="act danger" :disabled="busy !== null" @click.stop="confirmDelete('mcp', item)">删除</button>
                </template>
                <button v-else class="act" :disabled="busy !== null" @click.stop="confirmMcpInstall(item)">
                  {{ busy === item.id ? '安装中…' : '安装' }}
                </button>
              </div>
            </div>
          </div>
        </div>
      </template>

      <!-- Skills -->
      <template v-else>
        <p class="core-hint">核心创作能力已随 Storydex 内置，使用频率和检索深度随推理强度调整，无需联网安装。</p>
        <div class="cards core-grid">
          <div v-for="ability in CORE_ABILITIES" :key="ability[0]" class="card core-card"><span class="tile on"><CoomiIcon name="sparkle" :size="18" /></span><span class="core-copy"><b>{{ ability[0] }}</b><small>{{ ability[1] }}</small></span><span class="badge ok">内置</span></div>
        </div>
        <div v-if="false" class="cards">
          <div v-for="item in visibleSkills" :key="item.id" class="card">
            <button class="card-head" @click.stop="toggleExpanded(item.id)">
              <span class="tile" :class="{ on: item.installed }">
                <CoomiIcon name="wrench" :size="18" />
              </span>
              <span class="cname">{{ item.name }}</span>
              <span v-if="item.installed" class="badge" :class="item.enabled ? 'ok' : 'off'">
                {{ item.enabled ? '已启用' : '已停用' }}
              </span>
              <span v-else class="badge plain">未安装</span>
              <CoomiIcon name="chevronRight" :size="14" class="chev" :class="{ open: expanded === item.id }" />
            </button>
            <div v-if="expanded === item.id" class="detail">
              <p class="cdesc">{{ item.description || '本机安装的创作能力' }}</p>
              <span v-if="item.repository" class="cmeta"><CoomiIcon name="globe" :size="12" />{{ item.repository }}</span>
              <span v-if="item.path" class="cmeta path"><CoomiIcon name="folder" :size="12" />{{ item.path }}</span>
              <div class="dops">
                <template v-if="item.installed">
                  <button class="act" :disabled="busy !== null" @click.stop="setEnabled('skill', item, !item.enabled)">
                    {{ item.enabled ? '停用' : '启用' }}
                  </button>
                  <button class="act danger" :disabled="busy !== null" @click.stop="confirmDelete('skill', item)">删除</button>
                </template>
                <button v-else class="act" :disabled="busy !== null" @click.stop="confirmSkillInstall(item)">
                  {{ busy === item.id ? '安装中…' : '安装' }}
                </button>
              </div>
            </div>
          </div>
        </div>
      </template>

      <!-- 彻底删除确认（管理页卸载 = 停用可恢复，删除 = 彻底删除）。
           删除进行中不许点遮罩关闭：关掉不会撤销已经发出的 DELETE。 -->
      <BottomSheet
        v-if="askDelete"
        variant="card"
        role="alertdialog"
        :dismissible="busy === null"
        @close="askDelete = null"
      >
        <div class="stitle">
          <CoomiIcon :name="askDelete.kind === 'mcp' ? 'plug' : 'wrench'" :size="17" />
          彻底删除{{ askDelete.kind === 'mcp' ? '通用工具' : '创作能力' }}「{{ askDelete.item.name }}」？
        </div>
        <p class="sdesc">
          {{ askDelete.kind === 'mcp'
            ? '将从 config/mcp_servers.json 中移除该服务（不可恢复）。若只是想暂时不用，请改用「停用」。'
            : '将删除已安装的 Skill 目录与配置记录（不可恢复）。若只是想暂时不用，请改用「停用」。' }}
        </p>
        <template #actions>
          <button class="btn ghost" @click="askDelete = null">取消</button>
          <button class="btn danger-solid" :disabled="busy !== null" @click="deleteItem">确认彻底删除</button>
        </template>
      </BottomSheet>

      <!-- MCP 安装确认 -->
      <BottomSheet v-if="askMcp" variant="card" @close="askMcp = null">
        <div class="stitle"><CoomiIcon name="plug" :size="17" />安装通用工具「{{ askMcp.name }}」？</div>
        <p class="sdesc">{{ askMcp.description }}</p>
        <div class="sinfo">
          <span><CoomiIcon name="link" :size="13" />{{ askMcp.transport }}</span>
          <span><CoomiIcon name="folder" :size="13" />写入本地工具配置</span>
          <span><CoomiIcon name="refresh" :size="13" />重启引擎或新开会话后生效</span>
        </div>
        <template #actions>
          <button class="btn ghost" @click="askMcp = null">取消</button>
          <button class="btn primary" @click="proceedMcp">继续配置</button>
        </template>
      </BottomSheet>

      <!-- Skill 安装确认 -->
      <BottomSheet v-if="askSkill" variant="card" :dismissible="busy === null" @close="askSkill = null">
        <div class="stitle"><CoomiIcon name="wrench" :size="17" />安装创作能力「{{ askSkill.name }}」？</div>
        <p class="sdesc">{{ askSkill.description }}</p>
        <div class="sinfo">
          <span v-if="askSkill.repository"><CoomiIcon name="globe" :size="13" />{{ askSkill.repository }}</span>
          <span><CoomiIcon name="folder" :size="13" />安装到创作能力目录</span>
          <span><CoomiIcon name="refresh" :size="13" />重启引擎或新开会话后生效</span>
        </div>
        <template #actions>
          <button class="btn ghost" @click="askSkill = null">取消</button>
          <button class="btn primary" :disabled="busy !== null" @click="proceedSkill">
            {{ busy === askSkill.id ? '安装中…' : '确认安装' }}
          </button>
        </template>
      </BottomSheet>

      <!-- MCP 安装参数表单 -->
      <BottomSheet v-if="installingMcp" variant="card" :dismissible="busy === null" @close="closeInstallForm">
        <div class="stitle">配置 {{ installingMcp.name }}</div>
        <p class="sdesc">{{ installingMcp.description }}</p>
        <label v-for="p in installingMcp.required_parameters" :key="p.key" class="field">
          <span>{{ p.label }}<em v-if="!p.secret" class="req">必填</em></span>
          <input
            v-model="installValues[p.key]"
            :type="p.secret ? 'password' : 'text'"
            :placeholder="p.key"
            autocomplete="off"
          />
        </label>
        <p v-if="installingMcp.required_parameters.length === 0" class="sdesc">该工具无需额外配置，直接安装即可。</p>
        <template #actions>
          <button class="btn ghost" @click="closeInstallForm">取消</button>
          <button class="btn primary" :disabled="busy !== null || !installReady" @click="installMcp">
            {{ busy === installingMcp.id ? '安装中…' : installReady ? '安装' : '请填写必填项' }}
          </button>
        </template>
      </BottomSheet>
    </main>
  </div>
</template>

<style scoped>
.page { display: flex; flex-direction: column; height: 100%; background: var(--page); }
.body {
  flex: 1; min-height: 0; overflow-y: auto;
  padding: 14px 12px calc(var(--safe-bottom) + 24px);
  -webkit-overflow-scrolling: touch; overscroll-behavior-y: contain;
}
.tabs { display: flex; gap: 8px; margin-bottom: 14px; }
/* 外观来自 components/ui/Segments，这里只给它和下面页签之间的间距。 */
.seg { margin-bottom: 12px; }
.tab {
  flex: 1; display: flex; align-items: center; justify-content: center; gap: 6px;
  min-height: 42px; border-radius: var(--r-md);
  background: var(--fill-strong); color: var(--text-2);
  font-size: 14px; font-weight: 550;
}
.tab.on { background: var(--blue-soft); color: var(--blue); }
.cnt {
  min-width: 18px; height: 18px; padding: 0 5px; border-radius: var(--r-pill);
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--fill); font-size: 11px; font-weight: 650;
}
.tab.on .cnt { background: var(--blue); color: var(--on-accent); }

.notice {
  margin: 0 0 12px; padding: 10px 12px; border-radius: var(--r-md);
  background: var(--fill); font-size: 13px; line-height: 1.6; color: var(--text);
}
.notice.err { background: var(--danger-soft); color: var(--danger); }
.hint { margin: 18px 0; text-align: center; font-size: 13px; color: var(--text-3); }

.cards { display: flex; flex-direction: column; gap: 8px; }
.core-hint { margin:0 0 10px; color:var(--text-3); font-size:12px; line-height:1.6; }
.core-card { flex-wrap:nowrap; }.core-copy { display:flex; min-width:0; flex:1; flex-direction:column; gap:3px; }.core-copy b { font-size:13.5px; }.core-copy small { color:var(--text-3); font-size:11.5px; line-height:1.45; }
/* 底色 / 圆角 / 投影来自 global.css 的 .card。 */
.card {
  display: flex; flex-wrap: wrap; align-items: center; gap: 10px;
  padding: 10px 12px;
}
/* 卡片头部：整行可点击，折叠时只显示名称。 */
.card-head {
  display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0;
  padding: 0; border: 0; background: none; text-align: left;
}
.tile {
  flex-shrink: 0; width: 38px; height: 38px; border-radius: 11px;
  display: flex; align-items: center; justify-content: center;
  background: var(--fill-strong); color: var(--text-2);
}
.tile.on { background: var(--blue-soft); color: var(--blue); }
.cname { flex: 1; min-width: 0; font-size: 14.5px; font-weight: 600; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.badge {
  flex-shrink: 0; padding: 1.5px 8px; border-radius: var(--r-pill);
  font-size: 10.5px; font-weight: 650;
}
.badge.ok { background: var(--ok-soft); color: var(--ok); }
.badge.off { background: var(--fill); color: var(--text-2); }
.badge.plain { background: var(--fill); color: var(--text-3); }
.chev { flex-shrink: 0; color: var(--text-3); transition: transform .18s; }
.chev.open { transform: rotate(90deg); }
/* 展开详情：flex-basis 100% 全宽换行，与头部隔开。 */
.detail {
  flex-basis: 100%; min-width: 0;
  display: flex; flex-direction: column; gap: 2px;
  margin-top: 4px; padding-top: 9px; border-top: 1px dashed var(--border);
}
.cdesc {
  margin: 0; font-size: 12.5px; line-height: 1.55; color: var(--text-2);
  word-break: break-word;
}
.cmeta {
  display: inline-flex; align-items: center; gap: 4px; margin-top: 4px;
  font-size: 11px; color: var(--text-3);
}
.cmeta.path { display: block; word-break: break-all; }
.dops { display: flex; gap: 8px; margin-top: 9px; }
.act {
  flex-shrink: 0; min-width: 62px; height: 34px; padding: 0 14px; border-radius: var(--r-pill);
  background: var(--blue); color: var(--on-accent); font-size: 13px; font-weight: 600;
}
.act:active { opacity: 0.85; }
.act:disabled { opacity: 0.5; }
.act.danger { background: var(--danger-soft); color: var(--danger); }
.done { flex-shrink: 0; width: 28px; height: 28px; display: flex; align-items: center; justify-content: center; border-radius: 50%; background: var(--ok-soft); color: var(--ok); }

/* ── 弹层内容（外壳与遮罩由 components/ui/BottomSheet 提供）── */
.stitle {
  display: flex; align-items: center; gap: 8px;
  font-size: 15.5px; font-weight: 650; color: var(--text);
}
.sdesc { margin: 8px 0 0; font-size: 12.5px; line-height: 1.65; color: var(--text-2); }
.sinfo {
  display: flex; flex-direction: column; gap: 6px; margin-top: 12px;
  padding: 10px 12px; border-radius: var(--r-md); background: var(--fill);
  font-size: 12px; color: var(--text-2);
}
.sinfo span { display: flex; align-items: center; gap: 6px; }
.field { display: flex; flex-direction: column; gap: 5px; margin-top: 12px; font-size: 12.5px; color: var(--text-2); }
.req { margin-left: 5px; padding: 0 5px; border-radius: 4px; background: var(--blue-soft); color: var(--blue); font-style: normal; font-size: 10px; font-weight: 650; }
.field input {
  height: 42px; padding: 0 12px; border-radius: var(--r-md); border: 1px solid var(--border);
  background: var(--bg-input); color: var(--text); font-size: 15px;
}
/* 按钮等分由 BottomSheet 的 .actions 统一给出，这里只描述外观。 */
.btn { min-height: 42px; border-radius: var(--r-md); font-size: 14.5px; font-weight: 600; }
.btn.primary { background: var(--blue); color: var(--on-accent); }
.btn.ghost { background: var(--fill-strong); color: var(--text); }
.btn.danger-solid { background: var(--danger); color: var(--on-accent); }
.btn:disabled { opacity: 0.6; }
</style>
