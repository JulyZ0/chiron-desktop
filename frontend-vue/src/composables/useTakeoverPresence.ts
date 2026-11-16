/**
 * Super-agent 接管的「用户还在操作」心跳。
 *
 * ## 为什么需要它
 *
 * 引擎判断"能不能接管"时取三路信号之或：会话 run 锁、presence 心跳、最后一条
 * 消息时间。锁只能覆盖"已经有 run 在跑"，最后消息时间只能覆盖"刚说过话"。中间
 * 那段 —— **用户打开了会话、正在读、还没提交** —— 只有心跳能覆盖。没有心跳，
 * Agent 会在用户敲字的间隙把会话抢走。
 *
 * ## 为什么是 30 秒一次，而不是"每次输入就发"
 *
 * 后端 TTL 是 15 分钟（`PRESENCE_TTL_SECONDS`），30 秒上报意味着即使连丢 29 次
 * 也不会误判为离开。按输入节流会让打字快的人一秒几十个请求，而这些请求**不携带
 * 任何额外信息** —— 后端只存一个时间戳。
 *
 * ## 为什么交互时也补一拍
 *
 * 定时器在后台标签页会被浏览器降到分钟级（Chrome 最长 1 分钟一次），恰好在
 * "切回来开始干活"时最容易漏。用户在界面上有任何动静就补一拍，把那一刻的延迟
 * 抹平；用 5 秒节流挡住高频输入。
 *
 * ## 失败为什么静默
 *
 * 心跳是不可见的后台行为。网络抖动时弹"心跳失败"只会让用户困惑 —— 它不代表
 * 任何他们需要立刻处理的事情，下一拍会自己补上。
 */
import { onMounted, onUnmounted } from 'vue'
import { reportPresence } from '../api'

/** 定时上报间隔。远小于后端 15 分钟 TTL，连丢多次也不会误判 */
const HEARTBEAT_MS = 30_000
/** 交互触发的补拍最小间隔 */
const INTERACTION_THROTTLE_MS = 5_000
/** 视为"用户在操作"的事件。刻意不含 mousemove —— 鼠标划过页面不代表在做事 */
const ACTIVITY_EVENTS = ['keydown', 'pointerdown', 'wheel'] as const

export function useTakeoverPresence() {
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null
  let lastSentAt = 0

  function beat() {
    lastSentAt = Date.now()
    // reportPresence 内部吞掉异常并返回 ok:false，这里不需要 catch
    void reportPresence()
  }

  function onActivity() {
    if (Date.now() - lastSentAt < INTERACTION_THROTTLE_MS) return
    beat()
  }

  // 从后台切回来时立刻补一拍：此刻定时器可能已经迟了很久
  function onVisible() {
    if (document.visibilityState === 'visible') beat()
  }

  onMounted(() => {
    beat()
    heartbeatTimer = setInterval(beat, HEARTBEAT_MS)
    for (const evt of ACTIVITY_EVENTS) {
      // passive：这些监听只读，不阻塞滚动与输入的合成
      window.addEventListener(evt, onActivity, { passive: true })
    }
    document.addEventListener('visibilitychange', onVisible)
  })

  onUnmounted(() => {
    if (heartbeatTimer) clearInterval(heartbeatTimer)
    heartbeatTimer = null
    for (const evt of ACTIVITY_EVENTS) {
      window.removeEventListener(evt, onActivity)
    }
    document.removeEventListener('visibilitychange', onVisible)
  })
}
