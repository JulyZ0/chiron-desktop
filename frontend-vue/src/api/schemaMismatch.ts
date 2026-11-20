/**
 * schema 漂移探测（网关 `GET /healthz`）。
 *
 * ## 为什么需要它
 *
 * 网关在启动时比对「代码期望的迁移 head」与「数据库 alembic_version」。不一致时它
 * **不再静默退出**，而是只起一个最小 HTTP 服务：`/healthz` 回 200 +
 * `{status:"schema_mismatch", expected, actual, fix}`，其它路径回 503
 * （见 cmd/chiron/schema_mismatch.go）。进程活着、端口在听，所以壳层认为"网关已就绪"，
 * 前端于是照常渲染 —— 但每个业务请求都是 503。用户看到的是"界面能开、什么都点不动"。
 *
 * 这里只做一件事：探一次 `/healthz`，把 `status === "schema_mismatch"` 的那份详情
 * 取回来，由路由侧决定是否跳转。
 *
 * ## 刻意**不**做的事
 *
 * - **不**把非 `schema_mismatch` 的响应当成错误：正常网关的 `/healthz` 是 404
 *   （它注册的是 `/health` 与 `/ready`），探测失败/超时也只意味着"没发现漂移"。
 *   把它当成错误会让每一次正常导航都多一条告警。
 * - **不**在这里做重试：漂移是配置问题，重试不会自愈；重试按钮由页面提供，
 *   用户修好迁移后点一下即可。
 */
import { api } from './index'

/** 漂移详情。字段与网关 schemaMismatchBody 一一对应（见 cmd/chiron/schema_mismatch.go）。 */
export interface SchemaMismatchInfo {
  /** 代码期望的迁移 head（migrations/versions 解析结果） */
  expected: string
  /** 数据库 alembic_version 里的当前值（可能为空：从没迁移过） */
  actual: string
  /** `revision` = 版本不一致；`missing_tables` = 版本对但表缺失 */
  reason?: string
  /** reason=missing_tables 时缺失的表名 */
  missing?: string[]
  /** 能照抄执行的修复命令 */
  fix: string
}

/** 网关约定的 status 值。改名等于改协议。 */
export const SCHEMA_MISMATCH_STATUS = 'schema_mismatch'

/** 兜底修复命令：网关没给 `fix` 时也要有可执行的东西，不能显示空白。 */
export const DEFAULT_SCHEMA_FIX = 'run alembic upgrade head'

/** 探测超时。这是**非阻塞**探测，卡住比探不到更糟（会拖慢启动路径）。 */
export const SCHEMA_PROBE_TIMEOUT_MS = 4000

/**
 * 从任意 payload 里解析漂移详情；不是漂移就返回 null。
 *
 * 抽成纯函数是为了能单测：页面与路由都依赖"只有 status=schema_mismatch 才动作"这条
 * 判定 —— 若真网关的 `/healthz`（404 HTML 或 `{error:...}`）被误判，用户会被莫名其妙
 * 弹到漂移页，而且没有任何线索。
 */
export function parseSchemaMismatch(payload: unknown): SchemaMismatchInfo | null {
  if (!payload || typeof payload !== 'object') return null
  const data = payload as Record<string, unknown>
  if (data.status !== SCHEMA_MISMATCH_STATUS) return null
  const missing = Array.isArray(data.missing_tables)
    ? data.missing_tables.filter((item): item is string => typeof item === 'string')
    : undefined
  return {
    expected: typeof data.expected === 'string' ? data.expected : '',
    actual: typeof data.actual === 'string' ? data.actual : '',
    reason: typeof data.reason === 'string' && data.reason ? data.reason : undefined,
    missing: missing && missing.length > 0 ? missing : undefined,
    fix: typeof data.fix === 'string' && data.fix.trim() ? data.fix : DEFAULT_SCHEMA_FIX,
  }
}

/**
 * 探测一次网关是否处在 schema 漂移态。**任何失败都返回 null**（= 没发现漂移）。
 *
 * `validateStatus` 恒真：漂移态下非 `/healthz` 端点回 503，而我们对 `/healthz` 只关心
 * 响应体；让 axios 抛错再吞掉只会多一层无用分支。
 */
export async function probeSchemaMismatch(
  timeoutMs: number = SCHEMA_PROBE_TIMEOUT_MS,
): Promise<SchemaMismatchInfo | null> {
  try {
    const res = await api.get('/healthz', {
      timeout: timeoutMs,
      validateStatus: () => true,
      // 探针不能被任何一层的缓存挡住：用户刚跑完 migrate 就点重试，必须看到新结果
      headers: { 'Cache-Control': 'no-cache', Pragma: 'no-cache' },
    })
    return parseSchemaMismatch(res?.data)
  } catch {
    return null
  }
}
