import { describe, expect, it } from 'vitest'
import { mergeToolNames, parseToolNames, toToolList } from '../toolList'

describe('toToolList（宽松解析 /v1/tools）', () => {
  it('解析标准形态 { tools: [{name, description}] }', () => {
    const list = toToolList({ tools: [{ name: 'read_file', description: '读文件' }] })
    expect(list).toEqual([{ name: 'read_file', description: '读文件' }])
  })

  it('容忍外层再包一层 data（拦截器差异）', () => {
    expect(toToolList({ data: { tools: [{ name: 'write_file', description: '' }] } })[0]!.name).toBe('write_file')
    expect(toToolList({ data: [{ name: 'shell', description: '' }] })[0]!.name).toBe('shell')
  })

  it('解析 OpenAI 风格 function 嵌套（MCP 代理工具常见）', () => {
    const list = toToolList([{ function: { name: 'mcp__fs__read', description: '来自插件的工具' } }])
    expect(list).toEqual([{ name: 'mcp__fs__read', description: '来自插件的工具' }])
  })

  it('接受纯字符串数组', () => {
    expect(toToolList(['a', 'b']).map(t => t.name)).toEqual(['a', 'b'])
  })

  it('丢弃没有名字的条目，并对重名去重', () => {
    expect(toToolList([{ description: '没有名字' }, null, 42])).toEqual([])
    expect(toToolList([{ name: 'dup' }, { name: 'dup' }])).toHaveLength(1)
  })

  it('未知结构返回空数组而不是抛错', () => {
    expect(toToolList(null)).toEqual([])
    expect(toToolList({ unexpected: 1 })).toEqual([])
    expect(toToolList('not-a-list')).toEqual([])
  })
})

describe('parseToolNames / mergeToolNames（手写 + 勾选）', () => {
  it('识别换行、中英逗号、顿号与空格分隔', () => {
    expect(parseToolNames('a\nb, c，d、e f')).toEqual(['a', 'b', 'c', 'd', 'e', 'f'])
  })

  it('合并时手写在前、勾选在后，并去重', () => {
    expect(mergeToolNames('read_file\nwrite_file', ['write_file', 'shell']))
      .toBe('read_file\nwrite_file\nshell')
  })

  it('勾选为空时保持手写内容不变（选择器只是补充）', () => {
    expect(mergeToolNames('read_file,write_file', [])).toBe('read_file\nwrite_file')
  })

  it('手写与勾选都为空时输出空串（不会写出空行）', () => {
    expect(mergeToolNames('', [])).toBe('')
    expect(mergeToolNames('   ', ['', '  '])).toBe('')
  })

  it('回填往返稳定：解析后再合并不产生重复', () => {
    const text = 'read_file, shell'
    expect(mergeToolNames(text, parseToolNames(text))).toBe('read_file\nshell')
  })
})
