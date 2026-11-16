import { describe, it, expect } from 'vitest'
import { createLedger } from '../transcriptMeasurementLedger'

describe('transcriptMeasurementLedger（测量台账）', () => {
  it('暂存不改变已发布几何，发布后一次性生效', () => {
    const ledger = createLedger()
    ledger.stage('a', 100)
    expect(ledger.sizes().size).toBe(0)      // 未发布 → 几何不变
    const snap = ledger.publish()
    expect(snap?.get('a')).toBe(100)
    expect(ledger.sizes().get('a')).toBe(100)
  })

  it('无实际变更时不发布（DOM 抖动不该触发重渲染）', () => {
    const ledger = createLedger()
    ledger.stage('a', 100)
    ledger.publish()
    expect(ledger.publish()).toBeNull()      // 空批次

    ledger.stage('a', 100)                   // 重复上报同一尺寸
    expect(ledger.publish()).toBeNull()
  })

  it('部分变更时合并新旧尺寸', () => {
    const ledger = createLedger()
    ledger.stage('a', 100)
    ledger.stage('b', 200)
    ledger.publish()

    ledger.stage('b', 250)
    const snap = ledger.publish()
    expect(snap?.get('a')).toBe(100)
    expect(snap?.get('b')).toBe(250)
  })

  it('已发布快照不可变：后续发布不会改动先前拿到的几何', () => {
    const ledger = createLedger()
    ledger.stage('a', 100)
    const first = ledger.publish()
    ledger.stage('a', 500)
    ledger.publish()
    expect(first?.get('a')).toBe(100)        // 第一代几何保持原值
    expect(ledger.sizes().get('a')).toBe(500)
  })

  it('忽略非法测量值', () => {
    const ledger = createLedger()
    ledger.stage('', 100)
    ledger.stage('a', Number.NaN)
    ledger.stage('b', -5)
    ledger.stage('c', Number.POSITIVE_INFINITY)
    expect(ledger.pendingCount()).toBe(0)
    expect(ledger.publish()).toBeNull()
  })

  it('discard 丢弃整批暂存（切换会话/整批作废）', () => {
    const ledger = createLedger()
    ledger.stage('a', 100)
    expect(ledger.pendingCount()).toBe(1)
    ledger.discard()
    expect(ledger.pendingCount()).toBe(0)
    expect(ledger.publish()).toBeNull()
  })

  it('计数可用于诊断', () => {
    const ledger = createLedger()
    ledger.stage('a', 10)
    ledger.stage('b', 20)
    ledger.publish()
    ledger.stage('c', 30)
    expect(ledger.committedCount()).toBe(2)
    expect(ledger.pendingCount()).toBe(1)
  })
})
