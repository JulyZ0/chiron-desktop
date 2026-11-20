/**
 * schema 漂移的**非阻塞**跳转器。
 *
 * ## 为什么要它
 *
 * 网关于 schema 漂移时只提供 `/healthz`（200）与 503（其它路径），业务路由全不可用。
 * 前端若照常渲染，用户面对的是"界面能开、每个操作都失败"，并且没有任何提示指向
 * 真正的原因（迁移没跑）。这里在**启动时与每次路由切换后**探一次 `/healthz`，
 * 只在 `status === "schema_mismatch"` 时把用户送到 `/schema-mismatch`。
 *
 * ## 三条刻意的不变量
 *
 * 1. **不阻塞正常路由**：探测是 fire-and-forget。`beforeEach` 里 `await` 一次网络请求
 *    会让每次导航都多等一个 RTT（并且探测超时会直接把导航拖 4s）。
 * 2. **只在漂移时跳**：`/healthz` 在正常网关上是 404、探测失败是 null —— 一律不动。
 *    误跳的代价极高（用户会被困在错误页，而系统其实完全正常）。
 * 3. **不自旋**：路由切换后最多每 `MIN_PROBE_INTERVAL_MS` 探一次；已在漂移页时不再探
 *    （页面上有自己的重试按钮）。否则点一下侧边栏就会打一次 `/healthz`。
 */
import type { Router } from 'vue-router'
import { probeSchemaMismatch, type SchemaMismatchInfo } from '../api/schemaMismatch'

/** 漂移页的路由名。路由表、跳转器、页面自身共用这一个常量。 */
export const SCHEMA_MISMATCH_ROUTE = 'SchemaMismatch'

/** 两次探测之间的最小间隔：够快发现状态变化，又不至于每次导航都打一次。 */
export const MIN_PROBE_INTERVAL_MS = 15_000

/** 把漂移详情折成查询参数：页面首屏无需再请求就能显示 expected/actual。 */
export function buildSchemaMismatchQuery(info: SchemaMismatchInfo): Record<string, string> {
  const query: Record<string, string> = { fix: info.fix }
  if (info.expected) query.expected = info.expected
  if (info.actual) query.actual = info.actual
  if (info.reason) query.reason = info.reason
  if (info.missing && info.missing.length > 0) query.missing = info.missing.join(',')
  return query
}

export interface SchemaMismatchWatchDeps {
  /** 探测函数（单测注入） */
  probe?: () => Promise<SchemaMismatchInfo | null>
  /** 时间源（单测注入，避免依赖真实时钟） */
  now?: () => number
}

/**
 * 装到 router 上：启动探一次 + 每次导航完成后探一次（有节流）。
 *
 * 返回 `check({ force })` 供需要时手动触发（例如错误页的"重试"）。
 */
export function setupSchemaMismatchWatch(router: Router, deps: SchemaMismatchWatchDeps = {}) {
  const probe = deps.probe || probeSchemaMismatch
  const now = deps.now || (() => Date.now())

  let inFlight = false
  let lastProbedAt = 0

  const check = (options: { force?: boolean } = {}): void => {
    if (inFlight) return
    // 已经停在漂移页：页面上有显式重试，自动探测只会重复同一请求
    if (router.currentRoute.value.name === SCHEMA_MISMATCH_ROUTE) return
    if (!options.force && now() - lastProbedAt < MIN_PROBE_INTERVAL_MS) return

    inFlight = true
    lastProbedAt = now()
    void probe()
      .then(info => {
        inFlight = false
        if (!info) return
        // 探测期间用户可能已经手动进了漂移页（或已切走）——再 replace 一次会打断他
        if (router.currentRoute.value.name === SCHEMA_MISMATCH_ROUTE) return
        void router.replace({
          name: SCHEMA_MISMATCH_ROUTE,
          query: buildSchemaMismatchQuery(info),
        })
      })
      .catch(() => {
        inFlight = false
      })
  }

  router.afterEach(() => {
    check()
  })
  // 启动即探一次：首屏可能就是漂移态，不该等用户先点一次导航
  check({ force: true })

  return { check }
}
