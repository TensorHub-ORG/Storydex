<script setup lang="ts">
/**
 * 顶栏：汉堡 / 模型名 / 上下文用量。
 * 忙的时候底边跑一条 2px 蓝色扫光，让「正在干活」这件事在最顶层也能看见。
 */
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useConfigStore } from '@/stores/config'
import { useSessionStore } from '@/stores/session'
import { useConnectionStore } from '@/stores/connection'
import CoomiIcon from './CoomiIcon.vue'

defineEmits<{ menu: [] }>()

const config = useConfigStore()
const session = useSessionStore()
const connection = useConnectionStore()
const router = useRouter()
const modelOpen = ref(false)
const usageOpen = ref(false)
const pathPickerOpen = ref(false)
const pathInput = ref('')
const pathNotice = ref('')
const appFilesDir = window.CoomiAndroid?.getFilesDirPath?.() || '/data/user/0/com.storydex.android/files'
const quickPaths = [appFilesDir, `${appFilesDir}/coomi`]
const providerGroups = computed(() => [...config.providers].sort((a, b) => Number(b.id === config.activeId) - Number(a.id === config.activeId)))
const usagePercent = computed(() => Math.min(100, Math.max(0, Math.round((session.usage?.contextRatio ?? 0) * 100))))
const usageStroke = computed(() => `${usagePercent.value} ${100 - usagePercent.value}`)

function formatTokens(value: number): string {
  if (value >= 1000000) return (value / 1000000).toFixed(1) + 'M'
  if (value >= 1000) return (value / 1000).toFixed(1) + 'k'
  return String(value)
}

function choose(providerId: string, model: string) {
  session.selectModel(providerId, model)
  modelOpen.value = false
}

function toggleModel() {
  modelOpen.value = !modelOpen.value
  usageOpen.value = false
}

function toggleUsage() {
  usageOpen.value = !usageOpen.value
  modelOpen.value = false
}

// ── 会话标记路径（第三批 5：绑定为会话执行目录）──
function openPathPicker() {
  pathInput.value = session.cwd || ''
  pathNotice.value = ''
  pathPickerOpen.value = true
  usageOpen.value = false
}

function pickPath(path: string) {
  pathInput.value = path
}

async function savePath() {
  const path = pathInput.value.trim()
  if (!path) return
  const ok = await session.setSessionCwd(path)
  pathNotice.value = ok ? '已设置，后续对话将在此目录执行' : '设置失败：路径不存在或引擎不可用'
  if (ok) setTimeout(() => { pathPickerOpen.value = false }, 900)
}

function browseInFileManager() {
  pathPickerOpen.value = false
  router.push('/files')
}
</script>

<template>
  <header class="topbar">
    <button class="icon-btn" aria-label="会话历史" @click="$emit('menu')">
      <CoomiIcon name="menu" />
    </button>

    <button class="center" :aria-expanded="modelOpen" @click="toggleModel">
      <span class="model">{{ config.currentModel }}</span>
      <span v-if="connection.demo" class="demo">演示</span>
      <span v-if="config.planMode" class="plan">计划</span>
      <CoomiIcon name="chevronDown" :size="13" class="caret" />
    </button>

    <button v-if="modelOpen" class="model-scrim" aria-label="关闭模型选择" @click="modelOpen = false" />
    <div v-if="modelOpen" class="model-menu">
      <template v-if="providerGroups.some(p => p.models.length)">
        <section v-for="p in providerGroups" :key="p.id" class="model-group">
          <p v-if="p.models.length" class="provider-name">{{ p.name }}<span v-if="p.id === config.activeId">当前</span></p>
          <button
            v-for="m in p.models" :key="p.id + ':' + m" class="model-row"
            :class="{ selected: p.id === config.currentProviderId && m === config.currentModel }"
            @click="choose(p.id, m)"
          >
            <span>{{ m }}</span><CoomiIcon v-if="p.id === config.currentProviderId && m === config.currentModel" name="check" :size="15" />
          </button>
        </section>
      </template>
      <p v-else class="model-empty">当前 Provider 没有可用模型</p>
    </div>

    <button class="usage-button" :aria-expanded="usageOpen" aria-label="上下文用量" @click="toggleUsage">
      <svg class="usage-ring" viewBox="0 0 36 36" aria-hidden="true">
        <circle class="usage-track" cx="18" cy="18" r="15" pathLength="100" />
        <circle class="usage-value" cx="18" cy="18" r="15" pathLength="100" :stroke-dasharray="usageStroke" />
      </svg>
    </button>

    <button v-if="usageOpen" class="usage-scrim" aria-label="关闭上下文数据" @click="usageOpen = false" />
    <div v-if="usageOpen" class="usage-menu">
      <p class="usage-title">本次会话用量</p>
      <div v-if="session.usage" class="usage-stats">
        <div><span>会话 Token</span><strong>{{ formatTokens(session.usage.total) }}</strong></div>
        <div><span>上下文使用</span><strong>{{ formatTokens(session.usage.contextUsed) }} / {{ formatTokens(session.usage.contextWindow) }}</strong></div>
      </div>
      <p v-else class="usage-empty">此对话尚无用量数据</p>
      <div class="usage-path">
        <span>会话标记路径</span>
        <button class="path-btn" @click="openPathPicker">{{ session.cwd || '点击选择' }}</button>
      </div>
    </div>

    <div v-if="pathPickerOpen" class="path-mask" @click="pathPickerOpen = false">
      <div class="path-sheet" @click.stop>
        <p class="path-title">会话标记路径</p>
        <p class="path-desc">绑定为当前会话的执行目录，Storydex 将在此目录下工作。</p>
        <input v-model="pathInput" class="path-input" :placeholder="appFilesDir" spellcheck="false" @keyup.enter="savePath" />
        <div class="path-quick">
          <button v-for="p in quickPaths" :key="p" class="chip" @click="pickPath(p)">{{ p.split('/').pop() || p }}</button>
          <button class="chip" @click="browseInFileManager">在文件管理器中浏览…</button>
        </div>
        <p v-if="pathNotice" class="path-notice">{{ pathNotice }}</p>
        <div class="path-actions">
          <button class="btn ghost" @click="pathPickerOpen = false">取消</button>
          <button class="btn primary" @click="savePath">设置</button>
        </div>
      </div>
    </div>

    <div v-if="session.isBusy" class="sweep"><i /></div>
  </header>
</template>

<style scoped>
.topbar {
  position: relative;
  display: flex; align-items: center; gap: 4px;
  min-height: 52px; padding: calc(var(--safe-top) + 6px) 8px 6px;
  background: var(--bg);
}
.model-scrim { position: fixed; inset: 0; z-index: 19; border: 0; background: transparent; }
.model-menu {
  position: absolute; z-index: 20; top: calc(var(--safe-top) + 49px); left: 50%;
  width: min(78vw, 300px); max-height: min(52vh, 380px); overflow-y: auto;
  transform: translateX(-50%); padding: 6px; border: 1px solid var(--border);
  border-radius: var(--r-card); background: var(--bg); box-shadow: var(--shadow-2);
}
.usage-scrim { position: fixed; inset: 0; z-index: 19; border: 0; background: transparent; }
.usage-menu {
  position: absolute; z-index: 20; top: calc(var(--safe-top) + 49px); right: 8px;
  width: min(74vw, 246px); padding: 12px 13px;
  border: 1px solid var(--border); border-radius: var(--r-card);
  background: var(--bg); box-shadow: var(--shadow-2);
}
.usage-title { margin: 0 0 9px; font-size: 12px; font-weight: 650; color: var(--text-2); }
.usage-stats { display: grid; gap: 8px; }
.usage-stats div { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.usage-stats span { font-size: 12px; color: var(--text-3); }
.usage-stats strong { font-family: var(--font-mono); font-size: 12.5px; color: var(--text); }
.usage-empty { margin: 0; font-size: 12px; line-height: 1.5; color: var(--text-3); }
.usage-path {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  margin-top: 10px; padding-top: 9px; border-top: 1px solid var(--border);
}
.usage-path span { font-size: 12px; color: var(--text-3); flex-shrink: 0; }
.path-btn {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-family: var(--font-mono); font-size: 11.5px; color: var(--blue);
  background: var(--blue-soft); border-radius: var(--r-sm); padding: 5px 9px;
}
.path-mask { position: fixed; inset: 0; z-index: 60; background: rgba(0, 0, 0, 0.4); display: flex; align-items: flex-end; }
.path-sheet {
  width: 100%;
  background: var(--bg-card);
  border-radius: 18px 18px 0 0;
  padding: 18px 16px calc(16px + var(--safe-bottom));
}
.path-title { margin: 0; font-size: 16px; font-weight: 650; }
.path-desc { margin: 4px 0 12px; font-size: 12.5px; color: var(--text-3); }
.path-input {
  width: 100%;
  min-height: 44px;
  padding: 0 12px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  background: var(--bg-input);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 12.5px;
}
.path-quick { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.chip {
  padding: 6px 12px;
  border-radius: var(--r-pill);
  background: var(--fill-strong);
  color: var(--text-2);
  font-size: 12px;
}
.path-notice { margin: 10px 0 0; font-size: 12.5px; color: var(--ok); }
.path-actions { display: flex; gap: 10px; margin-top: 16px; }
.path-actions .btn { flex: 1; }
.btn.primary { background: var(--blue); color: #fff; }
.btn.ghost { background: var(--fill-strong); color: var(--text); }
.model-group + .model-group { border-top: 1px solid var(--border); margin-top: 5px; padding-top: 5px; }
.provider-name { display: flex; align-items: center; gap: 6px; padding: 5px 8px 3px; font-size: 11.5px; color: var(--text-3); }
.provider-name span { padding: 1px 5px; border-radius: var(--r-pill); background: var(--blue-soft); color: var(--blue); }
.model-row { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 38px; padding: 7px 9px; border: 0; border-radius: var(--r-sm); background: none; color: var(--text); text-align: left; }
.model-row span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--font-mono); font-size: 12.5px; }
.model-row.selected { background: var(--blue-soft); color: var(--blue); }
.model-row:active { background: var(--fill-press); }
.model-empty { padding: 16px 10px; text-align: center; font-size: 12.5px; color: var(--text-3); }
.icon-btn {
  display: grid; place-items: center; flex-shrink: 0;
  width: 40px; height: 40px;
  border: 0; border-radius: 50%; background: none; color: var(--text-2);
}
.icon-btn:active { background: var(--fill); }
.usage-button {
  position: relative; display: grid; place-items: center; flex-shrink: 0;
  width: 40px; height: 40px; border: 0; border-radius: 50%; background: none; color: var(--text-2);
}
.usage-button:active { background: var(--fill); }
.usage-ring { width: 30px; height: 30px; transform: rotate(-90deg); }
.usage-ring circle { fill: none; stroke-width: 3.8; }
.usage-track { stroke: var(--border-strong); }
.usage-value { stroke: var(--blue); stroke-linecap: round; transition: stroke-dasharray .22s ease; }

.center {
  flex: 1; min-width: 0;
  display: inline-flex; align-items: center; justify-content: center; gap: 5px;
  height: 36px; padding: 0 10px;
  border: 0; border-radius: var(--r-pill); background: none; color: var(--text);
}
.center:active { background: var(--fill); }
.model {
  font-size: 15.5px; font-weight: 600; letter-spacing: -.1px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.plan {
  flex-shrink: 0; padding: 2px 7px; border-radius: var(--r-pill);
  background: var(--blue-soft); color: var(--blue);
  font-size: 11px; font-weight: 600;
}
/* 演示标记用点缀橙，和蓝色的功能性标记（计划）区分开。 */
.demo {
  flex-shrink: 0; padding: 2px 7px; border-radius: var(--r-pill);
  background: var(--orange-soft); color: var(--orange);
  font-size: 11px; font-weight: 600;
}
.caret { color: var(--text-3); }

/* 底边扫光：不表示进度，只表示「还在动」。 */
.sweep {
  position: absolute; left: 0; right: 0; bottom: 0;
  height: 2px; overflow: hidden;
}
.sweep i {
  display: block; width: 100%; height: 100%;
  background: linear-gradient(90deg, transparent, var(--blue), transparent);
  animation: coomi-sweep 1.25s ease-in-out infinite;
}
</style>
