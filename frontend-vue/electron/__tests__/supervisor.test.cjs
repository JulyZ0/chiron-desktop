'use strict'
/**
 * `supervisor.cjs` 的契约测试。
 *
 * 这里钉的都是"错了会让用户**彻底得不到结论**"的判定 —— 也就是持久运行的反面：
 *
 * 1. 主动关停被当成崩溃 ⇒ 退出应用后后台残留一个引擎进程；
 * 2. 起动即崩却无限重启 ⇒ CPU/磁盘被烧掉，界面依然一句话都没有；
 * 3. 稳定运行很久后偶发崩溃却被永久放弃 ⇒ 本来自愈一下就好，结果整晚报废；
 * 4. 一次崩溃走两条退出通道（error + exit）⇒ 吃掉两次预算并抢同一端口。
 *
 * 用假时钟与假定时器，不碰真实进程。
 */

const test = require('node:test')
const assert = require('node:assert/strict')

const { createSupervisor, DEFAULT_MAX_RESTARTS, DEFAULT_BACKOFF_MS } = require('../supervisor.cjs')

/** 假时钟 + 立即执行的定时器：退避等待不需要真的 sleep */
function fakeClock() {
  let t = 0
  const timers = []
  return {
    now: () => t,
    advance: ms => {
      t += ms
    },
    setTimeout: (fn, ms) => {
      const timer = { fn, ms, cleared: false }
      timers.push(timer)
      // 立即排队执行，但把"应等多长时间"记下来给用例断言
      setImmediate(() => {
        if (!timer.cleared) fn()
      })
      return timer
    },
    clearTimeout: timer => {
      if (timer) timer.cleared = true
    },
    delays: () => timers.map(x => x.ms),
  }
}

function setup(overrides = {}) {
  const clock = fakeClock()
  const restarts = []
  const giveUps = []
  const logs = []
  const sup = createSupervisor({
    restart: async (role, attempt) => {
      restarts.push({ role, attempt })
      return overrides.restartResult ? overrides.restartResult(role, attempt) : { ok: true }
    },
    onGiveUp: (role, info) => giveUps.push({ role, info }),
    now: clock.now,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    log: msg => logs.push(msg),
    ...overrides.options,
  })
  return { sup, clock, restarts, giveUps, logs }
}

// ------------------------------------------------------------------ 基本自愈

test('非预期退出后按退避重启，并回到可服务状态', async () => {
  const { sup, restarts, clock } = setup()
  sup.markAlive('engine')

  const verdict = await sup.handleExit('engine', { code: 1 })
  await new Promise(r => setImmediate(r))

  assert.equal(verdict.action, 'restart')
  assert.deepEqual(restarts, [{ role: 'engine', attempt: 1 }])
  assert.equal(clock.delays()[0], DEFAULT_BACKOFF_MS[0], '第一次退避应当是 1s，不是立即重启')
  assert.equal(sup.snapshot('engine').restarts, 1)
})

test('退避随次数递增，且不超过表长', async () => {
  const { sup, clock } = setup()

  await sup.handleExit('engine', { code: 1 })
  await new Promise(r => setImmediate(r))
  await sup.handleExit('engine', { code: 1 })
  await new Promise(r => setImmediate(r))

  assert.deepEqual(clock.delays(), [1000, 2000], '连续崩溃必须越等越久，否则等于忙等重试')
})

// ------------------------------------------------------------------ 判定 1

test('stop() 之后到达的退出事件一律忽略（主动关停不重启）', async () => {
  const { sup, restarts } = setup()
  sup.stop()

  const verdict = await sup.handleExit('engine', { code: null, signal: 'SIGTERM' })

  assert.equal(verdict.action, 'ignored')
  assert.deepEqual(restarts, [], '关停过程中再拉起一个进程，会让用户退出应用后仍留下后台引擎')
})

test('stop() 会取消尚未执行的退避定时器', async () => {
  const { sup, restarts } = setup()

  const pending = sup.handleExit('engine', { code: 1 })
  sup.stop()
  const verdict = await pending
  await new Promise(r => setImmediate(r))

  assert.equal(verdict.action, 'ignored')
  assert.deepEqual(restarts, [], '退避期间被关停，就不该再重启了')
})

// ------------------------------------------------------------------ 判定 2

test('连续崩溃超过预算就放弃，并通知用户', async () => {
  const { sup, giveUps, restarts } = setup({
    restartResult: () => ({ ok: false, reason: 'still-broken' }),
  })

  let verdict = await sup.handleExit('engine', { code: 1 })
  for (let i = 0; i < DEFAULT_MAX_RESTARTS + 2; i += 1) {
    if (verdict.action !== 'restart') break
    await new Promise(r => setImmediate(r))
    verdict = await sup.handleExit('engine', { code: 1 })
  }

  assert.equal(verdict.action, 'give-up', '起重启失败必须收敛到"放弃 + 通知"，不能无限重试')
  assert.ok(giveUps.length >= 1, '放弃时必须回调 onGiveUp，否则用户看不到任何结论')
  assert.ok(
    restarts.length <= DEFAULT_MAX_RESTARTS,
    `重启次数不能超过预算（实际 ${restarts.length} 次）`,
  )
})

test('重启成功后再次崩溃，预算是重新计算的（不是累计永不恢复）', async () => {
  const { sup } = setup()
  sup.markAlive('engine')

  const first = await sup.handleExit('engine', { code: 1 })
  await new Promise(r => setImmediate(r))
  assert.equal(first.action, 'restart')

  // 模拟"刚重启成功"的那次退出：预算已被吃掉 1 次，还能继续
  const second = await sup.handleExit('engine', { code: 1 })
  await new Promise(r => setImmediate(r))
  assert.equal(second.action, 'restart')
  assert.equal(sup.snapshot('engine').restarts, 2)
})

// ------------------------------------------------------------------ 判定 3

test('稳定运行超过窗口后崩溃，预算清零重来', async () => {
  const { sup, clock } = setup()

  // 先把预算用掉两次
  await sup.handleExit('engine', { code: 1 })
  await new Promise(r => setImmediate(r))
  await sup.handleExit('engine', { code: 1 })
  await new Promise(r => setImmediate(r))
  assert.equal(sup.snapshot('engine').restarts, 2)

  // 这一次跑够了稳定期：就绪之后经过的时长才是"稳定运行了多久"
  sup.markAlive('engine')
  clock.advance(120_000)

  await sup.handleExit('engine', { code: 1 })
  await new Promise(r => setImmediate(r))

  assert.equal(
    sup.snapshot('engine').restarts,
    1,
    '跑了一整天才偶发崩一次，不该被之前的累计次数拖进"放弃"',
  )
})

// ------------------------------------------------------------------ 判定 4

test('同一个进程的重复退出事件只处理一次（error + exit 双通道）', async () => {
  const { sup, restarts } = setup()
  sup.markAlive('engine')

  const first = sup.handleExit('engine', { code: 1 })
  const second = sup.handleExit('engine', { code: 1 }) // 重入：尚未处理完
  const [a, b] = await Promise.all([first, second])
  await new Promise(r => setImmediate(r))

  assert.equal(a.action, 'restart')
  assert.equal(b.action, 'ignored', '一次崩溃走两条通道，第二次必须被忽略')
  assert.equal(restarts.length, 1, '否则会吃掉两次预算、并拉两个进程抢同一端口')
})

test('不同角色的预算互不干扰', async () => {
  const { sup } = setup()

  await sup.handleExit('engine', { code: 1 })
  await new Promise(r => setImmediate(r))
  await sup.handleExit('gateway', { code: 1 })
  await new Promise(r => setImmediate(r))

  assert.equal(sup.snapshot('engine').restarts, 1)
  assert.equal(sup.snapshot('gateway').restarts, 1, '网关崩了不该让引擎的预算也被吃掉')
})
