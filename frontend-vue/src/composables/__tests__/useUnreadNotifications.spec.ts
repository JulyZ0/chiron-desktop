import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../../api/notifications', () => ({
  fetchUnreadCount: vi.fn(),
}))

import { fetchUnreadCount } from '../../api/notifications'
import { announceNotificationsChanged, useUnreadNotifications } from '../useUnreadNotifications'

const mockedFetch = vi.mocked(fetchUnreadCount)

describe('useUnreadNotifications', () => {
  let setTrayUnread: ReturnType<typeof vi.fn>
  let notifyNative: ReturnType<typeof vi.fn>

  beforeEach(() => {
    setTrayUnread = vi.fn()
    notifyNative = vi.fn()
    ;(globalThis as { chironDesktop?: unknown }).chironDesktop = { setTrayUnread, notifyNative }
  })

  afterEach(() => {
    delete (globalThis as { chironDesktop?: unknown }).chironDesktop
    mockedFetch.mockReset()
    Object.defineProperty(document, 'hidden', { configurable: true, value: false })
  })

  it('start 后拉一次未读数并推给托盘', async () => {
    mockedFetch.mockResolvedValue(3)
    const { unread, start, stop } = useUnreadNotifications()
    start()
    await vi.waitFor(() => expect(unread.value).toBe(3))
    expect(setTrayUnread).toHaveBeenLastCalledWith(3)
    stop()
  })

  it('轮询失败保留旧值（通知是旁路，不清零）', async () => {
    mockedFetch.mockResolvedValueOnce(2)
    const { unread, start, stop, refresh } = useUnreadNotifications()
    start()
    await vi.waitFor(() => expect(unread.value).toBe(2))

    mockedFetch.mockRejectedValueOnce(new Error('401'))
    await refresh()

    expect(unread.value).toBe(2)
    stop()
  })

  it('stop（登出/卸载）把托盘计数收回 0', async () => {
    mockedFetch.mockResolvedValue(5)
    const { start, stop } = useUnreadNotifications()
    start()
    await vi.waitFor(() => expect(setTrayUnread).toHaveBeenCalledWith(5))
    stop()
    expect(setTrayUnread).toHaveBeenLastCalledWith(0)
  })

  it('stop 之后不再轮询', async () => {
    mockedFetch.mockResolvedValue(1)
    const { start, stop } = useUnreadNotifications()
    start()
    await vi.waitFor(() => expect(mockedFetch).toHaveBeenCalledTimes(1))
    stop()
    mockedFetch.mockClear()
    await new Promise(resolve => setTimeout(resolve, 20))
    expect(mockedFetch).not.toHaveBeenCalled()
  })

  it('页内标已读后立刻补拍（不等 30s 的下一拍）', async () => {
    mockedFetch.mockResolvedValueOnce(4)
    const { unread, start, stop } = useUnreadNotifications()
    start()
    await vi.waitFor(() => expect(unread.value).toBe(4))

    mockedFetch.mockResolvedValueOnce(1)
    announceNotificationsChanged()
    await vi.waitFor(() => expect(unread.value).toBe(1))
    stop()
  })

  it('只有窗口在后台且未读变多才弹系统通知', async () => {
    mockedFetch.mockResolvedValueOnce(1)
    const { refresh, start, stop } = useUnreadNotifications()
    start()
    await vi.waitFor(() => expect(setTrayUnread).toHaveBeenCalledWith(1))

    // 前台：变多不打断（角标已经能看到）
    Object.defineProperty(document, 'hidden', { configurable: true, value: false })
    mockedFetch.mockResolvedValueOnce(2)
    await refresh()
    expect(notifyNative).not.toHaveBeenCalled()

    // 后台：变多才弹
    Object.defineProperty(document, 'hidden', { configurable: true, value: true })
    mockedFetch.mockResolvedValueOnce(3)
    await refresh()
    expect(notifyNative).toHaveBeenCalledTimes(1)
    expect(notifyNative.mock.calls[0]?.[0]?.body).toBeTruthy()

    // 没变多（已读后减少）同样不弹
    mockedFetch.mockResolvedValueOnce(1)
    await refresh()
    expect(notifyNative).toHaveBeenCalledTimes(1)

    stop()
  })
})
