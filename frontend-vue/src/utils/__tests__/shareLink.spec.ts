/**
 * 分享链接解析的单测。
 *
 * 守的是一条真实事故：桌面端在 `file://` 下用 origin 拼链接，得到
 * `file:///share/<token>`（打包形态里表现为 `file:///s/<token>`）—— 看起来像链接，
 * 换台机器就是「找不到文件」。所以：
 *   1. 后端给了绝对 url 就用它（唯一能让链接真正可达的来源）；
 *   2. 只有页面本身是 http(s) 时才允许按 origin 兜底；
 *   3. 其余情况**必须抛错或返回空串**，绝不返回相对路径。
 */
import { describe, expect, it } from 'vitest'
import {
  SHARE_PATH_PREFIX,
  isAbsoluteHttpUrl,
  resolveShareUrl,
  safeShareUrl,
  sharePath,
  shareUrl,
  ShareLinkUnavailableError,
} from '../shareLink'

describe('resolveShareUrl：优先后端绝对地址', () => {
  it('后端给了 http(s) url 就用它（桌面 file:// 也能拿到可达链接）', () => {
    expect(resolveShareUrl('https://chat.example.com/s/tok', 'file://', 'tok'))
      .toBe('https://chat.example.com/s/tok')
  })

  it('后端 url 首尾空白会被裁掉', () => {
    expect(resolveShareUrl('  https://chat.example.com/s/tok  ', 'file://', 'tok'))
      .toBe('https://chat.example.com/s/tok')
  })

  it('没有后端 url、页面是 http(s) 时按 origin 兜底', () => {
    expect(resolveShareUrl(undefined, 'https://chat.example.com', 'oqum3iu4opa75c7l'))
      .toBe('https://chat.example.com/s/oqum3iu4opa75c7l')
  })

  it('origin 结尾斜杠不会拼出双斜杠', () => {
    expect(resolveShareUrl('', 'http://127.0.0.1:8790/', 'tok'))
      .toBe('http://127.0.0.1:8790/s/tok')
  })

  it('file:// 且没有后端 url 时抛错（绝不产出相对路径）', () => {
    expect(() => resolveShareUrl(undefined, 'file://', 'tok')).toThrow(ShareLinkUnavailableError)
    expect(() => resolveShareUrl('', 'file:///C:/app/dist', 'tok')).toThrow(ShareLinkUnavailableError)
    expect(() => resolveShareUrl('', 'null', 'tok')).toThrow(ShareLinkUnavailableError)
  })

  it('后端给的是相对路径时也抛错：在 file:// 下它等同于坏链', () => {
    for (const bad of ['/s/tok', 's/tok', 'file:///s/tok']) {
      expect(() => resolveShareUrl(bad, 'file://', 'tok')).toThrow(ShareLinkUnavailableError)
    }
  })

  it('空 token 且无后端 url 时抛错', () => {
    expect(() => resolveShareUrl(undefined, 'https://x.test', '  ')).toThrow(ShareLinkUnavailableError)
  })
})

describe('safeShareUrl：模板绑定用，永不抛', () => {
  it('可用时与 resolve 一致，不可用时返回空串', () => {
    expect(safeShareUrl('https://x.test/s/a', 'file://', 'a')).toBe('https://x.test/s/a')
    expect(safeShareUrl('', 'file://', 'a')).toBe('')
    expect(safeShareUrl(undefined, 'null', 'a')).toBe('')
  })
})

describe('sharePath / shareUrl 兼容出口', () => {
  it('站内路径与完整链接共用同一个前缀', () => {
    expect(sharePath('abc123')).toBe(`${SHARE_PATH_PREFIX}abc123`)
    expect(shareUrl('abc123', 'https://x.test')).toBe(`https://x.test${sharePath('abc123')}`)
  })

  it('token 做 URL 编码，不会因为将来换了字符集造出断链', () => {
    expect(sharePath('a/b c')).toBe('/s/a%2Fb%20c')
  })

  it('空 token 返回空串，而不是指向分享列表根路径的假链接', () => {
    expect(shareUrl('', 'https://x.test')).toBe('')
    expect(shareUrl('   ', 'https://x.test')).toBe('')
    expect(shareUrl(undefined as unknown as string, 'https://x.test')).toBe('')
  })

  it('未注入 origin 时取当前页面 origin（jsdom 下为 http://localhost:3000）', () => {
    expect(shareUrl('t1')).toBe(`${window.location.origin}/s/t1`)
  })

  it('桌面壳 file:// 下不产出坏 URL（返回空串而不是相对路径）', () => {
    expect(shareUrl('t1', 'null')).toBe('')
    expect(shareUrl('t1', '')).toBe('')
    expect(shareUrl('t1', 'file://')).toBe('')
  })
})

describe('isAbsoluteHttpUrl', () => {
  it('只认 http(s) 绝对地址', () => {
    expect(isAbsoluteHttpUrl('https://a.test/x')).toBe(true)
    expect(isAbsoluteHttpUrl('http://127.0.0.1:1/x')).toBe(true)
    expect(isAbsoluteHttpUrl('/s/x')).toBe(false)
    expect(isAbsoluteHttpUrl('file:///s/x')).toBe(false)
    expect(isAbsoluteHttpUrl('')).toBe(false)
  })
})
