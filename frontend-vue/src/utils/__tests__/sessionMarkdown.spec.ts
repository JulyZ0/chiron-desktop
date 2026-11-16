import { describe, expect, it } from 'vitest'
import { sessionToMarkdown } from '../sessionMarkdown'
import type { ChatItem } from '../../components/chat/chat-types'

const text = (role: 'user' | 'assistant', content: string): ChatItem =>
  ({ kind: 'text', role, content } as ChatItem)
const other = (kind: string): ChatItem => ({ kind } as ChatItem)

describe('sessionToMarkdown（对话沉淀为知识库文档）', () => {
  it('按角色分节导出正文', () => {
    const md = sessionToMarkdown([text('user', '什么是 RAG？'), text('assistant', '检索增强生成')])
    expect(md).toContain('## 用户')
    expect(md).toContain('什么是 RAG？')
    expect(md).toContain('## 助手')
    expect(md).toContain('检索增强生成')
  })

  it('跳过思考与工具卡片（运行细节不该进知识库）', () => {
    const md = sessionToMarkdown([
      text('user', '问题'),
      other('reasoning'),
      other('tool_call'),
      text('assistant', '回答'),
    ])
    expect(md).not.toContain('reasoning')
    expect(md).not.toContain('tool_call')
    expect(md.match(/## /g)).toHaveLength(2)
  })

  it('跳过空内容的消息（不产出空标题）', () => {
    const md = sessionToMarkdown([text('user', '   '), text('assistant', '有内容')])
    expect(md.match(/## /g)).toHaveLength(1)
    expect(md).toContain('有内容')
  })

  it('带标题时以 H1 开头', () => {
    const md = sessionToMarkdown([text('user', 'hi')], '  关于 RAG 的讨论  ')
    expect(md.startsWith('# 关于 RAG 的讨论')).toBe(true)
  })

  it('没有任何正文时返回空串（调用方据此禁止上传）', () => {
    expect(sessionToMarkdown([])).toBe('')
    expect(sessionToMarkdown([other('reasoning')])).toBe('')
    expect(sessionToMarkdown([text('user', '  ')])).toBe('')
  })

  it('产出以换行结尾，便于追加', () => {
    expect(sessionToMarkdown([text('user', 'x')]).endsWith('\n')).toBe(true)
  })
})
