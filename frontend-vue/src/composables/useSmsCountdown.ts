import { ref, onUnmounted, getCurrentInstance } from 'vue'

/**
 * 短信验证码发送倒计时（登录页 / 手机绑定共用）。
 *
 * start() 置位并开始倒数；remaining 为 0 时可再次发送。
 * 组件内使用时卸载自动清理定时器；组件外（纯逻辑测试）也可安全调用。
 */
export function useSmsCountdown(initialSeconds = 60) {
  const remaining = ref(0)
  let timer: ReturnType<typeof setInterval> | null = null

  function stop() {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }

  function start(seconds?: number) {
    const total = seconds ?? initialSeconds
    if (total <= 0) return
    stop()
    remaining.value = total
    timer = setInterval(() => {
      remaining.value--
      if (remaining.value <= 0) {
        remaining.value = 0
        stop()
      }
    }, 1000)
  }

  if (getCurrentInstance()) {
    onUnmounted(stop)
  }

  return { remaining, start, stop }
}
