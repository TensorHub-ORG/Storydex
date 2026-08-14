<script setup lang="ts">
import { computed, ref } from 'vue'
import { useConfigStore } from '@/stores/config'
import { useSessionStore } from '@/stores/session'
import { useConnectionStore } from '@/stores/connection'
import { authedFetch } from '@/bridge/http'
import CoomiIcon from './CoomiIcon.vue'

defineEmits<{ menu: [] }>()
const config = useConfigStore()
const session = useSessionStore()
const connection = useConnectionStore()
const modelOpen = ref(false)
const usageOpen = ref(false)
const usageView = ref<'current' | 'project'>('current')
const projectMode = ref<'story' | 'narrator' | 'agent'>('story')
const periodConfirm = ref(false)
const usageNotice = ref('')
const providerGroups = computed(() => [...config.providers].sort((a, b) => Number(b.id === config.activeId) - Number(a.id === config.activeId)))
const usagePercent = computed(() => Math.min(100, Math.max(0, Math.round((session.usage?.contextRatio ?? 0) * 100))))
const usageStroke = computed(() => `${usagePercent.value} ${100 - usagePercent.value}`)
const projectStats = computed(() => session.usage?.project.modes?.[projectMode.value])

const MODE_LABELS = { story: '剧情', narrator: '旁白', agent: 'Agent' }
const CATEGORY_LABELS: Record<string, string> = {
  rules: '创作规则', story: '剧情内容', characters_world: '角色与世界', memory: '记忆与连续性',
  scripts_time: '剧本与时间', constraints: '生成约束', player_interaction: '玩家交互', capabilities: '内置能力与检索',
  narrative_source: '已发生剧情', occurred_scripts: '已发生剧本', narration_constraints: '旁白约束', user_request: '用户请求',
  conversation: '对话历史', project_files: '项目文件', tool_results: '工具结果', plans: '任务计划',
}
function formatTokens(value = 0): string {
  if (value >= 1_000_000) return (value / 1_000_000).toFixed(1) + 'M'
  if (value >= 1000) return (value / 1000).toFixed(1) + 'k'
  return String(value)
}
function formatRate(value = 0): string { return `${Math.round(value * 100)}%` }
function sortedCategories(values: Record<string, number> | undefined) {
  return Object.entries(values ?? {}).sort((a, b) => b[1] - a[1])
}
function choose(providerId: string, model: string) { session.selectModel(providerId, model); modelOpen.value = false }
function toggleModel() { modelOpen.value = !modelOpen.value; usageOpen.value = false }
function toggleUsage() { usageOpen.value = !usageOpen.value; modelOpen.value = false; usageNotice.value = ''; periodConfirm.value = false }
async function newPeriod() {
  const response = await authedFetch('/api/storydex/usage/new-period', { method: 'POST' })
  periodConfirm.value = false
  usageNotice.value = response.ok ? '已新建统计周期，旧账本仍保留' : `新建失败（HTTP ${response.status}）`
  if (response.ok) await session.refreshProjectUsage()
}
</script>

<template>
  <header class="topbar">
    <button class="icon-btn" aria-label="会话历史" @click="$emit('menu')"><CoomiIcon name="menu" /></button>
    <button class="center" :aria-expanded="modelOpen" @click="toggleModel"><span class="model">{{ config.currentModel }}</span><span v-if="connection.demo" class="demo">演示</span><span v-if="config.planMode" class="plan">计划</span><CoomiIcon name="chevronDown" :size="13" class="caret" /></button>
    <button v-if="modelOpen" class="scrim" aria-label="关闭模型选择" @click="modelOpen = false" />
    <div v-if="modelOpen" class="model-menu">
      <template v-if="providerGroups.some(p => p.models.length)"><section v-for="provider in providerGroups" :key="provider.id" class="model-group"><p v-if="provider.models.length" class="provider-name">{{ provider.name }}<span v-if="provider.id === config.activeId">当前</span></p><button v-for="modelName in provider.models" :key="provider.id + modelName" class="model-row" :class="{ selected: provider.id === config.currentProviderId && modelName === config.currentModel }" @click="choose(provider.id, modelName)"><span>{{ modelName }}</span><CoomiIcon v-if="provider.id === config.currentProviderId && modelName === config.currentModel" name="check" :size="15" /></button></section></template>
      <p v-else class="empty">当前没有可用模型</p>
    </div>
    <button class="usage-button" :aria-expanded="usageOpen" aria-label="上下文与项目用量" @click="toggleUsage"><svg class="usage-ring" viewBox="0 0 36 36" aria-hidden="true"><circle class="usage-track" cx="18" cy="18" r="15" pathLength="100" /><circle class="usage-value" cx="18" cy="18" r="15" pathLength="100" :stroke-dasharray="usageStroke" /></svg></button>
    <button v-if="usageOpen" class="scrim" aria-label="关闭上下文数据" @click="usageOpen = false" />
    <div v-if="usageOpen" class="usage-menu">
      <div class="usage-tabs"><button :class="{ on: usageView === 'current' }" @click="usageView = 'current'">当前上下文</button><button :class="{ on: usageView === 'project' }" @click="usageView = 'project'">项目累计</button></div>
      <template v-if="usageView === 'current'">
        <div class="headline"><div><span>上下文窗口</span><strong>{{ formatTokens(session.usage?.contextUsed) }} / {{ formatTokens(session.usage?.contextWindow) }}</strong></div><b>{{ usagePercent }}%</b></div>
        <div class="metrics"><div><span>本轮缓存命中率</span><b>{{ formatRate(session.usage?.turnCacheRate) }}</b></div><div><span>本轮输入 / 输出</span><b>{{ formatTokens(session.usage?.turnInput) }} / {{ formatTokens(session.usage?.turnOutput) }}</b></div><div v-if="session.usage?.turnReasoning"><span>推理 Token</span><b>{{ formatTokens(session.usage.turnReasoning) }}</b></div></div>
        <div class="categories"><div v-for="[key, value] in sortedCategories(session.usage?.categories)" :key="key"><span>{{ CATEGORY_LABELS[key] ?? key }}</span><i><em :style="{ width: `${Math.min(100, value / Math.max(1, session.usage?.contextUsed ?? 1) * 100)}%` }" /></i><b>{{ formatTokens(value) }}</b></div></div>
        <p v-if="!session.usage" class="empty">本轮尚无用量数据</p>
      </template>
      <template v-else>
        <div class="mode-tabs"><button v-for="(label, mode) in MODE_LABELS" :key="mode" :class="{ on: projectMode === mode }" @click="projectMode = mode">{{ label }}</button></div>
        <div v-if="projectStats" class="metrics"><div><span>累计输入 / 输出</span><b>{{ formatTokens(projectStats.input_tokens) }} / {{ formatTokens(projectStats.output_tokens) }}</b></div><div><span>全生命周期缓存命中</span><b>{{ formatRate(projectStats.cache_rate) }}</b></div><div><span>最近 10 轮平均</span><b>{{ formatRate(projectStats.recent_10_cache_rate) }}</b></div><div><span>统计轮数</span><b>{{ projectStats.turns }}</b></div></div>
        <div class="categories"><div v-for="[key, value] in sortedCategories(projectStats?.categories)" :key="key"><span>{{ CATEGORY_LABELS[key] ?? key }}</span><i><em :style="{ width: `${Math.min(100, value / Math.max(1, projectStats?.input_tokens ?? 1) * 100)}%` }" /></i><b>{{ formatTokens(value) }}</b></div></div>
        <p v-if="!projectStats?.turns" class="empty">此模式暂无项目累计数据</p>
        <button class="period-button" @click="periodConfirm = true">新建统计周期</button><p v-if="usageNotice" class="usage-notice">{{ usageNotice }}</p>
      </template>
      <div v-if="periodConfirm" class="period-confirm"><b>开始新统计周期？</b><p>仅新建分期标记，项目累计与推理强度均轮统计会继续保留。</p><div><button @click="periodConfirm = false">取消</button><button class="primary" @click="newPeriod">新建</button></div></div>
    </div>
    <div v-if="session.isBusy" class="sweep"><i /></div>
  </header>
</template>

<style scoped>
.topbar { position:relative; display:flex; align-items:center; gap:4px; min-height:52px; padding:calc(var(--safe-top) + 6px) 8px 6px; background:var(--bg); }
.icon-btn,.usage-button { display:grid; place-items:center; flex:0 0 40px; width:40px; height:40px; border-radius:50%; color:var(--text-2); }.icon-btn:active,.usage-button:active { background:var(--fill); }
.center { display:flex; align-items:center; justify-content:center; gap:5px; min-width:0; height:36px; flex:1; padding:0 10px; border-radius:18px; color:var(--text); }.center:active { background:var(--fill); }.model { overflow:hidden; font-size:15px; font-weight:600; text-overflow:ellipsis; white-space:nowrap; }.plan,.demo { padding:2px 7px; border-radius:10px; font-size:11px; }.plan { background:var(--blue-soft); color:var(--blue); }.demo { background:var(--orange-soft); color:var(--orange); }.caret { color:var(--text-3); }
.scrim { position:fixed; z-index:19; inset:0; background:transparent; }.model-menu,.usage-menu { position:absolute; z-index:20; top:calc(var(--safe-top) + 49px); border:1px solid var(--border); border-radius:7px; background:var(--bg); box-shadow:var(--shadow-2); }.model-menu { left:50%; width:min(78vw,300px); max-height:min(52vh,380px); overflow-y:auto; padding:6px; transform:translateX(-50%); }.provider-name { display:flex; gap:6px; margin:0; padding:6px 8px 3px; color:var(--text-3); font-size:11px; }.provider-name span { color:var(--blue); }.model-group + .model-group { margin-top:5px; padding-top:5px; border-top:1px solid var(--border); }.model-row { display:flex; align-items:center; width:100%; min-height:38px; gap:8px; padding:7px 9px; border-radius:5px; color:var(--text); text-align:left; }.model-row span { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }.model-row.selected { background:var(--blue-soft); color:var(--blue); }
.usage-button { position:relative; }.usage-ring { width:31px; height:31px; transform:rotate(-90deg); }.usage-ring circle { fill:none; stroke-width:3.5; }.usage-track { stroke:var(--border-strong); }.usage-value { stroke:var(--blue); stroke-linecap:round; }
.usage-menu { right:8px; width:min(84vw,320px); max-height:min(76vh,620px); overflow-y:auto; padding:11px; }.usage-tabs,.mode-tabs { display:grid; grid-template-columns:repeat(2,1fr); padding:3px; border-radius:6px; background:var(--fill-strong); }.mode-tabs { grid-template-columns:repeat(3,1fr); margin:10px 0; }.usage-tabs button,.mode-tabs button { min-height:31px; border-radius:4px; color:var(--text-3); font-size:11.5px; }.usage-tabs button.on,.mode-tabs button.on { background:var(--bg); color:var(--blue); box-shadow:var(--shadow-1); }
.headline { display:flex; align-items:center; gap:10px; margin:12px 0; }.headline > div { display:flex; flex:1; flex-direction:column; gap:3px; }.headline span,.metrics span { color:var(--text-3); font-size:11px; }.headline strong { font-family:var(--font-mono); font-size:15px; }.headline > b { font-size:22px; color:var(--blue); }
.metrics { display:grid; gap:7px; padding:9px 0; border-block:1px solid var(--border); }.metrics > div { display:flex; justify-content:space-between; gap:10px; }.metrics b { font-family:var(--font-mono); font-size:11.5px; }
.categories { display:grid; gap:8px; padding:11px 0; }.categories > div { display:grid; grid-template-columns:minmax(80px,1fr) minmax(60px,1.2fr) 42px; align-items:center; gap:7px; }.categories span { overflow:hidden; color:var(--text-2); font-size:11px; text-overflow:ellipsis; white-space:nowrap; }.categories i { height:5px; overflow:hidden; border-radius:3px; background:var(--fill-strong); }.categories em { display:block; height:100%; border-radius:3px; background:var(--blue); }.categories b { text-align:right; font-family:var(--font-mono); font-size:10.5px; }
.empty { margin:8px 0; color:var(--text-3); font-size:11.5px; text-align:center; }.period-button { width:100%; min-height:36px; border-radius:6px; background:var(--fill-strong); color:var(--text-2); font-size:12px; }.usage-notice { margin:7px 0 0; color:var(--ok); font-size:11px; }.period-confirm { margin-top:9px; padding:10px; border-radius:6px; background:var(--fill); }.period-confirm > b { font-size:12.5px; }.period-confirm p { margin:3px 0 9px; color:var(--text-3); font-size:11px; }.period-confirm > div { display:flex; gap:7px; }.period-confirm button { min-height:34px; flex:1; border-radius:5px; background:var(--bg); }.period-confirm button.primary { background:var(--blue); color:#fff; }
.sweep { position:absolute; right:0; bottom:0; left:0; height:2px; overflow:hidden; }.sweep i { display:block; width:100%; height:100%; background:linear-gradient(90deg,transparent,var(--blue),transparent); animation:coomi-sweep 1.25s ease-in-out infinite; }
</style>
