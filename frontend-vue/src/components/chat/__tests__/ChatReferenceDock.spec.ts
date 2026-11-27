import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ChatReferenceDock from '../ChatReferenceDock.vue'

function mountDock(overrides: Record<string, unknown> = {}) {
  return mount(ChatReferenceDock, {
    props: {
      referenceOpen: true,
      subagentOpen: true,
      sessionId: 's1',
      referenceSessionId: 's2',
      referenceTitle: 'ref',
      sessions: [],
      excludeSessionId: 's1',
      handleSide: 'right',
      liveEvents: [],
      focusRunId: '',
      focusSeq: 0,
      active: 'reference',
      ...overrides,
    },
    global: {
      stubs: { SessionPreviewPane: true, SubAgentPane: true },
    },
  })
}

describe('ChatReferenceDock', () => {
  it('两者同时打开时只显示 tab，不新增第二条分割线', () => {
    const wrapper = mountDock()
    const tabs = wrapper.findAll('[role="tab"]')
    expect(tabs).toHaveLength(2)
    expect(tabs[0]?.attributes('aria-selected')).toBe('true')
  })

  it('点击子 Agent tab 切换 active', async () => {
    const wrapper = mountDock()
    await wrapper.findAll('[role="tab"]')[1]!.trigger('click')
    expect(wrapper.emitted('update:active')?.[0]).toEqual(['subagent'])
  })
})
