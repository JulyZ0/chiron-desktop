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

beforeEach(() => {
  net.listCalls.length = 0
  net.restoreCalls.length = 0
  net.restoreResult = okResult()
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
    net.listCalls[0].resolve([cp()])
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
    net.listCalls[0].resolve([cp({ state: 'failed', error_code: 'cas_write_failed' })])
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
    net.listCalls[0].resolve([cp({ id: 'stale', label: 'STALE_FROM_P1' })])
    await flush()
    expect(wrapper.text()).not.toContain('STALE_FROM_P1')

    net.listCalls[1].resolve([cp({ id: 'fresh', label: 'FRESH_FROM_P2' })])
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
    net.listCalls[0].resolve([cp()])
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
    net.listCalls[0].resolve([cp()])
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
