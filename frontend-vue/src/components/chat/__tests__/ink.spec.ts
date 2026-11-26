import { describe, expect, it } from 'vitest'

import {
  INK_MAX_STAGGER,
  INK_MAX_TAIL_CHARS,
  inkIndexFor,
  nextBaseChars,
  revealSlice,
} from '../ink'

describe('ink：本批新增字符的水墨浮现', () => {
  it('前缀（已提交）与 tail（本批）按字符切分', () => {
    const out = revealSlice('abcdef', 3)
    expect(out.prefix).toBe('abc')
    expect(out.tail).toEqual(['d', 'e', 'f'])
    expect(out.overflow).toBe(false)
  })

  it('base 越界/负数都夹到合法范围', () => {
    expect(revealSlice('ab', 99)).toMatchObject({ prefix: 'ab', tail: [] })
    expect(revealSlice('ab', -3)).toMatchObject({ prefix: '', tail: ['a', 'b'] })
    expect(revealSlice('ab', Number.NaN)).toMatchObject({ prefix: '', tail: ['a', 'b'] })
  })

  it('按码点切，不劈开 emoji / 扩展汉字', () => {
    // "a中😀b" = 4 个码点；base=2 时 tail 必须是 ["😀","b"]，而不是半个代理对
    const out = revealSlice('a中😀b', 2)
    expect(out.prefix).toBe('a中')
    expect(out.tail).toEqual(['😀', 'b'])
  })

  it('单批过大时退回纯文本（不建几万 span）', () => {
    const out = revealSlice('x'.repeat(INK_MAX_TAIL_CHARS + 1), 0)
    expect(out.overflow).toBe(true)
    expect(out.tail).toEqual([])
    expect(out.prefix).toBe('')
  })

  it('封顶到 12 档：一次一大段也只铺 ~340ms', () => {
    expect(inkIndexFor(0)).toBe(0)
    expect(inkIndexFor(5)).toBe(5)
    expect(inkIndexFor(500)).toBe(INK_MAX_STAGGER)
    expect(inkIndexFor(-2)).toBe(0)
  })

  it('纯追加只提交上一次长度；缩短/替换归零', () => {
    expect(nextBaseChars('abc', 'abcde')).toBe(3)
    expect(nextBaseChars('abc', 'ab')).toBe(0)
    expect(nextBaseChars('', 'ab')).toBe(0)
    // emoji 也按码点算
    expect(nextBaseChars('a😀', 'a😀b')).toBe(2)
  })
})
