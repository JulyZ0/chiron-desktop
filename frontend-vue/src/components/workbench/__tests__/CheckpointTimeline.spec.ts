/**
 * CheckpointTimeline 的行为契约。
 *
 * 这里只测**回滚这条写磁盘路径上"错了也不报错"的情形**，不测像素：
 *
 *  1. 「默认不删」必须与后端同口径。这条一旦漂移，用户的回滚会顺手删掉他之后
 *     新建的文件 —— 而界面上的成功提示看起来完全正常。
 *  2. `state !== 'ok'` 的点必须**根本发不出请求**。快照本身没写全，拿它回滚会
 *     得到半截工作区；禁用只是提示，真正拦住请求的是 `canRestore`。
 *  3. 切项目时上一个项目晚到的时间线响应必须丢弃 —— 否则用户会照着另一个项目的
 *     时刻回滚，而 `restore` 是按 checkpoint_id 走的，界面上不会报任何错。
 *  4. `protected_removed` / `unsupported` 必须显示。它们是"没动"的路径，不显示
 *     会让用户以为工作区已经完全回到那一刻。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

const net = vi.hoisted(() => ({
  listCalls: [] as Array<{ projectId: string; resolve: (v: unknown) => void; reject: (e: unknown) => void }>,
  restoreCalls: [] as Array<{ id: string; options: Record<string, unknown> }>,
  restoreResult: {} as Record<string, unknown>,
  previewCalls: [] as Array<{ id: string; resolve: (v: unknown) => void; reject: (e: unknown) => void }>,
  gcCalls: [] as Array<{ options: Record<string, unknown> }>,
  gcResult: {} as Record<string, unknown>,
}))

vi.mock('../../../api/checkpoints', () => ({
  listProjectCheckpoints: (projectId: string) =>
    new Promise((resolve, reject) => {
      net.listCalls.push({ projectId, resolve, reject })
    }),
  restoreCheckpoint: (id: string, options: Record<string, unknown> = {}) => {
    net.restoreCalls.push({ id, options })
    return Promise.resolve(net.restoreResult)
  },
  previewCheckpointDiff: (id: string) =>
    new Promise((resolve, reject) => {
      net.previewCalls.push({ id, resolve, reject })
    }),
  runCheckpointGc: (options: Record<string, unknown> = {}) => {
    net.gcCalls.push({ options })
    return Promise.resolve(net.gcResult)
  },
}))

/** antd Modal.confirm 在 jsdom 里不会真渲染，拦下来后直接放行 onOk 即可 */
vi.mock('ant-design-vue', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown>>()
  return {
    ...actual,
    Modal: {
      confirm: (opts: { onOk?: () => unknown }) => {
        void opts.onOk?.()
      },
    },
  }
})

const flush = () => new Promise(r => setTimeout(r, 0))

import CheckpointTimeline from '../CheckpointTimeline.vue'

function cp(over: Record<string, unknown> = {}) {
  return {
    id: 'cp-1',
    seq: 3,
    kind: 'pre_turn',
    state: 'ok',
    label: '',
    commit_sha: 'abc',
    ref_name: 'refs/chiron/cp-1',
    file_count: 2,
    skipped_count: 0,
    byte_size: 100,
    created_at: '2026-11-14T10:00:00Z',
    ...over,
  }
}

function okResult(over: Record<string, unknown> = {}) {
  return {
    ok: true,
    written: ['a.ts'],
    added: [],
    deleted: [],
    pruned_dirs: [],
    failed: [],
    skipped_removed: [],
    protected_removed: [],
    unsupported: [],
    ...over,
  }
}

function diffResult(over: Record<string, unknown> = {}) {
  return {
    restored: [],
    added: [],
    removed: [],
    protected_removed: [],
    unsupported: [],
    untracked_omitted: false,
    stats: { sample_count: 0, avg_file_count: 0, avg_byte_size: 0, window_days: 30 },
    ...over,
  }
}

beforeEach(() => {
  net.listCalls.length = 0
  net.restoreCalls.length = 0
  net.restoreResult = okResult()
  net.previewCalls.length = 0
  net.gcCalls.length = 0
  net.gcResult = {}
})

describe('还原点时间线', () => {
  it('没有项目时不发请求', async () => {
    mount(CheckpointTimeline, { props: { projectId: '' } })
    await flush()
    expect(net.listCalls).toHaveLength(0)
  })

  it('默认回滚不请求删除（allow_delete 保持后端默认 false）', async () => {
    net.restoreResult = okResult({ skipped_removed: ['new.ts'] })
    const wrapper = mount(CheckpointTimeline, { props: { projectId: 'p1' } })
    await flush()
    net.listCalls[0].resolve({ checkpoints: [cp()], stats: null })
    await flush()

    await wrapper.find('.cp-btn-primary').trigger('click')
    await flush()

    expect(net.restoreCalls).toHaveLength(1)
    // 关键：请求体里不能出现 allow_delete。一旦前端自己填了 true，
    // "默认不删" 这条承诺就只剩在后端代码里，而用户看到的是一致的成功提示。
    expect(net.restoreCalls[0].options).not.toHaveProperty('allowDelete')
    expect(wrapper.text()).toContain('未删除')
  })

  it('state 非 ok 的点禁用回滚，且点下去不发请求', async () => {
    const wrapper = mount(CheckpointTimeline, { props: { projectId: 'p1' } })
    await flush()
    net.listCalls[0].resolve({ checkpoints: [cp({ state: 'failed', error_code: 'cas_write_failed' })], stats: null })
    await flush()

    const btn = wrapper.find('.cp-btn-primary')
    expect(btn.attributes('disabled')).toBeDefined()
    await btn.trigger('click')
    await flush()

    expect(net.restoreCalls).toHaveLength(0)
    // 失败原因要能读出来，否则用户只看到按钮是灰的
    expect(wrapper.text()).toContain('cas_write_failed')
  })

  it('切项目时丢弃上一个项目晚到的时间线', async () => {
    const wrapper = mount(CheckpointTimeline, { props: { projectId: 'p1' } })
    await flush()

    await wrapper.setProps({ projectId: 'p2' })
    await flush()
    expect(net.listCalls).toHaveLength(2)

    // p1 的响应现在才回来：属于上一次的时间线，必须丢弃
    net.listCalls[0].resolve({ checkpoints: [cp({ id: 'stale', label: 'STALE_FROM_P1' })], stats: null })
    await flush()
    expect(wrapper.text()).not.toContain('STALE_FROM_P1')

    net.listCalls[1].resolve({ checkpoints: [cp({ id: 'fresh', label: 'FRESH_FROM_P2' })], stats: null })
    await flush()
    expect(wrapper.text()).toContain('FRESH_FROM_P2')
  })

  it('命中保护名单的路径与不支持的路径都要显示', async () => {
    net.restoreResult = okResult({
      protected_removed: ['.env'],
      unsupported: ['link-to-somewhere'],
    })
    const wrapper = mount(CheckpointTimeline, { props: { projectId: 'p1' } })
    await flush()
    net.listCalls[0].resolve({ checkpoints: [cp()], stats: null })
    await flush()

    await wrapper.find('.cp-btn-primary').trigger('click')
    await flush()
    await nextTick()

    expect(wrapper.text()).toContain('保护名单')
    expect(wrapper.text()).toContain('符号链接')
  })

  it('删除新增文件时只把 skipped_removed 作为 paths 发出去', async () => {
    net.restoreResult = okResult({ skipped_removed: ['new-a.ts', 'new-b.ts'] })
    const wrapper = mount(CheckpointTimeline, { props: { projectId: 'p1' } })
    await flush()
    net.listCalls[0].resolve({ checkpoints: [cp()], stats: null })
    await flush()

    await wrapper.find('.cp-btn-primary').trigger('click')
    await flush()
    await nextTick()

    // 结果块里出现独立的删除入口
    const del = wrapper.findAll('.cp-btn').find(b => b.text().includes('删除这'))
    expect(del, '缺少删除新增文件的独立入口').toBeTruthy()

    net.restoreResult = okResult({ deleted: ['new-a.ts', 'new-b.ts'] })
    await del!.trigger('click')
    await flush()

    const second = net.restoreCalls[1]
    expect(second.id).toBe('cp-1')
    // 只删这些路径：不带 paths 会让引擎按全量 diff 重算，把刚恢复的文件也纳入
    expect(second.options.allowDelete).toBe(true)
    expect(second.options.paths).toEqual(['new-a.ts', 'new-b.ts'])
  })

  it('预览差异列出路径清单与计数，且不触发回滚', async () => {
    const wrapper = mount(CheckpointTimeline, { props: { projectId: 'p1' } })
    await flush()
    net.listCalls[0].resolve({ checkpoints: [cp()], stats: null })
    await flush()

    const previewBtn = wrapper.findAll('.cp-btn').find(b => b.text().includes('预览差异'))
    expect(previewBtn, '每个点都应有预览入口').toBeTruthy()
    await previewBtn!.trigger('click')
    await flush()

    expect(net.previewCalls).toHaveLength(1)
    expect(net.previewCalls[0].id).toBe('cp-1')
    net.previewCalls[0].resolve(diffResult({
      restored: ['src/a.ts'],
      removed: ['draft.md'],
      protected_removed: ['.env'],
      stats: { sample_count: 4, avg_file_count: 12, avg_byte_size: 2048, window_days: 30 },
    }))
    await flush()

    // 路径清单与每栏计数都要能读出来
    expect(wrapper.text()).toContain('src/a.ts')
    expect(wrapper.text()).toContain('draft.md')
    expect(wrapper.text()).toContain('.env')
    expect(wrapper.find('.cp-preview').exists()).toBe(true)
    // 预览是只读的：绝不能顺手发一个回滚请求
    expect(net.restoreCalls).toHaveLength(0)
  })

  it('清单读不到时预览必须说明只覆盖 git 跟踪的文件', async () => {
    const wrapper = mount(CheckpointTimeline, { props: { projectId: 'p1' } })
    await flush()
    net.listCalls[0].resolve({ checkpoints: [cp()], stats: null })
    await flush()

    const previewBtn = wrapper.findAll('.cp-btn').find(b => b.text().includes('预览差异'))
    await previewBtn!.trigger('click')
    await flush()
    net.previewCalls[0].resolve(diffResult({ untracked_omitted: true }))
    await flush()

    expect(wrapper.text()).toContain('未跟踪文件清单读不到')
  })

  it('预览失败就地报错，而不是显示"没有差异"', async () => {
    const wrapper = mount(CheckpointTimeline, { props: { projectId: 'p1' } })
    await flush()
    net.listCalls[0].resolve({ checkpoints: [cp()], stats: null })
    await flush()

    const previewBtn = wrapper.findAll('.cp-btn').find(b => b.text().includes('预览差异'))
    await previewBtn!.trigger('click')
    await flush()
    net.previewCalls[0].reject(new Error('commit not found'))
    await flush()

    expect(wrapper.find('.cp-preview.is-bad').exists()).toBe(true)
    expect(wrapper.text()).toContain('预览失败')
    // 关键：失败**不能**退化成"没有差异"
    expect(wrapper.text()).not.toContain('没有差异')
  })

  it('state 非 ok 的点预览按钮禁用且点不出请求', async () => {
    const wrapper = mount(CheckpointTimeline, { props: { projectId: 'p1' } })
    await flush()
    net.listCalls[0].resolve({ checkpoints: [cp({ state: 'failed' })], stats: null })
    await flush()

    const previewBtn = wrapper.findAll('.cp-btn').find(b => b.text().includes('预览差异'))
    expect(previewBtn!.attributes('disabled')).toBeDefined()
    await previewBtn!.trigger('click')
    await flush()
    expect(net.previewCalls).toHaveLength(0)
  })

  it('管理员才看到回收入口，且先预演再确认', async () => {
    // 非管理员：不显示按钮（权限仍由后端兜底）
    const plain = mount(CheckpointTimeline, { props: { projectId: 'p1' } })
    await flush()
    net.listCalls[0].resolve({ checkpoints: [cp()], stats: null })
    await flush()
    expect(plain.find('.cp-gc').exists()).toBe(false)

    net.gcResult = {
      scanned: 3, referenced: 5, deleted: 2, freed_bytes: 1024, dry_run: true,
      retention_days: 30, planned: [], dangling_refs: [],
      stats: { sample_count: 0, avg_file_count: 0, avg_byte_size: 0, window_days: 30 },
    }
    const admin = mount(CheckpointTimeline, { props: { projectId: 'p1', canAdmin: true } })
    await flush()
    net.listCalls[1].resolve({ checkpoints: [cp()], stats: null })
    await flush()

    const gcBtn = admin.find('.cp-gc .cp-btn')
    expect(gcBtn.exists()).toBe(true)
    await gcBtn.trigger('click')
    await flush()

    // 顺序不能反：**先预演**（dryRun true）拿到"会删多少"，确认之后才真删。
    // 第一次就是 dryRun=false 的话，用户看到的数字永远只是事后统计。
    expect(net.gcCalls).toHaveLength(2)
    expect(net.gcCalls[0].options.dryRun).toBe(true)
    expect(net.gcCalls[0].options.workspaceId).toBe('p1')
    // Modal.confirm 在测试里被替换成"直接放行 onOk"，因此紧接着就是真删。
    expect(net.gcCalls[1].options.dryRun).toBe(false)
  })

  it('头部显示近 30 天平均规模', async () => {
    const wrapper = mount(CheckpointTimeline, { props: { projectId: 'p1' } })
    await flush()
    net.listCalls[0].resolve({
      checkpoints: [cp()],
      stats: { sample_count: 6, avg_file_count: 9, avg_byte_size: 4096, window_days: 30 },
    })
    await flush()

    expect(wrapper.find('.cp-stats').exists()).toBe(true)
    expect(wrapper.text()).toContain('近 30 天')
    expect(wrapper.text()).toContain('9 个文件')
  })

  it('列表请求失败时就地报错，而不是显示"还没有还原点"', async () => {
    const wrapper = mount(CheckpointTimeline, { props: { projectId: 'p1' } })
    await flush()
    net.listCalls[0].reject(new Error('boom'))
    await flush()

    // "空列表"与"读失败"是两件事：前者正常，后者要能重试
    expect(wrapper.find('.cp-error').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('还没有还原点')
  })
})
