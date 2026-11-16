import { describe, expect, it } from 'vitest'
import { isNewerStreamId, parseStreamId } from '../streamId'

describe('parseStreamId', () => {
  it('解析 `<ms>-<seq>`', () => {
    expect(parseStreamId('1700000000000-3')).toEqual({ ms: 1700000000000, seq: 3 })
    expect(parseStreamId('0-0')).toEqual({ ms: 0, seq: 0 })
  })

  it('不是流 ID 的一律返回 null', () => {
    for (const bad of ['', '-', '123', '123-', '-1', '12-34-56', 'abc-1', '1-abc', ' 1-2', '1-2 ', '1.5-2']) {
      expect(parseStreamId(bad), bad).toBeNull()
    }
  })

  it('超出安全整数范围返回 null（宁可算"新"，也不要静默丢事件）', () => {
    expect(parseStreamId('99999999999999999999-1')).toBeNull()
  })
})

describe('isNewerStreamId', () => {
  it('没有基线（首连）时一律算新', () => {
    expect(isNewerStreamId('1700000000000-0', '')).toBe(true)
  })

  it('同一毫秒按 seq **数值**比较（字符串比较会把 -10 判成比 -9 旧）', () => {
    expect(isNewerStreamId('1700000000000-10', '1700000000000-9')).toBe(true)
    expect(isNewerStreamId('1700000000000-9', '1700000000000-10')).toBe(false)
  })

  it('跨毫秒按 ms 比较', () => {
    expect(isNewerStreamId('1700000000001-0', '1700000000000-99')).toBe(true)
    expect(isNewerStreamId('1699999999999-99', '1700000000000-0')).toBe(false)
  })

  it('同一条事件（重连补发重叠的典型场景）不算新 ⇒ 上层直接丢弃', () => {
    expect(isNewerStreamId('1700000000000-7', '1700000000000-7')).toBe(false)
  })

  it('任一侧不可解析都算新：不可比时宁可多渲染一次也不丢事件', () => {
    expect(isNewerStreamId('zzz', '1700000000000-1')).toBe(true)
    expect(isNewerStreamId('1700000000000-1', 'zzz')).toBe(true)
  })
})
