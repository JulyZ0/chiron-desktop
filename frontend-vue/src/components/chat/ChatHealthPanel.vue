<script setup lang="ts">
/**
 * 组件健康与内存明细面板。
 *
 * 为什么单独一个组件：状态栏只有 26px 高，塞不下五个依赖的可用性与占用；
 * 而"到底哪个组件挂了/内存去哪了"恰恰是排查时最需要的。这里按组件分行，
 * 每行给出**可用性 + 该组件自己的体积指标**，取不到的指标显示「—」而不是 0。
 *
 * 数据来自 `GET /v1/system/components`（网关聚合 pg / redis / engine / gateway）。
 *
 * ## 为什么这里能重启组件
 *
 * 引擎/网关卡死时，此前用户唯一的选择是"重启整个应用"—— 那会连带着把本地栈
 * 全部拆掉重建（端口变化、页面重载）。而它们本来就是 Electron 拉起的子进程，
 * 单独重建一个既快又不影响别的东西。按钮只在桌面壳里出现（Web 端没有子进程管理）。
 *
 * 只开放 `restartProcess` 真正支持的那几个：postgres / redis 由引导器托管，
 * 重启它们会让整个数据栈断连，那是"重启本地后端"的职责，不是这里。
 */
import { computed, ref } from 'vue'
import { message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { isComponentUp, type ComponentsHealth } from '@/api/systemHealth'
import { useDeviceStore } from '@/stores/device'

const { t } = useI18n()
const device = useDeviceStore()

const props = defineProps<{
  components?: ComponentsHealth | null
}>()

/** 正在重启的组件 key（同一时刻只允许一个，避免并发重建把端口抢乱）。 */
const restarting = ref('')

/** 能否对这一行执行单组件重启：只有桌面壳 + `restartProcess` 支持的组件才有入口。 */
function canRestart(key: string): boolean {
  if (!device.isDesktop) return false
  if (restarting.value) return false
  return key === 'engine' || key === 'gateway'
}

async function restart(key: string): Promise<void> {
  if (!canRestart(key)) return
  restarting.value = key
  try {
    const result = await device.restartComponent(key)
    if (result.ok) {
      message.success(t('common.component_restart_ok', { name: key }))
    } else if (result.reason === 'unsupported') {
      message.warning(t('common.component_restart_unsupported'))
    } else {
      message.error(
        t('common.component_restart_failed', {
          detail: result.message || result.reason || 'unknown',
        }),
      )
    }
  } finally {
    restarting.value = ''
  }
}

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
        <button
          v-if="canRestart(row.key)"
          type="button"
          class="chp-btn"
          :disabled="Boolean(restarting)"
          :aria-busy="restarting === row.key"
          @click="restart(row.key)"
        >
          {{ restarting === row.key ? $t('common.component_restarting') : $t('common.component_restart') }}
        </button>
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
/* 4 列：状态点 / 名称 / 明细（吃掉剩余宽度）/ 重启按钮。按钮列用 auto，
   没有按钮的行（postgres / redis）自然塌缩，不会留下空洞。 */
.chp-row { display: grid; grid-template-columns: 8px 96px 1fr auto; align-items: center; gap: 8px; }
.chp-dot { width: 8px; height: 8px; border-radius: var(--radius-full); background: var(--success, #22c55e); }
.chp-dot.down { background: var(--danger, #ef4444); }
.chp-name { font-family: var(--font-mono); color: var(--text-primary); }
.chp-details { color: var(--text-tertiary); font-variant-numeric: tabular-nums; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chp-btn {
  padding: 1px 8px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-secondary);
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: background var(--dur-fast), color var(--dur-fast), border-color var(--dur-fast);
}
.chp-btn:hover:not(:disabled) { background: var(--bg-card); color: var(--primary); border-color: var(--primary); }
.chp-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.chp-btn:focus-visible { outline: 2px solid var(--border-focus); outline-offset: 1px; }
.chp-err { grid-column: 2 / -1; color: var(--warning); font-size: var(--fs-xs); overflow: hidden; text-overflow: ellipsis; }
</style>
