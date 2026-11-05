import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import {
  DEVICE_MODE_STORAGE_KEY,
  DEV_AUTH_BYPASS_STORAGE_KEY,
  currentDeviceMode,
  isDevAuthBypassEnabled,
  setActiveMode,
  syncDevAuthBypassFromUrl,
  useDeviceStore,
} from '../device'

/**
 * 设备级运行模式 store 的契约测试。
 *
 * 重点钉住三件容易回归的事：
 *   1) Web 端（无 `window.chironDesktop`）**永远**是 cloud —— 本地模式是桌面独有能力，
 *      一旦这里退化成 local，Web 端会开始跳过登录守卫（安全回归）；
 *   2) preload 暴露的 `mode` 是真相，本地镜像只是兜底；
 *   3) 外壳不支持 `setMode` 时**必须抛错**，不能假装切换成功（重启后会悄悄回到原模式）。
 */
type FakeHost = Record<string, unknown>

function installHost(host?: FakeHost) {
  if (host) (globalThis as { chironDesktop?: FakeHost }).chironDesktop = host
  else delete (globalThis as { chironDesktop?: FakeHost }).chironDesktop
}

describe('device store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    setActiveMode(null)
    localStorage.clear()
    installHost(undefined)
  })

  it('Web 端恒为 cloud，且不可切换', () => {
    expect(currentDeviceMode()).toBe('cloud')
    const store = useDeviceStore()
    expect(store.mode).toBe('cloud')
    expect(store.isCloud()).toBe(true)
    expect(store.isLocal()).toBe(false)
    expect(store.isDesktop).toBe(false)
  })

  it('Web 端没有 openSetup：canOpenSetup 为 false，调用返回 false 而不是抛错', async () => {
    const store = useDeviceStore()
    expect(store.canOpenSetup).toBe(false)
    // 改服务地址的入口在 Web 端不存在：返回 false 让界面去禁用/提示，静默成功是骗人
    await expect(store.openConnectionSetup()).resolves.toBe(false)
  })

  it('桌面壳暴露 openSetup 时：调用它并返回 true（改地址的入口必须够得着）', async () => {
    const openSetup = vi.fn(async () => true)
    installHost({ isDesktop: true, mode: 'cloud', apiUrl: 'http://127.0.0.1:8080', openSetup })
    const store = useDeviceStore()
    expect(store.canOpenSetup).toBe(true)
    await expect(store.openConnectionSetup()).resolves.toBe(true)
    expect(openSetup).toHaveBeenCalledTimes(1)
  })

  it('老外壳（preload 里没有 openSetup）：入口判据为 false，界面据此禁用按钮', async () => {
    installHost({ isDesktop: true, mode: 'cloud', apiUrl: 'http://127.0.0.1:8080' })
    const store = useDeviceStore()
    expect(store.canOpenSetup).toBe(false)
    await expect(store.openConnectionSetup()).resolves.toBe(false)
  })

  it('preload 的 mode 是真相，并写入 localStorage 镜像', async () => {
    installHost({ isDesktop: true, apiUrl: 'http://127.0.0.1:8080', mode: 'local' })
    const store = useDeviceStore()
    expect(store.mode).toBe('local')
    expect(store.isLocal()).toBe(true)
    expect(store.isDesktop).toBe(true)
    await store.ensureReady()
    expect(localStorage.getItem(DEVICE_MODE_STORAGE_KEY)).toBe('local')
  })

  it('ensureReady 读取 getDeviceSettings 并采用其中的 mode', async () => {
    const getDeviceSettings = vi.fn().mockResolvedValue({
      mode: 'cloud',
      apiUrl: 'https://cloud.example.test',
      local: { dataDir: '', lastStartedAt: '' },
      ui: { locale: 'zh-CN', theme: 'system' },
      migration: { legacyCloudDetected: true, modeChosenAt: '' },
    })
    installHost({ isDesktop: true, mode: 'local', getDeviceSettings })
    const store = useDeviceStore()
    expect(store.mode).toBe('local')
    await store.ensureReady()
    expect(getDeviceSettings).toHaveBeenCalled()
    expect(store.mode).toBe('cloud')
    expect(currentDeviceMode()).toBe('cloud')
    expect(localStorage.getItem(DEVICE_MODE_STORAGE_KEY)).toBe('cloud')
  })

  it('setMode 委托 preload（含 deleteLocalData）并同步模式与镜像', async () => {
    const setMode = vi.fn().mockResolvedValue(undefined)
    const restart = vi.fn().mockResolvedValue(undefined)
    installHost({ isDesktop: true, mode: 'local', setMode, restart })
    const store = useDeviceStore()
    await store.setMode('cloud', { deleteLocalData: true })
    expect(setMode).toHaveBeenCalledWith('cloud', { deleteLocalData: true })
    expect(store.mode).toBe('cloud')
    expect(currentDeviceMode()).toBe('cloud')
    expect(localStorage.getItem(DEVICE_MODE_STORAGE_KEY)).toBe('cloud')
    await store.restart()
    expect(restart).toHaveBeenCalled()
  })

  it('默认不删除本地数据：未传 deleteLocalData 时按 false 透传', async () => {
    const setMode = vi.fn().mockResolvedValue(undefined)
    installHost({ isDesktop: true, mode: 'cloud', setMode })
    const store = useDeviceStore()
    await store.setMode('local')
    expect(setMode).toHaveBeenCalledWith('local', { deleteLocalData: false })
  })

  it('外壳不支持 setMode 时抛错，且不改变当前模式', async () => {
    installHost({ isDesktop: true, mode: 'cloud' })
    const store = useDeviceStore()
    await expect(store.setMode('local')).rejects.toThrow()
    expect(store.mode).toBe('cloud')
  })

  it('旧外壳（无 mode 字段）：恒为 local —— 云端模式已废除，有 apiUrl 也不再判成 cloud', () => {
    // 曾经这里用「有 apiUrl 就当 cloud」兜底。云端模式废除后只有本地一种形态，
    // 继续按 apiUrl 猜会让配置里残留地址的老用户被误判成云端模式。
    installHost({ isDesktop: true, apiUrl: 'http://example.test' })
    expect(currentDeviceMode()).toBe('local')
    installHost({ isDesktop: true, apiUrl: '' })
    expect(currentDeviceMode()).toBe('local')
  })
})

/**
 * 开发环境登录绕过标志位。
 *
 * 钉住三件事：
 *   1) 默认**关闭** —— 不加标志位时行为与改造前完全一致；
 *   2) URL 参数能一次性开启并持久化（刷新后仍生效），`=0` 能关掉；
 *   3) 绕过只放宽"登录态/管理员"判定，**不**篡改模式判定 —— 否则 api 层会误判成
 *      local 模式，把请求打到不存在的本地会话端点上。
 */
describe('开发环境登录绕过标志位', () => {
  const clearUrl = () => {
    // jsdom 的 location.search 可通过 history 改写
    globalThis.history.replaceState({}, '', '/')
  }

  beforeEach(() => {
    setActivePinia(createPinia())
    setActiveMode(null)
    localStorage.clear()
    clearUrl()
    installHost(undefined)
  })

  it('默认关闭：没有标志位时不绕过', () => {
    expect(isDevAuthBypassEnabled()).toBe(false)
  })

  it('localStorage 置 1 后开启', () => {
    localStorage.setItem(DEV_AUTH_BYPASS_STORAGE_KEY, '1')
    expect(isDevAuthBypassEnabled()).toBe(true)
  })

  it('URL 参数 ?devAuthBypass=1 开启并写回 localStorage（刷新后仍生效）', () => {
    globalThis.history.replaceState({}, '', '/?devAuthBypass=1')
    syncDevAuthBypassFromUrl()

    expect(localStorage.getItem(DEV_AUTH_BYPASS_STORAGE_KEY)).toBe('1')
    expect(isDevAuthBypassEnabled()).toBe(true)
  })

  it('URL 参数 ?devAuthBypass=0 可撤销，随即关闭', () => {
    localStorage.setItem(DEV_AUTH_BYPASS_STORAGE_KEY, '1')
    expect(isDevAuthBypassEnabled()).toBe(true)

    globalThis.history.replaceState({}, '', '/?devAuthBypass=0')
    syncDevAuthBypassFromUrl()

    expect(localStorage.getItem(DEV_AUTH_BYPASS_STORAGE_KEY)).toBeNull()
    expect(isDevAuthBypassEnabled()).toBe(false)
  })

  it('绕过不改变模式判定：Web 端仍是 cloud', () => {
    // 若这里退化成 local，api 层会把请求当成"本地会话"处理并触发 501 路径
    localStorage.setItem(DEV_AUTH_BYPASS_STORAGE_KEY, '1')
    expect(isDevAuthBypassEnabled()).toBe(true)
    expect(currentDeviceMode()).toBe('cloud')
  })
})
