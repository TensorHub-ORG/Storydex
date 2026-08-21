<script setup lang="ts" generic="T extends string">
/**
 * 药丸分段控件。此前 EmptyState 里写了两遍（.seg/.sitem 与 .freedom）、CatalogView
 * 里又写了一遍，三份 CSS 的容器与选中态完全一致（--fill 底、选中项 --bg + --blue +
 * --shadow-1），只有高度和字号不同——把差异收成 size，其余合并。
 *
 * 顺带修掉两处语义问题：
 *   · CatalogView 的容器写了 role="tablist"，但里面的按钮没有 role="tab"，
 *     ARIA 上是个不成立的组合（tablist 只认 tab 子项），读屏会当普通按钮念；
 *   · .freedom 是在设一个取值而不是切换面板，本来就不该是 tablist——
 *     这里用 role 显式区分，radiogroup 走 aria-checked。
 */
import CoomiIcon from '../CoomiIcon.vue'

type Item = { key: T; label: string; icon?: string }

withDefaults(defineProps<{
  items: readonly Item[]
  value: T
  /** md=34px 高 / 13.5px（首屏主控件）；sm=30px / 12.5px（页内切换）。 */
  size?: 'md' | 'sm'
  /** tablist=切换面板；radiogroup=设定一个取值。 */
  role?: 'tablist' | 'radiogroup'
  /** 没有可见标题时给读屏一个名字。 */
  ariaLabel?: string
  /** 无图标的等宽分段给一个下限，避免「沉浸/叙事/自由」宽度参差。 */
  itemMinWidth?: string
}>(), { size: 'md', role: 'tablist' })

const emit = defineEmits<{ pick: [T] }>()
</script>

<template>
  <div class="seg" :class="size" :role="role" :aria-label="ariaLabel">
    <button
      v-for="item in items"
      :key="item.key"
      class="sitem"
      :class="{ on: item.key === value }"
      :style="{ minWidth: itemMinWidth }"
      :role="role === 'tablist' ? 'tab' : 'radio'"
      :aria-selected="role === 'tablist' ? item.key === value : undefined"
      :aria-checked="role === 'radiogroup' ? item.key === value : undefined"
      @click="emit('pick', item.key)"
    >
      <CoomiIcon v-if="item.icon" :name="item.icon" :size="size === 'md' ? 15 : 14" />
      <span>{{ item.label }}</span>
    </button>
  </div>
</template>

<style scoped>
.seg {
  display: flex; gap: 2px; padding: 3px;
  /* fit-content 而不是靠 align-self：三个调用方的父容器都是纵向 flex，
     宽度 auto 会被 align-self:stretch 拉满整行。写死在这里，调用方就不用各自
     记得补一句 align-self:flex-start。外边距一律由调用方给——同一个控件在
     首屏和在列表页上下留白本来就不一样。 */
  width: fit-content;
  border-radius: var(--r-pill); background: var(--fill);
}

.sitem {
  display: inline-flex; align-items: center; justify-content: center; gap: 5px;
  border: 0; border-radius: var(--r-pill); background: none;
  color: var(--text-3); font-weight: 600;
  transition: background .16s, color .16s;
}
.seg.md > .sitem { height: 34px; padding: 0 14px; font-size: 13.5px; }
.seg.sm > .sitem { height: 30px; padding: 0 13px; font-size: 12.5px; }
/* 选中项是「浮起来的一片」：底色回到 --bg（比 --fill 亮一档）再加一层最轻的投影。 */
.sitem.on { background: var(--bg); color: var(--blue); box-shadow: var(--shadow-1); }
</style>
