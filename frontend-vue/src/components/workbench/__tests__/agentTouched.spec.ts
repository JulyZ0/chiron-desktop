import { describe, expect, it } from 'vitest'
import type { ChatItem } from '../../chat/chat-types'
import { collectAgentWrittenPaths, normalizeForCompare } from '../agentTouched'

function toolCall(name: string, args: Record<string, unknown>): ChatItem {
  return {
    kind: 'tool_call',
    id: `${name}-${Math.random()}`,
    name,
    arguments: JSON.stringify(args),
    status: 'done',
  } as ChatItem
}

describe('workbench/agentTouched（agent 写过的文件）', () => {
  it('只收集写工具的目标路径', () => {
    const paths = collectAgentWrittenPaths([
      toolCall('write_file', { path: 'src/a.ts' }),
      toolCall('edit_file', { path: 'src/b.ts' }),
    ])
    expect(paths).toEqual(['src/a.ts', 'src/b.ts'])
  })

  it('读工具不算改动', () => {
    // read_file 也有 path。把它算成"改过"，改动列表就退化成访问日志
    const paths = collectAgentWrittenPaths([
      toolCall('read_file', { path: 'src/a.ts' }),
      toolCall('list_dir', { path: 'src' }),
    ])
    expect(paths).toEqual([])
  })

  it('去掉重复路径，保留首次出现顺序', () => {
    const paths = collectAgentWrittenPaths([
      toolCall('edit_file', { path: 'src/b.ts' }),
      toolCall('write_file', { path: 'src/a.ts', content: 'x' }),
      toolCall('edit_file', { path: './src/b.ts', new_string: 'y' }),
    ])
    expect(paths).toEqual(['src/b.ts', 'src/a.ts'])
  })

  it('参数不是 JSON 时安静跳过', () => {
    const broken = {
      kind: 'tool_call',
      id: 'x',
      name: 'write_file',
      arguments: 'not json',
      status: 'done',
    } as ChatItem
    expect(collectAgentWrittenPaths([broken])).toEqual([])
  })

  it('非工具条目不影响结果', () => {
    const items = [
      { kind: 'text', id: 't', role: 'assistant', content: 'hi' },
      toolCall('edit_file', { path: 'src/a.ts' }),
    ] as ChatItem[]
    expect(collectAgentWrittenPaths(items)).toEqual(['src/a.ts'])
  })

  it('比较键抹平分隔符与大小写', () => {
    expect(normalizeForCompare('D:\\Proj\\Src\\A.ts')).toBe('d:/proj/src/a.ts')
  })
})
