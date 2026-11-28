<script setup lang="ts">
/**
 * 单行文本跑马灯：**只有真正溢出时才滚动**。
 *
 * 用在标题栏的项目标签上：项目名 + 当前会话名合并后经常超过标签宽度，而标签宽度
 * 必须保持不变（否则一长名字就把同行其它入口挤走）。用 `…` 截断会永久丢掉后半段，
 * 所以改为滚动播放；短文本保持完全静态，避免整条标题栏无谓地晃动。
 *
 * 三条边界：
 *  1. **读屏只读一遍**：复制出来用于无缝循环的那份带 `aria-hidden`。
 *  2. **悬停 / 聚焦暂停**：滚动的目的是"看得全"，能停下来读才有意义。
 *  3. **减少动效时退化为静态截断**（`prefers-reduced-motion`）：不做无限动画。
 *
 * 时长按 `宽度 / speed` 计算而不是固定值 —— 固定时长会让长文本滚得过快、短文本过慢。
 * 计算出的秒数只出现在内联自定义属性里，动画声明本身仍走 `--dur-*` token 兜底
 * （见 scripts/check-motion-tokens.mjs）。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    text: string
    /** 滚动速度（px/s） */
    speed?: number
  }>(),
  { speed: 32 },
)

const host = ref<HTMLElement | null>(null)
const content = ref<HTMLElement | null>(null)
const overflowing = ref(false)
/** 滚动时长（秒）；0 = 不滚动 */
const duration = ref(0)
const reducedMotion = ref(false)

let observer: ResizeObserver | null = null
let motionQuery: MediaQueryList | null = null

const scrolling = computed(() => overflowing.value && !reducedMotion.value)

function measure(): void {
  const h = host.value
  const c = content.value
  if (!h || !c) return
  // 容器宽度为 0 时（隐藏的标签页、展开动画首帧）不做判断，等 RO 再来一次
  if (h.clientWidth === 0) return
  const over = c.scrollWidth > h.clientWidth + 1
  overflowing.value = over
  if (!over) {
    duration.value = 0
    return
  }
  const speed = props.speed > 0 ? props.speed : 32
  const secs = c.scrollWidth / speed
  duration.value = Math.min(30, Math.max(6, Math.round(secs * 10) / 10))
}

function syncMotion(): void {
  reducedMotion.value = !!motionQuery?.matches
  void nextTick(measure)
}

onMounted(() => {
  if (typeof window.matchMedia === 'function') {
    motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
    reducedMotion.value = motionQuery.matches
    motionQuery.addEventListener('change', syncMotion)
  }
  // jsdom 没有 ResizeObserver：单测里退化为"挂载时测量一次"，不会抛错
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(measure)
    if (host.value) observer.observe(host.value)
    if (content.value) observer.observe(content.value)
  }
  measure()
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  motionQuery?.removeEventListener('change', syncMotion)
  motionQuery = null
})

watch(
  () => props.text,
  () => {
    // 切到别的项目/会话后内容换了一整段：先让 DOM 落地再量
    void nextTick(measure)
  },
)
</script>

<template>
  <span
    ref="host"
    class="marquee"
    :class="{ 'is-scrolling': scrolling }"
    :style="scrolling ? { '--marquee-dur': `${duration}s` } : undefined"
  >
    <span
      class="marquee__track"
      :class="{ 'is-scrolling': scrolling }"
    >
      <span
        ref="content"
        class="marquee__text"
      >{{ text }}</span>
      <!-- 无缝循环的第二份：读屏跳过，避免同一句话被念两遍 -->
      <span
        v-if="scrolling"
        class="marquee__text"
        aria-hidden="true"
      >{{ text }}</span>
    </span>
  </span>
</template>

<style scoped>
.marquee {
  display: block;
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  white-space: nowrap;
}

.marquee__track {
  display: block;
  min-width: 0;
}

/* 溢出且允许动效时才切换成"两份并排"的轨道 */
.marquee__track.is-scrolling {
  display: inline-flex;
  gap: 24px;
  /* 轨道总宽 = 2×文本 + 24px；平移 (50% + 12px) 恰好等于一个周期（文本 + 间距），
     于是第二份接上第一份时看不出接缝。 */
  animation: chiron-marquee-scroll var(--marquee-dur, var(--dur-slow)) linear infinite;
}

.marquee__text {
  display: block;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 滚动时每份文本都不能被压缩，否则两段宽度不等会出现跳变 */
.marquee__track.is-scrolling .marquee__text {
  flex: none;
  overflow: visible;
  text-overflow: clip;
}

/* 悬停或键盘聚焦时暂停：滚动的意义是读全，能停下来才有用 */
.marquee:hover .marquee__track.is-scrolling,
.marquee:focus-within .marquee__track.is-scrolling {
  animation-play-state: paused;
}

@keyframes chiron-marquee-scroll {
  from { transform: translateX(0); }
  to { transform: translateX(calc(-50% - 12px)); }
}

/* RTL：内容从右向左排，第二份在左侧，需要往右平移一个周期才能露出来 */
[dir='rtl'] .marquee__track.is-scrolling {
  animation-name: chiron-marquee-scroll-rtl;
}

@keyframes chiron-marquee-scroll-rtl {
  from { transform: translateX(0); }
  to { transform: translateX(calc(50% + 12px)); }
}

/* 系统要求减少动效：不做无限滚动，退化为静态截断 + title 原生提示 */
@media (prefers-reduced-motion: reduce) {
  .marquee__track.is-scrolling {
    display: block;
    animation: none;
  }
}
</style>
