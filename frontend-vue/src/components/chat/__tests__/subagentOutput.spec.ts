import { describe, expect, it } from 'vitest'
import { projectSubagentOutput, subagentRunIdOf } from '../subagentOutput'
import type { SubagentEvent } from '../../../api/subagent'

/**
 * 子 Agent 输出投影：事件流 → 可渲染的行。
 *
 * 这里钉住的是**配对规则**——它决定「输出」tab 读起来是不是和主对话流一样：
 * 调用卡与结果块必须成对、顺序不变、失败要标红、重复要吞掉。
 */
function ev(partial: Partial<SubagentEvent> & { type: string }): SubagentEvent {
  return { run_id: 'rs_1', depth: 1, ...partial }
}

describe('projectSubagentOutput', () => {
  it('调用与结果按 tool_call_id 配对：结果挂在调用卡下，状态随之变化', () => {
    const rows = projectSubagentOutput([
      ev({ type: 'subagent.tool_call', id: '1', tool_call_id: 'tc_1', tool_name: 'read_file', tool_arguments: '{"path":"a.ts"}' }),
      ev({ type: 'subagent.text', id: '2', content: '看了下' }),
      ev({ type: 'subagent.tool_result', id: '3', tool_call_id: 'tc_1', tool_name: 'read_file', content: '{"path":"a.ts"}' }),
    ])

    // 三行：调用卡在它被发起的位置，结果挂在同一条行上（不是追加到末尾）
    expect(rows).toHaveLength(2)
    expect(rows[0]).toMatchObject({ kind: 'tool' })
    const tool = rows[0]
    if (tool.kind !== 'tool') throw new Error('expected tool row')
    expect(tool.call?.name).toBe('read_file')
    expect(tool.call?.arguments).toBe('{"path":"a.ts"}')
    expect(tool.result?.content).toBe('{"path":"a.ts"}')
    expect(tool.call?.status).toBe('done')
  })

  it('失败结果标红（isError），且不看 content 里有没有 error 字样', () => {
    const rows = projectSubagentOutput([
      ev({ type: 'subagent.tool_call', id: '1', tool_call_id: 'tc_1', tool_name: 'shell_exec' }),
      // 成功返回体里也常带 error 键（批量操作的部分失败汇总）——只有事件级 error 才算失败
      ev({ type: 'subagent.tool_result', id: '2', tool_call_id: 'tc_1', content: '{"error":"partial"}' }),
      ev({ type: 'subagent.tool_call', id: '3', tool_call_id: 'tc_2', tool_name: 'shell_exec' }),
      ev({ type: 'subagent.tool_result', id: '4', tool_call_id: 'tc_2', content: '', error: 'exit 1' }),
    ])

    const [ok, bad] = rows
    if (ok.kind !== 'tool' || bad.kind !== 'tool') throw new Error('expected tool rows')
    expect(ok.result?.isError).toBe(false)
    expect(ok.call?.status).toBe('done')
    expect(bad.result?.isError).toBe(true)
    expect(bad.call?.status).toBe('error')
  })

  it('没有结果的调用保持 running（分不清时不能假装完成）', () => {
    const rows = projectSubagentOutput([
      ev({ type: 'subagent.tool_call', id: '1', tool_call_id: 'tc_1', tool_name: 'read_file' }),
    ])
    const tool = rows[0]
    if (tool.kind !== 'tool') throw new Error('expected tool row')
    expect(tool.call?.status).toBe('running')
    expect(tool.result).toBeUndefined()
  })

  it('重复的 tool_call 只留一张卡（重连补发不该看起来像调了两次）', () => {
    const rows = projectSubagentOutput([
      ev({ type: 'subagent.tool_call', id: '1', tool_call_id: 'tc_1', tool_name: 'read_file' }),
      ev({ type: 'subagent.tool_call', id: '2', tool_call_id: 'tc_1', tool_name: 'read_file' }),
    ])
    expect(rows).toHaveLength(1)
  })

  it('结果先于调用到达：仍渲染结果块，不把它丢掉', () => {
    const rows = projectSubagentOutput([
      ev({ type: 'subagent.tool_result', id: '1', tool_call_id: 'tc_9', content: 'late result' }),
    ])
    expect(rows).toHaveLength(1)
    const tool = rows[0]
    if (tool.kind !== 'tool') throw new Error('expected tool row')
    expect(tool.call).toBeUndefined()
    expect(tool.result?.content).toBe('late result')
  })

  it('保留到达顺序，审批/提问/终态各占一行', () => {
    const rows = projectSubagentOutput([
      ev({ type: 'subagent.reasoning', id: '1', content: '想一下' }),
      ev({ type: 'subagent.approval', id: '2', tool_call_id: 'tc_1', tool_name: 'shell_exec' }),
      ev({ type: 'subagent.ask', id: '3', tool_call_id: 'tc_2', question: '选哪个？' }),
      ev({ type: 'subagent.status', id: '4', status: 'tool' }),
      ev({ type: 'subagent.done', id: '5', status: 'completed' }),
    ])
    expect(rows.map(r => r.kind)).toEqual(['reasoning', 'approval', 'ask', 'status', 'done'])
  })

  it('空内容的增量不占行，未知类型不渲染（原始排查交给「事件流」tab）', () => {
    const rows = projectSubagentOutput([
      ev({ type: 'subagent.text', id: '1', content: '' }),
      ev({ type: 'subagent.mystery', id: '2' }),
    ])
    expect(rows).toHaveLength(0)
  })
})

describe('subagentRunIdOf', () => {
  it('认出引擎放在返回体顶层的 result_ref / run_id（同步与后台两种形状）', () => {
    // 同步委派：to_tool_payload 只给 result_ref
    expect(subagentRunIdOf(JSON.stringify({ status: 'completed', output: '好', result_ref: 'rs_abc123456789' })))
      .toBe('rs_abc123456789')
    // 后台委派：同时给 run_id 与 result_ref（同一个值）
    expect(subagentRunIdOf(JSON.stringify({ status: 'async_launched', run_id: 'rs_bb', result_ref: 'rs_bb' })))
      .toBe('rs_bb')
    // read_subagent_result 的返回只有 run_id
    expect(subagentRunIdOf(JSON.stringify({ run_id: 'rs_cc', status: 'completed' })))
      .toBe('rs_cc')
  })

  it('普通工具结果不给入口（不是子 Agent 的返回）', () => {
    expect(subagentRunIdOf(JSON.stringify({ path: 'a.ts', content: 'x' }))).toBe('')
    expect(subagentRunIdOf(JSON.stringify({ stdout: 'ok', exit_code: 0 }))).toBe('')
    expect(subagentRunIdOf('plain text')).toBe('')
    expect(subagentRunIdOf('')).toBe('')
  })

  it('带一列 run 的返回不给入口：点开看哪一个没有正确答案', () => {
    // 例如 subagent_list：runs 里每条都有自己的 run_id，但顶层没有单值
    expect(subagentRunIdOf(JSON.stringify({ runs: [{ run_id: 'rs_1' }, { run_id: 'rs_2' }] }))).toBe('')
    // 顶层是数组同样不算
    expect(subagentRunIdOf(JSON.stringify([{ run_id: 'rs_1' }]))).toBe('')
  })

  it('run_id 不是字符串（或空串）时不认', () => {
    expect(subagentRunIdOf(JSON.stringify({ result_ref: 42 }))).toBe('')
    expect(subagentRunIdOf(JSON.stringify({ result_ref: '   ' }))).toBe('')
  })

  it('预览被截断（不是合法 JSON）时仍能认出顶层 result_ref', () => {
    // 列表接口只带前 4096 字符，而 output 经常更长：截断不该让入口消失。
    const truncated = '{"status":"completed","result_ref":"rs_ab12cd34ef56","output":"很长的摘要…".repeat(10)'.slice(0, 60)
    expect(subagentRunIdOf(truncated)).toBe('rs_ab12cd34ef56')
  })

  it('截断的正文里出现多个不同的 result_ref：不猜，直接不给入口', () => {
    // 例如子 Agent 的 output 本身是一段 JSON dump，里面嵌着别的 run 的 id。
    const text = '{"status":"completed","result_ref":"rs_aaaa","output":"{\\"result_ref\\":\\"rs_bbbb\\"'
    expect(subagentRunIdOf(text)).toBe('')
  })

  it('完全不是 JSON 的正文不给入口', () => {
    expect(subagentRunIdOf('ls: cannot access a.ts')).toBe('')
  })
})
