/**
 * 未读通知轮询（F10 后半 / 通知中心角标）。
 *
 * ## 为什么在这个层做
 *
 * 通知的**产生**在网关（子 Agent 失败、分享被撤销），用户通道的 SSE 只是加速；
 * 角标必须有自己的轮询兜底，否则窗口在后台、SSE 被系统降频时数字会停住。
 * `GET /v1/notifications/unread` 是一条走部分索引的 `COUNT(*)`，30s 一次成本可忽略。
 *
 * ## 为什么挂在 AppLayout 而不是 App
 *
 * 通知属于**登录态**：登录页/分享落地页轮询只会得到 401（虽然请求已 `__silent`，
 * 但没意义）。AppLayout 只在已登录的页面壳里挂载，卸载时顺手把托盘计数清零 ——
 * 否则退出登录后托盘还挂着一个属于上一个账号的数字。
 *
 * ## 失败语义
 *
 * 通知是旁路：网络抖动/401 都保留上一次的数字，不弹错、不清零。清零只发生在
 * **明确卸载**（登出）时。
 */
import { getCurrentInstance, onUnmounted, ref } from 'vue'
import { fetchUnreadCount } from '../api/notifications'
import { desktopHost } from '../stores/device'
import { i18n } from '../i18n'

/** 轮询间隔。与侧栏会话刷新同一量级；通知不需要秒级。 */
export const UNREAD_POLL_MS = 30_000

/**
 * "已读状态变了"的进程内信号。
 *
 * 通知页标已读/删除后，角标与托盘应该**立刻**变，而不是最多等 30s 的下一拍。
 * 由写入方 `announceNotificationsChanged()` 派发，轮询方监听后补一次拉取。
 */
export const NOTIFICATIONS_CHANGED_EVENT = 'chiron:notifications-changed'

/** 通知页在写操作成功后调用（标记已读 / 删除 / 清理）。 */
export function announceNotificationsChanged(): void {
  window.dispatchEvent(new Event(NOTIFICATIONS_CHANGED_EVENT))
}

export function useUnreadNotifications() {
  const unread = ref(0)
  let timer: ReturnType<typeof setInterval> | null = null
  let inFlight = false
  let active = false
  /** 上一拍的未读数：只有"变多"才弹系统通知（首次拉取不算）。 */
  let lastCount: number | null = null

  function pushToTray(count: number): void {
    // 老外壳没有这条通道：可选链降级，界面照常。
    desktopHost()?.setTrayUnread?.(count)
  }

  async function refresh(): Promise<void> {
    if (!active || inFlight) return
    inFlight = true
    try {
      const next = await fetchUnreadCount()
      const prev = lastCount
      lastCount = next
      unread.value = next
      pushToTray(next)
      // 窗口在后台时，未读数**变多**才值得打断：前台有角标，弹通知反而是噪音。
      // `document.hidden` 在 Electron 里对"最小化/被别的应用遮住"同为 true。
      if (prev !== null && next > prev && document.hidden) {
        desktopHost()?.notifyNative?.({
          title: 'Chiron',
          body: i18n.global.t('notifications.native_body', { n: next - prev }),
        })
      }
    } catch {
      /* 旁路：保留旧值，等下一拍 */
    } finally {
      inFlight = false
    }
  }

  function onVisible(): void {
    if (document.visibilityState === 'visible') void refresh()
  }

  /** 页内已读/删除后的立刻补拍（不受可见性影响：用户刚操作完，页面必然在前台）。 */
  function onChanged(): void {
    void refresh()
  }

  function start(): void {
    if (timer) return
    active = true
    void refresh()
    timer = setInterval(() => {
      // 后台也要继续轮询：窗口最小化时的"新通知"要靠这一拍才看得见（并弹系统通知）。
      // Chromium 会把后台定时器降频到分钟级，恰好匹配通知的时效，不必额外补偿。
      void refresh()
    }, UNREAD_POLL_MS)
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('online', onVisible)
    window.addEventListener(NOTIFICATIONS_CHANGED_EVENT, onChanged)
  }

  function stop(): void {
    active = false
    lastCount = null
    if (timer) {
      clearInterval(timer)
      timer = null
    }
    document.removeEventListener('visibilitychange', onVisible)
    window.removeEventListener('online', onVisible)
    window.removeEventListener(NOTIFICATIONS_CHANGED_EVENT, onChanged)
    // 登出/离开应用壳：托盘上的数字属于上一个账号，必须收回。
    pushToTray(0)
  }

  // 单测里在组件外调用时不注册（否则 Vue 会 warn "no active component instance"）。
  if (getCurrentInstance()) onUnmounted(stop)

  return { unread, refresh, start, stop }
}
