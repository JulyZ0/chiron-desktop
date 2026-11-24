import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick, reactive, ref } from 'vue'
import DataPanel from '../DataPanel.vue'
import type { CacheConfig, SettingsSectionState } from '../types'

if (!window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}

const createModel = (): CacheConfig => ({
  l1Capacity: 2048,
  l2Ttl: 3600,
  semanticThreshold: 0.95,
  prefetchEnabled: true,
})

const mountPanel = (state: SettingsSectionState, exporting = false) => {
  const model = ref(createModel())
  const wrapper = mount(DataPanel, {
    props: {
      modelValue: model.value,
      cacheState: state,
      exporting,
    },
  })
  return { wrapper, model }
}

describe('DataPanel', () => {
  it('缓存只在有未保存修改时可保存，丢弃动作独立冒泡', async () => {
    const state = reactive<SettingsSectionState>({ ready: true, dirty: false, saving: false })
    const { wrapper, model } = mountPanel(state)

    expect(wrapper.find('[data-test="cache-config-save"]').attributes('disabled')).toBeDefined()

    model.value.l1Capacity = 4096
    state.dirty = true
    await nextTick()
    expect(wrapper.find('[data-test="cache-config-save"]').attributes('disabled')).toBeUndefined()

    await wrapper.find('[data-test="cache-config-save"]').trigger('click')
    await wrapper.find('[data-test="cache-config-discard"]').trigger('click')
    expect(wrapper.emitted('saveCache')).toHaveLength(1)
    expect(wrapper.emitted('discardCache')).toHaveLength(1)
    wrapper.unmount()
  })

  it('数据导出不依赖缓存加载状态，并透传导出 loading', () => {
    const state: SettingsSectionState = { ready: false, dirty: false, saving: false }
    const { wrapper } = mountPanel(state, true)
    const button = wrapper.find('[data-test="data-export-btn"]')

    expect(button.attributes('disabled')).toBeUndefined()
    expect(button.classes()).toContain('ant-btn-loading')
    wrapper.unmount()
  })
})
