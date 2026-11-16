/**
 * 工作台代码预览的纯逻辑：语言识别 + 大文件截断。
 *
 * 这两条都是"错了也不报错"的行为：
 *
 *  1. **语言识别**错了，高亮器退回 `highlightAuto` —— 界面照常显示，只是配色全错，
 *     而用户会以为"这个编辑器不支持我的语言"。
 *  2. **不截断**，2MB 源码（十几万行）会整段进 DOM：每个字符一个文本节点，滚动与
 *     高亮都会卡死标签页。用户看到的是"点开大文件以后整个界面没反应" ——
 *     比"只显示前 2000 行"糟得多，而且没有任何报错可循。
 *
 * 直接 import 产品代码，不复制实现：复制的副本通过只证明副本正确。
 */
import { describe, it, expect } from 'vitest'

import { PREVIEW_LINE_LIMIT, langOf, truncatePreview } from '../previewCode'

describe('预览语言识别', () => {
  it('按扩展名给出 highlight.js 语言名', () => {
    expect(langOf('src/main.ts')).toBe('typescript')
    expect(langOf('README.md')).toBe('markdown')
    expect(langOf('main.go')).toBe('go')
  })

  it('.vue 映射到 xml 而不是 javascript', () => {
    // SFC 的形态接近 HTML 模板；映射错会让整份组件变成一片乱色
    expect(langOf('Comp.vue')).toBe('xml')
  })

  it('大写扩展名同样识别（Windows 上并不罕见）', () => {
    expect(langOf('MAIN.PY')).toBe('python')
  })

  it('无扩展名 / 未知扩展名返回空串', () => {
    // 空串让高亮器走 highlightAuto，而不是解析失败或抛错。
    // 注意 `Dockerfile` 不在此列 —— 它由整名映射识别为 dockerfile，
    // 见下面「无扩展名 / 约定文件名」那组。
    expect(langOf('weird.zzz')).toBe('')
    expect(langOf('')).toBe('')
    expect(langOf('LICENSE')).toBe('')
  })

  it('路径中间的点不干扰扩展名判定', () => {
    expect(langOf('my.dir/file.ts')).toBe('typescript')
    expect(langOf('a.b.c/file')).toBe('')
  })
})

describe('大文件截断', () => {
  it('小文件原样返回，不报截断', () => {
    const raw = 'let a = 1\nlet b = 2'
    expect(truncatePreview(raw)).toEqual({ text: raw, dropped: 0 })
  })

  it('恰好等于上限也不截断（边界不能差一行）', () => {
    const raw = Array.from({ length: PREVIEW_LINE_LIMIT }, (_, i) => `line ${i}`).join('\n')
    expect(truncatePreview(raw).dropped).toBe(0)
  })

  it('超出部分被裁掉，并如实报告还剩多少行', () => {
    const total = PREVIEW_LINE_LIMIT + 37
    const raw = Array.from({ length: total }, (_, i) => `line ${i}`).join('\n')
    const { text, dropped } = truncatePreview(raw)

    expect(dropped).toBe(37)
    expect(text.split('\n')).toHaveLength(PREVIEW_LINE_LIMIT)
    // 必须保留**前** N 行：文件开头的 import/声明比结尾有用
    expect(text.startsWith('line 0')).toBe(true)
    expect(text.endsWith(`line ${PREVIEW_LINE_LIMIT - 1}`)).toBe(true)
  })

  it('十几万行的文件不会把整段内容带进 DOM', () => {
    const raw = Array.from({ length: 150_000 }, (_, i) => `row ${i}`).join('\n')
    const { text, dropped } = truncatePreview(raw)

    expect(dropped).toBe(148_000)
    expect(text.length).toBeLessThan(raw.length / 50)
  })

  it('空内容与 null 都不炸', () => {
    expect(truncatePreview('')).toEqual({ text: '', dropped: 0 })
    // 后端可能整段省略 content（二进制），调用方传 undefined 时不该抛
    expect(truncatePreview(undefined as unknown as string)).toEqual({ text: '', dropped: 0 })
  })

  it('后端分屏时必须按 total_lines 报出剩余行数', () => {
    // 关键回归：后端对大文件只返回一屏，此时本地行数恰好 ≤ 上限，
    // 单看本地会算出 dropped=0 —— 界面于是显示"文件已完整"，用户据此
    // 判断"这个函数不存在"。所以后端的 truncated 必须参与判定。
    const raw = Array.from({ length: PREVIEW_LINE_LIMIT }, (_, i) => `line ${i}`).join('\n')
    const { text, dropped } = truncatePreview(raw, { serverTruncated: true, totalLines: 9000 })

    expect(text).toBe(raw)
    expect(dropped).toBe(9000 - PREVIEW_LINE_LIMIT)
  })

  it('后端说还有内容但 total_lines 未知时，至少报 1 行', () => {
    // "还有内容"与"0 行未显示"是自相矛盾的，宁可保守报 1
    expect(truncatePreview('a\nb', { serverTruncated: true }).dropped).toBe(1)
  })

  it('后端未标记截断时，即使传了 totalLines 也不虚报', () => {
    expect(truncatePreview('a\nb', { serverTruncated: false, totalLines: 99 }).dropped).toBe(0)
  })
})

describe('无扩展名 / 约定文件名', () => {
  it('go.mod / Makefile 这类按整名识别', () => {
    // 它们的"扩展名"要么是 mod（查不到），要么根本没有 —— 只按扩展名会全部落空
    expect(langOf('go.mod')).toBe('go')
    expect(langOf('Makefile')).toBe('makefile')
    expect(langOf('Dockerfile')).toBe('dockerfile')
  })

  it('整名优先于扩展名', () => {
    // go.mod 的扩展名是 mod，若先按扩展名查就会落空
    expect(langOf('sub/dir/go.mod')).toBe('go')
  })

  it('大小写不敏感', () => {
    expect(langOf('GO.MOD')).toBe('go')
    expect(langOf('makefile')).toBe('makefile')
  })
})
