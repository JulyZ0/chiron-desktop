import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * `DepsView` 的五个会误导用户的点：
 *
 *  1. **安装必须二次确认**：点"安装"只是弹确认框（显示规格 + "会改运行环境 / 装完要重启"），
 *     确认之后才发请求 —— 装包改的是整台机器共享的运行环境；
 *  2. **命令行式安装必须先过前端校验**：非法规格（分号/`-r`/路径）要在按下按钮之前
 *     就显示原因并禁用按钮；
 *  3. **"可升级"徽标只在真的有更新时出现**：同一版本不能提示升级；
 *  4. **安装走后台任务 + 轮询**：终态到达后必须刷新包列表；
 *  5. **快照恢复先看差异再确认**：恢复会卸载"快照里没有的包"，必须先 dry_run 预览差异、
 *     再二次确认，确认后走后台任务。
 */

const apiMocks = vi.hoisted(() => ({
  listDeps: vi.fn(),
  searchPypi: vi.fn(),
  installDep: vi.fn(),
  uninstallDep: vi.fn(),
  getDepJob: vi.fn(),
  listDepSnapshots: vi.fn(),
  createDepSnapshot: vi.fn(),
  previewDepRestore: vi.fn(),
  restoreDepSnapshot: vi.fn(),
}))

vi.mock('../../../api/deps', async importOriginal => {
  const actual = await importOriginal<typeof import('../../../api/deps')>()
  return {
    // 校验与版本比较用**真实实现**：那正是这一页的安全边界，不该在测试里被替换掉。
    validatePipSpec: actual.validatePipSpec,
    validatePackageName: actual.validatePackageName,
    isNewerVersion: actual.isNewerVersion,
    isTerminalJob: actual.isTerminalJob,
    // 冲突详情解析也是真实实现：它决定"409 是冲突还是任务在跑"，糊掉就没意义了。
    depConflictOf: actual.depConflictOf,
    MAX_SPEC_LEN: actual.MAX_SPEC_LEN,
    MAX_RESTORE_INSTALLS: actual.MAX_RESTORE_INSTALLS,
    PIP_SPEC_RE: actual.PIP_SPEC_RE,
    PACKAGE_NAME_RE: actual.PACKAGE_NAME_RE,
    listDeps: apiMocks.listDeps,
    searchPypi: apiMocks.searchPypi,
    installDep: apiMocks.installDep,
    uninstallDep: apiMocks.uninstallDep,
    getDepJob: apiMocks.getDepJob,
    listDepSnapshots: apiMocks.listDepSnapshots,
    createDepSnapshot: apiMocks.createDepSnapshot,
    previewDepRestore: apiMocks.previewDepRestore,
    restoreDepSnapshot: apiMocks.restoreDepSnapshot,
  }
})

import DepsView from '../DepsView.vue'

// jsdom 没有 matchMedia，而 antd 的 Table 通过 useBreakpoint 在挂载时读它。
// 只在本文件补齐（不要放进全局 test-setup：那会让别的用例误以为环境支持响应式）。
if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    configurable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}

const PACKAGES = {
  python: '3.12.0',
  executable: '/opt/venv/bin/python',
  // 桌面运行时 venv 里没有 pip，引擎会退回随包 uv（见 python-engine/app/api/deps.py）
  tool: 'uv' as const,
  packages: [
    { name: 'fastapi', version: '0.115.0' },
    { name: 'requests', version: '2.31.0' },
  ],
  count: 2,
}

const SNAPSHOTS = {
  snapshots: [
    {
      id: '20250101T000000000Z-abcdef',
      created_at: 1735689600,
      trigger: 'install:requests==2.32.0',
      label: 'before upgrade',
      package_count: 2,
    },
  ],
  count: 1,
  retention: 20,
}

const RESTORE_DIFF = {
  missing: [{ name: 'pillow', version: '10.0.0' }],
  extra: [{ name: 'legacy-pkg', version: '0.1' }],
  changed: [{ name: 'requests', from: '2.32.0', to: '2.31.0' }],
  unchanged: 1,
  install_count: 2,
  uninstall_count: 1,
  total_changes: 3,
  limit: 50,
  exceeds_limit: false,
}

function mountView() {
  // Modal 走 Teleport 到 body：不 attach 就查不到弹窗内容。
  return mount(DepsView, { attachTo: document.body })
}

/** 弹窗里的主按钮（确认）。antd 的 footer 在 body 里，不在 wrapper 里。 */
function modalOkButton(): HTMLButtonElement {
  const buttons = Array.from(
    document.body.querySelectorAll<HTMLButtonElement>('.ant-modal-footer .ant-btn-primary'),
  )
  const hit = buttons.at(-1)
  if (!hit) throw new Error('弹窗里没有确认按钮')
  return hit
}

function modalText(): string {
  const modals = Array.from(document.body.querySelectorAll<HTMLElement>('.ant-modal'))
  return modals.map(node => node.textContent ?? '').join('\n')
}

function cmdInput(wrapper: ReturnType<typeof mountView>) {
  // antd 的 Input 会把 class 透传到 <input> 本身，所以直接按行选择更稳。
  return wrapper.find('.cmd-row input')
}

function installButton(wrapper: ReturnType<typeof mountView>) {
  const buttons = wrapper.findAll('.cmd-row button')
  if (!buttons.length) throw new Error('cmd-row 里没有按钮')
  // antd 会在**两个汉字**之间插入空格（"安 装"），比较前先去掉空白。
  const hit = buttons.find(b => b.text().replace(/\s/g, '').includes('安装'))
  if (!hit) throw new Error(`命令行安装按钮不存在：${buttons.map(b => JSON.stringify(b.text())).join(', ')}`)
  return hit
}

describe('DepsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
    // 重启横幅跨页持久（sessionStorage）；用例之间必须清掉，否则前一个用例的
    // "需要重启"会漏到下一个用例里，让断言变成"永远为真"。
    sessionStorage.clear()
    apiMocks.listDeps.mockResolvedValue(PACKAGES)
    apiMocks.searchPypi.mockResolvedValue({ query: '', results: [], count: 0, found: false })
    apiMocks.listDepSnapshots.mockResolvedValue(SNAPSHOTS)
    apiMocks.createDepSnapshot.mockResolvedValue(SNAPSHOTS.snapshots[0])
    apiMocks.previewDepRestore.mockResolvedValue({
      dry_run: true,
      snapshot: SNAPSHOTS.snapshots[0],
      diff: RESTORE_DIFF,
      job: null,
    })
    apiMocks.installDep.mockResolvedValue({
      id: 'job1',
      kind: 'install',
      target: 'requests==2.31.0',
      status: 'running',
      message: '',
      progress: 10,
      output: '',
      created_at: 0,
    })
    apiMocks.getDepJob.mockResolvedValue({
      id: 'job1',
      kind: 'install',
      target: 'requests==2.31.0',
      status: 'succeeded',
      message: 'installed',
      progress: 100,
      output: '',
      created_at: 0,
      finished_at: 1,
    })
  })

  it('渲染已安装包并展示引擎解释器', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('fastapi')
    expect(wrapper.text()).toContain('0.115.0')
    // 解释器路径必须显示：用户据此确认"装的是不是同一个 venv"
    expect(wrapper.text()).toContain('/opt/venv/bin/python')
    wrapper.unmount()
  })

  it('展示当前包管理器（uv），而不是让用户猜为什么没有 pip', async () => {
    const wrapper = mountView()
    await flushPromises()

    const tool = wrapper.find('.engine-tool')
    expect(tool.exists()).toBe(true)
    expect(tool.text()).toContain('uv')
    // 正常路径不该出现降级提示
    expect(wrapper.find('.ant-alert-warning').exists()).toBe(false)
    wrapper.unmount()
  })

  it('无 pip/uv（tool=metadata）时提示只能读，并说明原因', async () => {
    apiMocks.listDeps.mockResolvedValue({
      ...PACKAGES,
      tool: 'metadata',
      degraded: true,
      tool_reason: 'uv not found (checked CHIRON_UV_BIN, CHIRON_RESOURCES_DIR, PATH, runtime dir)',
    })

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('.engine-tool').text()).toContain('metadata')
    const alert = wrapper.find('.ant-alert-warning')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('CHIRON_UV_BIN')
    wrapper.unmount()
  })

  it('非法规格：立即显示原因并禁用安装按钮，且不发起请求', async () => {
    const wrapper = mountView()
    await flushPromises()

    await cmdInput(wrapper).setValue('requests; rm -rf /')

    expect(wrapper.find('.cmd-error').exists()).toBe(true)
    expect(installButton(wrapper).attributes('disabled')).toBeDefined()
    await installButton(wrapper).trigger('click')
    await flushPromises()
    expect(apiMocks.installDep).not.toHaveBeenCalled()
    // 非法输入连确认框都不该弹
    expect(document.body.querySelector('.ant-modal')).toBeNull()
    wrapper.unmount()
  })

  it('合法规格：先弹确认框（显示规格与风险），确认后才创建任务 → 轮询 → 刷新', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(apiMocks.listDeps).toHaveBeenCalledTimes(1)

    await cmdInput(wrapper).setValue('requests==2.31.0')
    expect(wrapper.find('.cmd-error').exists()).toBe(false)
    await installButton(wrapper).trigger('click')
    await flushPromises()

    // 点"安装"只是打开确认框：规格、风险、自动快照三件事都必须写出来
    expect(apiMocks.installDep).not.toHaveBeenCalled()
    const text = modalText()
    expect(text).toContain('requests==2.31.0')
    expect(text).toContain('运行环境')
    expect(text).toContain('重启')
    expect(text).toContain('快照')

    modalOkButton().click()
    await flushPromises()
    expect(apiMocks.installDep).toHaveBeenCalledWith('requests==2.31.0')

    // 轮询间隔 1200ms（见组件内 POLL_INTERVAL_MS）
    await new Promise(resolve => setTimeout(resolve, 1500))
    await flushPromises()

    expect(apiMocks.getDepJob).toHaveBeenCalled()
    expect(wrapper.text()).toContain('已完成')
    // 终态后必须重新取列表：否则界面停在旧清单上
    expect(apiMocks.listDeps).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('搜索结果里同版本不提示升级，新版本才提示', async () => {
    apiMocks.searchPypi.mockResolvedValue({
      query: 'requests',
      results: [
        { name: 'requests', version: '2.31.0', summary: 'same' },
        { name: 'fastapi', version: '0.120.0', summary: 'newer' },
      ],
      count: 2,
      found: true,
    })

    const wrapper = mountView()
    await flushPromises()

    const input = wrapper.find('.search-row input')
    await input.setValue('requests')
    await wrapper.find('.search-row button').trigger('click')
    await flushPromises()

    const rows = wrapper.findAll('.pypi-row')
    expect(rows).toHaveLength(2)
    // requests：与已安装版本相同 ⇒ 不提示升级，安装按钮禁用
    expect(rows[0]!.text()).toContain('已安装')
    expect(rows[0]!.find('button').attributes('disabled')).toBeDefined()
    // fastapi：PyPI 版本更高 ⇒ 可升级
    expect(rows[1]!.text()).toContain('可升级')
    expect(rows[1]!.find('button').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('快照列表可见，手动创建会带上标签并刷新列表', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(apiMocks.listDepSnapshots).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('install:requests==2.32.0')
    expect(wrapper.text()).toContain('before upgrade')

    const createButton = wrapper.findAll('button').find(b => b.text().replace(/\s/g, '').includes('创建快照'))
    expect(createButton).toBeTruthy()
    await createButton!.trigger('click')
    await flushPromises()

    const labelInput = document.body.querySelector<HTMLInputElement>('.snapshot-create input')
    expect(labelInput).toBeTruthy()
    labelInput!.value = '手动拍的'
    labelInput!.dispatchEvent(new Event('input'))
    await flushPromises()

    modalOkButton().click()
    await flushPromises()

    expect(apiMocks.createDepSnapshot).toHaveBeenCalledWith('手动拍的')
    expect(apiMocks.listDepSnapshots).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('恢复快照：先 dry_run 预览差异并二次确认，确认后才真的恢复', async () => {
    apiMocks.restoreDepSnapshot.mockResolvedValue({
      dry_run: false,
      snapshot: SNAPSHOTS.snapshots[0],
      diff: RESTORE_DIFF,
      job: {
        id: 'restore1',
        kind: 'restore',
        target: SNAPSHOTS.snapshots[0]!.id,
        status: 'running',
        message: '',
        progress: 20,
        output: '',
        created_at: 0,
      },
    })

    const wrapper = mountView()
    await flushPromises()

    const restoreButton = wrapper.findAll('button').find(b => b.text().replace(/\s/g, '') === '恢复')
    expect(restoreButton).toBeTruthy()
    await restoreButton!.trigger('click')
    await flushPromises()

    // 第一步：只算差异，绝不执行
    expect(apiMocks.previewDepRestore).toHaveBeenCalledWith('20250101T000000000Z-abcdef')
    expect(apiMocks.restoreDepSnapshot).not.toHaveBeenCalled()

    const text = modalText()
    // 差异摘要必须让用户看到"会卸载什么、会装什么、哪些会改版本"
    expect(text).toContain('legacy-pkg')
    expect(text).toContain('pillow')
    expect(text).toContain('2.32.0')
    expect(text).toContain('2.31.0')

    modalOkButton().click()
    await flushPromises()
    expect(apiMocks.restoreDepSnapshot).toHaveBeenCalledWith('20250101T000000000Z-abcdef')

    // 恢复也是后台任务：轮询到终态后刷新包清单与快照列表
    await new Promise(resolve => setTimeout(resolve, 1500))
    await flushPromises()
    expect(apiMocks.getDepJob).toHaveBeenCalled()
    expect(apiMocks.listDeps).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('差异超过单次上限：确认按钮禁用（引擎也会 400，但界面先挡住）', async () => {
    apiMocks.previewDepRestore.mockResolvedValue({
      dry_run: true,
      snapshot: SNAPSHOTS.snapshots[0],
      diff: { ...RESTORE_DIFF, install_count: 51, exceeds_limit: true, limit: 50, total_changes: 52 },
      job: null,
    })

    const wrapper = mountView()
    await flushPromises()

    const restoreButton = wrapper.findAll('button').find(b => b.text().replace(/\s/g, '') === '恢复')
    await restoreButton!.trigger('click')
    await flushPromises()

    expect(modalText()).toContain('50')
    expect(modalOkButton().disabled).toBe(true)
    wrapper.unmount()
  })

  it('安装成功且引擎要求重启：顶部出现常驻横幅（不是一闪而过的 toast）', async () => {
    apiMocks.getDepJob.mockResolvedValue({
      id: 'job1',
      kind: 'install',
      target: 'requests==2.31.0',
      status: 'succeeded',
      message: 'installed',
      progress: 100,
      output: '',
      restart_required: true,
      created_at: 0,
      finished_at: 1,
    })

    const wrapper = mountView()
    await flushPromises()

    await cmdInput(wrapper).setValue('requests==2.31.0')
    await installButton(wrapper).trigger('click')
    await flushPromises()
    modalOkButton().click()
    await flushPromises()

    await new Promise(resolve => setTimeout(resolve, 1500))
    await flushPromises()

    // 横幅必须**留在页面上**：包已经装上，但引擎进程里还是旧模块。
    expect(wrapper.find('.restart-banner').exists()).toBe(true)
    expect(wrapper.text()).toContain('需重启引擎')
    // 也说明该怎么重启（我们刻意不提供"重启引擎"的 HTTP 端点）
    expect(wrapper.text()).toContain('重新打开应用')
    // 跨页/刷新仍然可见
    expect(sessionStorage.getItem('chiron:deps:restart-required')).toBe('1')

    // 用户确认重启后横幅消失（不新造危险端点，只有本地状态与刷新）
    const ack = wrapper.findAll('.restart-banner button').at(-1)
    expect(ack).toBeTruthy()
    await ack!.trigger('click')
    await flushPromises()
    expect(wrapper.find('.restart-banner').exists()).toBe(false)
    expect(sessionStorage.getItem('chiron:deps:restart-required')).toBeNull()
    wrapper.unmount()
  })

  it('引擎的 pip check 报冲突（409）：把冲突原文摆出来，而不是只说"操作失败"', async () => {
    apiMocks.installDep.mockRejectedValue({
      response: {
        status: 409,
        data: {
          error: 'dependency conflicts detected',
          params: {
            conflicts: 'requests 2.31.0 has requirement urllib3<2, but you have urllib3 2.0.7.',
          },
        },
      },
    })

    const wrapper = mountView()
    await flushPromises()

    await cmdInput(wrapper).setValue('requests==2.31.0')
    await installButton(wrapper).trigger('click')
    await flushPromises()
    modalOkButton().click()
    await flushPromises()

    const banner = wrapper.find('.conflict-banner')
    expect(banner.exists()).toBe(true)
    // 冲突文本是用户唯一的行动依据：必须能读到"哪个包要什么版本"
    expect(banner.text()).toContain('urllib3<2')
    expect(wrapper.text()).toContain('依赖冲突')
    // 冲突时不该出现"需要重启"——环境根本没改
    expect(wrapper.find('.restart-banner').exists()).toBe(false)
    wrapper.unmount()
  })

  it('快照降级到文件后端时明确提示"只在本机可见"', async () => {
    apiMocks.listDepSnapshots.mockResolvedValue({ ...SNAPSHOTS, storage: 'file' })

    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('本机文件')
    wrapper.unmount()
  })
})
