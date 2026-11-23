import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * 「运行模式」面板：本地/远程两种形态 + 「清空本机数据」这个唯一的破坏性动作。
 * 改服务地址走「打开连接设置」，本地模式下也必须是可达入口。
 *
 * 这里钉的是**确认链**：勾选、手打确认词、然后才放行。三道确认里前两道在渲染进程
 * （防误触），Windows Hello 在主进程（防冒用）—— 主进程那道不在这里测（见
 * electron/__tests__/wipe-data.test.cjs 与 main.cjs 的 IPC）。
 *
 * 之所以值得单独钉：这是全应用唯一会让用户数据不可恢复消失的按钮。
 * 少一道确认的代价是用户全部长期数据，而"少一道"这件事在界面上看不出来。
 */
type FakeStore = Record<string, unknown>

const deviceStoreMock = vi.hoisted(() => ({
  isDesktop: true,
  wiping: false,
  mode: 'local',
  settings: { apiUrl: '', local: { dataDir: '' } } as {
    apiUrl: string
    local?: { dataDir?: string }
  },
  isLocal: () => true,
  isCloud: () => false,
  ensureReady: vi.fn(async () => {}),
  // 本地模式改连局域网/公网服务的唯一入口（主进程建窗分流见 shared.cjs）
  openConnectionSetup: vi.fn(async () => true),
  // 返回类型显式放宽：成功与各失败分支的形状不同（失败只有 reason/message），
  // 让 TS 从首个返回值推断会把它锁成窄类型，后续用例再赋值就报错。
  wipeLocalData: vi.fn(async (): Promise<Record<string, unknown>> => ({ ok: true, relaunched: true })),
  checkHelloAvailability: vi.fn(async () => true),
  // 挂载依赖块：桌面壳不支持时返回 null（本组用例不渲染该块）
  getMountDepsStatus: vi.fn(async () => null),
  installMountDep: vi.fn(async () => ({ ok: true })),
}))

vi.mock('../../../stores/device', () => ({
  useDeviceStore: () => deviceStoreMock as unknown as FakeStore,
}))

vi.mock('../../../stores/auth', () => ({
  useAuthStore: () => ({ user: { email: 'me@example.com', role: 'user' } }),
}))

// 运行状态面板会在挂载时探测 /v1/system/components 与 /v1/models
const apiMock = vi.hoisted(() => ({
  get: vi.fn(async (url: string) => {
    if (url === '/v1/system/components') {
      return {
        data: {
          data: {
            gateway: { available: true },
            engine: { healthy: true },
            postgres: { available: true },
            redis: { available: true },
          },
        },
      }
    }
    if (url === '/v1/models') {
      return { data: { data: { models: [{ name: 'deepseek-v4.1-flash', provider: 'opencode-go' }] } } }
    }
    return { data: { data: {} } }
  }),
}))

vi.mock('../../../api', () => ({
  API_URL: 'http://127.0.0.1:12345',
  api: apiMock,
}))

const messageMock = vi.hoisted(() => ({
  success: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
}))

vi.mock('ant-design-vue', async importOriginal => {
  const actual = await importOriginal<typeof import('ant-design-vue')>()
  return { ...actual, message: messageMock }
})

import RunModePanel from '../RunModePanel.vue'

function mountPanel() {
  return mount(RunModePanel, {
    global: {
      stubs: {
        'a-alert': true,
        'a-tag': true,
        // stub 必须显式给 name，否则 findComponent({ name }) 找不到它：
        // Vue 生成的匿名 stub 叫 `a-checkbox-stub`，按组件名查会落空。
        'a-checkbox': { name: 'ACheckbox', template: '<label><slot /></label>' },
        'a-input': { name: 'AInput', template: '<input />' },
        'a-button': { name: 'AButton', template: '<button type="button"><slot /></button>' },
        // modal 需要真实插槽内容才能断言里面的确认控件；
        // 同时把 okButtonProps.disabled 透到 DOM 上 —— stub 不声明 props 时读不到它
        'a-modal': {
          name: 'AModal',
          props: ['okButtonProps', 'confirmLoading'],
          template:
            '<div class="modal-stub" :data-ok-disabled="String(!!(okButtonProps && okButtonProps.disabled))"><slot /></div>',
        },
      },
    },
  })
}

beforeEach(() => {
  deviceStoreMock.isDesktop = true
  deviceStoreMock.wiping = false
  deviceStoreMock.mode = 'local'
  deviceStoreMock.settings = { apiUrl: '', local: { dataDir: 'C:\\data\\chiron' } }
  deviceStoreMock.openConnectionSetup.mockClear()
  apiMock.get.mockClear()
  deviceStoreMock.wipeLocalData = vi.fn(async (): Promise<Record<string, unknown>> => ({ ok: true, relaunched: true }))
  messageMock.success.mockClear()
  messageMock.warning.mockClear()
  messageMock.error.mockClear()
})

describe('RunModePanel', () => {
  it('不再提供「切换模式」：改地址走「打开连接设置」', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.text()).not.toContain('切换模式')
    expect(wrapper.text()).not.toContain('切换到')
    expect(wrapper.text()).toContain('本地')
  })

  it('本地模式下也能打开连接设置（改连局域网/公网服务）', async () => {
    deviceStoreMock.mode = 'local'
    deviceStoreMock.openConnectionSetup.mockClear()
    const wrapper = mountPanel()
    await flushPromises()

    const button = wrapper.findAll('button').find(b => b.text().includes('打开连接设置'))
    expect(button, '本地模式是连接远程服务的起点，入口不能缺失').toBeTruthy()
    await button!.trigger('click')
    await flushPromises()

    expect(deviceStoreMock.openConnectionSetup).toHaveBeenCalledTimes(1)
  })

  it('远程模式：状态面板说明谁在跑 agent、工具与文件在哪、数据在哪', async () => {
    deviceStoreMock.mode = 'cloud'
    deviceStoreMock.settings = { apiUrl: 'https://chiron.example.com', local: { dataDir: '' } }
    const wrapper = mountPanel()
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('运行状态')
    expect(text).toContain('Agent 循环')
    expect(text).toContain('服务地址主机上的 Python 引擎')
    expect(text).toContain('服务端沙箱 / 服务端项目目录')
    expect(text).toContain('只渲染界面并转发请求')
    expect(text).toContain('服务端主机')
    expect(text).toContain('https://chiron.example.com')
    expect(apiMock.get).toHaveBeenCalledWith('/v1/system/components', expect.anything())
  })

  it('提供「清空本机数据」入口，并说明不可恢复', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const wipe = wrapper.findAll('button').find(b => b.text().includes('清空本机数据'))
    expect(wipe, '清空本机数据是本页唯一的破坏性动作，必须可达').toBeTruthy()
    expect(wrapper.text()).toContain('不可恢复')
  })

  it('Web 端（非桌面壳）禁用清空入口：Web 端没有本机数据可清', async () => {
    deviceStoreMock.isDesktop = false
    const wrapper = mountPanel()
    await flushPromises()

    const wipe = wrapper.findAll('button').find(b => b.text().includes('清空本机数据'))
    expect(wipe).toBeTruthy()
    expect(wipe!.attributes('disabled')).toBeDefined()
  })

  it('确认链：未勾选或未打确认词时都不放行', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.find('.modal-stub').attributes('data-ok-disabled')).toBe('true')
  })

  it('三道确认齐备后才调用 wipeLocalData', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const confirmInput = wrapper.findComponent({ name: 'AInput' })
    await confirmInput.vm.$emit('update:value', 'DELETE')
    const checkbox = wrapper.findComponent({ name: 'ACheckbox' })
    await checkbox.vm.$emit('update:checked', true)
    await flushPromises()

    await wrapper.findComponent({ name: 'AModal' }).vm.$emit('ok')
    await flushPromises()

    expect(deviceStoreMock.wipeLocalData).toHaveBeenCalledTimes(1)
    expect(messageMock.success).toHaveBeenCalled()
  })

  it('确认词错误时即使勾选了也不放行', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const confirmInput = wrapper.findComponent({ name: 'AInput' })
    await confirmInput.vm.$emit('update:value', 'delete')
    const checkbox = wrapper.findComponent({ name: 'ACheckbox' })
    await checkbox.vm.$emit('update:checked', true)
    await flushPromises()

    expect(wrapper.find('.modal-stub').attributes('data-ok-disabled')).toBe('true')
  })

  it('Hello 上被拒绝：提示 warning 而不是报成「清空失败」', async () => {
    deviceStoreMock.wipeLocalData = vi.fn(async () => ({
      ok: false,
      reason: 'denied',
      message: 'Windows Hello 验证未通过',
    }))
    const wrapper = mountPanel()
    await flushPromises()

    const confirmInput = wrapper.findComponent({ name: 'AInput' })
    await confirmInput.vm.$emit('update:value', 'DELETE')
    const checkbox = wrapper.findComponent({ name: 'ACheckbox' })
    await checkbox.vm.$emit('update:checked', true)
    await flushPromises()
    await wrapper.findComponent({ name: 'AModal' }).vm.$emit('ok')
    await flushPromises()

    expect(messageMock.warning).toHaveBeenCalled()
    expect(messageMock.error).not.toHaveBeenCalled()
  })

  it('清空过程出错：报 error', async () => {
    deviceStoreMock.wipeLocalData = vi.fn(async () => ({
      ok: false,
      reason: 'wipe-failed',
      message: '磁盘忙',
    }))
    const wrapper = mountPanel()
    await flushPromises()

    const confirmInput = wrapper.findComponent({ name: 'AInput' })
    await confirmInput.vm.$emit('update:value', 'DELETE')
    const checkbox = wrapper.findComponent({ name: 'ACheckbox' })
    await checkbox.vm.$emit('update:checked', true)
    await flushPromises()
    await wrapper.findComponent({ name: 'AModal' }).vm.$emit('ok')
    await flushPromises()

    expect(messageMock.error).toHaveBeenCalled()
  })
})
