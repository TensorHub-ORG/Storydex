<template>
  <details class="writing-guidance" open>
    <summary>自然写作指导</summary>
    <p>分别开启需要的建议，展开可修改提示词。本次要求与本书文风优先，保存后随主预设生效。</p>
    <details v-for="module in modules" :key="module.id" class="writing-guidance-module">
      <summary>
        <label @click.stop>
          <input type="checkbox" :checked="module.enabledByDefault"
            @change="update(module.id, { enabledByDefault: ($event.target as HTMLInputElement).checked })" />
          {{ module.title }}
        </label>
        <span>编辑提示词</span>
      </summary>
      <textarea :aria-label="`${module.title}提示词`" :value="module.content" rows="6"
        @input="update(module.id, { content: ($event.target as HTMLTextAreaElement).value })" />
    </details>
  </details>
</template>

<script setup lang="ts">
import { computed } from "vue";
import type { PresetDocumentPayload, PresetModulePayload } from "@/api/presets";
import { writingGuidanceFor, updateWritingGuidance } from "@/constants/writingGuidance";

const props = defineProps<{ document: PresetDocumentPayload }>();
const emit = defineEmits<{ change: [document: PresetDocumentPayload] }>();
const modules = computed(() => writingGuidanceFor(props.document));
function update(id: string, patch: Partial<PresetModulePayload>) {
  emit("change", updateWritingGuidance(props.document, id, patch));
}
</script>

<style scoped>
.writing-guidance { padding: 14px; border: 1px solid var(--border-color, #d9d9df); border-radius: 10px; }
summary { cursor: pointer; font-weight: 600; }
p { margin: 10px 0; font-size: 12px; line-height: 1.6; opacity: .7; }
.writing-guidance-module { margin-top: 10px; }
.writing-guidance-module summary { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; }
label { display: flex; gap: 8px; align-items: center; cursor: pointer; }
summary span { font-weight: 400; opacity: .6; white-space: nowrap; }
textarea { width: 100%; box-sizing: border-box; margin-top: 10px; padding: 10px; font: inherit; font-size: 13px; line-height: 1.7; color: inherit; background: transparent; border: 1px solid var(--border-color, #d9d9df); border-radius: 6px; resize: vertical; }
</style>
