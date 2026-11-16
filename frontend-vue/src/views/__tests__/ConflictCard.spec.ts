import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as any
}

vi.mock('../../api/memory', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/memory')>()
  return {
    ...actual,
    resolveConflict: vi.fn(),
    deleteConflict: vi.fn(),
  }
})

import ConflictCard from '../../components/memory/ConflictCard.vue'
import * as memoryApi from '../../api/memory'
import type { MemoryConflict } from '../../api/memory'

function makeConflict(overrides: Partial<MemoryConflict> = {}): MemoryConflict {
  return {
    conflict_id: 'c-1',
    slot: 'fact',
    item_key: 'city',
    old_value: '上海',
    new_value: '北京',
    source: 'derived',
    created_at: Math.floor(Date.now() / 1000) - 5 * 60, // 5 分钟前
    ...overrides,
  }
}

describe('ConflictCard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('渲染冲突标题、旧值/新值与描述文案', async () => {
    const wrapper = mount(ConflictCard, {
      props: { conflict: makeConflict() },
    })
    const text = wrapper.text()
    expect(text).toContain('记忆冲突')
    expect(text).toContain('事实')
    expect(text).toContain('city')
    expect(text).toContain('上海')
    expect(text).toContain('北京')
    expect(text).toContain('AI 发现了与您已确认信息冲突')
  })

  it('保留当前值按钮触发 resolveConflict(keep_old) 并发出 resolved 事件', async () => {
    vi.mocked(memoryApi.resolveConflict).mockResolvedValue({
      conflict_id: 'c-1',
      final_value: '上海',
      resolution: 'keep_old',
      slot: 'fact',
      item_key: 'city',
    })
    const wrapper = mount(ConflictCard, {
      props: { conflict: makeConflict() },
    })
    const keepBtn = wrapper.find('.btn-keep')
    await keepBtn.trigger('click')
    await flushPromises()

    expect(memoryApi.resolveConflict).toHaveBeenCalledWith('c-1', 'keep_old', undefined)
    const emitted = wrapper.emitted('resolved')
    expect(emitted).toBeTruthy()
    expect(emitted![0]).toEqual(['c-1'])
  })

  it('采用新值按钮触发 resolveConflict(use_new) 并发出 resolved 事件', async () => {
    vi.mocked(memoryApi.resolveConflict).mockResolvedValue({
      conflict_id: 'c-1',
      final_value: '北京',
      resolution: 'use_new',
      slot: 'fact',
      item_key: 'city',
    })
    const wrapper = mount(ConflictCard, {
      props: { conflict: makeConflict() },
    })
    const useBtn = wrapper.find('.btn-use')
    await useBtn.trigger('click')
    await flushPromises()

    expect(memoryApi.resolveConflict).toHaveBeenCalledWith('c-1', 'use_new', undefined)
    expect(wrapper.emitted('resolved')![0]).toEqual(['c-1'])
  })

  it('忽略按钮调用 deleteConflict 并发出 dismissed 事件', async () => {
    vi.mocked(memoryApi.deleteConflict).mockResolvedValue({ deleted: 'c-1' })
    const wrapper = mount(ConflictCard, {
      props: { conflict: makeConflict() },
    })
    const dismissBtn = wrapper.find('.btn-dismiss')
    await dismissBtn.trigger('click')
    await flushPromises()

    expect(memoryApi.deleteConflict).toHaveBeenCalledWith('c-1')
    expect(wrapper.emitted('dismissed')![0]).toEqual(['c-1'])
  })

  it('手动修改对话框：点击按钮→输入值→确认，以 manual 方式提交', async () => {
    vi.mocked(memoryApi.resolveConflict).mockResolvedValue({
      conflict_id: 'c-1',
      final_value: '深圳',
      resolution: 'manual',
      slot: 'fact',
      item_key: 'city',
    })
    const wrapper = mount(ConflictCard, {
      props: { conflict: makeConflict() },
    })

    // 打开手动修改对话框
    await wrapper.find('.btn-manual').trigger('click')
    expect(wrapper.find('.manual-dialog').exists()).toBe(true)

    // 输入新值并确认
    const input = wrapper.find('.manual-input')
    await input.setValue('深圳')
    const confirmBtn = wrapper.findAll('.manual-actions button').find((b) => b.text() === '确认')
    await confirmBtn!.trigger('click')
    await flushPromises()

    expect(memoryApi.resolveConflict).toHaveBeenCalledWith('c-1', 'manual', '深圳')
    expect(wrapper.emitted('resolved')![0]).toEqual(['c-1'])
  })

  it('裁决进行中时所有操作按钮禁用，防止重复提交', async () => {
    // 让 API 保持 pending 以模拟 resolving 状态
    let resolvePromise: Promise<any> | null = null
    vi.mocked(memoryApi.resolveConflict).mockImplementation(
      () => (resolvePromise = new Promise(() => {})),
    )

    const wrapper = mount(ConflictCard, {
      props: { conflict: makeConflict() },
    })

    // 触发一次操作进入 resolving
    const keepBtn = wrapper.find('.btn-keep')
    keepBtn.trigger('click').catch(() => {})
    await flushPromises()

    // 所有按钮应被禁用（disabled 属性或 class）
    const buttons = wrapper.findAll('.conflict-actions button')
    for (const btn of buttons) {
      expect((btn.element as HTMLButtonElement).disabled).toBe(true)
    }

    resolvePromise = null
  })

  it('slot 映射正确：identity/preference/decision/fact', async () => {
    const cases: Array<[string, string]> = [
      ['identity', '身份'],
      ['preference', '偏好'],
      ['decision', '关键决策'],
      ['fact', '事实'],
      ['unknown_slot', 'unknown_slot'],
    ]
    for (const [slot, expectedLabel] of cases) {
      const wrapper = mount(ConflictCard, {
        props: { conflict: makeConflict({ slot }) },
      })
      expect(wrapper.text()).toContain(expectedLabel)
    }
  })
})
