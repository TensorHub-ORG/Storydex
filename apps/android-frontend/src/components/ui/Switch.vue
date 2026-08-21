<script setup lang="ts">
/**
 * 开关的轨道与滑块。设置页原本有两套写法：
 *   · `.switch-row > i`（38×22，滑块用 ::after，**没有过渡**）
 *   · `.toggle`（34×21，滑块是子元素，有 .15s 过渡）
 * 同一个页面上一个瞬变一个滑动。这里合并成一个控件，差异收成 size。
 *
 * 语义也一并补上：原来两处都只是视觉元素，读屏念不出开还是关。
 * 默认这一支自己就是按钮，带 role="switch" + aria-checked；
 * 整行本身已经是按钮的场合（设置页的开关行）传 interactive=false，
 * 由外层承担语义——按钮不能套按钮。
 */
withDefaults(defineProps<{
  checked: boolean
  /** md=38×22（独立开关行）；sm=34×21（列表项右侧的启用开关）。 */
  size?: 'md' | 'sm'
  interactive?: boolean
  ariaLabel?: string
  disabled?: boolean
}>(), { size: 'md', interactive: true })

const emit = defineEmits<{ change: [boolean] }>()
</script>

<template>
  <button
    v-if="interactive"
    class="sw" :class="[size, { on: checked }]"
    type="button"
    role="switch"
    :aria-checked="checked"
    :aria-label="ariaLabel"
    :disabled="disabled"
    @click="emit('change', !checked)"
  >
    <i />
  </button>
  <!-- 外层是按钮时只画图形，状态由外层的 aria-checked 报给读屏。 -->
  <span v-else class="sw" :class="[size, { on: checked }]" aria-hidden="true"><i /></span>
</template>

<style scoped>
.sw {
  flex-shrink: 0; display: block;
  border: 0; background: var(--fill-strong);
  transition: background .15s;
}
.sw > i {
  display: block; border-radius: 50%;
  background: var(--bg); box-shadow: var(--shadow-1);
  transition: transform .15s;
}
.sw.on { background: var(--blue); }
.sw:disabled { opacity: .45; }

.sw.md { width: 38px; height: 22px; padding: 3px; border-radius: 11px; }
.sw.md > i { width: 16px; height: 16px; }
.sw.md.on > i { transform: translateX(16px); }

.sw.sm { width: 34px; height: 21px; padding: 2px; border-radius: 11px; }
.sw.sm > i { width: 17px; height: 17px; }
.sw.sm.on > i { transform: translateX(13px); }
</style>
