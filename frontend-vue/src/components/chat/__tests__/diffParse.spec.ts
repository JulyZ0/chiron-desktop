import { describe, expect, it } from 'vitest'
import { looksLikeDiff, parseUnifiedDiff } from '../diffParse'

/** 与 python-engine 的 difflib.unified_diff 输出形态一致 */
const SAMPLE = [
  '--- a/app/main.py',
  '+++ b/app/main.py',
  '@@ -1,3 +1,4 @@',
  ' import os',
  '-print("old")',
  '+print("new")',
  '+print("extra")',
  ' return 0',
  '',
].join('\n')

describe('diffParse（unified diff 解析）', () => {
  it('识别真实的 unified diff', () => {
    expect(looksLikeDiff(SAMPLE)).toBe(true)
  })

  it('不把普通文本误判成 diff（只有 +/- 开头的列表或分隔线不算）', () => {
    expect(looksLikeDiff('今日待办\n- 买菜\n- 写代码\n+ 加一条')).toBe(false)
    expect(looksLikeDiff('一些说明文字\n--- 分隔线 ---\n继续说明')).toBe(false)
    expect(looksLikeDiff('')).toBe(false)
  })

  it('按行分类并统计增删', () => {
    const parsed = parseUnifiedDiff(SAMPLE)
    expect(parsed.additions).toBe(2)
    expect(parsed.deletions).toBe(1)
    expect(parsed.lines[0]!.kind).toBe('file')
    expect(parsed.lines[2]!.kind).toBe('hunk')
    expect(parsed.lines[4]!.kind).toBe('del')
    expect(parsed.lines[5]!.kind).toBe('add')
    expect(parsed.lines[3]!.kind).toBe('context')
  })

  it('收集改动文件并去掉 a/ b/ 前缀（同名只记一次）', () => {
    const parsed = parseUnifiedDiff(SAMPLE)
    expect(parsed.files).toEqual(['app/main.py'])
  })

  it('去掉 split 产生的尾部空行（不进入渲染序列）', () => {
    const parsed = parseUnifiedDiff(SAMPLE)
    expect(parsed.lines[parsed.lines.length - 1]!.text).toBe(' return 0')
  })

  it('兼容 CRLF 与 diff --git 头', () => {
    const content = 'diff --git a/x.ts b/x.ts\r\n--- a/x.ts\r\n+++ b/x.ts\r\n@@ -1 +1 @@\r\n-a\r\n+b\r\n'
    const parsed = parseUnifiedDiff(content)
    expect(parsed.files).toEqual(['x.ts'])
    expect(parsed.additions).toBe(1)
    expect(parsed.deletions).toBe(1)
    expect(parsed.lines.some(line => line.kind === 'hunk')).toBe(true)
  })
})
