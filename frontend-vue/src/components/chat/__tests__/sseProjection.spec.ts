import { describe, expect, it, vi } from 'vitest'

import type { ChatItem } from '../chat-types'
import {
  projectCompletion,
  projectSteerNotice,
  projectTodoEvent,
  projectToolCall,
  projectToolResult,
  projectUsage,
  projectVerification,
} from '../sseProjection'

describe('SSE 纯投影', () => {
  it('tool_call → running 卡片；tool_result 置 done 并补结果块', () => {
    const items: ChatItem[] = []
    projectToolCall(items, { id: 'tc1', name: 'read_file', arguments: '{"path":"a"}' })
    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({
      kind: 'tool_call',
      id: 'tc1',
      name: 'read_file',
      status: 'running',
    })

    projectToolResult(items, { tool_call_id: 'tc1', content: 'hello' })
    expect(items[0]).toMatchObject({ kind: 'tool_call', status: 'done' })
    expect(items[1]).toMatchObject({
      kind: 'tool_result',
      toolCallId: 'tc1',
      id: 'tc1:res',
      content: 'hello',
      isError: false,
    })
  })

  it('tool_result 空内容不插块；对象内容序列化', () => {
    const items: ChatItem[] = []
    projectToolCall(items, { id: 'tc2', name: 'x' })
    projectToolResult(items, { tool_call_id: 'tc2', content: '' })
    expect(items).toHaveLength(1)

    projectToolResult(items, { tool_call_id: 'tc2', content: { ok: true } })
    expect(items[1]).toMatchObject({ kind: 'tool_result', content: '{"ok":true}' })
  })

  it('todo 事件按显式 sid 归属，非法 JSON 退化为空数组', () => {
    const apply = vi.fn()
    projectTodoEvent('s1', { content: '[{"id":"t"}]', revision: 3 }, {}, apply)
    expect(apply).toHaveBeenCalledWith('s1', [{ id: 't' }], 3)

    projectTodoEvent('s2', { content: 'not-json' }, {}, apply)
    expect(apply).toHaveBeenLastCalledWith('s2', [], undefined)
  })

  it('completion 只在未完成时插 notice', () => {
    const items: ChatItem[] = []
    projectCompletion(items, '', () => '', () => 1000)
    expect(items).toHaveLength(0)

    projectCompletion(items, 'raw', () => '未完成：缺少测试', () => 1000)
    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({
      kind: 'notice',
      tone: 'warning',
      content: '未完成：缺少测试',
      id: 'completion:1000',
    })
  })

  it('usage 累加到同一条 turn_stats（零用量不建行）', () => {
    const items: ChatItem[] = []
    const st = { streamStatsId: '' }
    const merge = vi.fn(
      (_items: ChatItem[], id: string, usage: { inputTokens: number }, gen: () => string) => {
        if (!id) return 'stats-1'
        expect(usage.inputTokens).toBeGreaterThan(0)
        expect(gen()).toBeTruthy()
        return id
      },
    )
    let seq = 0
    const gen = () => `id-${seq++}`

    projectUsage(items, st, { input_tokens: 0 }, {}, merge, gen)
    expect(merge).not.toHaveBeenCalled()

    projectUsage(items, st, { input_tokens: 10, output_tokens: 5 }, {}, merge, gen)
    expect(st.streamStatsId).toBe('stats-1')
    projectUsage(items, st, { input_tokens: 3 }, {}, merge, gen)
    expect(st.streamStatsId).toBe('stats-1')
  })

  it('steer 只落一行小字，正文为空则不插', () => {
    const items: ChatItem[] = []
    projectSteerNotice(items, '', '已应用补充指令', () => 2000)
    expect(items).toHaveLength(0)

    projectSteerNotice(items, '先跑单测', '已应用补充指令', () => 2000)
    expect(items[0]).toMatchObject({
      kind: 'notice',
      tone: 'info',
      content: '已应用补充指令',
      id: 'steer:2000',
    })
  })

  describe('N6 验证结论', () => {
    const t = (key: string, params: Record<string, unknown> = {}) => {
      if (key === 'common.verification_passed') return `通过:${params.command}`
      if (key === 'common.verification_failed') return `失败:${params.command}:${params.code}`
      if (key === 'common.verification_unverified') return '未验证'
      return key
    }

    it('通过落 info，失败落 warning 并带退出码', () => {
      const items: ChatItem[] = []
      projectVerification(items, { status: 'passed', command: 'pnpm test' }, t, () => 1)
      expect(items[0]).toMatchObject({ kind: 'notice', tone: 'info', content: '通过:pnpm test' })

      const failed: ChatItem[] = []
      projectVerification(
        failed,
        { status: 'failed', command: 'go test ./...', exit_code: 2 },
        t,
        () => 2,
      )
      expect(failed[0]).toMatchObject({
        kind: 'notice',
        tone: 'warning',
        content: '失败:go test ./...:2',
      })
    })

    it('未验证是 warning；not-needed 与缺字段都不插行', () => {
      const items: ChatItem[] = []
      projectVerification(items, { status: 'unverified', wrote_files: true }, t, () => 3)
      expect(items[0]).toMatchObject({ kind: 'notice', tone: 'warning', content: '未验证' })

      const quiet: ChatItem[] = []
      projectVerification(quiet, { status: 'not-needed' }, t)
      projectVerification(quiet, undefined, t)
      projectVerification(quiet, {}, t)
      expect(quiet).toHaveLength(0)
    })
  })
})
