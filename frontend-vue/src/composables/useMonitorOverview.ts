import { onScopeDispose, ref } from 'vue'

import { getMonitorOverview, type MonitorOverview, type MonitorWindow } from '@/api/admin'

/**
 * 「运行时监控」页的共享数据源。
 *
 * 三个面板（性能 / 队列 / 缓存）读的是**同一份** overview —— 后端一次采集、
 * 一个 generated_at。因此状态放在模块级单例里，而不是每个面板各拉各的：
 * 三个 Tab 各自轮询会造成三倍请求、三份时间戳，而且切 Tab 时数字会互相错位。
 *
 * 刷新节奏：10s 一次，可暂停。暂停是**停掉定时器**而不是"继续请求但忽略结果"——
 * 后者在排障时最要命：用户以为停了，后台仍在打引擎与 Redis。
 */

/** 自动刷新间隔（毫秒）。与后端 8s 采集超时配合：一次慢采集不该拖住下一轮。 */
export const MONITOR_REFRESH_MS = 10_000

/** 客户端保留的趋势点数（10s × 60 = 最近 10 分钟）。 */
const HISTORY_MAX = 60

const overview = ref<MonitorOverview | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const windowRange = ref<MonitorWindow>('15m')
const autoRefresh = ref(true)
const updatedAt = ref<string | null>(null)

/** 队列深度 / 命中率没有服务端时序，这里按本页打开以来的刷新累积（口径见 UI 文案）。 */
const history = ref<{
  queueDepth: number[]
  redisHitRate: number[]
  llmHitRate: number[]
}>({ queueDepth: [], redisHitRate: [], llmHitRate: [] })

let timer: ReturnType<typeof setInterval> | null = null
let inFlight: Promise<void> | null = null
let consumers = 0

function pushHistory(list: number[], value: number | null | undefined) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return
  list.push(value)
  if (list.length > HISTORY_MAX) list.shift()
}

function errorText(e: unknown): string {
  if (e instanceof Error && e.message) return e.message
  return String(e)
}

/** 拉一次概览。并发调用合并成一次（三个面板可能同时触发）。 */
export function refreshMonitorOverview(): Promise<void> {
  if (inFlight) return inFlight
  loading.value = true
  inFlight = (async () => {
    try {
      const data = await getMonitorOverview(windowRange.value)
      overview.value = data
      updatedAt.value = data.generated_at
      error.value = null
      const engineStream = data.queue?.streams?.find(s => s.name === 'engine')
      pushHistory(history.value.queueDepth, engineStream?.depth)
      pushHistory(history.value.redisHitRate, data.cache?.redis?.hit_rate_percent)
      pushHistory(history.value.llmHitRate, data.cache?.llm?.hit_rate_percent)
    } catch (e) {
      // 保留上一份数据：排障时"最后一次成功的数字"比一片空白有用，
      // 但 error 会一直显示，避免让人误以为看到的是最新值。
      error.value = errorText(e)
    } finally {
      loading.value = false
      inFlight = null
    }
  })()
  return inFlight
}

function stopTimer() {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}

function startTimer() {
  if (timer !== null || !autoRefresh.value) return
  timer = setInterval(() => {
    void refreshMonitorOverview()
  }, MONITOR_REFRESH_MS)
}

/** 切换时间窗：清空窗口相关状态并立刻重取（不等待下一个 tick）。 */
export function setMonitorWindow(next: MonitorWindow): void {
  if (windowRange.value === next) return
  windowRange.value = next
  void refreshMonitorOverview()
}

/** 开/关自动刷新。开启时立刻补一次，避免"刚打开却看到几秒前的旧值"。 */
export function setMonitorAutoRefresh(on: boolean): void {
  autoRefresh.value = on
  if (on) {
    startTimer()
    void refreshMonitorOverview()
  } else {
    stopTimer()
  }
}

/**
 * 供测试重置模块级状态。生产代码不应调用 —— 单例的生命周期就是页面/应用的生命周期。
 */
export function resetMonitorOverviewForTest(): void {
  stopTimer()
  inFlight = null
  consumers = 0
  overview.value = null
  loading.value = false
  error.value = null
  windowRange.value = '15m'
  autoRefresh.value = true
  updatedAt.value = null
  history.value = { queueDepth: [], redisHitRate: [], llmHitRate: [] }
}

/** 面板/工具栏共用：首次使用时启动轮询，最后一个使用者卸载时停掉。 */
export function useMonitorOverview() {
  consumers++
  startTimer()
  if (overview.value === null && !inFlight) {
    void refreshMonitorOverview()
  }
  onScopeDispose(() => {
    consumers--
    if (consumers <= 0) {
      consumers = 0
      stopTimer()
    }
  })
  return {
    overview,
    loading,
    error,
    windowRange,
    autoRefresh,
    updatedAt,
    history,
    refresh: refreshMonitorOverview,
    setWindow: setMonitorWindow,
    setAutoRefresh: setMonitorAutoRefresh,
  }
}
