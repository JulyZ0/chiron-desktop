import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

/**
 * `DepsView` 的三个会误导用户的点：
 *
 *  1. **命令行式安装必须先过前端校验**：非法规格（分号/`-r`/路径）要在按下按钮之前
 *     就显示原因并禁用按钮 —— 否则用户只会看到一次 400，且不知道哪里不对；
 *  2. **"可升级"徽标只在真的有更新时出现**：同一版本不能提示升级（否则用户会反复点）；
 *  3. **安装走后台任务 + 轮询**：终态到达后必须刷新包列表，否则界面停在"安装中"、
 *     而包其实已经装好了。
 */

const apiMocks = vi.hoisted(() => ({
  listDeps: vi.fn(),
  searchPypi: vi.fn(),
  installDep: vi.fn(),
  uninstallDep: vi.fn(),
  getDepJob: vi.fn(),
}))

vi.mock('../../../api/deps', async importOriginal => {
  const actual = await importOriginal<typeof import('../../../api/deps')>()
  return {
    // 校验与版本比较用**真实实现**：那正是这一页的安全边界，不该在测试里被替换掉。
    validatePipSpec: actual.validatePipSpec,
    validatePackageName: actual.validatePackageName,
    isNewerVersion: actual.isNewerVersion,
    isTerminalJob: actual.isTerminalJob,
    MAX_SPEC_LEN: actual.MAX_SPEC_LEN,
    PIP_SPEC_RE: actual.PIP_SPEC_RE,
    PACKAGE_NAME_RE: actual.PACKAGE_NAME_RE,
    listDeps: apiMocks.listDeps,
    searchPypi: apiMocks.searchPypi,
    installDep: apiMocks.installDep,
    uninstallDep: apiMocks.uninstallDep,
    getDepJob: apiMocks.getDepJob,
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
  packages: [
    { name: 'fastapi', version: '0.115.0' },
    { name: 'requests', version: '2.31.0' },
  ],
  count: 2,
}

function mountView() {
  return mount(DepsView)
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
    apiMocks.listDeps.mockResolvedValue(PACKAGES)
    apiMocks.searchPypi.mockResolvedValue({ query: '', results: [], count: 0, found: false })
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

  it('非法规格：立即显示原因并禁用安装按钮，且不发起请求', async () => {
    const wrapper = mountView()
    await flushPromises()

    await cmdInput(wrapper).setValue('requests; rm -rf /')

    expect(wrapper.find('.cmd-error').exists()).toBe(true)
    expect(installButton(wrapper).attributes('disabled')).toBeDefined()
    await installButton(wrapper).trigger('click')
    expect(apiMocks.installDep).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('合法规格：创建任务 → 轮询到终态 → 刷新包清单', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(apiMocks.listDeps).toHaveBeenCalledTimes(1)

    await cmdInput(wrapper).setValue('requests==2.31.0')
    expect(wrapper.find('.cmd-error').exists()).toBe(false)
    await installButton(wrapper).trigger('click')
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
})
