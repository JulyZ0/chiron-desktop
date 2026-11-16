<script setup lang="ts">
/**
 * Chiron 品牌图形（内联 SVG）。
 *
 * 为什么用内联 SVG 而不是图片：图形整体以 `currentColor` 上色，父级只要设置
 * `color` 就能随主题（明/暗/强调色）联动 —— 位图做不到，多套 PNG 也会在主题
 * 切换时闪烁。
 *
 * 造型：圆角底板 + 开口向右的「C」弧（Chiron 首字母）+ 弧口处一个实心点
 * （象征轨迹/加速）。全部用描边与 currentColor，无硬编码色值。
 */
withDefaults(
  defineProps<{
    /** 无障碍标签：图形无文字，读屏需要它 */
    label?: string
  }>(),
  { label: 'Chiron' },
)
</script>

<template>
  <svg
    class="brand-mark"
    viewBox="0 0 32 32"
    role="img"
    :aria-label="label"
    xmlns="http://www.w3.org/2000/svg"
    focusable="false"
  >
    <!-- 圆角底板：低透明度描边，形成轻量容器而不抢前景 -->
    <rect
      x="1.25"
      y="1.25"
      width="29.5"
      height="29.5"
      rx="8.5"
      fill="none"
      stroke="currentColor"
      stroke-width="1.5"
      opacity="0.35"
    />
    <!-- 首字母 C：开口向右的弧 -->
    <path
      d="M21.6 11.1a7.3 7.3 0 1 0 0 9.8"
      fill="none"
      stroke="currentColor"
      stroke-width="2.6"
      stroke-linecap="round"
    />
    <!-- 弧口实心点：轨迹/加速的意象 -->
    <circle
      cx="22.7"
      cy="16"
      r="1.75"
      fill="currentColor"
    />
  </svg>
</template>

<style scoped>
.brand-mark {
  display: block;
  width: 24px;
  height: 24px;
  flex-shrink: 0;
  /* 颜色由父级 color 决定：SVG 内部全部用 currentColor ⇒ 随主题自动变化 */
  color: var(--text-primary);
}
</style>
