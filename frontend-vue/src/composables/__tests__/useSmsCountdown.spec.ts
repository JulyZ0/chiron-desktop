import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { isValidPhone } from '../../api/auth'
import { useSmsCountdown } from '../useSmsCountdown'

describe('isValidPhone（与后端 ValidateSmsPhone 对齐）', () => {
  it('接受合法手机号', () => {
    expect(isValidPhone('13800138000')).toBe(true)
    expect(isValidPhone('+8613800138000')).toBe(true)
    expect(isValidPhone('+85212345678')).toBe(true)
    expect(isValidPhone(' 12345 ')).toBe(true) // trim 后 5 位数字
  })

  it('拒绝非法手机号', () => {
    expect(isValidPhone('')).toBe(false)
    expect(isValidPhone('abc')).toBe(false)
    expect(isValidPhone('1234')).toBe(false) // 太短
    expect(isValidPhone('123456789012345678901')).toBe(false) // 太长
    expect(isValidPhone('138-0013-8000')).toBe(false)
    expect(isValidPhone('+86 138')).toBe(false) // 含空格
  })
})

describe('useSmsCountdown', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('start 后每秒递减，到 0 停止', () => {
    const { remaining, start } = useSmsCountdown(60)
    expect(remaining.value).toBe(0)
    start(3)
    expect(remaining.value).toBe(3)
    vi.advanceTimersByTime(1000)
    expect(remaining.value).toBe(2)
    vi.advanceTimersByTime(2000)
    expect(remaining.value).toBe(0)
    // 停止后不再递减
    vi.advanceTimersByTime(3000)
    expect(remaining.value).toBe(0)
  })

  it('重复 start 重置倒计时', () => {
    const { remaining, start } = useSmsCountdown(60)
    start(5)
    vi.advanceTimersByTime(2000)
    expect(remaining.value).toBe(3)
    start(10)
    expect(remaining.value).toBe(10)
  })

  it('非正数秒数不启动', () => {
    const { remaining, start } = useSmsCountdown(60)
    start(0)
    expect(remaining.value).toBe(0)
  })
})
