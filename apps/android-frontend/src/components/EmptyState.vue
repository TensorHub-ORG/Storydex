<script setup lang="ts">
/**
 * 空态首屏：品牌标记 + 模式分段控件 + 任务建议。
 * 三个模式不是装饰，各自映射到真实命令：
 *   快速 → set_permission_mode('auto')；计划 → enter_plan_mode；谨慎 → set_permission_mode('ask')。
 */
import { computed } from 'vue'
import { useSessionStore } from '@/stores/session'
import { useStoryStore, type AgentMode, type NarrativeMode } from '@/stores/story'
import { useConnectionStore } from '@/stores/connection'
import CoomiIcon from './CoomiIcon.vue'
import CoomiMark from './CoomiMark.vue'

const session = useSessionStore()
const story = useStoryStore()
const connection = useConnectionStore()

const MODES: { key: AgentMode; label: string; icon: string; desc: string }[] = [
  { key: 'story', label: '剧情', icon: 'sparkle', desc: '沉浸式推进剧情，拒绝明确 OOC 与越权操控' },
  { key: 'narrator', label: '旁白', icon: 'article', desc: '作为故事内的系统面板，只解说、不续写' },
  { key: 'agent', label: 'Agent', icon: 'bolt', desc: '不受剧情约束的完整 Coomi Agent' },
]

const FREEDOM: { key: NarrativeMode; label: string }[] = [
  { key: 'immersive', label: '沉浸' }, { key: 'narrative', label: '叙事' }, { key: 'free', label: '自由' },
]
const suggestions = computed(() => story.latest?.suggestions ?? ['回顾我现在的处境', '观察眼前的人', '检查周围环境', '迈出下一步'])

const hint = computed(() => MODES.find(m => m.key === story.agentMode)?.desc ?? '')

function pick(key: AgentMode) {
  story.setAgentMode(key)
  session.setPermissionMode(key === 'agent' ? 'full' : 'auto')
}
</script>

<template>
  <div class="empty">
    <CoomiMark :size="52" class="logo" />
    <h1>剧情可以怎么发展？</h1>
    <p class="sub">{{ story.latest?.summary || '选择一种行动，让这个故事从这里持续向前。' }}</p>

    <p v-if="connection.demo" class="demobar">
      <CoomiIcon name="alert" :size="14" />
      <span>演示模式：对话由脚本驱动，只用来预览界面，不会真的执行任何命令。</span>
    </p>

    <div class="seg" role="tablist">
      <button
        v-for="m in MODES"
        :key="m.key"
        class="sitem"
        :class="{ on: story.agentMode === m.key }"
        role="tab"
        :aria-selected="story.agentMode === m.key"
        @click="pick(m.key)"
      >
        <CoomiIcon :name="m.icon" :size="15" />
        <span>{{ m.label }}</span>
      </button>
    </div>
    <p class="hint">{{ hint }}</p>

    <div v-if="story.agentMode !== 'agent'" class="freedom" aria-label="剧情控制强度">
      <button v-for="m in FREEDOM" :key="m.key" :class="{ on: story.narrativeMode === m.key }" @click="story.setNarrativeMode(m.key)">{{ m.label }}</button>
    </div>

    <div class="sugs">
      <button
        v-for="(s, i) in suggestions"
        :key="s"
        class="sug cascade"
        :style="{ animationDelay: 40 * i + 'ms' }"
        @click="session.sendMessage(s)"
      >
        <span class="sicon"><CoomiIcon name="sparkle" :size="17" /></span>
        <span class="stext">{{ s }}</span>
        <CoomiIcon name="chevronRight" :size="14" class="sarrow" />
      </button>
    </div>
  </div>
</template>

<style scoped>
.empty {
  margin: auto 0; padding: 22px 4px 8px;
  display: flex; flex-direction: column; align-items: center;
  text-align: center;
}
.logo { margin-bottom: 14px; }
h1 { font-size: 21px; font-weight: 600; letter-spacing: -.3px; color: var(--text); }
.sub {
  max-width: 268px; margin-top: 8px;
  font-size: 13.5px; line-height: 1.65; color: var(--text-3);
}
.demobar {
  display: flex; align-items: flex-start; gap: 7px;
  max-width: 320px; margin-top: 14px; padding: 9px 12px;
  border-radius: var(--r-md); background: var(--orange-soft);
  font-size: 12.5px; line-height: 1.55; color: #8a4a30; text-align: left;
}
.demobar :deep(svg) { flex-shrink: 0; margin-top: 1px; color: var(--orange); }

.seg {
  display: flex; gap: 2px; margin-top: 20px; padding: 3px;
  border-radius: var(--r-pill); background: var(--fill);
}
.sitem {
  display: inline-flex; align-items: center; gap: 5px;
  height: 34px; padding: 0 14px;
  border: 0; border-radius: var(--r-pill); background: none;
  font-size: 13.5px; font-weight: 600; color: var(--text-3);
  transition: background .16s, color .16s;
}
.sitem.on { background: var(--bg); color: var(--blue); box-shadow: var(--shadow-1); }
.hint { min-height: 17px; margin-top: 10px; font-size: 12px; color: var(--text-3); }
.freedom { display: flex; gap: 2px; padding: 3px; border-radius: var(--r-pill); background: var(--fill); }
.freedom button { min-width: 62px; height: 30px; border-radius: var(--r-pill); font-size: 12.5px; color: var(--text-3); }
.freedom button.on { background: var(--bg); color: var(--blue); box-shadow: var(--shadow-1); }

.sugs { width: 100%; display: flex; flex-direction: column; gap: 8px; margin-top: 18px; }
.sug {
  display: flex; align-items: center; gap: 11px;
  padding: 12px 12px 12px 11px;
  border: 1px solid var(--border); border-radius: var(--r-card);
  background: var(--bg); text-align: left;
}
.sug:active { background: var(--fill); }
.sicon {
  display: grid; place-items: center; flex-shrink: 0;
  width: 32px; height: 32px; border-radius: 10px;
  background: var(--blue-soft); color: var(--blue);
}
.stext { flex: 1; font-size: 14.5px; line-height: 1.4; color: var(--text); }
.sarrow { color: var(--text-3); }
</style>

