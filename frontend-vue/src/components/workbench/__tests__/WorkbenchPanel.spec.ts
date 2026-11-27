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
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'

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
  searchResult: { files: [] as Array<{ path: string; line: number; text: string }>, truncated: false, unsupported: false },
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
  searchProjectContent: () => Promise.resolve(net.searchResult),
}))

/**
 * 还原点页签会真的被渲染，所以它的数据面也要拦掉 —— 否则这里会去打真实网络。
 * 返回空列表即可：本文件要验证的是**回滚事件如何驱动文件树**，不是时间线本身。
 */
vi.mock('../../../api/checkpoints', () => ({
  listProjectCheckpoints: () => Promise.resolve({ checkpoints: [], stats: null }),
  restoreCheckpoint: () => Promise.reject(new Error('unused')),
  previewCheckpointDiff: () => Promise.reject(new Error('unused')),
  runCheckpointGc: () => Promise.reject(new Error('unused')),
}))

/**
 * 改动页签 / 单文件 diff 的数据面。
 *
 * diff 请求故意用可手动 resolve 的 Promise：要能断言"请求发出去了"和"回来之后
 * 才渲染"，而不是只测最终文本。
 */
const changesNet = vi.hoisted(() => ({
  diffs: [] as Array<{ projectId: string; path: string; resolve: (v: unknown) => void }>,
  /** N5 P1：stage/unstage 调用记录与返回的下一份 status */
  indexCalls: [] as Array<{ action: 'stage' | 'unstage'; projectId: string; body: unknown }>,
  nextChanges: null as Record<string, unknown> | null,
  changes: {
    repo: true,
    branch: 'main',
    head: 'abc12345',
    truncated: false,
    changes: [
      { path: 'src/a.ts', status: 'M', staged: false, unstaged: true, insertions: 1, deletions: 1 },
      { path: 'new.txt', status: '?', staged: false, unstaged: true },
    ],
  } as Record<string, unknown>,
}))

vi.mock('../../../api/projectChanges', () => ({
  listProjectChanges: () => Promise.resolve(changesNet.changes),
  stageProjectPaths: (projectId: string, body: unknown) => {
    changesNet.indexCalls.push({ action: 'stage', projectId, body })
    return Promise.resolve(changesNet.nextChanges ?? changesNet.changes)
  },
  unstageProjectPaths: (projectId: string, body: unknown) => {
    changesNet.indexCalls.push({ action: 'unstage', projectId, body })
    return Promise.resolve(changesNet.nextChanges ?? changesNet.changes)
  },
  readProjectDiff: (projectId: string, path: string) =>
    new Promise(resolve => {
      changesNet.diffs.push({ projectId, path, resolve })
    }),
  readProjectLog: () => Promise.resolve({
    repo: true,
    branch: 'main',
    upstream: 'origin/main',
    ahead: 2,
    behind: 1,
    commits: [
      { sha: 'aaaaaaaaaaaaaaaa', short: 'aaaaaaa', subject: 'feat: one', author: 'A', date: '2026-11-27T00:00:00Z' },
      { sha: 'bbbbbbbbbbbbbbbb', short: 'bbbbbbb', subject: 'fix: two', author: 'B', date: '2026-11-26T00:00:00Z' },
    ],
  }),
  readCommitDiff: () => Promise.resolve({
    repo: true,
    path: 'aaaaaaa',
    untracked: false,
    binary: false,
    truncated: false,
    diff: 'diff --git a/a b/a\n+from-commit\n',
  }),
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
  // 面板现在会读 auth store 来判断"是否显示管理员入口"，pinia 必须装到挂载环境里。
  return mount(WorkbenchPanel, { props, global: { plugins: [createPinia()] } })
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

    // 必须拼成绝对路径：openPath 只接受绝对路径，相对路径会被主进程拒掉。
    // `root` 是同一份载荷的一部分（主进程据此把打开动作限制在项目根内）——
    // 只断言 path 时"忘了带 root"也会绿，而那正好是越界打开的缺口。
    expect(openPath).toHaveBeenCalledWith({ path: 'C:\\proj\\简历.docx', root: 'C:\\proj' })
  })
})

/**
 * 改动页签 → 单文件 diff。
 *
 * 这条链有两个安静的失败方式：
 *  1. 点了一条改动什么都不发生（旧行为是跳回文件树，用户以为点空了）；
 *  2. 回来的是**上一个文件**的 diff（切条目时没有代次守卫）。
 */
describe('改动页签的单文件 diff', () => {
  beforeEach(() => {
    net.calls.length = 0
    changesNet.diffs.length = 0
    changesNet.indexCalls.length = 0
    changesNet.nextChanges = null
  })

  async function openChangesTab() {
    const wrapper = mountPanel({ projectId: 'p1' })
    await flush()
    net.calls[0].resolve(listing('C:/p1', 'a.ts'))
    await flush()
    const tabs = wrapper.findAll('.wb-tab')
    await tabs[1].trigger('click')
    await flush()
    return wrapper
  }

  it('点一条改动会请求 diff 并按增/删着色', async () => {
    const wrapper = await openChangesTab()
    const row = wrapper.findAll('.wb-change')[0]
    expect(row.text()).toContain('src/a.ts')
    await row.trigger('click')
    await flush()

    expect(changesNet.diffs).toHaveLength(1)
    expect(changesNet.diffs[0]).toMatchObject({ projectId: 'p1', path: 'src/a.ts' })
    expect(wrapper.find('.wb-diff-panel').text()).toContain('加载中')

    changesNet.diffs[0].resolve({
      repo: true,
      path: 'src/a.ts',
      untracked: false,
      binary: false,
      truncated: false,
      diff: 'diff --git a/src/a.ts b/src/a.ts\n@@ -1 +1 @@\n-old\n+new\n',
    })
    await flush()

    expect(wrapper.find('.wb-diff-panel').text()).toContain('+new')
    expect(wrapper.find('.wb-diff-line.add').text()).toBe('+new')
    expect(wrapper.find('.wb-diff-line.del').text()).toBe('-old')
    expect(wrapper.find('.wb-diff-line.hunk').text()).toContain('@@')
  })

  it('选中 diff 行后可把审阅意见发给父组件（N2）', async () => {
    const wrapper = await openChangesTab()
    await wrapper.findAll('.wb-change')[0].trigger('click')
    await flush()
    changesNet.diffs[0].resolve({
      repo: true,
      path: 'src/a.ts',
      untracked: false,
      binary: false,
      truncated: false,
      diff: 'diff --git a/src/a.ts b/src/a.ts\n@@ -1 +1 @@\n-old\n+new\n',
    })
    await flush()

    // hunk 头不可选：它不是代码行
    await wrapper.find('.wb-diff-line.hunk').trigger('click')
    expect(wrapper.find('.wb-review').exists()).toBe(false)

    await wrapper.find('.wb-diff-line.del').trigger('click')
    await wrapper.find('.wb-diff-line.add').trigger('click')
    expect(wrapper.find('.wb-review').exists()).toBe(true)
    expect(wrapper.findAll('.wb-diff-line.selected')).toHaveLength(2)

    await wrapper.findAll('.wb-review .wb-btn')[0].trigger('click')
    await wrapper.find('.wb-review-box textarea').setValue('把 new 改成 new2')
    await wrapper.find('.wb-review-box .wb-btn-primary').trigger('click')

    // N2 v2：先进入托盘，不立即发送
    expect(wrapper.emitted('fix-request')).toBeUndefined()
    expect(wrapper.find('.wb-review-tray').exists()).toBe(true)
    expect(wrapper.find('.wb-review-tray').text()).toContain('src/a.ts')
    // 加入托盘后选择态清空，避免同一批意见被重复加入
    expect(wrapper.find('.wb-review').exists()).toBe(false)

    await wrapper.find('.wb-review-tray .wb-btn-primary').trigger('click')
    const emitted = wrapper.emitted('fix-request')
    expect(emitted).toHaveLength(1)
    const payload = emitted?.[0]?.[0] as {
      items: Array<{ path: string; comment: string; lines: string[] }>
    }
    expect(payload.items).toHaveLength(1)
    expect(payload.items[0]).toMatchObject({ path: 'src/a.ts', comment: '把 new 改成 new2' })
    // 按 diff 顺序取行，而不是按点击顺序
    expect(payload.items[0].lines).toEqual(['-old', '+new'])
    // 发送后托盘清空
    expect(wrapper.find('.wb-review-tray').exists()).toBe(false)
  })

  it('Shift 连选把锚点到当前行之间的可选中行一起加入（N2 v2）', async () => {
    const wrapper = await openChangesTab()
    await wrapper.findAll('.wb-change')[0].trigger('click')
    await flush()
    changesNet.diffs[0].resolve({
      repo: true,
      path: 'src/a.ts',
      untracked: false,
      binary: false,
      truncated: false,
      diff: 'diff --git a/src/a.ts b/src/a.ts\n@@ -1,2 +1,2 @@\n-old1\n-old2\n+new1\n+new2\n',
    })
    await flush()

    const dels = wrapper.findAll('.wb-diff-line.del')
    const adds = wrapper.findAll('.wb-diff-line.add')
    expect(dels).toHaveLength(2)
    expect(adds).toHaveLength(2)

    await adds[0].trigger('click')
    await adds[1].trigger('click', { shiftKey: true })
    // 锚点 add[0] 到 add[1] 之间只有这两行 add
    expect(wrapper.findAll('.wb-diff-line.selected')).toHaveLength(2)

    // 从 add[1] 反向 Shift 到 del[0]：区间覆盖 del/add，中间连续行全选
    await dels[0].trigger('click', { shiftKey: true })
    expect(wrapper.findAll('.wb-diff-line.selected')).toHaveLength(4)
  })

  it('勾选改动行会调用 stage，并用返回的 status 更新列表（N5 P1）', async () => {
    changesNet.nextChanges = {
      ...(changesNet.changes as Record<string, unknown>),
      changes: [
        { path: 'src/a.ts', status: 'M', staged: true, unstaged: false, insertions: 1, deletions: 1 },
        { path: 'new.txt', status: '?', staged: false, unstaged: true },
      ],
    }
    const wrapper = await openChangesTab()
    const first = wrapper.findAll('.wb-change')[0]
    const checkbox = first.find('input[type="checkbox"]')
    expect((checkbox.element as HTMLInputElement).checked).toBe(false)

    await checkbox.trigger('change')
    await flush()

    expect(changesNet.indexCalls).toEqual([
      { action: 'stage', projectId: 'p1', body: { paths: ['src/a.ts'] } },
    ])
    expect(
      (wrapper.findAll('.wb-change')[0].find('input[type="checkbox"]').element as HTMLInputElement).checked,
    ).toBe(true)
  })

  it('未跟踪文件不渲染假 diff，给出打开文件的入口', async () => {
    const wrapper = await openChangesTab()
    await wrapper.findAll('.wb-change')[1].trigger('click')
    await flush()
    expect(changesNet.diffs[0].path).toBe('new.txt')

    changesNet.diffs[0].resolve({
      repo: true,
      path: 'new.txt',
      untracked: true,
      binary: false,
      truncated: false,
      diff: '',
    })
    await flush()

    expect(wrapper.find('.wb-preview-body').text()).toContain('尚未纳入 git')
    expect(wrapper.find('.wb-diff').exists()).toBe(false)
  })

  it('先点 A 再点 B：A 的迟到响应不会覆盖 B', async () => {
    const wrapper = await openChangesTab()
    const rows = wrapper.findAll('.wb-change')
    await rows[0].trigger('click')
    await flush()
    await rows[1].trigger('click')
    await flush()

    // B 先回来
    changesNet.diffs[1].resolve({
      repo: true, path: 'new.txt', untracked: false, binary: false, truncated: false,
      diff: 'diff --git a/new.txt b/new.txt\n+from-B\n',
    })
    await flush()
    expect(wrapper.find('.wb-diff-panel').text()).toContain('+from-B')

    // A 后回来：必须被代次守卫丢弃
    changesNet.diffs[0].resolve({
      repo: true, path: 'src/a.ts', untracked: false, binary: false, truncated: false,
      diff: 'diff --git a/src/a.ts b/src/a.ts\n+from-A\n',
    })
    await flush()
    expect(wrapper.find('.wb-diff-panel').text()).not.toContain('+from-A')
    expect(wrapper.find('.wb-diff-panel').text()).toContain('+from-B')
  })

  it('还原点页签不再同时渲染改动列表（v-else 串错页签）', async () => {
    const wrapper = await openChangesTab()
    expect(wrapper.findAll('.wb-change').length).toBeGreaterThan(0)
    const tabs = wrapper.findAll('.wb-tab')
    await tabs[2].trigger('click')
    await flush()
    expect(wrapper.findAll('.wb-change')).toHaveLength(0)
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
    // 页签顺序：文件 / 改动 / 还原点 / 提交 —— 还原点是第 3 个（index 2）。
    await tabs[2].trigger('click')
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


describe('工作台的提交历史（F5）', () => {
  it('显示分支/领先落后与提交，点开看提交 diff', async () => {
    const wrapper = mountPanel({ projectId: 'p1' })
    await flushPromises()
    // 四个页签：文件 / 改动 / 还原点 / 提交
    const tabs = wrapper.findAll('.wb-tab')
    expect(tabs).toHaveLength(4)
    await tabs[3].trigger('click')
    await flushPromises()

    expect(wrapper.find('.wb-branch').text()).toBe('main')
    expect(wrapper.find('.wb-sync').text()).toContain('↑2')
    expect(wrapper.find('.wb-sync').text()).toContain('↓1')
    const commits = wrapper.findAll('.wb-commit')
    expect(commits).toHaveLength(2)
    expect(commits[0].text()).toContain('feat: one')

    await commits[0].trigger('click')
    await flushPromises()
    expect(wrapper.find('.wb-diff-panel').text()).toContain('+from-commit')
  })
})


describe('工作台的内容搜索（F6）', () => {
  it('回车搜索并显示命中行', async () => {
    net.searchResult = {
      files: [{ path: 'src/a.ts', line: 3, text: 'const needle = 1' }],
      truncated: false,
      unsupported: false,
    }
    const wrapper = mountPanel({ projectId: 'p1' })
    await flush()
    net.calls[0]?.resolve(listing('C:/p1', 'a.ts'))
    await flush()

    const input = wrapper.find('.wb-search-input')
    await input.setValue('needle')
    await input.trigger('keydown.enter')
    await flush()

    const hits = wrapper.findAll('.wb-hit')
    expect(hits).toHaveLength(1)
    expect(hits[0].text()).toContain('src/a.ts:3')
    expect(hits[0].text()).toContain('const needle = 1')
  })

  it('设备侧项目：明确说不支持，而不是空结果', async () => {
    net.searchResult = { files: [], truncated: false, unsupported: true }
    const wrapper = mountPanel({ projectId: 'p1' })
    await flush()
    net.calls[0]?.resolve(listing('C:/p1', 'a.ts'))
    await flush()

    const input = wrapper.find('.wb-search-input')
    await input.setValue('needle')
    await input.trigger('keydown.enter')
    await flush()

    expect(wrapper.text()).toContain('暂不支持内容搜索')
  })
})


describe('工作台的「本回合改动」（F4）', () => {
  it('用回合级路径列出改动，点开复用单文件 diff', async () => {
    net.calls.length = 0
    const wrapper = mountPanel({ projectId: 'p1', turnWrittenPaths: ['C:/p1/src/a.ts'] })
    await flush()
    net.calls[0].resolve(listing('C:/p1', 'a.ts'))
    await flush()

    const tabs = wrapper.findAll('.wb-tab')
    await tabs[1].trigger('click') // 改动页签
    await flush()

    const turn = wrapper.find('.wb-turn')
    expect(turn.exists()).toBe(true)
    expect(turn.text()).toContain('本回合改动')
    expect(turn.text()).toContain('src/a.ts')

    await turn.find('.wb-change').trigger('click')
    await flush()
    expect(changesNet.diffs.at(-1)?.path).toBe('src/a.ts')
  })
})
