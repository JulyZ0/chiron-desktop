/**
 * 思考档位（reasoning effort）的唯一事实源。
 *
 * 档位会在**两个地方**出现：请求构建（ChatView 的 `buildLlmConfig`）和
 * 「对话设置」弹层（ChatInput）。两边各写一份顺序/文案一定会漂移，
 * 所以顺序、归一化、标签都收在这里。
 *
 * 空串 = 不携带 —— 是否真正发送由后端 `app/providers/effort.py` 归一化决定
 * （各家服务商词表不一致，直接透传会 400）。
 */
export type EffortValue = '' | 'low' | 'high' | 'max'

/** 顺序即"关 → 低 → 高 → 最高"；下拉与提示文案都读它 */
export const EFFORT_ORDER: readonly EffortValue[] = ['', 'low', 'high', 'max']

/**
 * 下拉里的"关"需要非空 value 才稳定命中选项（空串会被当成未选中），
 * 因此用哨兵值在 UI 层映射回 `''`。
 */
export const EFFORT_DEFAULT = 'default'

export function normalizeEffort(value: unknown): EffortValue {
  const raw = String(value ?? '')
  return (EFFORT_ORDER as readonly string[]).includes(raw) ? (raw as EffortValue) : ''
}

export function effortFromSelect(value: unknown): EffortValue {
  const raw = String(value ?? '')
  return normalizeEffort(raw === EFFORT_DEFAULT ? '' : raw)
}

export function effortLabelOf(t: (key: string) => string, value: unknown): string {
  const labels: Record<string, string> = {
    '': t('common.off'),
    low: t('common.low'),
    high: t('common.high'),
    max: t('common.highest'),
  }
  return labels[normalizeEffort(value)] ?? labels['']
}

export function effortSelectOptions(t: (key: string) => string): { value: string; label: string }[] {
  return EFFORT_ORDER.map(v => ({ value: v || EFFORT_DEFAULT, label: effortLabelOf(t, v) }))
}
