import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * 「执行隔离」面板里**启用受限令牌助手失败**这条路径的展示契约。
 *
 * 真机事故：用户点「启用助手（需要管理员授权）」，界面回
 * 「受限令牌执行助手：启用助手失败：本机无法启用此档位。」
 *
 * 这句话有两个问题，正是这组用例要钉住的：
 *
 *  1. 「本机无法启用此档位」不是引擎说的任何一句话 —— 它是前端 `catch` 里的**兜底
 *     文案**。真正的病因是网关少注册了 `/v1/system/isolation/helper` 三条路由
 *     （404）。兜底文案把 "404" 这个唯一线索盖掉了，于是看起来像"本机能力不足"，
 *     排查方向直接跑偏到令牌权限/UAC 上去。⇒ 后端给了原文就必须显示原文。
 *  2. 引擎明确回 `ok:false` + `reason` 时，界面上不能只剩一条几秒后消失的 toast：
 *     UAC 弹窗会把焦点抢走，用户回来时 toast 已经没了，只剩"助手未运行"的状态，
 *     能做的最多是把同一个按钮再点一次。⇒ 原因要**留在面板上**，并带上后端给的
 *     可执行指引（`hint`）。
 */
const apiMock = vi.hoisted(() => ({
  getIsolationStatus: vi.fn(),
  getIsolationHelper: vi.fn(),
  setIsolationLevel: vi.fn(),
  prepareContainerRuntime: vi.fn(),
  clearIsolationLevel: vi.fn(),
  revokeIsolationArtifacts: vi.fn(),
  startIsolationHelper: vi.fn(),
  stopIsolationHelper: vi.fn(),
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
  // 启用助手前的二次确认：直接点「确定」，把测试聚焦在失败展示上。
  confirm: vi.fn((opts: { onOk?: () => void }) => {
    opts.onOk?.()
  }),
}))

vi.mock('ant-design-vue', async importOriginal => {
  const actual = await importOriginal<typeof import('ant-design-vue')>()
  return { ...actual, message: messageMock, Modal: modalMock }
})

import IsolationPanel from '../IsolationPanel.vue'

/** 后端某台机器的档位快照：`restricted` 不可选，因为助手没起来。 */
function statusPayload(overrides: Record<string, unknown> = {}) {
  return {
    requested: 'restricted',
    effective: 'low_il',
    degraded: true,
    floor: 'low_il',
    below_floor: false,
    user_selected: false,
    reasons: ['受限令牌执行层不可用（CreateProcessAsUserW 失败 err=5）'],
    downgrade_allowed: true,
    selectable: ['none', 'job', 'low_il', 'appcontainer'],
    acknowledge_required: ['none'],
    unavailable_reasons: { restricted: '受限令牌执行层不可用（CreateProcessAsUserW 失败 err=5）' },
    capabilities: {},
    appcontainer_name: 'chiron-sandbox',
    appcontainer_workspace: 'C:\\work',
    runtime_override: null,
    ...overrides,
  }
}

function helperPayload(overrides: Record<string, unknown> = {}) {
  return {
    available: false,
    detail: '提权 helper 未启动（缺少端口文件）',
    engine_elevated: false,
    supported: true,
    ...overrides,
  }
}

function mountPanel() {
  return mount(IsolationPanel, {
    global: {
      stubs: {
        // stub 必须带 name，否则按组件名查找会落空（见 RunModePanel.spec.ts 的同类说明）；
        // 同时要把 message/description 渲染出来 —— 用例断言的正是"面板上留下了什么"。
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

/** 找到「启用助手」按钮并点它（二次确认由 Modal stub 自动同意）。 */
async function clickEnable(wrapper: ReturnType<typeof mountPanel>) {
  const button = wrapper.findAll('button').find(item => item.text().includes('启用助手'))
  expect(button, '助手未运行时必须给出「启用助手」入口，否则用户无法修复').toBeTruthy()
  await button!.trigger('click')
  await flushPromises()
}

beforeEach(() => {
  apiMock.getIsolationStatus.mockReset()
  apiMock.getIsolationHelper.mockReset()
  apiMock.startIsolationHelper.mockReset()
  apiMock.stopIsolationHelper.mockReset()
  apiMock.getIsolationStatus.mockResolvedValue(statusPayload())
  apiMock.getIsolationHelper.mockResolvedValue(helperPayload())
  messageMock.error.mockClear()
  messageMock.success.mockClear()
  messageMock.info.mockClear()
  messageMock.warning.mockClear()
})

describe('IsolationPanel 启用助手失败的展示', () => {
  it('引擎回了 ok:false + reason：原文进提示，且留在面板上；不用通用兜底', async () => {
    const reason = '提权助手已启动但未就绪（提权 helper 未启动（缺少端口文件））'
    apiMock.startIsolationHelper.mockResolvedValue({
      ...statusPayload(),
      ok: false,
      cancelled: false,
      reason,
      hint: '启用「受限令牌」档需要在系统授权（UAC）窗口点一次「是」。',
      helper: helperPayload({ detail: reason }),
    })

    const wrapper = mountPanel()
    await flushPromises()
    await clickEnable(wrapper)

    const shown = String(messageMock.error.mock.calls[0]?.[0] ?? '')
    expect(shown, '原因必须来自后端').toContain(reason)
    expect(shown, '不得退化成"本机无法启用此档位"这种空话').not.toContain('本机无法启用此档位')

    // 留在面板上：toast 几秒就没了，而用户要拿着这句话去排障
    expect(wrapper.text()).toContain(reason)
    expect(wrapper.text(), '后端给的可执行指引要一起渲染').toContain('UAC')
  })

  it('请求本身失败（网关 404/超时）时显示后端原文，而不是"本机能力不足"', async () => {
    apiMock.startIsolationHelper.mockRejectedValue({
      response: { status: 404, data: { error: '404 page not found' } },
    })

    const wrapper = mountPanel()
    await flushPromises()
    await clickEnable(wrapper)

    const shown = String(messageMock.error.mock.calls[0]?.[0] ?? '')
    expect(shown, '网关/引擎原文是指向病因的唯一线索').toContain('404 page not found')
    expect(shown).not.toContain('本机无法启用此档位')
  })

  it('请求失败且没有后端原文时才用兜底文案（且只在那种情况下）', async () => {
    apiMock.startIsolationHelper.mockRejectedValue(new Error('Network Error'))

    const wrapper = mountPanel()
    await flushPromises()
    await clickEnable(wrapper)

    const shown = String(messageMock.error.mock.calls[0]?.[0] ?? '')
    expect(shown).toContain('本机无法启用此档位')
  })

  it('用户取消 UAC：用 info 而不是 error，且不留下失败痕迹', async () => {
    apiMock.startIsolationHelper.mockResolvedValue({
      ...statusPayload(),
      ok: false,
      cancelled: true,
      reason: '启动提权 helper 被拒绝，ShellExecuteW=5（用户取消了 UAC 授权）',
      hint: '',
      helper: helperPayload(),
    })

    const wrapper = mountPanel()
    await flushPromises()
    await clickEnable(wrapper)

    expect(messageMock.error, '取消授权是用户的正常选择，不该报错').not.toHaveBeenCalled()
    expect(messageMock.info).toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('启用助手失败')
  })
})


/**
 * 容器挡位（Kiri）：依赖**不内置**，面板要说清"缺什么、能不能下载、怎么装"。
 */
describe('IsolationPanel 的容器挡位运行时', () => {
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

  it('未安装时显示运行时版本、原因与下载入口', async () => {
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
