import { describe, expect, it } from 'vitest'
import { SHARE_PATH_PREFIX, sharePath, shareUrl } from '../shareLink'

describe('分享链接出口（/s/ 短链）', () => {
  it('对外发出去的短链形态是 origin + /s/{token}', () => {
    expect(shareUrl('abc123', 'https://chiron.example.com')).toBe('https://chiron.example.com/s/abc123')
  })

  it('站内路径与完整链接共用同一个前缀，改前缀只需改一处', () => {
    expect(sharePath('abc123')).toBe(`${SHARE_PATH_PREFIX}abc123`)
    expect(shareUrl('abc123', 'https://x.test')).toBe(`https://x.test${sharePath('abc123')}`)
  })

  it('token 做 URL 编码，不会因为将来换了字符集造出断链', () => {
    expect(sharePath('a/b c')).toBe('/s/a%2Fb%20c')
  })

  it('空 token 返回空串，而不是指向分享列表根路径的假链接', () => {
    // 调用点普遍写成 copyShareLink(shareUrl(id))，拿到 '' 才能被调用方识别为"还没有链接"。
    expect(shareUrl('', 'https://x.test')).toBe('')
    expect(shareUrl('   ', 'https://x.test')).toBe('')
    expect(shareUrl(undefined as unknown as string, 'https://x.test')).toBe('')
  })

  it('未注入 origin 时取当前页面 origin（jsdom 下为 http://localhost:3000）', () => {
    expect(shareUrl('t1')).toBe(`${window.location.origin}/s/t1`)
  })

  it('桌面壳 file:// 下 origin 是字符串 "null"：退化为站内路径，不产出坏 URL', () => {
    // 若原样拼出 "null/s/xxx"，复制出去的是一段看着像链接、点开必然失败的文本。
    expect(shareUrl('t1', 'null')).toBe('/s/t1')
    expect(shareUrl('t1', '')).toBe('/s/t1')
  })
})
