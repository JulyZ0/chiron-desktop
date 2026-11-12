<script setup lang="ts">
/**
 * 组件健康与内存明细面板。
 *
 * 为什么单独一个组件：状态栏只有 26px 高，塞不下五个依赖的可用性与占用；
 * 而"到底哪个组件挂了/内存去哪了"恰恰是排查时最需要的。这里按组件分行，
 * 每行给出**可用性 + 该组件自己的体积指标**，取不到的指标显示「—」而不是 0。
 *
 * 数据来自 `GET /v1/system/components`（网关聚合 pg / redis / engine / gateway）。
 */
import { computed } from 'vue'
import { isComponentUp, type ComponentsHealth } from '@/api/systemHealth'

const props = defineProps<{
  components?: ComponentsHealth | null
}>()

/** 字节人类可读。负数/非有限值一律「—」，避免把 NaN 显示成内存用量。 */
function formatBytes(v: unknown): string {
  if (typeof v !== 'number' || !Number.isFinite(v) || v < 0) return '—'
  if (v < 1024) return `${v} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let n = v / 1024
  let i = 0
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i++
  }
  return `${n < 10 ? n.toFixed(1) : Math.round(n)} ${units[i]}`
}

/** 取某个组件的某个字节型指标（路径可能是顶层或嵌在 memory 下）。 */
function bytesOf(group: string, key: string): string {
  const mem = (props.components as Record<string, unknown> | null | undefined)?.memory as
    | Record<string, Record<string, unknown>>
    | undefined
  return formatBytes(mem?.[group]?.[key])
}

interface Row {
  key: string
  /** 技术名词，按原样展示（postgres / redis / engine / gateway），不翻译 */
  label: string
  up: boolean
  /** 该组件的明细行（"标签：值"），只列取得到的 */
  details: string[]
  error: string
}

const rows = computed<Row[]>(() => {
  const c = props.components
  if (!c) return []
  const mk = (key: string, label: string): Row => {
    const h = c[key as keyof ComponentsHealth]
    const details: string[] = []
    return { key, label, up: isComponentUp(h), details, error: (h?.ping_error || h?.error || h?.reason || '') as string }
  }

  const pg = mk('postgres', 'postgres')
  const pgConns = (props.components as Record<string, unknown> | null)?.memory
    ? (props.components as unknown as { memory?: Record<string, Record<string, unknown>> }).memory?.postgres?.connections
    : undefined
  pg.details.push(`DB: ${bytesOf('postgres', 'database_bytes')}`)
  pg.details.push(`connections: ${typeof pgConns === 'number' ? pgConns : '—'}`)

  const rd = mk('redis', 'redis')
  rd.details.push(`used: ${bytesOf('redis', 'used_memory_bytes')}`)

  const en = mk('engine', 'python-engine')
  en.details.push(`RSS: ${bytesOf('engine', 'rss_bytes')}`)

  const gw = mk('gateway', 'gateway')
  const gwh = c.gateway as Record<string, unknown> | undefined
  gw.up = true // 能返回本响应就说明网关进程在跑
  gw.details.push(`heap: ${bytesOf('gateway', 'heap_bytes')}`)
  gw.details.push(`sys: ${bytesOf('gateway', 'sys_bytes')}`)
  if (typeof gwh?.goroutines === 'number') gw.details.push(`goroutines: ${gwh.goroutines}`)
  if (typeof gwh?.uptime_seconds === 'number') gw.details.push(`uptime: ${Math.round(gwh.uptime_seconds)}s`)

  return [pg, rd, en, gw]
})
</script>

<template>
  <div class="chp">
    <div
      v-if="!components"
      class="chp-empty"
    >
      —
    </div>
    <ul
      v-else
      class="chp-list"
    >
      <li
        v-for="row in rows"
        :key="row.key"
        class="chp-row"
      >
        <span
          class="chp-dot"
          :class="{ down: !row.up }"
          aria-hidden
        />
        <span class="chp-name">{{ row.label }}</span>
        <span class="chp-details">{{ row.details.filter(Boolean).join(' · ') }}</span>
        <span
          v-if="row.error"
          class="chp-err"
          :title="row.error"
        >{{ row.error }}</span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.chp { font-size: var(--fs-sm); color: var(--text-secondary); }
.chp-empty { color: var(--text-muted); text-align: center; padding: 12px 0; }
.chp-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.chp-row { display: grid; grid-template-columns: 8px 96px 1fr; align-items: center; gap: 8px; }
.chp-dot { width: 8px; height: 8px; border-radius: var(--radius-full); background: var(--success, #22c55e); }
.chp-dot.down { background: var(--danger, #ef4444); }
.chp-name { font-family: var(--font-mono); color: var(--text-primary); }
.chp-details { color: var(--text-tertiary); font-variant-numeric: tabular-nums; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chp-err { grid-column: 2 / -1; color: var(--warning); font-size: var(--fs-xs); overflow: hidden; text-overflow: ellipsis; }
</style>
