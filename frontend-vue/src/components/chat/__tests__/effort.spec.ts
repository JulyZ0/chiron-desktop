import { describe, expect, it } from 'vitest'
import {
  EFFORT_DEFAULT,
  EFFORT_ORDER,
  effortFromSelect,
  effortLabelOf,
  effortSelectOptions,
  normalizeEffort,
} from '../effort'

/** 只回显 key 后缀的替身：标签本身是 i18n 的事，这里只钉住"哪一档对哪一句" */
const t = (key: string) => key.replace('common.', '')

describe('effort（思考档位）', () => {
  it('顺序固定为 关 → 低 → 高 → 最高', () => {
    expect([...EFFORT_ORDER]).toEqual(['', 'low', 'high', 'max'])
    expect(effortSelectOptions(t).map(o => o.label)).toEqual(['off', 'low', 'high', 'highest'])
  })

  it('下拉用非空哨兵值，并映射回空串（空串 = 不携带）', () => {
    expect(effortSelectOptions(t)[0].value).toBe(EFFORT_DEFAULT)
    expect(effortFromSelect(EFFORT_DEFAULT)).toBe('')
    expect(effortFromSelect('high')).toBe('high')
  })

  it('非法值一律归一化为空串（后端只认真实档位）', () => {
    expect(normalizeEffort('ultra')).toBe('')
    expect(normalizeEffort(undefined)).toBe('')
    expect(effortFromSelect('ultra')).toBe('')
  })

  it('标签文案跟随档位，未知档位回落到"关"', () => {
    expect(effortLabelOf(t, 'max')).toBe('highest')
    expect(effortLabelOf(t, 'nope')).toBe('off')
  })
})
