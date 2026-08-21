<script setup lang="ts">
/**
 * 底部弹层外壳。此前 9 个文件各写了一遍 scrim + panel，抄出四类分歧：
 *   · 5 处把遮罩写死成 rgba(17,22,31,.3x) 而不是 var(--scrim) —— 沉浸书卷、青瓷、
 *     亚麻和四档深色下遮罩会明显偏色，这是实打实的显示错误，不只是重复；
 *   · z-index 有 60 / 70 / 80 / 90 四种，谁压谁全靠巧合；
 *   · 圆角有 14 / 16 / 18 / 22px 四种，面板底色 --bg 与 --bg-card 混用
 *     （深色档位下这两个不是一个颜色，混用会让弹层与页面分不出层次）；
 *   · 两个文件各定义了一个语义不同的 @keyframes rise，靠 scoped 侥幸没打架。
 *
 * 行为刻意与原来保持一致：调用方仍然用 v-if 挂载/卸载本组件，所以只有进场动画、
 * 没有退场动画（Vue 把组件卸载之后没法再播）。这样迁移不必把 v-if 改成 :open，
 * 插槽里那些 `askDelete.name` 之类的写法也就不用逐个补空值判断。
 *
 * 刻意不做的事：不锁 body 滚动。全项目现在都没锁，而 body 很可能就是聊天页的滚动容器，
 * 加 overflow:hidden 在 WebView 上会把滚动位置弹回顶部——那比背景能滚更难受。
 */
import { computed } from 'vue'
import CoomiIcon from '../CoomiIcon.vue'

const props = withDefaults(defineProps<{
  title?: string
  subtitle?: string
  /** 顶部小横条。不需要的弹层（自绘标题行、编辑器）显式传 false。 */
  grip?: boolean
  /** 标题行右侧的关闭叉。 */
  closable?: boolean
  /** 点遮罩是否关闭。进行中不可打断的弹层要显式传 false。 */
  dismissible?: boolean
  /** sheet=贴底整宽贴边；card=离边 12px 的浮起卡片，四角全圆、限宽。
   *  两者都贴在底部——浮层竖直居中在手机上够不着，这里刻意不提供那种排布。 */
  variant?: 'sheet' | 'card'
  /** 固定高度（如 'min(72vh, 620px)'）。给了它内容区就按 flex 撑满，用于编辑器类弹层。 */
  height?: string
  maxWidth?: string
  /** 破坏性确认用 alertdialog：读屏会立刻打断当前朗读。 */
  role?: 'dialog' | 'alertdialog'
  /** 无标题行（或标题行是自绘的）时给读屏一个可读名字。 */
  ariaLabel?: string
}>(), {
  grip: true,
  closable: false,
  dismissible: true,
  variant: 'sheet',
  role: 'dialog',
})

const emit = defineEmits<{ close: [] }>()

const panelStyle = computed(() => ({
  height: props.height,
  maxWidth: props.maxWidth,
}))

function onScrim() {
  if (props.dismissible) emit('close')
}
</script>

<template>
  <div
    class="scrim"
    :class="variant === 'card' ? 'at-float' : 'at-end'"
    @click.self="onScrim"
  >
    <div
      class="panel"
      :class="[variant === 'card' ? 'as-card' : 'as-sheet', { 'no-grip': !grip }]"
      :style="panelStyle"
      :role="role"
      :aria-label="ariaLabel"
      aria-modal="true"
    >
      <div v-if="grip" class="grip" />

      <!-- 有 title 就用标准标题行；需要自定义（图标、右侧按钮组）的走 head 插槽。 -->
      <div v-if="title || $slots.head" class="head">
        <slot name="head">
          <div class="htext">
            <span class="title">{{ title }}</span>
            <span v-if="subtitle" class="subtitle">{{ subtitle }}</span>
          </div>
          <button v-if="closable" class="x" aria-label="关闭" @click="emit('close')">
            <CoomiIcon name="close" :size="18" />
          </button>
        </slot>
      </div>

      <slot />

      <div v-if="$slots.actions" class="actions">
        <slot name="actions" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.scrim {
  position: fixed; inset: 0;
  /* 唯一的弹层层级。左侧抽屉 60 在下，图片查看器 200 在上，中间只剩这一档。 */
  z-index: 90;
  display: flex;
  background: var(--scrim);
  animation: sheet-fade .18s ease-out;
}
.scrim.at-end { align-items: flex-end; }
.scrim.at-float { align-items: flex-end; justify-content: center; padding: 12px; }
@keyframes sheet-fade { from { opacity: 0; } }

.panel {
  display: flex; flex-direction: column;
  width: 100%;
  /* 传了 maxWidth 时靠这一行居中：scrim 是 flex 容器，flex 子项不吃 text-align，
     只有 auto 外边距会把剩余空间对半分掉。没传时 width:100% 让它无效果。 */
  margin-inline: auto;
  /* --bg-card 而不是 --bg：深色档位下前者是「浮起来的面」，弹层要比页面亮一档。 */
  background: var(--bg-card);
  box-shadow: var(--shadow-sheet);
  min-height: 0;
}
.panel.as-sheet {
  max-height: 91vh;
  padding: 6px 16px calc(var(--safe-bottom) + 16px);
  border-radius: var(--r-sheet) var(--r-sheet) 0 0;
  animation: sheet-rise .26s cubic-bezier(.2, .8, .2, 1);
}
/* 6px 顶内边距是给小横条留的（它自己还带 4px 上边距）。没有横条时内容会顶到圆角上，
   所以补齐成正常的一档内边距。 */
.panel.as-sheet.no-grip { padding-top: 18px; }
/* 浮起卡片同样贴底，只是四周留 12px（由 .at-float 的 padding 给出），所以不需要
   安全区内边距——scrim 的 padding 已经把它顶离底边。 */
.panel.as-card {
  max-width: 460px; max-height: 86vh;
  padding: 6px 18px 18px;
  border-radius: var(--r-card);
  box-shadow: var(--shadow-2);
  animation: sheet-pop .2s cubic-bezier(.2, .8, .2, 1);
}
.panel.as-card.no-grip { padding-top: 18px; }
@keyframes sheet-rise { from { transform: translateY(100%); } }
@keyframes sheet-pop { from { transform: translateY(18px); opacity: 0; } }

.grip {
  flex-shrink: 0;
  width: 38px; height: 4px; margin: 4px auto 14px;
  border-radius: 2px; background: var(--border-strong);
}

.head { display: flex; align-items: flex-start; gap: 10px; flex-shrink: 0; }
.htext { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.title { font-size: 17px; font-weight: 650; color: var(--text); }
.subtitle { font-size: 12.5px; line-height: 1.5; color: var(--text-2); }
.x {
  flex-shrink: 0; display: grid; place-items: center;
  width: 34px; height: 34px; margin: -4px -6px 0 0;
  border: 0; border-radius: var(--r-sm);
  background: none; color: var(--text-3);
}
.x:active { background: var(--fill-press); }

.actions { display: flex; gap: 8px; margin-top: 14px; flex-shrink: 0; }
/* 按钮行默认等分。插槽内容带的是调用方的 scope id，所以必须 :deep 才选得到；
   写在这里是为了让 9 个调用方都不用再各写一遍 `.xxx .btn { flex: 1 }` ——
   那种写法要穿透到本组件的内部结构，改一次内部布局就会同时崩 9 处。
   竖排按钮组的调用方自己覆写 flex-direction 与 flex 即可。 */
.actions > :deep(*) { flex: 1; min-width: 0; }
</style>
