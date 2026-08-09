<script setup lang="ts">
/** Transparent Storydex mark generated from the canonical assets/storydex.png. */
withDefaults(defineProps<{ size?: number; muted?: boolean }>(), { size: 44, muted: false })
</script>

<template>
  <img
    class="mark"
    :class="{ muted }"
    :width="size"
    :height="size"
    src="/storydex-icon.png"
    alt=""
    aria-hidden="true"
  />
</template>

<style scoped>
.mark { display: block; flex-shrink: 0; object-fit: contain; }
.mark.muted { filter: grayscale(1); opacity: .58; }
</style>
