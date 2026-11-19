/**
 * 分享链接的唯一出口。
 *
 * 此前 `/share/{id}` 这个前缀散落在三处各自拼接（`useChatShare` / `useSettingsShares` /
 * 后台 `SharesView`），任一处改动都会让不同入口发出去的链接指向不同地址 —— 而分享链接
 * 一旦发出去就收不回来，改前缀只能靠**新增别名**而不是改旧路径。
 *
 * 因此：
 * - `SHARE_PATH_PREFIX` 是唯一的短链前缀，`/s/:token` 与旧 `/share/:id` 都由路由
 *   提供（旧路径保留为兼容别名，已发出的链接不失效）；
 * - 对外发链接一律走 `shareUrl()`，不要在调用点自己拼 `window.location.origin`。
 *
 * 用 `encodeURIComponent`：share id 目前是服务端生成的 token（URL 安全），但链接是
 * 用户可见产物，不该因为将来换了字符集就造出一个断链。
 */
export const SHARE_PATH_PREFIX = '/s/'

/** 站内分享路径（相对，用于 router.push / <a href>）。 */
export function sharePath(token: string): string {
  return `${SHARE_PATH_PREFIX}${encodeURIComponent(token)}`
}

/**
 * 完整的可复制分享链接。
 *
 * `origin` 可注入（测试 / 桌面壳 file:// 场景），缺省取当前页面 origin。
 * 空 token 返回空串而不是 `.../s/`：调用点普遍写成 `copyShareLink(shareUrl(id))`，
 * 拿到一个"指向分享列表根路径"的链接比拿到空串更难排查。
 */
export function shareUrl(token: string, origin?: string): string {
  const t = String(token ?? '').trim()
  if (!t) return ''
  const base = origin ?? (typeof window !== 'undefined' ? window.location.origin : '')
  // 桌面壳以 `file://` 打开构建产物，此时 `location.origin` 是字符串 `"null"` ——
  // 拼出来就是 `null/s/xxx` 这种谁点都打不开的链接。这种环境下没有对外的站点
  // 地址可用（本地模式的网关地址只在进程内），退化为站内相对路径：至少复制出去的
  // 是一段不会被误当真链接的文本，而不是一个看着像 URL 的坏 URL。
  if (!base || base === 'null') return sharePath(t)
  return `${base}${sharePath(t)}`
}
