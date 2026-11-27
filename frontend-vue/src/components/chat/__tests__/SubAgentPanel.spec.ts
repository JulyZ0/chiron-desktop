import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import SubAgentPanel from '../SubAgentPanel.vue'

/**
 * 子 Agent 面板的审批链路（P3-后续）。
 *
 * 这条链路此前**根本不存在**：引擎只把 approval 转成一行 notice，前端没有任何控件，
 * 用户唯一能做的是看着子 Agent 空转 300 秒后失败。这里钉住三件事：
 *   1. 有审批在等时，**不点开也能看见**（运行行上的待确认标记）；
 *   2. 点开后有可操作的允许/拒绝按钮；
 *   3. 决定经 `/v1/agent/approval` 回传（与主 Agent 审批同一通道）。
 */
const api = vi.hoisted(() => ({ submitApproval: vi.fn(), submitAnswer: vi.fn() }))
vi.mock('../../../api', () => api)

const subagentApi = vi.hoisted(() => ({
  listSubagentRuns: vi.fn(),
  getSubagentRunEvents: vi.fn(),
  cancelSubagentRun: vi.fn(),
  cancelSessionSubagents: vi.fn(),
  rerunSubagentRun: vi.fn(),
  steerSubagentRun: vi.fn(),
  dispatchSubagent: vi.fn(),
}))
vi.mock('../../../api/subagent', () => subagentApi)

enableAutoUnmount(afterEach)

const APPROVAL_EVENT = {
  type: 'subagent.approval',
  run_id: 'rs_1',
  depth: 1,
  tool_call_id: 'tc_1',
  tool_name: 'shell_exec',
  tool_arguments: '{"command":"ls"}',
  content: '请求执行 shell_exec（级别 high）',
}

function mountPanel(liveEvents: unknown[]) {
  return mount(SubAgentPanel, {
    props: { sessionId: 's1', liveEvents: liveEvents as never },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  subagentApi.listSubagentRuns.mockResolvedValue({ runs: [], source: 'redis' })
  subagentApi.getSubagentRunEvents.mockResolvedValue({ events: [APPROVAL_EVENT], source: 'redis' })
  api.submitApproval.mockResolvedValue(true)
  api.submitAnswer.mockResolvedValue(true)
  subagentApi.steerSubagentRun.mockResolvedValue({ delivery: 'shared' })
  subagentApi.dispatchSubagent.mockResolvedValue({ run_id: 'rs_new' })
})

describe('SubAgentPanel 的审批链路', () => {
  it('运行行上的待确认标记：不点开也知道有东西在等', async () => {
    const wrapper = mountPanel([
      { type: 'subagent.started', run_id: 'rs_1', depth: 1 },
      APPROVAL_EVENT,
    ])
    await flushPromises()

    const badge = wrapper.find('.run-approval')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('待处理')
  })

  it('点待确认标记 → 打开事件流并渲染可操作的审批卡片', async () => {
    const wrapper = mountPanel([
      { type: 'subagent.started', run_id: 'rs_1', depth: 1 },
      APPROVAL_EVENT,
    ])
    await flushPromises()
    await wrapper.find('.run-approval').trigger('click')
    await flushPromises()

    // 回放（事件端点）里的事件也要能渲染成卡片 —— 刷新页面后审批不能消失
    expect(subagentApi.getSubagentRunEvents).toHaveBeenCalledWith('rs_1')
    const card = wrapper.find('.approval-card')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('shell_exec')
    expect(wrapper.find('.approval-btn.allow').exists()).toBe(true)
  })

  it('点"允许"把决定回传到 /v1/agent/approval 并显示结果', async () => {    const wrapper = mountPanel([
      { type: 'subagent.started', run_id: 'rs_1', depth: 1 },
      APPROVAL_EVENT,
    ])
    await flushPromises()
    await wrapper.find('.run-approval').trigger('click')
    await flushPromises()

    await wrapper.find('.approval-btn.allow').trigger('click')
    await flushPromises()

    expect(api.submitApproval).toHaveBeenCalledTimes(1)
    expect(api.submitApproval).toHaveBeenCalledWith({
      session_id: 's1',
      tool_call_id: 'tc_1',
      approved: true,
      decision: 'approve',
    })
    expect(wrapper.find('.approval-done').text()).toContain('已允许')
  })

  it('C5：编辑参数后以 decision=edit 回传编辑后的参数', async () => {
    const wrapper = mountPanel([
      { type: 'subagent.started', run_id: 'rs_1', depth: 1 },
      APPROVAL_EVENT,
    ])
    await flushPromises()
    await wrapper.find('.run-approval').trigger('click')
    await flushPromises()

    await wrapper.find('.approval-link').trigger('click')
    await flushPromises()
    // 面板可能同时渲染多张卡片（历史回放 + 实时各一处），这里只看**第一张**
    const card = wrapper.findAll('.approval-card')[0]
    const textarea = card.find('.approval-edit-text')
    expect(textarea.exists()).toBe(true)
    await textarea.setValue('{"command":"ls -la"}')
    await flushPromises()

    // 编辑态下有两个 allow 按钮：前一个"允许"，后一个"按编辑后的参数执行"
    const allows = card.findAll('.approval-btn.allow')
    expect(allows.length).toBe(2)
    await allows[1].trigger('click')
    await flushPromises()

    expect(api.submitApproval).toHaveBeenCalledTimes(1)
    expect(api.submitApproval).toHaveBeenCalledWith({
      session_id: 's1',
      tool_call_id: 'tc_1',
      approved: true,
      decision: 'edit',
      arguments: '{"command":"ls -la"}',
    })
  })

  it('C5：非法 JSON 不提交，并给出原因', async () => {
    const wrapper = mountPanel([
      { type: 'subagent.started', run_id: 'rs_1', depth: 1 },
      APPROVAL_EVENT,
    ])
    await flushPromises()
    await wrapper.find('.run-approval').trigger('click')
    await flushPromises()

    await wrapper.find('.approval-link').trigger('click')
    await flushPromises()
    const card = wrapper.findAll('.approval-card')[0]
    await card.find('.approval-edit-text').setValue('{ not json')
    await flushPromises()

    expect(card.find('.approval-edit-error').exists()).toBe(true)

    const allows = card.findAll('.approval-btn.allow')
    const submit = allows[allows.length - 1]
    expect(submit.attributes('disabled')).toBeDefined()
    await submit.trigger('click')
    await flushPromises()
    expect(api.submitApproval).not.toHaveBeenCalled()
  })

  it('引擎说"没生效"时明确报错，而不是把卡片默默撤掉', async () => {
    api.submitApproval.mockResolvedValue(false)
    const wrapper = mountPanel([
      { type: 'subagent.started', run_id: 'rs_1', depth: 1 },
      APPROVAL_EVENT,
    ])
    await flushPromises()
    await wrapper.find('.run-approval').trigger('click')
    await flushPromises()

    await wrapper.find('.approval-btn.allow').trigger('click')
    await flushPromises()

    expect(wrapper.find('.approval-error').text()).toContain('审批未生效')
    // 决定未生效 → 不标记为已允许，用户可重试
    expect(wrapper.find('.approval-done').exists()).toBe(false)
    expect(wrapper.find('.approval-btn.allow').attributes('disabled')).toBeUndefined()
  })
})

describe('SubAgentPanel 的提问链路', () => {
  const ASK_EVENT = {
    type: 'subagent.ask',
    run_id: 'rs_1',
    depth: 1,
    tool_call_id: 'tc_ask',
    question: '要发布到哪个环境？',
    options: ['staging', 'prod'],
    allow_free_text: true,
  }

  it('子 Agent 的提问渲染成可回答的卡片，答案送到 /v1/agent/answer', async () => {
    subagentApi.getSubagentRunEvents.mockResolvedValue({ events: [ASK_EVENT], source: 'redis' })
    const wrapper = mountPanel([
      { type: 'subagent.started', run_id: 'rs_1', depth: 1 },
      ASK_EVENT,
    ])
    await flushPromises()

    // 提问也算"待处理"：不点开也知道它在等人
    expect(wrapper.find('.run-approval').text()).toContain('待处理')

    await wrapper.find('.run-approval').trigger('click')
    await flushPromises()

    // 复用主对话流的 AskCard（同一套 UI）
    expect(wrapper.find('.ask-question').text()).toContain('要发布到哪个环境？')
    await wrapper.findAll('.ask-option')[0]!.trigger('click')
    await flushPromises()

    expect(api.submitAnswer).toHaveBeenCalledWith({
      session_id: 's1',
      tool_call_id: 'tc_ask',
      answer: 'staging',
    })
    expect(wrapper.find('.ask-done').text()).toContain('staging')
  })

  it('答案没送达时明确报错，并把卡片标为失效（不做假成功）', async () => {
    api.submitAnswer.mockResolvedValue(false)
    subagentApi.getSubagentRunEvents.mockResolvedValue({ events: [ASK_EVENT], source: 'redis' })
    const wrapper = mountPanel([
      { type: 'subagent.started', run_id: 'rs_1', depth: 1 },
      ASK_EVENT,
    ])
    await flushPromises()
    await wrapper.find('.run-approval').trigger('click')
    await flushPromises()

    await wrapper.findAll('.ask-option')[0]!.trigger('click')
    await flushPromises()

    expect(wrapper.find('.approval-error').text()).toContain('答案未送达')
    expect(wrapper.find('.ask-done').exists()).toBe(false)
  })
})

describe('SubAgentPanel 的运行跟随', () => {
  it('新派发的子 Agent 会被自动选中（不必刷新页面）', async () => {
    // 一轮对话结束后仍在跑的后台 run：列表里第一个是**已完成的旧 run**。
    // 旧实现只在"从未选中"时才选 runs[0] —— 于是新 run 永远不会被选中，
    // 只有它的终态轮询在跑，用户看不到它的进度与审批，只能刷新页面。
    subagentApi.listSubagentRuns.mockResolvedValue({
      runs: [
        { run_id: 'rs_done', status: 'completed', depth: 1 },
        { run_id: 'rs_live', status: 'running', depth: 1 },
      ],
      source: 'redis',
    })
    mountPanel([])
    await flushPromises()

    expect(subagentApi.getSubagentRunEvents).toHaveBeenCalledWith('rs_live')
  })

  it('全部终态时选中最近完成的一个（有东西可看）', async () => {
    subagentApi.listSubagentRuns.mockResolvedValue({
      runs: [
        { run_id: 'rs_1', status: 'completed', depth: 1 },
        { run_id: 'rs_2', status: 'failed', depth: 1 },
      ],
      source: 'db',
    })
    mountPanel([])
    await flushPromises()

    expect(subagentApi.getSubagentRunEvents).toHaveBeenCalledWith('rs_2')
  })
})

/**
 * P0-2：卡片上的「重试」要**真的重跑**，被拒才回退到"放回输入框"。
 *
 * 这条链路的两种结局必须都测到：
 *  - 成功：调 `POST /v1/subagent/runs/{id}/rerun`，**不**把任务丢回输入框
 *    （丢回去等于让用户重发一条毫无血缘的新任务）；
 *  - 被拒（409 非终态/无原任务、404 不属于本会话）：回退 emit `retry-run`，
 *    并明确告诉用户为什么没能直接重跑 —— 不做假成功。
 */
describe('SubAgentPanel 的失败重试（P0-2）', () => {
  const FAILED_RUN = {
    run_id: 'rs_failed',
    status: 'failed',
    depth: 1,
    profile: 'reviewer',
    summary: '下标越界，改到一半停了',
    usage: { input_tokens: 1200, output_tokens: 340, steps: 7 },
  }

  function mountWithFailedRun() {
    subagentApi.listSubagentRuns.mockResolvedValue({ runs: [FAILED_RUN], source: 'db' })
    subagentApi.getSubagentRunEvents.mockResolvedValue({ events: [], source: 'db' })
    return mount(SubAgentPanel, { props: { sessionId: 's1', liveEvents: [] as never } })
  }

  it('重跑成功：调用重跑接口、选中新 run，且不把任务放回输入框', async () => {
    subagentApi.rerunSubagentRun.mockResolvedValue({
      status: 'accepted',
      run_id: 'rs_new',
      rerun_of: 'rs_failed',
      parent_run_id: 'rs_failed',
      depth: 2,
    })
    const wrapper = mountWithFailedRun()
    await flushPromises()

    const retry = wrapper.find('.run-retry')
    expect(retry.exists()).toBe(true)
    await retry.trigger('click')
    await flushPromises()

    expect(subagentApi.rerunSubagentRun).toHaveBeenCalledWith('rs_failed')
    // 兜底路径**不该**被触发
    expect(wrapper.emitted('retry-run')).toBeUndefined()
    // 新 run 立刻被选中（用户不必等 3s 轮询）
    expect(subagentApi.getSubagentRunEvents).toHaveBeenCalledWith('rs_new')
  })

  it('重跑被拒（409）：回退到放回输入框，并说明原因', async () => {
    subagentApi.rerunSubagentRun.mockRejectedValue(
      Object.assign(new Error('conflict'), { response: { status: 409 } }),
    )
    const wrapper = mountWithFailedRun()
    await flushPromises()

    await wrapper.find('.run-retry').trigger('click')
    await flushPromises()

    expect(subagentApi.rerunSubagentRun).toHaveBeenCalledWith('rs_failed')
    const emitted = wrapper.emitted('retry-run') as
      | Array<[{ runId: string; text: string }]>
      | undefined
    expect(emitted).toBeTruthy()
    expect(emitted![0][0].runId).toBe('rs_failed')
    expect(emitted![0][0].text).toContain('下标越界')
  })

  it('重跑 404：同样回退（不假装重试成功）', async () => {
    subagentApi.rerunSubagentRun.mockRejectedValue(
      Object.assign(new Error('not found'), { response: { status: 404 } }),
    )
    const wrapper = mountWithFailedRun()
    await flushPromises()

    await wrapper.find('.run-retry').trigger('click')
    await flushPromises()

    expect(wrapper.emitted('retry-run')).toBeTruthy()
  })

  it('预算超限：卡片高亮 + 头部汇总（来自 runs 列表数据）', async () => {
    subagentApi.listSubagentRuns.mockResolvedValue({
      runs: [
        { ...FAILED_RUN, run_id: 'rs_partial', status: 'partial', budget_exceeded: true },
        { run_id: 'rs_ok', status: 'completed', depth: 1, created_at: new Date().toISOString() },
        { run_id: 'rs_bad', status: 'failed', depth: 1, created_at: new Date().toISOString() },
      ],
      source: 'db',
    })
    subagentApi.getSubagentRunEvents.mockResolvedValue({ events: [], source: 'db' })
    const wrapper = mount(SubAgentPanel, { props: { sessionId: 's1', liveEvents: [] as never } })
    await flushPromises()

    // 撞预算墙的那张卡高亮 + 带标记
    expect(wrapper.find('.run-card.budget-hit').exists()).toBe(true)
    expect(wrapper.text()).toContain('预算超限')
    // 头部失败率：近 24h 2 条里有 1 条 failed → 50%
    expect(wrapper.find('.failure-rate').text()).toContain('50')
  })
})

/**
 * 子 Agent 运行区的「查看」与层级面包屑。
 *
 * 修前：卡片上的「查看」只设置 `selectedRunId`，不切 tab —— 用户还在「运行」页，
 * 界面上除了一行高亮没有任何变化（"点击无效"）。现在点它切到「输出」区，
 * 并在顶部给出"主会话 → 祖先 run → 当前 run"的层级链；祖先可点、根节点回列表。
 */
describe('SubAgentPanel 的「查看」与层级面包屑', () => {
  const HIERARCHY = [
    { run_id: 'rs_parent', depth: 1, status: 'completed', profile: 'reviewer' },
    { run_id: 'rs_child', parent_run_id: 'rs_parent', depth: 2, status: 'completed' },
  ]

  function mountWithRuns(runs: unknown[]) {
    subagentApi.listSubagentRuns.mockResolvedValue({ runs, source: 'db' })
    subagentApi.getSubagentRunEvents.mockResolvedValue({
      events: [{ type: 'subagent.text', run_id: 'rs_child', depth: 2, content: '中途输出' }],
      source: 'db',
    })
    return mount(SubAgentPanel, { props: { sessionId: 's1', liveEvents: [] as never } })
  }

  it('点「查看」切到输出区，并显示完整层级链', async () => {
    const wrapper = mountWithRuns(HIERARCHY)
    await flushPromises()

    // 两张卡片各有一个「查看」；点子 run 那一个。
    const openButtons = wrapper.findAll('.run-open')
    expect(openButtons.length).toBe(2)
    await openButtons[1].trigger('click')
    await flushPromises()

    expect(wrapper.find('.output').isVisible()).toBe(true)
    const crumbs = wrapper.findAll('.run-breadcrumb .crumb').map(el => el.text())
    expect(crumbs[0]).toBe('主会话')
    expect(crumbs[1]).toContain('reviewer')
    expect(crumbs[crumbs.length - 1]).toContain('子 Agent')
    expect(wrapper.find('.crumb-current').text()).toBe(crumbs[crumbs.length - 1])
    expect(subagentApi.getSubagentRunEvents).toHaveBeenCalledWith('rs_child')
  })

  it('点祖先面包屑切到父 run', async () => {
    const wrapper = mountWithRuns(HIERARCHY)
    await flushPromises()
    await wrapper.findAll('.run-open')[1].trigger('click')
    await flushPromises()
    subagentApi.getSubagentRunEvents.mockClear()

    // crumb[0] 是「主会话」根，crumb[1] 是父 run。
    await wrapper.findAll('.run-breadcrumb .crumb')[1].trigger('click')
    await flushPromises()

    expect(subagentApi.getSubagentRunEvents).toHaveBeenCalledWith('rs_parent')
    expect(wrapper.find('.crumb-current').text()).toContain('reviewer')
  })

  it('点根面包屑回到运行列表', async () => {
    const wrapper = mountWithRuns(HIERARCHY)
    await flushPromises()
    await wrapper.findAll('.run-open')[1].trigger('click')
    await flushPromises()

    await wrapper.find('.run-breadcrumb .crumb-root').trigger('click')
    await flushPromises()

    expect(wrapper.find('.output').isVisible()).toBe(false)
    expect(wrapper.find('.timeline').isVisible()).toBe(true)
  })

  it('父级已轮转：保留层级但不可点开（不抹掉关系）', async () => {
    const wrapper = mountWithRuns([
      { run_id: 'rs_orphan_child', parent_run_id: 'rs_gone', depth: 2, status: 'completed' },
    ])
    await flushPromises()
    const crumbs = wrapper.findAll('.run-breadcrumb .crumb')
    expect(crumbs.map(el => el.text())[0]).toBe('主会话')
    // 中间那个是缺失的父级：显示但 disabled。
    const missing = crumbs[1]
    expect(missing.attributes('disabled')).toBeDefined()
    expect(missing.text()).toContain('rs_gone'.slice(-6))
  })
})


describe('SubAgentPanel 的父→子 steer（F1）', () => {
  function mountWithStatus(status: string) {
    subagentApi.listSubagentRuns.mockResolvedValue({
      runs: [{ run_id: 'rs_live', depth: 1, status, profile: 'reviewer' }],
      source: 'redis',
    })
    subagentApi.getSubagentRunEvents.mockResolvedValue({ events: [], source: 'redis' })
    return mount(SubAgentPanel, { props: { sessionId: 's1', liveEvents: [] as never } })
  }

  it('运行中的 run 可以追加指令，送达后清空输入', async () => {
    const wrapper = mountWithStatus('running')
    await flushPromises()
    await wrapper.find('.run-open').trigger('click')
    await flushPromises()

    const input = wrapper.find('.steer-input')
    expect(input.exists()).toBe(true)
    await input.setValue('先跑单测再改')
    await wrapper.find('.steer-send').trigger('click')
    await flushPromises()

    expect(subagentApi.steerSubagentRun).toHaveBeenCalledWith('rs_live', '先跑单测再改')
    expect((wrapper.find('.steer-input').element as HTMLInputElement).value).toBe('')
  })

  it('终态 run 不显示 steer 输入', async () => {
    const wrapper = mountWithStatus('completed')
    await flushPromises()
    await wrapper.find('.run-open').trigger('click')
    await flushPromises()
    expect(wrapper.find('.steer-input').exists()).toBe(false)
  })

  it('未送达必须显式报错，而不是假装成功', async () => {
    subagentApi.steerSubagentRun.mockRejectedValueOnce({ response: { status: 409 } })
    const wrapper = mountWithStatus('running')
    await flushPromises()
    await wrapper.find('.run-open').trigger('click')
    await flushPromises()

    await wrapper.find('.steer-input').setValue('late')
    await wrapper.find('.steer-send').trigger('click')
    await flushPromises()

    // 失败时输入保留（用户可以重试/复制），且没有清空 —— 清空会让人以为已送出。
    expect((wrapper.find('.steer-input').element as HTMLInputElement).value).toBe('late')
  })
})
