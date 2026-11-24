/**
 * 审批 / 提问票据的**会话归属**（H59 + H61）。
 *
 * 这一层钉的是四条不许退化的语义（它们各自对应一个真实事故）：
 *
 * 1. **按会话建图**：A 的票不进 B 的桶（旧实现是两个全局 ref ⇒ 卡片画到别的会话上）；
 * 2. **拒绝错配提交**：票只属于它**到达时**的会话与那次运行代际 —— 归属不符就**不提交**，
 *    票原地保留（在 B 里点 A 的票，旧实现会把批准发到 B，A 一直等到超时）；
 * 3. **确认前不摘票**：提交失败只记原因、票留在原处（旧实现 `catch {}` 后照样删卡片）；
 * 4. **过期判定跨会话**：后台会话的票同样在超时，只清"当前视图"那份会让它切回来仍可点。
 */
import { describe, expect, it } from 'vitest'
import { claimTicket, createInteractionScope } from '../interactionScope'

function approval(id: string, sessionId = 's-a', expiresAt = Date.now() + 60_000) {
  return { id, toolName: 'run_command', arguments: '{"cmd":"ls"}', expiresAt, sessionId, runGen: 1 }
}

function question(id: string, sessionId = 's-a', expiresAt = Date.now() + 60_000) {
  return {
    id,
    question: '部署到哪个环境？',
    options: ['staging', 'prod'],
    allowFreeText: false,
    expiresAt,
    sessionId,
    runGen: 1,
  }
}

describe('票据按会话建图（H59）', () => {
  it('A 的审批只出现在 A 的桶里，B 读不到', () => {
    const scope = createInteractionScope()
    scope.addApproval(approval('call_1', 's-a'))

    expect(scope.approvalsOf('s-a').map((t) => t.id)).toEqual(['call_1'])
    expect(scope.approvalsOf('s-b')).toHaveLength(0)
    // 票上冻结着归属：提交时读它，而不是"当前视图会话"
    expect(scope.approvalsOf('s-a')[0]!.sessionId).toBe('s-a')
    expect(scope.approvalsOf('s-a')[0]!.runGen).toBe(1)
  })

  it('提问同样按会话隔离', () => {
    const scope = createInteractionScope()
    scope.addQuestion(question('q_1', 's-a'))
    expect(scope.questionsOf('s-a')).toHaveLength(1)
    expect(scope.questionsOf('s-b')).toHaveLength(0)
  })

  it('同一张票被重复送达（重放）时不重复，且不冲掉用户进行中的操作', () => {
    const scope = createInteractionScope()
    scope.addApproval(approval('call_1', 's-a'))
    scope.setBusy('approval', 'call_1', true)

    scope.addApproval({ ...approval('call_1', 's-a'), arguments: '{"cmd":"ls -la"}' })

    const list = scope.approvalsOf('s-a')
    expect(list).toHaveLength(1)
    expect(list[0]!.arguments).toContain('ls -la') // 新载荷生效
    expect(list[0]!.busy).toBe(true)               // 但"正在提交"没有被重放清掉
  })

  it('切会话只影响展示，不动别人家的票（旧实现会整片清空）', () => {
    const scope = createInteractionScope()
    scope.addApproval(approval('call_1', 's-a'))
    scope.addQuestion(question('q_1', 's-a'))

    // 切到 B 再切回 A：票一直在这里
    expect(scope.approvalsOf('s-b')).toHaveLength(0)
    expect(scope.approvalsOf('s-a')).toHaveLength(1)
    expect(scope.questionsOf('s-a')).toHaveLength(1)
  })
})

describe('claimTicket：唯一的放行判据（H59）', () => {
  const ctx = { sessionId: 's-a', gen: 1 }

  it('归属一致且空闲 ⇒ 放行', () => {
    expect(claimTicket({ sessionId: 's-a', runGen: 1 }, ctx)).toEqual({ ok: true })
  })

  it('会话错配 ⇒ 拒绝（这就是"在 B 里点 A 的票"）', () => {
    expect(claimTicket({ sessionId: 's-b', runGen: 1 }, { sessionId: 's-a', gen: 1 }))
      .toEqual({ ok: false, reason: 'mismatch' })
  })

  it('运行代际不符 ⇒ 拒绝（票属于该会话上一次运行时）', () => {
    expect(claimTicket({ sessionId: 's-a', runGen: 2 }, ctx))
      .toEqual({ ok: false, reason: 'stale-run' })
  })

  it('已在提交中 ⇒ 拒绝（连点两次不该发两次答复）', () => {
    expect(claimTicket({ sessionId: 's-a', runGen: 1, busy: true }, ctx))
      .toEqual({ ok: false, reason: 'busy' })
  })

  it('没有归属的票 ⇒ 拒绝（不猜一个会话去提交）', () => {
    expect(claimTicket({ runGen: 1 }, ctx)).toEqual({ ok: false, reason: 'missing-owner' })
  })
})

describe('确认前不摘票、失败可重试（H61）', () => {
  it('提交失败：票留在原处、错误可见、busy 复位', () => {
    const scope = createInteractionScope()
    scope.addApproval(approval('call_1', 's-a'))
    scope.setBusy('approval', 'call_1', true)

    scope.markFailed('approval', 'call_1', '网络不可达')

    const list = scope.approvalsOf('s-a')
    expect(list).toHaveLength(1)             // 旧实现此时已经删掉卡片了
    expect(list[0]!.error).toBe('网络不可达')
    expect(list[0]!.busy).toBe(false)        // 按钮可以再点
    // 重试仍然允许
    expect(claimTicket(list[0]!, { sessionId: 's-a', gen: 1 })).toEqual({ ok: true })
  })

  it('服务端确认（settle）后才摘票', () => {
    const scope = createInteractionScope()
    scope.addApproval(approval('call_1', 's-a'))
    scope.settle('approval', 'call_1')
    expect(scope.approvalsOf('s-a')).toHaveLength(0)
  })

  it('提问失败同样保留票据并允许重试', () => {
    const scope = createInteractionScope()
    scope.addQuestion(question('q_1', 's-a'))
    scope.setBusy('question', 'q_1', true)
    scope.markFailed('question', 'q_1', '票已被取走')
    const list = scope.questionsOf('s-a')
    expect(list).toHaveLength(1)
    expect(list[0]!.error).toBe('票已被取走')
    expect(list[0]!.busy).toBe(false)
  })

  it('liveIds 汇总全部会话的票据（与历史列表对账用）', () => {
    const scope = createInteractionScope()
    scope.addApproval(approval('call_1', 's-a'))
    scope.addQuestion(question('q_1', 's-b'))
    expect(scope.liveIds()).toEqual(new Set(['call_1', 'q_1']))
    scope.settle('approval', 'call_1')
    expect(scope.liveIds()).toEqual(new Set(['q_1']))
  })
})

describe('过期判定跨会话（H61）', () => {
  it('审批过期清理对后台会话同样生效', () => {
    const scope = createInteractionScope()
    scope.addApproval(approval('call_old', 's-a', Date.now() - 1000))
    scope.addApproval(approval('call_live', 's-b', Date.now() + 60_000))

    const remaining = scope.sweepApprovals(Date.now())

    expect(remaining).toBe(1)
    expect(scope.approvalsOf('s-a')).toHaveLength(0)
    expect(scope.approvalsOf('s-b').map((t) => t.id)).toEqual(['call_live'])
  })

  it('提问过期只置标记、保留卡片；正在作答的不判超时', () => {
    const scope = createInteractionScope()
    const past = Date.now() - 1000
    scope.addQuestion(question('q_old', 's-a', past))
    scope.addQuestion(question('q_drafting', 's-a', past))
    // 用户已经开始写答案（草稿非空）
    const drafting = scope.questionsOf('s-a').find((q) => q.id === 'q_drafting')!
    drafting.drafting = true

    const anyLive = scope.sweepQuestions(Date.now())

    expect(anyLive).toBe(true) // 起草中的那张还活着 ⇒ 定时器继续跑
    const list = scope.questionsOf('s-a')
    expect(list.find((q) => q.id === 'q_old')!.expired).toBe(true)
    expect(list.find((q) => q.id === 'q_drafting')!.expired).toBeUndefined()
    // 两张都还在：过期 ≠ 删除（AskCard 的 expired 语义）
    expect(list).toHaveLength(2)
  })
})
