import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * 「执行隔离」面板的展示契约。
 *
 * 产品只支持四档：无隔离 / 作业对象 / 低完整性 / 容器（Kiri）。
 * 这里钉住三件事：
 * 1. 页面不再出现已移除的「受限令牌 / 应用容器」；
 * 2. 请求值与实际值不同时，降级必须显式展示；
 * 3. 容器档位的运行时状态与下载入口不能和档位选择混在一起丢失。
 */
const apiMock = vi.hoisted(() => ({
  getIsolationStatus: vi.fn(),
  setIsolationLevel: vi.fn(),
  prepareContainerRuntime: vi.fn(),
  clearIsolationLevel: vi.fn(),
  revokeIsolationArtifacts: vi.fn(),
}))

vi.mock('../../../api', () => apiMock)

const authMock = vi.hoisted(() => ({
  ensureLocalSession: vi.fn(async () => true),
}))
vi.mock('../../../stores/auth', () => ({ useAuthStore: () => authMock }))

const messageMock = vi.hoisted(() => ({
  success: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
}))

const modalMock = vi.hoisted(() => ({
  confirm: vi.fn((opts: { onOk?: () => void }) => {
    opts.onOk?.()
  }),
}))

vi.mock('ant-design-vue', async importOriginal => {
  const actual = await importOriginal<typeof import('ant-design-vue')>()
  return { ...actual, message: messageMock, Modal: modalMock }
})

import IsolationPanel from '../IsolationPanel.vue'

function statusPayload(overrides: Record<string, unknown> = {}) {
  return {
    requested: 'low_il',
    effective: 'low_il',
    degraded: false,
    floor: 'low_il',
    below_floor: false,
    user_selected: false,
    reasons: [],
    downgrade_allowed: true,
    selectable: ['none', 'job', 'low_il', 'container'],
    acknowledge_required: ['none'],
    unavailable_reasons: {},
    capabilities: {},
    runtime_override: null,
    ...overrides,
  }
}

function containerStatus(overrides: Record<string, unknown> = {}): Record<string, any> {
  return statusPayload({
    container: {
      version: '0.2.2',
      platform: 'linux/amd64',
      supported: true,
      socket: '',
      installed: false,
      engine: null,
      reason: '未检测到 Kiri。可在设置页下载运行时包',
      asset: {
        name: 'kiri-lite_0.2.2_amd64.deb',
        sha256: '24798125ee50bb24db86594a9ee1469b1032d9b2ffd7da616b06e40763c723b9',
        url: 'https://example.invalid/pkg.deb',
        cached: '',
      },
      install_hint: 'sudo dpkg -i <pkg> && sudo kiri host-access $USER',
    },
    ...overrides,
  })
}

function mountPanel() {
  return mount(IsolationPanel, {
    global: {
      stubs: {
        'a-alert': {
          name: 'AAlert',
          props: ['message', 'description'],
          template: '<div class="alert-stub">{{ message }} {{ description }}</div>',
        },
        'a-tag': { name: 'ATag', template: '<span class="tag-stub"><slot /></span>' },
        'a-spin': true,
        'a-popconfirm': true,
        'a-button': { name: 'AButton', template: '<button type="button"><slot /></button>' },
      },
    },
  })
}

beforeEach(() => {
  apiMock.getIsolationStatus.mockReset()
  apiMock.setIsolationLevel.mockReset()
  apiMock.prepareContainerRuntime.mockReset()
  apiMock.clearIsolationLevel.mockReset()
  apiMock.revokeIsolationArtifacts.mockReset()
  modalMock.confirm.mockClear()
  messageMock.error.mockClear()
  messageMock.success.mockClear()
  messageMock.info.mockClear()
  messageMock.warning.mockClear()
  apiMock.getIsolationStatus.mockResolvedValue(statusPayload())
})

describe('IsolationPanel 的档位集合', () => {
  it('只渲染四个受支持档位，不再出现已移除的模式', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const text = wrapper.text()
    for (const label of ['无隔离', '作业对象', '低完整性', '容器（Kiri）']) {
      expect(text, `缺少档位 ${label}`).toContain(label)
    }
    expect(text).not.toContain('受限令牌')
    expect(text).not.toContain('应用容器')
  })

  it('请求值与实际值不同：显式展示降级与原因', async () => {
    apiMock.getIsolationStatus.mockResolvedValue(
      statusPayload({
        requested: 'low_il',
        effective: 'job',
        degraded: true,
        reasons: ['Low 完整性级别不可设置'],
      }),
    )
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.text()).toContain('已降级')
    expect(wrapper.text()).toContain('Low 完整性级别不可设置')
  })

  it('不可选档位置灰，并显示本机原因', async () => {
    apiMock.getIsolationStatus.mockResolvedValue(
      statusPayload({
        selectable: ['none', 'job', 'low_il'],
        unavailable_reasons: { container: '未检测到 Kiri' },
      }),
    )
    const wrapper = mountPanel()
    await flushPromises()

    const containerCard = wrapper.findAll('button').find(item => item.text().includes('容器（Kiri）'))
    expect(containerCard).toBeTruthy()
    expect(containerCard!.attributes('disabled')).toBeDefined()
    expect(containerCard!.text()).toContain('未检测到 Kiri')
  })
})

describe('IsolationPanel 的切换动作', () => {
  it('选低完整性：直接提交，不需二次确认', async () => {
    apiMock.getIsolationStatus.mockResolvedValue(
      statusPayload({ effective: 'job', requested: 'job' }),
    )
    apiMock.setIsolationLevel.mockResolvedValue(statusPayload())
    const wrapper = mountPanel()
    await flushPromises()

    const card = wrapper.findAll('button').find(item => item.text().includes('低完整性'))
    await card!.trigger('click')
    await flushPromises()

    expect(modalMock.confirm).not.toHaveBeenCalled()
    expect(apiMock.setIsolationLevel).toHaveBeenCalledWith('low_il', expect.any(String), false)
  })

  it('选无隔离：必须先确认，再带 acknowledge 提交', async () => {
    apiMock.getIsolationStatus.mockResolvedValue(statusPayload())
    apiMock.setIsolationLevel.mockResolvedValue(
      statusPayload({ requested: 'none', effective: 'none', below_floor: true, user_selected: true }),
    )
    const wrapper = mountPanel()
    await flushPromises()

    const card = wrapper.findAll('button').find(item => item.text().includes('无隔离'))
    await card!.trigger('click')
    await flushPromises()

    expect(modalMock.confirm).toHaveBeenCalledTimes(1)
    expect(apiMock.setIsolationLevel).toHaveBeenCalledWith('none', expect.any(String), true)
  })
})

describe('IsolationPanel 的容器运行时', () => {
  it('未安装时显示版本、原因与下载入口', async () => {
    apiMock.getIsolationStatus.mockResolvedValue(containerStatus())
    const wrapper = mountPanel()
    await flushPromises()

    const box = wrapper.find('[data-testid="iso-container-runtime"]')
    expect(box.exists()).toBe(true)
    expect(box.text()).toContain('Kiri 0.2.2')
    expect(box.text()).toContain('未检测到 Kiri')
    expect(wrapper.find('[data-testid="iso-container-prepare"]').exists()).toBe(true)
  })

  it('已就绪时不再显示下载入口', async () => {
    apiMock.getIsolationStatus.mockResolvedValue(
      containerStatus({
        container: {
          ...containerStatus().container,
          installed: true,
          socket: '/run/kiri/docker.sock',
          engine: { engine_version: '0.2.2', api_version: '1.47' },
          reason: '',
        },
      }),
    )
    const wrapper = mountPanel()
    await flushPromises()

    expect(wrapper.find('[data-testid="iso-container-runtime"]').text()).toContain('已就绪')
    expect(wrapper.find('[data-testid="iso-container-prepare"]').exists()).toBe(false)
  })

  it('点下载：调用 prepare，并把安装命令留在面板上（不自动提权）', async () => {
    apiMock.getIsolationStatus.mockResolvedValue(containerStatus())
    apiMock.prepareContainerRuntime.mockResolvedValue({
      ...containerStatus(),
      prepare: {
        ok: false,
        downloaded: '/home/u/.chiron/runtimes/kiri/0.2.2/kiri-lite.deb',
        error: '已下载并校验安装包，安装/启用需要管理员动作：sudo dpkg -i <pkg>',
      },
    })
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.find('[data-testid="iso-container-prepare"]').trigger('click')
    await flushPromises()

    expect(apiMock.prepareContainerRuntime).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('sudo dpkg -i')
    expect(messageMock.info).toHaveBeenCalled()
  })
})
