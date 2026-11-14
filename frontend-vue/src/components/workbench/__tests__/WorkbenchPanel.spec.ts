/**
 * WorkbenchPanel 的分栏手柄侧向契约。
 *
 * 只测**行为契约**，不测像素。这里要守的两件事都是"错了也不报错"的：
 *
 *  1. 不传 `handleSide` 时兜底贴右。默认布局下工作台在**最左**，与主会话相邻的
 *     是它的右边缘 —— 兜底写成 `'left'` 的话，手柄会贴到窗口外缘，用户再也拖不动
 *     这一栏，而且界面上没有任何提示。
 *  2. 换边时面板与手柄必须**一起**换。只换一半的后果是把手在一边、分隔线在另一边，
 *     线出现在窗口外缘，主会话与面板之间反而没有界线。
 *
 * 尺寸夹取（下限 300 / 上限容器一半）不在断言范围：起始宽度与容器同源，
 * jsdom 下无法构造出「起始窄于上限」的矩形，硬测只会得到脆弱断言。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'

import WorkbenchPanel from '../WorkbenchPanel.vue'
import CheckpointTimeline from '../CheckpointTimeline.vue'

/**
 * 把目录请求拦在"还没回来"的状态，用来构造切项目时的竞态。
 *
 * 用 `vi.hoisted` 而不是模块级变量：`vi.mock` 的工厂会被提升到 import 之前，
 * 普通变量在那时还没初始化。
 */
const net = vi.hoisted(() => ({
  calls: [] as Array<{ projectId: string; path: string; resolve: (v: unknown) => void }>,
  /** `readProjectFile` 的返回值：二进制预览那个分支要能构造出 `binary: true` */
  readResult: { path: '', binary: false, size: 0, content: '' },
}))

vi.mock('../../../api/projectFiles', () => ({
  listProjectFiles: (projectId: string, path: string) =>
    new Promise(resolve => {
      net.calls.push({ projectId, path, resolve })
    }),
  readProjectFile: () => Promise.resolve(net.readResult),
  projectFileObjectURL: () => Promise.reject(new Error('unused')),
  isImagePath: () => false,
  isPdfPath: () => false,
}))

/**
 * 还原点页签会真的被渲染，所以它的数据面也要拦掉 —— 否则这里会去打真实网络。
 * 返回空列表即可：本文件要验证的是**回滚事件如何驱动文件树**，不是时间线本身。
 */
vi.mock('../../../api/checkpoints', () => ({
  listProjectCheckpoints: () => Promise.resolve([]),
  restoreCheckpoint: () => Promise.reject(new Error('unused')),
}))

/** 让 resolve 之后串起来的微任务与 Vue 的下一次渲染跑完 */
const flush = () => new Promise(r => setTimeout(r, 0))

function listing(root: string, name: string) {
  return {
    root,
    path: '',
    entries: [{ name, path: name, type: 'file' }],
    truncated: false,
  }
}

/**
 * 不传 `projectId` 再挂载：`watch(immediate)` 会走一次 `reload()`，而 `loadDir`
 * 在没有项目时直接 return —— 测试因此不会发出任何网络请求，也不需要 mock api。
 */
function mountPanel(props: Record<string, unknown> = {}) {
  return mount(WorkbenchPanel, { props })
}

beforeEach(() => {
  localStorage.clear()
})

describe('分栏手柄侧向', () => {
  it('不传 handleSide 时兜底贴右', () => {
    const wrapper = mountPanel()
    const panel = wrapper.find('.workbench-panel')
    expect(panel.classes()).toContain('handle-right')
    expect(panel.classes()).not.toContain('handle-left')
    // 手柄与面板必须同侧：手柄贴错边就点不到
    expect(wrapper.find('.wb-resize').classes()).toContain('handle-right')
  })

  it('handleSide=left 时面板与手柄一起换到左侧', () => {
    const wrapper = mountPanel({ handleSide: 'left' })
    expect(wrapper.find('.workbench-panel').classes()).toContain('handle-left')
    expect(wrapper.find('.wb-resize').classes()).toContain('handle-left')
    // 不能残留旧的一侧，否则两条边框规则会同时命中
    expect(wrapper.find('.workbench-panel').classes()).not.toContain('handle-right')
  })

  it('父布局翻转时两个元素同步跟随', async () => {
    const wrapper = mountPanel({ handleSide: 'right' })
    expect(wrapper.find('.wb-resize').classes()).toContain('handle-right')
    await wrapper.setProps({ handleSide: 'left' })
    expect(wrapper.find('.workbench-panel').classes()).toContain('handle-left')
    expect(wrapper.find('.wb-resize').classes()).toContain('handle-left')
  })

  it('手柄是可键盘操作的 separator', () => {
    const handle = mountPanel().find('.wb-resize')
    expect(handle.attributes('role')).toBe('separator')
    expect(handle.attributes('aria-orientation')).toBe('vertical')
    expect(handle.attributes('tabindex')).toBe('0')
  })
})

/**
 * 切项目时的竞态：这是"面板一直空白/一直加载"的隐蔽来源，两个失效方式都不报错。
 *
 * 构造方式：先挂一个项目、让根目录请求悬在半空，然后在两次响应都还没回来时切到
 * 另一个项目。这正是用户在侧边栏连点两个项目（或刷新按钮连点）时的真实顺序。
 */
describe('切项目时的目录竞态', () => {
  beforeEach(() => {
    net.calls.length = 0
  })

  it('上一个项目晚到的响应不会写进新项目的树', async () => {
    const wrapper = mountPanel({ projectId: 'p1' })
    await flush()
    expect(net.calls).toHaveLength(1)
    expect(net.calls[0].projectId).toBe('p1')

    // 切项目：p1 的根目录请求仍然悬着
    await wrapper.setProps({ projectId: 'p2' })
    await flush()
    const forP2 = net.calls.find(c => c.projectId === 'p2')
    expect(forP2, '切项目后必须真的发出新项目的根目录请求').toBeTruthy()

    // p1 的响应现在才回来：它属于上一棵树，必须被丢弃
    net.calls[0].resolve(listing('C:/p1', 'STALE_从旧项目来.ts'))
    await flush()
    expect(wrapper.text()).not.toContain('STALE_从旧项目来.ts')

    // p2 自己的响应正常落地
    forP2!.resolve(listing('C:/p2', 'FRESH_新项目.ts'))
    await flush()
    expect(wrapper.text()).toContain('FRESH_新项目.ts')
  })

  it('上一个项目的根请求还在飞时，新项目的根请求不能被"加载中"守卫挡住', async () => {
    const wrapper = mountPanel({ projectId: 'p1' })
    await flush()

    // 关键：p1 的请求**不 resolve**，`loadingDirs` 里仍留着 '' 这一项
    await wrapper.setProps({ projectId: 'p2' })
    await flush()

    const rootsForP2 = net.calls.filter(c => c.projectId === 'p2' && c.path === '')
    expect(rootsForP2.length, '新项目的根目录请求被 loadingDirs 吞掉了').toBe(1)

    rootsForP2[0].resolve(listing('C:/p2', '新项目文件.ts'))
    await flush()
    expect(wrapper.text()).toContain('新项目文件.ts')
  })
})

/**
 * 二进制文件（docx / 压缩包等）本来就没有预览可看，提示语写着"用系统程序打开"。
 *
 * 若那里只是一个不能点的 `<div>`，用户读到的就是一句无法执行的建议 —— 而工作台
 * 是只读的，除了"用系统程序打开"，他没有第二条出路。
 */
describe('二进制文件的打开入口', () => {
  beforeEach(() => {
    net.calls.length = 0
    net.readResult = { path: '', binary: true, size: 1024, content: '' }
  })

  afterEach(() => {
    delete (globalThis as { chironDesktop?: unknown }).chironDesktop
  })

  it('二进制提示旁边给出可点击的「用系统程序打开」，并传绝对路径', async () => {
    const openPath = vi.fn(async () => ({ ok: true }))
    ;(globalThis as { chironDesktop?: unknown }).chironDesktop = { openPath }

    const wrapper = mountPanel({ projectId: 'p1' })
    await flush()
    net.calls[0].resolve(listing('C:\\proj', '简历.docx'))
    await flush()

    // 点文件树里的那一行 → 走 readProjectFile → binary: true
    await wrapper.find('.wb-row').trigger('click')
    await flush()
    expect(wrapper.text()).toContain('简历.docx')

    const button = wrapper.findAll('button').find(b => b.text().includes('用系统程序打开'))
    expect(button, '二进制提示里缺少可点击的打开入口').toBeTruthy()
    await button!.trigger('click')
    await flush()

    // 必须拼成绝对路径：openPath 只接受绝对路径，相对路径会被主进程拒掉
    expect(openPath).toHaveBeenCalledWith({ path: 'C:\\proj\\简历.docx' })
  })
})

/**
 * 回滚会改写工作区，但工作台的文件树是**独立于时间线**的另一份缓存。
 *
 * 不重拉的后果不是报错，而是"切过去还看见已经不存在的文件"——用户会据此以为
 * 回滚没生效，或者更糟：点开一个刚被删掉的文件去预览。这条必须靠事件联动守住。
 */
describe('回滚后文件树失效', () => {
  beforeEach(() => {
    net.calls.length = 0
  })

  it('时间线发出 restored 后重新拉根目录', async () => {
    const wrapper = mountPanel({ projectId: 'p1' })
    await flush()
    expect(net.calls).toHaveLength(1)
    net.calls[0].resolve(listing('C:/p1', 'OLD.ts'))
    await flush()
    expect(wrapper.text()).toContain('OLD.ts')

    // 切到还原点页签，让 CheckpointTimeline 真正挂载
    const tabs = wrapper.findAll('.wb-tab')
    await tabs[tabs.length - 1].trigger('click')
    await flush()

    const timeline = wrapper.findComponent(CheckpointTimeline)
    expect(timeline.exists(), '还原点页签没有渲染出时间线组件').toBe(true)
    timeline.vm.$emit('restored', {
      ok: true,
      written: [], added: [], deleted: [], pruned_dirs: [],
      failed: [], skipped_removed: [], protected_removed: [], unsupported: [],
    })
    await flush()

    // 必须真的发出新的根目录请求：只清缓存不重拉的话，树会停在空树上
    const reissue = net.calls.filter(c => c.path === '' && c.projectId === 'p1')
    expect(reissue.length, '回滚后没有重拉项目根目录').toBe(2)

    reissue[1].resolve(listing('C:/p1', 'NEW.ts'))
    await flush()

    // 文件树只在 files 页签渲染；切回去看它是否已经换成了回滚后的内容
    await wrapper.findAll('.wb-tab')[0].trigger('click')
    await flush()
    expect(wrapper.text()).toContain('NEW.ts')
    expect(wrapper.text()).not.toContain('OLD.ts')
  })
})
