/**
 * 逐组件健康态（网关 `GET /v1/system/components`）。
 *
 * 为什么需要它：状态栏的「已连接」此前只取浏览器 `navigator.onLine` 与一次
 * `/ping` 探测 —— 浏览器有网、但 postgres/redis/引擎任一挂掉时，用户仍然看到
 * 「已连接」，然后每次提问都失败。这里把每个依赖的真实可用性拿回来，
 * 状态栏据此显示，并能点开看是哪个组件挂了。
 */
import { api } from './index'

/** 单个组件的健康态。字段因组件而异，故用宽松类型 + 明确的取值函数。 */
export interface ComponentHealth {
  available?: boolean
  healthy?: boolean
  ping_ok?: boolean
  ping_error?: string
  error?: string
  reason?: string
  [key: string]: unknown
}

export interface ComponentsHealth {
  postgres?: ComponentHealth
  redis?: ComponentHealth
  engine?: ComponentHealth
  gateway?: ComponentHealth
  /**
   * 各组件内存/体积明细，键为组件名，值为该组件自己的指标。
   *
   * 后端**取不到的组件直接省略这个键**（见 `componentMemory`），因此这里的属性
   * 一律可选 —— 缺失表示"没采到"，而不是 0。
   */
  memory?: Record<string, Record<string, unknown>>
}

/**
 * 判定一个组件是否健康。
 *
 * 三者的字段名各不相同（DB/Redis 用 `available`、引擎用 `healthy`），
 * 且 `ping_ok` 比 `available` 更严格（连接池存在但 ping 失败仍算不可用）。
 * 这里统一成"**只有明确健康才算健康**"，缺字段一律视为不可用 ——
 * 宁可误报异常，也不要让用户以为能用。
 */
export function isComponentUp(h?: ComponentHealth | null): boolean {
  if (!h) return false
  if (h.ping_ok === false) return false
  return h.healthy === true || h.available === true
}

/** 拉取一次组件健康态。失败返回 null（调用方保留上次结果，不要显示成"全部正常"）。 */
export async function fetchComponentsHealth(): Promise<ComponentsHealth | null> {
  try {
    // 路径必须带 `/v1`：网关只在 `GET /v1/system/components` 注册了这条路由
    // （internal/api/routes_system.go），`/system/components` 是 404 —— 而下面 catch 会把
    // 404 吞成 null，面板于是永远显示「—」，看起来像"后端没采到数据"。
    // axios 的 baseURL 是 `API_URL`（不带 /v1），所以前缀必须在这里写全。
    const res = await api.get('/v1/system/components', { timeout: 6000 })
    // 网关统一用 `OK()` 包装成 `{success, data}`（见 internal/api/response.go）。
    // 这里**必须再解一层 `.data`**：早前取 `res.data` 拿到的是包装体本身，
    // 里面根本没有 postgres/redis/engine/gateway 这几个键，于是面板每行都是「—」，
    // 看起来像"组件健康没有数据"，实际上是取错了层级。
    const data = res?.data?.data ?? res?.data
    return (data && typeof data === 'object') ? (data as ComponentsHealth) : null
  } catch {
    return null
  }
}
