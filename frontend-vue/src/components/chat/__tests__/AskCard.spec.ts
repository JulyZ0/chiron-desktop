import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AskCard from '../AskCard.vue'

describe('AskCard（结构化提问卡）', () => {
  it('选项按钮点击后把选项值作为回答抛出', async () => {
    const wrapper = mount(AskCard, {
      props: { question: '要用哪个模型？', options: ['快速', { label: '高质量', value: 'high' }] },
    })
    const buttons = wrapper.findAll('.ask-option')
    expect(buttons.map(b => b.text())).toEqual(['快速', '高质量'])

    await buttons[1]!.trigger('click')
    expect(wrapper.emitted('answer')?.[0]?.[0]).toBe('high')
  })

  it('有选项时仍可自由输入回答', async () => {
    const wrapper = mount(AskCard, { props: { question: '选一个', options: ['A', 'B'] } })
    await wrapper.find('.ask-input').setValue('  C  ')
    await wrapper.find('.ask-submit').trigger('click')
    expect(wrapper.emitted('answer')?.[0]?.[0]).toBe('C')
  })

  it('关闭自由输入后只保留选项', () => {
    const wrapper = mount(AskCard, {
      props: { question: '选一个', options: ['A'], allowFreeText: false },
    })
    expect(wrapper.find('.ask-input').exists()).toBe(false)
  })

  it('没有选项时只给输入框（问题必须能回答）', () => {
    const wrapper = mount(AskCard, { props: { question: '你的项目名是什么？' } })
    expect(wrapper.findAll('.ask-option').length).toBe(0)
    expect(wrapper.find('.ask-input').exists()).toBe(true)
  })

  it('空白回答不提交', async () => {
    const wrapper = mount(AskCard, { props: { question: 'Q' } })
    await wrapper.find('.ask-input').setValue('   ')
    await wrapper.find('.ask-submit').trigger('click')
    expect(wrapper.emitted('answer')).toBeUndefined()
  })

  it('超时后禁用交互但保留问题上下文', async () => {
    const wrapper = mount(AskCard, {
      props: { question: '选一个', options: ['A'], expired: true },
    })
    expect(wrapper.text()).toContain('已超时')
    expect(wrapper.findAll('.ask-option')[0]!.attributes('disabled')).toBeDefined()

    await wrapper.findAll('.ask-option')[0]!.trigger('click')
    expect(wrapper.emitted('answer')).toBeUndefined()
  })
})
