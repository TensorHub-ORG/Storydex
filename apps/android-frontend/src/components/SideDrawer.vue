<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { useStoryStore, type StoryFragment } from '@/stores/story'
import CoomiIcon from './CoomiIcon.vue'

defineProps<{ open: boolean }>()
const emit = defineEmits<{ close: [] }>()
const router = useRouter()
const session = useSessionStore()
const story = useStoryStore()
const editing = ref<StoryFragment | null>(null)
const editText = ref('')
const editError = ref('')
const saving = ref(false)

function continueStory() { session.continueStory(); emit('close') }
function edit(fragment: StoryFragment) {
  editing.value = fragment
  editText.value = fragment.content
  editError.value = ''
}
async function save() {
  if (!editing.value) return
  saving.value = true
  editError.value = ''
  try {
    await story.updateFragment(editing.value.id, editText.value, session.sessionId)
    editing.value = null
  } catch (error) {
    editError.value = error instanceof Error ? error.message : '保存失败，请稍后重试'
  } finally {
    saving.value = false
  }
}
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
    <aside class="panel" role="dialog" aria-label="剧情片段">
      <header class="dhead">
        <div class="project">
          <span>剧情片段</span>
          <small>{{ story.projectPath.split('/').pop() || '默认故事' }}</small>
        </div>
        <button class="icon-btn" aria-label="设置" @click="go('/settings')"><CoomiIcon name="settings" :size="19" /></button>
      </header>

      <button class="continue" @click="continueStory">
        <CoomiIcon name="play" :size="17" /><span>继续故事</span>
      </button>

      <div class="list">
        <p v-if="story.fragments.length === 0" class="empty">剧情尚未开始。完成第一轮行动后，剧情片段会按顺序收进这里。</p>
        <p v-else class="sec-label">最近五条</p>
        <button v-for="fragment in story.latestFive" :key="fragment.id" class="fragment" @click="edit(fragment)">
          <span class="filename">{{ fragment.filename }}</span>
          <span class="summary">{{ fragment.summary }}</span>
          <CoomiIcon name="pencil" :size="14" />
        </button>

        <template v-if="story.older.length">
          <button class="older-toggle" @click="story.olderExpanded = !story.olderExpanded">
            <CoomiIcon :name="story.olderExpanded ? 'chevronDown' : 'chevronRight'" :size="15" />
            <span>更早的剧情片段（{{ story.older.length }}）</span>
          </button>
          <div v-if="story.olderExpanded" class="older-list">
            <button v-for="fragment in story.older" :key="fragment.id" class="fragment" @click="edit(fragment)">
              <span class="filename">{{ fragment.filename }}</span>
              <span class="summary">{{ fragment.summary }}</span>
            </button>
          </div>
        </template>
      </div>

      <footer class="dfoot">
        <button class="console" @click="openDashboard"><CoomiIcon name="terminal" :size="20" /><span>返回控制台</span><CoomiIcon name="chevronRight" :size="17" /></button>
      </footer>
    </aside>

    <div v-if="editing" class="editor-mask" @click.self="editing = null">
      <section class="editor-sheet">
        <header><span>{{ editing.filename }}</span><button aria-label="关闭" @click="editing = null"><CoomiIcon name="close" :size="18" /></button></header>
        <textarea v-model="editText" aria-label="编辑剧情片段" />
        <p v-if="editError" class="edit-error">{{ editError }}</p>
        <button class="save" :disabled="saving" @click="save">{{ saving ? '保存中…' : '保存修改' }}</button>
      </section>
    </div>
  </div>
</template>

<style scoped>
.drawer-root { position: fixed; inset: 0; z-index: 60; pointer-events: none; }
.drawer-root.open { pointer-events: auto; }
.scrim { position: absolute; inset: 0; background: rgba(17,22,31,.34); opacity: 0; transition: opacity .28s ease; }
.drawer-root.open .scrim { opacity: 1; }
.panel { position: absolute; inset: 0 auto 0 0; display: flex; flex-direction: column; width: 84%; max-width: 350px; padding-top: var(--safe-top); background: var(--bg); box-shadow: var(--shadow-drawer); transform: translateX(-102%); transition: transform .3s cubic-bezier(.22,.68,.19,1); }
.drawer-root.open .panel { transform: none; }
.dhead { display: flex; align-items: center; min-height: 58px; padding: 8px 10px 6px 16px; }
.project { flex: 1; min-width: 0; display: flex; flex-direction: column; }
.project span { font-size: 17px; font-weight: 650; }
.project small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-3); }
.continue { display: flex; align-items: center; gap: 9px; margin: 2px 10px 8px; padding: 10px 12px; border-radius: var(--r-md); background: var(--blue-soft); color: var(--blue); font-size: 15px; font-weight: 650; }
.list { flex: 1; overflow-y: auto; padding: 6px 10px 14px; }
.empty { margin: 24px 10px; color: var(--text-3); font-size: 13.5px; line-height: 1.7; }
.fragment { display: grid; grid-template-columns: 1fr auto; width: 100%; padding: 10px; border-radius: var(--r-sm); text-align: left; }
.fragment:active { background: var(--fill); }
.filename { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--font-mono); font-size: 11px; color: var(--text-3); }
.summary { grid-column: 1 / -1; margin-top: 3px; font-size: 14px; color: var(--text); line-height: 1.5; }
.fragment :deep(svg) { color: var(--text-3); }
.older-toggle { display: flex; align-items: center; gap: 6px; width: 100%; margin-top: 10px; padding: 9px 8px; color: var(--text-2); font-size: 13px; }
.older-list { border-left: 1px solid var(--border); margin-left: 14px; padding-left: 4px; }
.dfoot { border-top: 1px solid var(--border); padding: 8px 10px calc(8px + var(--safe-bottom)); }
.console { display: flex; align-items: center; gap: 10px; width: 100%; min-height: 42px; padding: 0 8px; color: var(--blue); }
.console span { flex: 1; text-align: left; color: var(--text); font-weight: 600; }
.editor-mask { position: absolute; inset: 0; z-index: 3; display: flex; align-items: flex-end; background: rgba(17,22,31,.38); }
.editor-sheet { display: flex; flex-direction: column; width: 100%; height: min(72vh, 620px); padding: 12px 14px calc(14px + var(--safe-bottom)); border-radius: 16px 16px 0 0; background: var(--bg); }
.editor-sheet header { display: flex; align-items: center; min-height: 38px; font-family: var(--font-mono); font-size: 12px; color: var(--text-2); }
.editor-sheet header span { flex: 1; }
.editor-sheet header button { width: 36px; height: 36px; }
.editor-sheet textarea { flex: 1; width: 100%; resize: none; padding: 12px; border: 1px solid var(--border); border-radius: var(--r-sm); background: var(--fill); color: var(--text); font: 15px/1.75 var(--font-ui); }
.edit-error { margin-top: 8px; color: var(--danger); font-size: 12.5px; }
.save { min-height: 44px; margin-top: 10px; border-radius: var(--r-sm); background: var(--blue); color: white; font-size: 15px; font-weight: 650; }
.save:disabled { opacity: .55; }
</style>
