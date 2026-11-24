/**
 * 破坏性回收预览绑定不可变目标（PRD H73 / 9.10）回归测试。
 *
 * 现场风险：checkpoint GC 的 dry-run 针对项目 A，确认框打开期间用户切到项目 B，
 * 旧实现 `executeGc()` 重新读 `props.projectId`，于是**对没预览过的 B 执行删除**，
 * 而界面上一切正常。这里钉三件事：
 *
 *  1. 执行请求的目标来自**预览时的绑定**（workspace + 代次 + 计划摘要），不是当前 props；
 *  2. 切项目让确认失效：一个请求都不发，并且必须报错（不能静默什么都不做）；
 *  3. 网关返回 `dry_run: true`（没真删）时不许显示"已回收"。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'

const net = vi.hoisted(() => ({
  listCalls: [] as Array<{ projectId: string; resolve: (v: unknown) => void; reject: (e: unknown) => void }>,
  gcCalls: [] as Array<{
    options: Record<string, unknown>
    resolve: (v: unknown) => void
    reject: (e: unknown) => void
  }>,
  modalCalls: [] as Array<{ content?: unknown; onOk?: () => unknown }>,
  success: [] as string[],
  errors: [] as string[],
}))

vi.mock('../../../api/checkpoints', () => ({
  listProjectCheckpoints: (projectId: string) =>
    new Promise((resolve, reject) => {
      net.listCalls.push({ projectId, resolve, reject })
    }),
  restoreCheckpoint: () => Promise.resolve({}),
  previewCheckpointDiff: () => new Promise(() => {}),
  runCheckpointGc: (options: Record<string, unknown> = {}) =>
    new Promise((resolve, reject) => {
      net.gcCalls.push({ options, resolve, reject })
    }),
}))

vi.mock('../../../api/projectVcs', () => ({
  getProjectVcs: () => Promise.resolve({ repo: true, git_missing: false }),
  initProjectVcs: () => Promise.resolve({ repo: true, git_missing: false }),
}))

/** 确认框只记录 onOk，由用例决定何时"点确定"（这正是本次要测的挂起窗口） */
vi.mock('ant-design-vue', async importOriginal => {
  const actual = await importOriginal<Record<string, unknown>>()
  return {
    ...actual,
    Modal: {
      confirm: (opts: { content?: unknown; onOk?: () => unknown }) => {
        net.modalCalls.push(opts)
        return { destroy: () => {} }
      },
    },
  }
})

vi.mock('../../../utils/notify', () => ({
  notify: {
    success: (msg: string) => { net.success.push(String(msg)) },
    error: (msg: string) => { net.errors.push(String(msg)) },
    info: () => {},
    warning: () => {},
  },
}))

const flush = () => new Promise(r => setTimeout(r, 0))

import CheckpointTimeline from '../CheckpointTimeline.vue'
import { computeGcPlanDigest, isGcPlanCurrent, verifyGcPlan } from '../../../utils/checkpointGcPlan'

function plan(over: Record<string, unknown> = {}) {
  return {
    scanned: 3,
    referenced: 5,
    deleted: 2,
    freed_bytes: 1024,
    dry_run: true,
    expired: 1,
    orphan: 0,
    kept_young: 2,
    store_files: 4,
    store_bytes: 4096,
    refs: 9,
    commits: 7,
    tree_paths: 11,
    dangling_refs: ['d1'],
    planned: ['sha-1', 'sha-2'],
    retention_days: 30,
    stats: { sample_count: 0, avg_file_count: 0, avg_byte_size: 0, window_days: 30 },
    ...over,
  } as unknown as import('../../../api/checkpoints').CheckpointGcResult
}

let listIndex = 0

beforeEach(() => {
  net.listCalls.length = 0
  net.gcCalls.length = 0
  net.modalCalls.length = 0
  net.success.length = 0
  net.errors.length = 0
  listIndex = 0
})

/** 挂载并让时间线请求返回一个空列表（与 GC 行为无关）。 */
async function mountAdmin(projectId = 'p1') {
  const wrapper = mount(CheckpointTimeline, { props: { projectId, canAdmin: true } })
  await flush()
  const call = net.listCalls[listIndex++]
  call?.resolve({ checkpoints: [], stats: null })
  await flush()
  return wrapper
}

async function startPreview(wrapper: Awaited<ReturnType<typeof mountAdmin>>) {
  const gcBtn = wrapper.find('.cp-gc .cp-btn')
  await gcBtn.trigger('click')
  await flush()
  return net.gcCalls[0]
}

describe('checkpoint GC 预览计划绑定（H73）', () => {
  it('执行用的是预览时的 workspace 与计划摘要，而不是当前 UI 状态', async () => {
    const wrapper = await mountAdmin('p1')
    const dryRun = await startPreview(wrapper)
    expect(dryRun.options.dryRun).toBe(true)
    expect(dryRun.options.workspaceId).toBe('p1')

    const p = plan()
    dryRun.resolve(p)
    await flush()
    expect(net.modalCalls).toHaveLength(1)

    void net.modalCalls[0].onOk?.()
    await flush()

    expect(net.gcCalls).toHaveLength(2)
    const exec = net.gcCalls[1].options
    expect(exec.dryRun).toBe(false)
    // 目标与摘要都来自绑定：同一份计划、同一个 workspace
    expect(exec.workspaceId).toBe('p1')
    expect(exec.planDigest).toBe(computeGcPlanDigest(p, 'p1'))

    net.gcCalls[1].resolve(plan({ dry_run: false, deleted: 2 }))
    await flush()
    expect(net.success).toHaveLength(1)
  })

  it('★ 确认框打开期间切项目 → 确认失效，一个请求都不发', async () => {
    const wrapper = await mountAdmin('p1')
    const dryRun = await startPreview(wrapper)
    dryRun.resolve(plan())
    await flush()
    expect(net.modalCalls).toHaveLength(1)

    // 用户切到项目 B（代次自增 + 时间线重新拉取）
    await wrapper.setProps({ projectId: 'p2' })
    await flush()
    net.listCalls[listIndex++]?.resolve({ checkpoints: [], stats: null })
    await flush()

    // 用户在 B 的界面上点了那个仍然挂着的确认框
    void net.modalCalls[0].onOk?.()
    await flush()

    // 关键：没有第二个请求 —— 绝不能对没预览过的 B 执行删除
    expect(net.gcCalls).toHaveLength(1)
    expect(net.success).toHaveLength(0)
    expect(net.errors.join('\n')).toContain('项目已切换')
  })

  it('预演期间切项目 → 不弹确认框，并报错', async () => {
    const wrapper = await mountAdmin('p1')
    const dryRun = await startPreview(wrapper)

    await wrapper.setProps({ projectId: 'p2' })
    await flush()
    net.listCalls[listIndex++]?.resolve({ checkpoints: [], stats: null })
    await flush()

    dryRun.resolve(plan())
    await flush()

    expect(net.modalCalls).toHaveLength(0)
    expect(net.errors.join('\n')).toContain('项目已切换')
  })

  it('★ 网关返回 dry_run=true（没真删）时不许显示"已回收"', async () => {
    const wrapper = await mountAdmin('p1')
    const dryRun = await startPreview(wrapper)
    dryRun.resolve(plan())
    await flush()

    void net.modalCalls[0].onOk?.()
    await flush()

    net.gcCalls[1].resolve(plan({ dry_run: true, deleted: 2 }))
    await flush()

    expect(net.success).toHaveLength(0)
    expect(net.errors.join('\n')).toContain('没有执行回收')
  })

  it('非管理员点不出预演', async () => {
    const wrapper = mount(CheckpointTimeline, { props: { projectId: 'p1' } })
    await flush()
    net.listCalls[listIndex++]?.resolve({ checkpoints: [], stats: null })
    await flush()
    expect(wrapper.find('.cp-gc').exists()).toBe(false)
    expect(net.gcCalls).toHaveLength(0)
  })
})

describe('计划绑定工具（computeGcPlanDigest / verifyGcPlan）', () => {
  it('同一份计划同一目标 → 摘要稳定，可复算', () => {
    const a = plan()
    expect(computeGcPlanDigest(a, 'p1')).toBe(computeGcPlanDigest(plan(), 'p1'))
  })

  it('换目标必换摘要（否则「切项目」这种漂移不会被摘要发现）', () => {
    const a = plan()
    expect(computeGcPlanDigest(a, 'p1')).not.toBe(computeGcPlanDigest(a, 'p2'))
  })

  it('计划内容变化 → 摘要变化', () => {
    expect(computeGcPlanDigest(plan(), 'p1')).not.toBe(
      computeGcPlanDigest(plan({ deleted: 3 }), 'p1'),
    )
  })

  it('目标不一致 → target_changed；计划被替换 → plan_tampered', () => {
    const p = plan()
    const binding = {
      workspaceId: 'p1',
      generation: 1,
      digest: computeGcPlanDigest(p, 'p1'),
      result: p,
    }
    expect(verifyGcPlan(binding, 'p1', 1)).toEqual({ ok: true })
    expect(verifyGcPlan(binding, 'p2', 1)).toEqual({ ok: false, reason: 'target_changed' })
    // 代次变了（切过项目又切回来）同样失效
    expect(verifyGcPlan(binding, 'p1', 2)).toEqual({ ok: false, reason: 'target_changed' })
    // 摘要与内容不自洽
    expect(verifyGcPlan({ ...binding, result: plan({ deleted: 9 }) }, 'p1', 1)).toEqual({
      ok: false,
      reason: 'plan_tampered',
    })
    expect(isGcPlanCurrent(binding, '', 1)).toBe(false)
  })
})
