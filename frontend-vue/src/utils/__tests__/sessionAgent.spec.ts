import { describe, expect, it } from 'vitest'
import { agentBindingsFromContext, sessionToAgent } from '../sessionAgent'
import type { ChatItem } from '../../components/chat/chat-types'

const text = (role: 'user' | 'assistant', content: string): ChatItem =>
  ({ kind: 'text', role, content } as ChatItem)
const other = (kind: string): ChatItem => ({ kind } as ChatItem)

const conversation = () => [
  text('user', '你是一位严谨的技术评审'),
  text('assistant', '好的，我会逐条指出问题'),
]

describe('sessionToAgent（对话沉淀为 Agent）', () => {
  it('把会话正文作为 system_prompt', () => {
    const draft = sessionToAgent(conversation(), '技术评审')
    expect(draft).not.toBeNull()
    expect(draft!.system_prompt).toContain('你是一位严谨的技术评审')
    expect(draft!.system_prompt).toContain('好的，我会逐条指出问题')
  })

  it('默认名字取会话标题', () => {
    expect(sessionToAgent(conversation(), '  技术评审  ')!.name).toBe('技术评审')
  })

  it('没有标题时用中性兜底（不产出空名）', () => {
    expect(sessionToAgent(conversation())!.name).toBe('新 Agent')
    expect(sessionToAgent(conversation(), '   ')!.name).toBe('新 Agent')
  })

  it('标题过长时截到 60（与 AgentsView 输入框上限一致）', () => {
    const draft = sessionToAgent(conversation(), 'x'.repeat(200))!
    expect(draft.name).toHaveLength(60)
  })

  it('多行标题只取第一行（标题里带换行会让名字变脏）', () => {
    expect(sessionToAgent(conversation(), '技术评审\n第二行')!.name).toBe('技术评审')
  })

  it('描述留空，等用户在弹窗里自己填', () => {
    expect(sessionToAgent(conversation(), 'T')!.description).toBe('')
  })

  it('跳过思考与工具卡片（只沉淀正文）', () => {
    const draft = sessionToAgent([text('user', '问题'), other('reasoning'), other('tool_call')], 'T')!
    expect(draft.system_prompt).toContain('问题')
    expect(draft.system_prompt).not.toContain('reasoning')
    expect(draft.system_prompt).not.toContain('tool_call')
  })

  it('正文为空时返回 null（调用方据此禁用入口）', () => {
    expect(sessionToAgent([], 'T')).toBeNull()
    expect(sessionToAgent([other('reasoning')], 'T')).toBeNull()
    expect(sessionToAgent([text('user', '   ')], 'T')).toBeNull()
  })

  it('system_prompt 去掉首尾空白', () => {
    const draft = sessionToAgent(conversation(), 'T')!
    expect(draft.system_prompt).toBe(draft.system_prompt.trim())
  })
})

describe('agentBindingsFromContext（对话绑定 → Agent 绑定列）', () => {
  it('把会话上下文的三类资源映射到 agents 的列名', () => {
    // 字段名不同正是这条映射存在的原因：会话侧是 skill_names / plugin_names，
    // agents 表是 skills / plugins。
    expect(
      agentBindingsFromContext({
        kb_id: 'kb-1',
        kb_ids: ['kb-1'],
        skill_names: ['pdf', 'web'],
        plugin_names: ['fs'],
      }),
    ).toEqual({ kb_id: 'kb-1', skills: ['pdf', 'web'], plugins: ['fs'] })
  })

  it('知识库取多值首个（agents.kb_id 是单值）', () => {
    expect(
      agentBindingsFromContext({ kb_ids: ['kb-2', 'kb-1'], kb_id: 'kb-1' }).kb_id,
    ).toBe('kb-2')
  })

  it('只有单值时回退单值字段', () => {
    expect(agentBindingsFromContext({ kb_id: 'solo' }).kb_id).toBe('solo')
  })

  it('知识库缺失时不产生该字段（不是空串）', () => {
    expect(agentBindingsFromContext({ skill_names: ['pdf'] })).toEqual({ skills: ['pdf'] })
  })

  it('没有上下文时返回空对象', () => {
    expect(agentBindingsFromContext(undefined)).toEqual({})
    expect(agentBindingsFromContext(null)).toEqual({})
    expect(agentBindingsFromContext({})).toEqual({})
  })

  it('只选中 Agent 时不产生任何绑定', () => {
    // agent_id 不进绑定列 —— 那是"这次用哪个 Agent"，不是 Agent 自身的能力
    expect(agentBindingsFromContext({ agent_id: 'ag-1' })).toEqual({})
  })

  it('去空、去重、丢弃非字符串项', () => {
    expect(
      agentBindingsFromContext({ skill_names: ['pdf', ' pdf ', '', 42, 'web'] }).skills,
    ).toEqual(['pdf', 'web'])
  })

  it('容忍单值字符串（手写 URL 的形态）', () => {
    expect(agentBindingsFromContext({ skill_names: 'pdf' }).skills).toEqual(['pdf'])
  })

  it('空数组不产生字段（避免写进一个空绑定）', () => {
    expect(agentBindingsFromContext({ skill_names: [], plugin_names: [] })).toEqual({})
  })
})
