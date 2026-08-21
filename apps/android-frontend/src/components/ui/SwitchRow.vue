<script setup lang="ts">
/**
 * 设置页的开关行：整行可点，右侧一个开关。设置页里这个形状重复了 13 次，
 * 每次都是一整行 `<button class="switch-row" @click="…"><span><b>标题</b><small>说明</small>…`，
 * 而且必须在 @click 里把 :class 上刚判断过的那个值再取反写一遍。
 *
 * 收成组件后：
 *   · role="switch" + aria-checked 只写一处（原来 13 个裸 button 对读屏完全不报开关状态）；
 *   · change 事件直接给出「切换后的值」，调用方不用再自己取反。
 *
 * 行的尺寸刻意与 SettingsView 的 .row / .field 对齐（min-height 58 / padding 10 13），
 * 三种行混排在同一个 .group 里必须一样高。分隔线由 .group.compact > * + * 提供。
 */
import Switch from './Switch.vue'

defineProps<{
  label: string
  desc?: string
  checked: boolean
  disabled?: boolean
}>()

const emit = defineEmits<{ change: [boolean] }>()
</script>

<template>
  <button
    class="switch-row"
    type="button"
    role="switch"
    :aria-checked="checked"
    :disabled="disabled"
    @click="emit('change', !checked)"
  >
    <span class="txt">
      <b>{{ label }}</b>
      <small v-if="desc">{{ desc }}</small>
    </span>
    <Switch :checked="checked" :interactive="false" />
  </button>
</template>

<style scoped>
.switch-row {
  display: flex; align-items: center; gap: 12px;
  width: 100%; min-height: 58px; padding: 10px 13px;
  text-align: left; color: var(--text);
}
.switch-row:disabled { opacity: .45; }
.txt { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 3px; }
b { font-size: 13.5px; font-weight: 650; }
small { color: var(--text-3); font-size: 11.5px; line-height: 1.45; }
</style>
