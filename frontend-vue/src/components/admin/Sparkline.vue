<script setup lang="ts">
import { computed } from 'vue'

/**
 * 轻量 SVG sparkline —— 不引图表库。
 *
 * 为什么不复用 ECharts：这三个面板的趋势只是"一根线 + 一个基线"，ECharts 每个实例
 * 都要 canvas、resize 监听与主题注册，一页三张图在低配机器上比数据本身还重。
 *
 * 数据不足时**不画假线**：
 *   - values 为空 → 渲染「—」（由调用方给的 emptyLabel 决定文案）；
 *   - 只有一个点 → 画一条水平短线段（一个点的位置），而不是看起来像"归零"的斜线。
 */
const props = withDefaults(
  defineProps<{
    values: number[]
    /** 曲线含义：同时作为 svg 的 aria-label（读屏用户需要知道这条线代表什么） */
    label: string
    /** 未知/无数据时的占位文案 */
    emptyLabel: string
    /** 危险色（错误率等"越低越好"且高值告警的序列） */
    tone?: 'accent' | 'danger' | 'success'
    height?: number
  }>(),
  { tone: 'accent', height: 44 },
)

const W = 100
const H = 30

const drawn = computed(() => {
  const vals = props.values.filter(v => Number.isFinite(v))
  if (vals.length === 0) return { points: '', last: null as { x: number; y: number } | null }
  const min = Math.min(...vals)
  const max = Math.max(...vals)
  // 全等（含全 0）时画在中间：压到底部会让"一直为 0"看起来像"没有数据"。
  const span = max - min
  const yOf = (v: number) => {
    if (span === 0) return H / 2
    return H - ((v - min) / span) * (H - 2) - 1
  }
  const step = vals.length === 1 ? 0 : W / (vals.length - 1)
  const pts = vals.map((v, i) => `${(i * step).toFixed(2)},${yOf(v).toFixed(2)}`)
  const last = {
    x: (vals.length - 1) * step,
    y: yOf(vals[vals.length - 1]),
  }
  if (vals.length === 1) {
    // 单点：从 0 画到 100 的水平线，明确"只有一个采样点"。
    return { points: `0,${last.y.toFixed(2)} ${W},${last.y.toFixed(2)}`, last }
  }
  return { points: pts.join(' '), last }
})
</script>

<template>
  <div
    class="sparkline"
    :class="`sparkline--${tone}`"
    :style="{ height: `${height}px` }"
  >
    <span
      v-if="!drawn.points"
      class="sparkline__empty"
    >{{ emptyLabel }}</span>
    <svg
      v-else
      :viewBox="`0 0 ${W} ${H}`"
      preserveAspectRatio="none"
      role="img"
      :aria-label="label"
      data-test="sparkline"
    >
      <polyline
        :points="drawn.points"
        fill="none"
        stroke="currentColor"
        stroke-width="1.5"
        stroke-linecap="round"
        stroke-linejoin="round"
        vector-effect="non-scaling-stroke"
      />
      <circle
        v-if="drawn.last"
        :cx="drawn.last.x"
        :cy="drawn.last.y"
        r="1.6"
        fill="currentColor"
      />
    </svg>
  </div>
</template>

<style scoped>
.sparkline {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--primary);
}

.sparkline--danger { color: var(--error); }
.sparkline--success { color: var(--success); }

.sparkline svg {
  width: 100%;
  height: 100%;
  display: block;
  overflow: visible;
}

.sparkline__empty {
  color: var(--text-quaternary);
  font-size: var(--fs-xs, 12px);
}
</style>
