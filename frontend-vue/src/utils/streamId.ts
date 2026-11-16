/**
 * Redis Stream ID（`<ms>-<seq>`）的比较 —— 与网关 `internal/broadcast/streamid.go` 同一套语义。
 *
 * 为什么客户端也要一份：SSE 的 `id:` 行就是服务端 `XADD` 返回的流 ID，而**同一条逻辑事件
 * 会被送达不止一次**：
 *
 *   1. 多实例下 `RelayEvent` 既把它写进本实例的会话流、又经 Redis pub/sub 扇出
 *      （internal/broadcast/hub.go），客户端可能先后收到两条；
 *   2. 断线重连时服务端按 `Last-Event-ID` 补发，补发窗口与实时推送天然重叠。
 *
 * 服务端那边用 `IsNewerStreamID` 兜住了"补发不越界"，但它兜不住客户端**已经处理过**的事件
 * 再被送一次 —— 那一步只有客户端知道，判据就是"这个 id 不晚于我看过的最后一条"。
 *
 * 必须按**数值**比，不能按字符串：Redis 的 seq 是变宽十进制（同一毫秒内 `-9` 之后是 `-10`），
 * 字符串比较会得出 `"…-9" > "…-10"` ⇒ 新事件被当成旧的丢掉。
 */

/** 解析 `<ms>-<seq>`；任一段不是纯十进制数字就返回 null（不是流 ID）。 */
export function parseStreamId(id: string): { ms: number; seq: number } | null {
  const dash = id.indexOf('-')
  if (dash <= 0 || dash === id.length - 1) return null
  const msPart = id.slice(0, dash)
  const seqPart = id.slice(dash + 1)
  // 用正则而不是 Number()：`Number(' 5')`、`Number('5\n')` 都会成功，而 Go 的
  // strconv.ParseInt 会失败 —— 两边判定必须一致，否则同一串 id 在两端含义不同。
  if (!/^\d+$/.test(msPart) || !/^\d+$/.test(seqPart)) return null
  const ms = Number(msPart)
  const seq = Number(seqPart)
  if (!Number.isSafeInteger(ms) || !Number.isSafeInteger(seq)) return null
  return { ms, seq }
}

/**
 * `id` 是否**严格晚于** `last`。语义与 Go 版逐条对齐（「宁可重复，不可丢弃」）：
 *
 *   - `last` 为空（首次连接）⇒ 基线不存在，一律算新；
 *   - 任一不可解析 ⇒ 无法比较，算新（不为省一次渲染去冒丢事件的风险）；
 *   - 都可解析 ⇒ 按 `(ms, seq)` 数值比较。
 */
export function isNewerStreamId(id: string, last: string): boolean {
  if (!last) return true
  const next = parseStreamId(id)
  const prev = parseStreamId(last)
  if (!next || !prev) return true
  if (next.ms !== prev.ms) return next.ms > prev.ms
  return next.seq > prev.seq
}
