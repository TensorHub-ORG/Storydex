<script setup lang="ts">
/**
 * 设置。分组白卡 + 行的结构，选中态用蓝勾而不是描边 ——
 * 和抽屉、空态里的选中语言保持一致。
 */
import { computed, ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useConfigStore, PERMISSION_MODES, THEME_MODES } from '@/stores/config'
import { useSessionStore } from '@/stores/session'
import { useStoryStore, type AgentMode, type NarrativeMode, type ReasoningEffort } from '@/stores/story'
import { useConnectionStore } from '@/stores/connection'
import PageHead from '@/components/PageHead.vue'
import CoomiIcon from '@/components/CoomiIcon.vue'

const router = useRouter()
const config = useConfigStore()
const session = useSessionStore()
const story = useStoryStore()
const connection = useConnectionStore()

/** 全局记忆开关同步失败时的行内提示。 */
const gmError = ref('')
async function toggleGlobalMemory() {
  gmError.value = ''
  try {
    await config.toggleGlobalMemory()
  } catch (e) {
    gmError.value = e instanceof Error ? e.message : String(e)
  }
}

const STORY_MODES: { mode: AgentMode; label: string; desc: string }[] = [
  { mode: 'story', label: '剧情模式', desc: '沉浸推进剧情并识别 OOC' },
  { mode: 'narrator', label: '剧情旁白', desc: '只解说故事状态，不续写正文' },
  { mode: 'agent', label: '独立 Agent', desc: '完整且不受剧情约束的 Coomi Agent' },
]
const NARRATIVE_MODES: { mode: NarrativeMode; label: string; desc: string }[] = [
  { mode: 'immersive', label: '沉浸', desc: '以角色为本，严格遵循设定' },
  { mode: 'narrative', label: '叙事', desc: '以引导者视角，合理引导走向' },
  { mode: 'free', label: '自由', desc: '以造物主姿态，大胆重塑世界' },
]
const REASONING_EFFORTS: { effort: ReasoningEffort; label: string; desc: string }[] = [
  { effort: 'auto', label: '自动', desc: '按模型能力自动选择' },
  { effort: 'low', label: '低', desc: '响应更快，适合简单推进' },
  { effort: 'medium', label: '中', desc: '速度与分析深度平衡' },
  { effort: 'high', label: '高', desc: '默认，适合连续剧情与设定核对' },
  { effort: 'xhigh', label: '超高', desc: '复杂伏笔、多人关系与长上下文' },
  { effort: 'max', label: '最大', desc: '使用模型可提供的最高推理档位' },
]

function updateFragmentLength(kind: 'min' | 'max', event: Event) {
  const value = Number((event.target as HTMLInputElement).value)
  story.setFragmentLength(
    kind === 'min' ? value : story.fragmentMin,
    kind === 'max' ? value : story.fragmentMax,
  )
}

/** provider × model 拍平成一维列表，省掉一层嵌套标题。 */
const modelRows = computed(() =>
  config.providers.flatMap(p =>
    p.models.map(m => ({ key: p.id + '::' + m, providerId: p.id, provider: p.name, model: m })),
  ),
)

function isCurrent(providerId: string, model: string): boolean {
  return config.currentProviderId === providerId && config.currentModel === model
}

/** 进入设置页时拉取定制提示词，保证入口副标题与引擎一致。 */
onMounted(() => { void config.fetchCustomPrompt() })
</script>
<template>
  <div class="page">
    <PageHead title="设置" @back="router.push('/')" />
    <main class="body">
      <p class="sec-label">Agent 模式</p>
      <div class="group">
        <button v-for="m in STORY_MODES" :key="m.mode" class="row" @click="story.setAgentMode(m.mode)">
          <span class="ri" :class="{ on: story.agentMode === m.mode }"><CoomiIcon name="sparkle" :size="17" /></span>
          <span class="rt">
            <span class="rmain">{{ m.label }}</span>
            <span class="rsub">{{ m.desc }}</span>
          </span>
          <CoomiIcon v-if="story.agentMode === m.mode" name="check" :size="17" class="tick" />
        </button>
      </div>

      <p class="sec-label">操作权限</p>
      <div class="group">
        <button v-for="item in PERMISSION_MODES" :key="item.mode" class="row" @click="session.setPermissionMode(item.mode)">
          <span class="ri" :class="{ on: config.permissionMode === item.mode }"><CoomiIcon name="shield" :size="17" /></span>
          <span class="rt">
            <span class="rmain">{{ item.label }}</span>
            <span class="rsub">{{ item.desc }}</span>
          </span>
          <CoomiIcon v-if="config.permissionMode === item.mode" name="check" :size="17" class="tick" />
        </button>
      </div>

      <template v-if="story.agentMode !== 'agent'">
      <p class="sec-label">剧情控制</p>
      <div class="group">
        <button v-for="m in NARRATIVE_MODES" :key="m.mode" class="row" @click="story.setNarrativeMode(m.mode)">
          <span class="ri" :class="{ on: story.narrativeMode === m.mode }"><CoomiIcon name="target" :size="17" /></span>
          <span class="rt">
            <span class="rmain">{{ m.label }}</span><span class="rsub">{{ m.desc }}</span>
          </span>
          <CoomiIcon v-if="story.narrativeMode === m.mode" name="check" :size="17" class="tick" />
        </button>
      </div>
      </template>

      <p class="sec-label">剧情片段</p>
      <div class="group length-group">
        <label class="number-row">
          <span class="rt">
            <span class="rmain">大概字数</span>
            <span class="rsub">软目标，默认每段 1000-2000 个中文字符</span>
          </span>
          <span class="range-inputs">
            <input type="number" inputmode="numeric" min="200" max="8000" step="100" :value="story.fragmentMin" aria-label="剧情片段最少字数" @change="updateFragmentLength('min', $event)" />
            <span>至</span>
            <input type="number" inputmode="numeric" min="200" max="8000" step="100" :value="story.fragmentMax" aria-label="剧情片段最多字数" @change="updateFragmentLength('max', $event)" />
          </span>
        </label>
      </div>

      <p class="sec-label">推理强度</p>
      <div class="group">
        <button v-for="item in REASONING_EFFORTS" :key="item.effort" class="row" @click="session.setReasoningEffort(item.effort)">
          <span class="ri" :class="{ on: story.reasoningEffort === item.effort }"><CoomiIcon name="sparkle" :size="17" /></span>
          <span class="rt">
            <span class="rmain">{{ item.label }}</span>
            <span class="rsub">{{ item.desc }}</span>
          </span>
          <CoomiIcon v-if="story.reasoningEffort === item.effort" name="check" :size="17" class="tick" />
        </button>
      </div>

      <p class="sec-label">身份定位</p>
      <div class="group">
        <button class="row" @click="router.push('/persona')">
          <span class="ri" :class="{ on: config.customPrompt.trim() !== '' }"><CoomiIcon name="sparkle" :size="17" /></span>
          <span class="rt">
            <span class="rmain">定制身份定位</span>
            <span class="rsub">{{ config.customPrompt.trim() ? '已配置，置于系统提示词最前生效' : '未设置。让 AI 认知自己的身份与定位' }}</span>
          </span>
          <CoomiIcon name="chevronRight" :size="15" class="arw" />
        </button>
      </div>

      <p class="sec-label">外观</p>
      <div class="group">
        <button v-for="m in THEME_MODES" :key="m.mode" class="row" @click="config.setThemeMode(m.mode)">
          <span class="ri" :class="{ on: config.themeMode === m.mode }">
            <CoomiIcon :name="m.mode === 'dark' ? 'moon' : m.mode === 'book' ? 'article' : 'phone'" :size="17" />
          </span>
          <span class="rt">
            <span class="rmain">{{ m.label }}</span>
            <span class="rsub">{{ m.desc }}</span>
          </span>
          <CoomiIcon v-if="config.themeMode === m.mode" name="check" :size="17" class="tick" />
        </button>
      </div>

      <p class="sec-label">模型</p>
      <div class="group model-list">
        <p v-if="modelRows.length === 0" class="empty">还没有可用模型，先到下面配置 Provider。</p>
        <button v-for="r in modelRows" :key="r.key" class="row" @click="session.selectModel(r.providerId, r.model)">
          <span class="rt">
            <span class="rmain mono">{{ r.model }}</span>
            <span class="rsub">{{ r.provider }}</span>
          </span>
          <CoomiIcon v-if="isCurrent(r.providerId, r.model)" name="check" :size="17" class="tick" />
        </button>
      </div>
      <div class="foot">
        <span class="conn" :class="{ on: connection.isOpen }"><i />{{ connection.label }}</span>
        <span class="sid">{{ session.sessionId }}</span>
      </div>

    </main>
  </div>
</template>
<style scoped>
.page { display: flex; flex-direction: column; height: 100%; background: var(--page); }
.body { flex: 1; overflow-y: auto; padding: 14px 12px calc(var(--safe-bottom) + 24px); }
.sec-label { margin: 16px 0 0; }
.sec-label:first-child { margin-top: 2px; }

.group { border-radius: var(--r-card); background: var(--bg); box-shadow: var(--shadow-1); overflow: hidden; }
.model-list {
  max-height: min(42vh, 360px); overflow-y: auto;
  overscroll-behavior: contain; -webkit-overflow-scrolling: touch;
}
.row {
  display: flex; align-items: center; gap: 11px;
  width: 100%; min-height: 56px; padding: 11px 13px;
  text-align: left; background: var(--bg);
}
.row + .row { border-top: 1px solid var(--border); }
.row:active { background: var(--fill); }

.ri {
  display: grid; place-items: center; flex-shrink: 0;
  width: 32px; height: 32px; border-radius: 9px;
  background: var(--fill-strong); color: var(--text-2);
  transition: background .16s, color .16s;
}
.ri.on { background: var(--blue-soft); color: var(--blue); }

.rt { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.rmain { font-size: 14.5px; font-weight: 550; color: var(--text); }
.rmain.mono { font-family: var(--font-mono); font-size: 13.2px; word-break: break-all; }
.rsub { font-size: 12.2px; line-height: 1.5; color: var(--text-3); }
.rsub.err { color: var(--danger, #d43d2e); }
.rside { flex-shrink: 0; font-size: 13px; color: var(--text-3); font-variant-numeric: tabular-nums; }
.tick { flex-shrink: 0; color: var(--blue); }
.arw { flex-shrink: 0; color: var(--text-3); }
.empty { padding: 15px 14px; font-size: 13px; line-height: 1.6; color: var(--text-3); }
.number-row { display: flex; align-items: center; gap: 12px; min-height: 72px; padding: 12px 13px; }
.range-inputs { display: flex; align-items: center; gap: 6px; flex-shrink: 0; color: var(--text-3); font-size: 12px; }
.range-inputs input {
  width: 72px; height: 38px; padding: 0 7px; border: 1px solid var(--border-strong);
  border-radius: var(--r-sm); background: var(--bg-input); color: var(--text);
  font-size: 13px; text-align: center; font-variant-numeric: tabular-nums;
}

.sw {
  position: relative; flex-shrink: 0;
  width: 44px; height: 26px; border-radius: 13px;
  background: var(--border-strong); transition: background .2s;
}
.sw::after {
  content: ''; position: absolute; top: 2.5px; left: 2.5px;
  width: 21px; height: 21px; border-radius: 50%;
  background: #fff; box-shadow: var(--shadow-1); transition: transform .2s;
}
.sw.on { background: var(--blue); }
.sw.on::after { transform: translateX(18px); }

.foot { display: flex; align-items: center; justify-content: center; gap: 9px; margin-top: 22px; }
.conn { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--text-3); }
.conn i { width: 6px; height: 6px; border-radius: 50%; background: var(--text-3); }
.conn.on i { background: var(--ok); }
.sid { max-width: 45vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--font-mono); font-size: 11.5px; color: var(--text-3); }
</style>
