import { describe, it, expect } from 'vitest'
import {
  KB_CLOUD_SUFFIXES,
  KB_SELF_HOSTED_SUFFIXES,
  isSupportedKbFile,
  kbAcceptAttr,
  kbSupportedSuffixes,
} from '../kbFileTypes'

/**
 * 知识库类型白名单的前端副本。
 *
 * 真正拦在入库前的是网关（internal/api/kb_file_types.go），这里只决定
 * "选文件时给什么提示、accept 写什么"。两边口径不一致会出现
 * "选文件时放行、上传后被 400 拒绝"这种最让人困惑的组合。
 */
describe('kbFileTypes', () => {
  it('自托管只吃 PDF', () => {
    expect(kbSupportedSuffixes('selfhosted')).toEqual(['.pdf'])
    expect(isSupportedKbFile('selfhosted', 'paper.pdf')).toBe(true)
    expect(isSupportedKbFile('selfhosted', 'resume.docx')).toBe(false)
  })

  it('cloud 额外支持 Office 文档', () => {
    for (const ext of KB_CLOUD_SUFFIXES) {
      expect(isSupportedKbFile('cloud', `file${ext}`)).toBe(true)
    }
    expect(isSupportedKbFile('cloud', 'notes.md')).toBe(false)
    expect(isSupportedKbFile('cloud', 'data.csv')).toBe(false)
  })

  it('后缀判定不区分大小写', () => {
    expect(isSupportedKbFile('selfhosted', 'REPORT.PDF')).toBe(true)
    expect(isSupportedKbFile('cloud', 'Deck.PPTX')).toBe(true)
  })

  // 网关侧已去掉 "ext 为空就当 txt" 的兜底（等于给任意文件开后门），前端必须同口径
  it('无扩展名的文件一律拒绝', () => {
    expect(isSupportedKbFile('cloud', 'README')).toBe(false)
    expect(isSupportedKbFile('selfhosted', 'Makefile')).toBe(false)
  })

  // 库还没加载完时 provider 是 undefined；此时按并集放行，别把用户挡在门外
  it('provider 未知时按并集放行', () => {
    const union = kbSupportedSuffixes(undefined)
    expect(union).toContain('.pdf')
    expect(union).toContain('.docx')
    expect(union).toEqual([...union].sort())
    expect(isSupportedKbFile(undefined, 'resume.docx')).toBe(true)
    expect(isSupportedKbFile(null, 'notes.md')).toBe(false)
  })

  it('并集不重复且覆盖两侧', () => {
    const union = kbSupportedSuffixes('unknown-provider')
    for (const ext of [...KB_SELF_HOSTED_SUFFIXES, ...KB_CLOUD_SUFFIXES]) {
      expect(union).toContain(ext)
    }
    expect(new Set(union).size).toBe(union.length)
  })

  it('accept 属性与白名单一致', () => {
    expect(kbAcceptAttr('selfhosted')).toBe('.pdf')
    expect(kbAcceptAttr('cloud')).toBe('.doc,.docx,.pdf,.ppt,.pptx')
  })
})
