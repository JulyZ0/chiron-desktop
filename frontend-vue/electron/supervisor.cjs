'use strict'
/**
 * 子进程崩溃自愈：退避、预算、放弃。
 *
 * 为什么单独成文件而不是塞进 `local-stack.cjs`：这里的判定（什么时候该重启、什么时候
 * 该放弃）是**纯决策**，用假的时钟就能把三种故障全部跑一遍；而 `local-stack.cjs` 里
 * 全是 spawn 与探针，任何用例都要拖上进程。决策与执行分开，边界才测得动。
 *
 * 三条判定各自对应一个真实故障：
 *
 * 1. **主动关停不重启**。退出应用时我们 SIGTERM 子进程，若把它当崩溃就会在关停过程中
 *    再拉起一个新的 —— 表现为"退出了应用，后台还留着一个引擎进程"。
 * 2. **连续崩溃超过预算就放弃**。无限重启只会把 CPU 与磁盘烧掉，而用户**仍然得不到
 *    任何结论**。放弃 + 明确通知才是可接受的终局。
 * 3. **稳定运行足够久之后预算清零**。跑了一整天的引擎偶发挂第三次，不该因为累计计数
 *    就永久放弃；反过来，起动即崩（永远到不了稳定期）三次就停，避免死循环刷日志。
 */

/** 默认退避表（毫秒）：与 `desktop-local/app/components/manager.py` 的 `RESTART_BACKOFF` 同口径 */
const DEFAULT_BACKOFF_MS = [1000, 2000, 4000]

/** 默认重启预算：与 `manager.py` 的 `MAX_CRASH_RESTARTS` 同口径 */
const DEFAULT_MAX_RESTARTS = 3

/** 稳定运行判定窗口：与 `manager.py` 的 `STABLE_UPTIME` 同口径 */
const DEFAULT_STABLE_MS = 60_000

/**
 * @param {object} options
 * @param {(role: string, attempt: number, info: object) => Promise<{ok: boolean, [k: string]: any}>} options.restart
 *   真正执行重启的回调。**必须**在同一个端口上重建，否则对端（网关↔引擎）的固定地址会失效。
 * @param {(role: string, info: object) => void} [options.onGiveUp] 预算耗尽时通知用户
 * @param {number[]} [options.backoffMs]
 * @param {number} [options.maxRestarts]
 * @param {number} [options.stableMs]
 * @param {() => number} [options.now]
 * @param {(fn: Function, ms: number) => any} [options.setTimeout]
 * @param {(t: any) => void} [options.clearTimeout]
 * @param {(msg: string) => void} [options.log]
 */
function createSupervisor(options = {}) {
  const restart = options.restart
  if (typeof restart !== 'function') throw new Error('createSupervisor 需要 restart 回调')

  const onGiveUp = options.onGiveUp || (() => {})
  const backoffMs = Array.isArray(options.backoffMs) && options.backoffMs.length
    ? options.backoffMs
    : DEFAULT_BACKOFF_MS
  const maxRestarts = Number.isInteger(options.maxRestarts) ? options.maxRestarts : DEFAULT_MAX_RESTARTS
  const stableMs = Number.isFinite(options.stableMs) ? options.stableMs : DEFAULT_STABLE_MS
  const now = options.now || Date.now
  const schedule = options.setTimeout || setTimeout
  const unschedule = options.clearTimeout || clearTimeout
  const log = options.log || (() => {})

  let active = true
  /** 待执行的退避定时器 → 其 resolve。stop() 必须把它们全部结算，否则等待方永远挂住。 */
  const timers = new Map()
  const slots = new Map()

  function slot(role) {
    // upSince 用 null 表示"从未就绪"：0 是合法时刻（假时钟/进程启动即 0ms），
    // 拿 0 当哨兵会让"在 0 时刻就绪"被判成"从未就绪"，稳定期永远不生效。
    if (!slots.has(role)) slots.set(role, { restarts: 0, upSince: null, pending: false })
    return slots.get(role)
  }

  /** 进程已就绪（或刚重启成功）：记下起点，供"稳定期"判定 */
  function markAlive(role) {
    slot(role).upSince = now()
  }

  function sleep(ms) {
    return new Promise(resolve => {
      const timer = schedule(() => {
        timers.delete(timer)
        resolve()
      }, ms)
      timers.set(timer, resolve)
    })
  }

  /**
   * 处理一次**非预期**退出（主动关停的不要送进来）。
   *
   * 返回最终裁决，便于调用方决定是否弹窗：
   * - `{action:'restart'}` 已重启成功；
   * - `{action:'give-up'}` 预算耗尽（或重启后仍失败），**必须**通知用户；
   * - `{action:'ignored'}` 已停用 / 同一进程的重入，什么都不做。
   */
  async function handleExit(role, info = {}) {
    const s = slot(role)
    if (!active) return { action: 'ignored', reason: 'stopped' }
    // 同一个子进程可能有多条退出通道（error + exit）；已经在排队的不要再排一次，
    // 否则一次崩溃会吃掉两次预算、并拉起两个进程去抢同一个端口。
    if (s.pending) return { action: 'ignored', reason: 'pending' }

    // 稳定够了就重新给满预算（见文件头第 3 条）
    if (s.upSince !== null && now() - s.upSince >= stableMs) s.restarts = 0
    s.upSince = null

    if (s.restarts >= maxRestarts) {
      log(`${role}: 连续崩溃已达上限（${maxRestarts} 次），不再重启`)
      onGiveUp(role, info)
      return { action: 'give-up', restarts: s.restarts, reason: 'budget-exhausted' }
    }

    s.pending = true
    const attempt = s.restarts + 1
    const delay = backoffMs[Math.min(s.restarts, backoffMs.length - 1)]
    s.restarts = attempt

    try {
      log(`${role}: 非预期退出（code=${info.code} signal=${info.signal || ''}），${delay}ms 后第 ${attempt} 次重启`)
      await sleep(delay)
      if (!active) return { action: 'ignored', reason: 'stopped' }
      const result = await restart(role, attempt, info)
      if (result && result.ok) {
        markAlive(role)
        log(`${role}: 第 ${attempt} 次重启成功`)
        return { action: 'restart', attempt, result }
      }
      log(`${role}: 第 ${attempt} 次重启失败（${(result && (result.reason || result.message)) || 'unknown'}）`)
    } catch (err) {
      log(`${role}: 第 ${attempt} 次重启抛错（${(err && err.message) || err}）`)
    } finally {
      s.pending = false
    }

    // 重启没成 = 又"崩"了一次，继续走预算（递归深度由 maxRestarts 兜住）
    return handleExit(role, { ...info, restartFailed: true })
  }

  /** 停用：清掉所有待执行的退避定时器。之后到达的退出事件一律忽略。 */
  function stop() {
    active = false
    for (const [timer, resolve] of timers) {
      unschedule(timer)
      // 必须结算等待方：只清定时器会让 `handleExit` 的 promise 永久挂住，
      // 关停路径上就留下一个永远不返回的 await（进程能退，但调用方以为还在重启）。
      resolve()
    }
    timers.clear()
  }

  function snapshot(role) {
    const s = slot(role)
    return { restarts: s.restarts, pending: s.pending, active }
  }

  return { handleExit, markAlive, stop, snapshot }
}

module.exports = {
  createSupervisor,
  DEFAULT_BACKOFF_MS,
  DEFAULT_MAX_RESTARTS,
  DEFAULT_STABLE_MS,
}
