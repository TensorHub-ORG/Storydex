<script setup lang="ts">
/**
 * 当前故事项目的模式独立会话抽屉。
 * sessions store 会按 story / narrator / agent 隔离列表与搜索结果。
 */
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { useStoryStore, type StoryFragment } from '@/stores/story'
import { formatSessionTime, useSessionsStore, type SessionMeta } from '@/stores/sessions'
import CoomiIcon from './CoomiIcon.vue'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()
const router = useRouter()
const session = useSessionStore()
const sessions = useSessionsStore()
const story = useStoryStore()

const modeLabel = computed(() => ({ story: '剧情', narrator: '旁白', agent: 'Agent' })[story.agentMode])

// 剧情模式搜索项目内已归档片段；另外两种模式搜索各自独立会话。
const fragmentQuery = ref('')
const filteredFragments = computed(() => {
  const query = fragmentQuery.value.trim().toLocaleLowerCase()
  const newest = [...story.fragments].reverse()
  if (!query) return newest
  return newest.filter(fragment => [fragment.filename, fragment.summary, fragment.content]
    .some(value => value.toLocaleLowerCase().includes(query)))
})

function continueStory() { session.continueStory(); emit('close') }
function standby() { session.standby(); emit('close') }
function openFragment(fragment: StoryFragment) {
  if (fragment.id === story.latest?.id) session.continueStory()
  else story.viewFragment(fragment.id)
  emit('close')
}

// ── 历史会话抽屉（agent）──
const menuFor = ref<SessionMeta | null>(null)
const renamingId = ref('')
const renameText = ref('')

const isEmpty = computed(() => sessions.groups.length === 0)

// 抽屉关闭时清掉临时态；打开时刷新当前模式的项目会话。
watch(() => props.open, v => {
  if (!v) {
    menuFor.value = null; renamingId.value = ''
    return
  }
  sessions.setCurrentMode(story.agentMode)
  sessions.refreshRunning()
})

function pick(id: string) {
  if (renamingId.value) return
  session.openSession(id)
  emit('close')
}

function startNew() {
  session.newSession()
  emit('close')
}

function closeMenu() { menuFor.value = null }

/** WebView 里 window.prompt 默认被吞掉，所以重命名走行内输入框。 */
function beginRename() {
  const m = menuFor.value
  if (!m) return
  renamingId.value = m.id
  renameText.value = m.title
  menuFor.value = null
}

/**
 * 输入框只在正在改名的那一行渲染，所以聚焦挂在它自己的 ref 上。
 * 原先是 nextTick 之后 document.querySelector('.drawer-root .rename')——
 * 跨组件边界按 class 名找元素，类名一改就静默失效（不会报错，只是不再聚焦），
 * 而且抽屉关闭时仍留在 DOM 里（靠 transform 移出屏幕），选择器完全可能命中别处。
 */
function focusRename(el: unknown) {
  if (!(el instanceof HTMLInputElement)) return
  el.focus()
  el.select()
}

function commitRename() {
  if (!renamingId.value) return
  sessions.rename(renamingId.value, renameText.value)
  renamingId.value = ''
}

function doPin() {
  if (!menuFor.value) return
  sessions.togglePin(menuFor.value.id)
  closeMenu()
}

function doDelete() {
  if (!menuFor.value) return
  session.deleteSession(menuFor.value.id)
  closeMenu()
}

// ── 公共 ──
function go(path: string) { router.push(path); emit('close') }
function openDashboard() {
  emit('close')
  if (window.CoomiAndroid?.openDashboard) window.CoomiAndroid.openDashboard()
  else window.location.href = 'coomi://dashboard'
}
</script>

<template>
  <div class="drawer-root" :class="{ open }">
    <div class="scrim" @click="emit('close')" />

    <aside class="panel" role="dialog" :aria-label="`${modeLabel}模式会话历史`">
      <header class="dhead">
        <div v-if="story.agentMode === 'story'" class="sfield">
          <CoomiIcon name="search" :size="17" />
          <input v-model="fragmentQuery" type="text" placeholder="搜索剧情片段" enterkeyhint="search" />
          <button v-if="fragmentQuery" class="clr" aria-label="清空" @click="fragmentQuery = ''">
            <CoomiIcon name="close" :size="12" />
          </button>
        </div>
        <div v-else class="sfield">
          <CoomiIcon name="search" :size="17" />
          <input v-model="sessions.query" type="text" :placeholder="`搜索${modeLabel}记录`" enterkeyhint="search" />
          <button v-if="sessions.query" class="clr" aria-label="清空" @click="sessions.query = ''">
            <CoomiIcon name="close" :size="12" />
          </button>
        </div>
        <button class="icon-btn" aria-label="设置" @click="go('/settings')">
          <CoomiIcon name="settings" :size="19" />
        </button>
      </header>

      <p class="mode-heading">{{ modeLabel }}模式 · {{ story.projectPath.split('/').pop() || '默认故事' }}</p>

      <button v-if="story.agentMode !== 'story'" class="newrow" @click="startNew">
        <span class="nicon"><CoomiIcon name="pencil" :size="17" /></span>
        <span>开启新{{ modeLabel }}对话</span>
      </button>

      <div v-if="story.agentMode !== 'story'" class="list">
        <!-- 历史会话列表始终可见；「全局会话记忆」开关只控制模型能否读取这些记录。 -->
        <p v-if="isEmpty" class="empty">
          还没有历史会话。<br />随便说点什么，标题会用你的第一句话。
        </p>
        <template v-for="g in sessions.groups" :key="g.label">
          <p class="sec-label">{{ g.label }}</p>
          <div
            v-for="m in g.items"
            :key="m.id"
            class="row"
            :class="{ cur: m.id === session.sessionId }"
            @click="pick(m.id)"
          >
            <div class="rmain">
              <input
                v-if="renamingId === m.id"
                :ref="focusRename"
                v-model="renameText"
                class="rename"
                @click.stop
                @keyup.enter="commitRename"
                @blur="commitRename"
              />
              <p v-else class="rtitle">{{ m.title }}</p>
              <p class="rmeta">
                <CoomiIcon v-if="m.pinned" name="pin" :size="11" />
                <span>{{ formatSessionTime(m.updatedAt) }}</span>
                <template v-if="m.turns">
                  <span>·</span><span>{{ m.turns }} 轮</span>
                </template>
                <span v-if="sessions.isRunning(m.id)" class="rspin" aria-label="后台运行中" />
              </p>
            </div>
            <button class="rmore" aria-label="更多" @click.stop="menuFor = m">
              <CoomiIcon name="more" :size="17" />
            </button>
          </div>
        </template>
      </div>

      <template v-else>
        <div class="story-actions">
          <button class="continue" @click="continueStory"><CoomiIcon name="play" :size="17" /><span>继续故事</span></button>
          <button class="standby" @click="standby"><CoomiIcon name="home" :size="17" /><span>剧情首页</span></button>
        </div>
        <div class="list">
          <p v-if="story.fragments.length === 0" class="empty">剧情尚未开始。完成第一轮行动后，剧情片段会按顺序收进这里。</p>
          <p v-else-if="filteredFragments.length === 0" class="empty">没有匹配的剧情片段。</p>
          <template v-else-if="fragmentQuery">
            <p class="sec-label">搜索结果 · {{ filteredFragments.length }}</p>
            <button v-for="fragment in filteredFragments" :key="fragment.id" class="fragment" @click="openFragment(fragment)">
              <span class="filename">{{ fragment.filename }}</span><span class="summary">{{ fragment.summary }}</span><CoomiIcon name="chevronRight" :size="14" />
            </button>
          </template>
          <template v-else>
            <p class="sec-label">最近五条</p>
            <button v-for="fragment in story.latestFive" :key="fragment.id" class="fragment" @click="openFragment(fragment)">
              <span class="filename">{{ fragment.filename }}</span><span class="summary">{{ fragment.summary }}</span><CoomiIcon name="chevronRight" :size="14" />
            </button>
          </template>
          <template v-if="!fragmentQuery && story.older.length">
            <button class="older-toggle" @click="story.olderExpanded = !story.olderExpanded">
              <CoomiIcon :name="story.olderExpanded ? 'chevronDown' : 'chevronRight'" :size="15" /><span>更早的剧情片段（{{ story.older.length }}）</span>
            </button>
            <div v-if="story.olderExpanded" class="older-list">
              <button v-for="fragment in story.older" :key="fragment.id" class="fragment" @click="openFragment(fragment)"><span class="filename">{{ fragment.filename }}</span><span class="summary">{{ fragment.summary }}</span></button>
            </div>
          </template>
        </div>
      </template>

      <footer class="dfoot">
        <button class="console" @click="openDashboard">
          <CoomiIcon name="terminal" :size="20" /><span>返回控制台</span><CoomiIcon name="chevronRight" :size="17" />
        </button>
      </footer>
    </aside>

    <!-- 当前模式会话操作 sheet -->
    <div v-if="menuFor" class="sheet-wrap" @click.self="closeMenu">
      <div class="sheet">
        <p class="sheet-title">{{ menuFor.title }}</p>
        <button class="sheet-item" @click="beginRename">
          <CoomiIcon name="pencil" :size="18" /><span>重命名</span>
        </button>
        <button class="sheet-item" @click="doPin">
          <CoomiIcon name="pin" :size="18" /><span>{{ menuFor.pinned ? '取消置顶' : '置顶' }}</span>
        </button>
        <button class="sheet-item danger" @click="doDelete">
          <CoomiIcon name="trash" :size="18" /><span>删除会话</span>
        </button>
        <button class="sheet-cancel" @click="closeMenu">取消</button>
      </div>
    </div>

  </div>
</template>

<style scoped>
.drawer-root { position: fixed; inset: 0; z-index: 60; pointer-events: none; }
.drawer-root.open { pointer-events: auto; }
.scrim { position: absolute; inset: 0; background: var(--scrim); opacity: 0; transition: opacity .28s ease; }
.drawer-root.open .scrim { opacity: 1; }
.panel { position: absolute; inset: 0 auto 0 0; display: flex; flex-direction: column; width: 84%; max-width: 350px; padding-top: var(--safe-top); background: var(--bg); transform: translateX(-102%); transition: transform .3s cubic-bezier(.22,.68,.19,1); }
.drawer-root.open .panel { transform: none; }

/* 公共头部 */
.dhead { display: flex; align-items: center; gap: 6px; min-height: 58px; padding: 8px 10px 6px 12px; }
.mode-heading { margin:0; padding:2px 14px 6px; color:var(--text-3); font-size:11px; }
.drawer-tabs { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:3px; margin:0 12px 5px; padding:3px; border-radius:6px; background:var(--fill-strong); }.drawer-tabs button { min-height:30px; border-radius:4px; color:var(--text-3); font-size:11.5px; }.drawer-tabs button.on { background:var(--bg); color:var(--blue); box-shadow:var(--shadow-1); }
.icon-btn {
  display: grid; place-items: center; flex-shrink: 0;
  width: 40px; height: 40px; border: 0; border-radius: 50%; background: none; color: var(--text-2);
}
.icon-btn:active { background: var(--fill); }
.list { flex: 1; overflow-y: auto; padding: 6px 10px 14px; -webkit-overflow-scrolling: touch; }
.empty { margin: 24px 10px; color: var(--text-3); font-size: 13.5px; line-height: 1.7; }
.sec-label { margin: 12px 0 4px; }
.console {
  display: flex; align-items: center; gap: 10px; width: 100%; min-height: 42px; padding: 0 8px;
  color: var(--blue);
}
.console span { flex: 1; text-align: left; color: var(--text); font-weight: 600; }
.dfoot { border-top: 1px solid var(--border); padding: 8px 10px calc(8px + var(--safe-bottom)); }

/* ── Agent 模式：历史会话 ── */
.sfield {
  flex: 1; display: flex; align-items: center; gap: 7px;
  height: 38px; padding: 0 10px 0 11px;
  border-radius: var(--r-pill); background: var(--fill); color: var(--text-3);
}
.sfield input {
  flex: 1; min-width: 0; border: 0; background: none; outline: none;
  font: inherit; font-size: 14.5px; color: var(--text);
}
.sfield input::placeholder { color: var(--text-3); }
.clr {
  display: grid; place-items: center; width: 18px; height: 18px;
  border: 0; border-radius: 50%; background: var(--text-3); color: var(--bg);
}
.newrow {
  display: flex; align-items: center; gap: 10px;
  margin: 2px 10px 4px; padding: 8px 10px;
  border: 0; border-radius: var(--r-md); background: none;
  font-size: 15.5px; font-weight: 600; color: var(--blue);
}
.newrow:active { background: var(--fill); }
.nicon {
  display: grid; place-items: center; width: 30px; height: 30px;
  border-radius: 50%; background: var(--blue-soft);
}
.row { display: flex; align-items: center; gap: 4px; padding: 9px 6px 9px 10px; border-radius: var(--r-md); }
.row:active { background: var(--fill); }
.row.cur { background: var(--blue-soft); }
.rmain { flex: 1; min-width: 0; }
.rtitle {
  font-size: 14.8px; color: var(--text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.row.cur .rtitle { color: var(--blue); font-weight: 600; }
.rmeta {
  display: flex; align-items: center; gap: 4px; margin-top: 3px;
  font-size: 11.5px; color: var(--text-3);
}
/* 会话在后台执行中的小圈（放在时间/轮数之后，与 meta 文字同高） */
.rspin {
  flex: none;
  width: 9px; height: 9px; border-radius: 50%;
  border: 2px solid var(--blue-soft);
  border-top-color: var(--blue);
  animation: coomi-rspin 0.9s linear infinite;
}
@keyframes coomi-rspin { to { transform: rotate(360deg); } }
.rename {
  width: 100%; padding: 4px 7px;
  border: 1px solid var(--blue-border); border-radius: var(--r-sm);
  background: var(--bg); outline: none;
  font: inherit; font-size: 14.5px; color: var(--text);
}
.rmore {
  display: grid; place-items: center; width: 30px; height: 30px;
  border: 0; border-radius: 50%; background: none; color: var(--text-3);
}
.sheet-wrap {
  /* 刻意不用 components/ui/BottomSheet：它是 position:fixed，而这一层必须限制在
     抽屉面板内部（position:absolute + inset:0），点抽屉外的区域仍要能关整个抽屉。 */
  position: absolute; inset: 0; z-index: 3;
  display: flex; align-items: flex-end;
  background: var(--scrim);
}
.sheet {
  width: 100%; padding: 4px 10px calc(10px + var(--safe-bottom));
  background: var(--bg-card); border-radius: var(--r-sheet) var(--r-sheet) 0 0;
  box-shadow: var(--shadow-sheet);
  animation: rise .22s cubic-bezier(.22, .68, .19, 1) both;
}
@keyframes rise { from { transform: translateY(18px); opacity: .5; } to { transform: none; opacity: 1; } }
.sheet-title {
  padding: 12px 12px 8px; font-size: 12.5px; color: var(--text-3);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.sheet-item {
  display: flex; align-items: center; gap: 12px;
  width: 100%; height: 50px; padding: 0 12px;
  border: 0; border-radius: var(--r-md); background: none;
  font-size: 15.5px; color: var(--text);
}
.sheet-item:active { background: var(--fill); }
.sheet-item.danger { color: var(--danger); }
.sheet-cancel {
  width: 100%; height: 48px; margin-top: 6px;
  border: 0; border-radius: var(--r-md); background: var(--fill);
  font-size: 15.5px; font-weight: 600; color: var(--text-2);
}

/* ── 剧情 / 旁白模式：剧情片段 ── */
.project { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.project span { font-size: 17px; font-weight: 650; }
.project small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-3); }
.continue, .standby {
  display: flex; flex: 1; align-items: center; justify-content: center; gap: 9px;
  padding: 10px 12px; border-radius: var(--r-md);
  font-size: 15px; font-weight: 650;
}
.story-actions { display: flex; gap: 8px; margin: 2px 10px 8px; }
.continue { background: var(--blue-soft); color: var(--blue); }
.standby { background: var(--fill-strong); color: var(--text-2); }
.fragment { display: grid; grid-template-columns: 1fr auto; width: 100%; padding: 10px; border-radius: var(--r-sm); text-align: left; }
.fragment:active { background: var(--fill); }
.filename { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--font-mono); font-size: 11px; color: var(--text-3); }
.summary { grid-column: 1 / -1; margin-top: 3px; font-size: 14px; color: var(--text); line-height: 1.5; }
.fragment :deep(svg) { color: var(--text-3); }
.older-toggle { display: flex; align-items: center; gap: 6px; width: 100%; margin-top: 10px; padding: 9px 8px; color: var(--text-2); font-size: 13px; }
.older-list { border-left: 1px solid var(--border); margin-left: 14px; padding-left: 4px; }
</style>
