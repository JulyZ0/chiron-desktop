import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('../../api', () => ({
  uploadFile: vi.fn(),
  listModels: vi.fn(async () => []),
}))

import ChatInput from '../ChatInput.vue'
import { useAuthStore } from '../../../stores/auth'
import { privateKey } from '../../../utils/privateStorage'

/** 输入历史按账号隔离（见 utils/privateStorage），测试里也要用带账号的键 */
const TEST_USER_ID = 'u1'
const HISTORY_KEY = privateKey('chiron:composer-history:v1', TEST_USER_ID)

function mountInput(props: Record<string, unknown> = {}) {
  return mount(ChatInput, {
    props: { loading: false, mode: 'normal', modeOptions: [], sessionId: 's1', ...props } as any,
  })
}

const valueOf = (wrapper: ReturnType<typeof mountInput>) =>
  (wrapper.find('textarea').element as HTMLTextAreaElement).value

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  // 组件用 auth.user.id 拼历史键；不注入 user 会退化成 :anonymous，就测不到真实路径
  useAuthStore().user = { id: TEST_USER_ID, email: '', name: '', role: 'user', tenant_id: 0 }
})

describe('ChatInput（输入区交互）', () => {
  it('↑ 召回最近发送的内容，↓ 越过最近一条后回到自己的草稿', async () => {
    const wrapper = mountInput()
    const ta = wrapper.find('textarea')

    await ta.setValue('第一个问题')
    await ta.trigger('keydown', { key: 'Enter' })
    expect(wrapper.emitted('send')?.[0]?.[0]).toBe('第一个问题')

    await ta.setValue('正在写一半的草稿')
    await ta.trigger('keydown', { key: 'ArrowUp' })
    expect(valueOf(wrapper)).toBe('第一个问题')

    await ta.trigger('keydown', { key: 'ArrowDown' })
    expect(valueOf(wrapper)).toBe('正在写一半的草稿')
  })

  it('Esc 放弃召回并恢复草稿', async () => {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(['历史内容']))
    const wrapper = mountInput()
    const ta = wrapper.find('textarea')

    await ta.setValue('我的草稿')
    await ta.trigger('keydown', { key: 'ArrowUp' })
    expect(valueOf(wrapper)).toBe('历史内容')

    await ta.trigger('keydown', { key: 'Escape' })
    expect(valueOf(wrapper)).toBe('我的草稿')
  })

  it('光标不在首行时 ↑ 不召回（留给光标移动）', async () => {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(['历史内容']))
    const wrapper = mountInput()
    const ta = wrapper.find('textarea')

    await ta.setValue('第一行\n第二行')
    await ta.trigger('keydown', { key: 'ArrowUp' })
    expect(valueOf(wrapper)).toBe('第一行\n第二行')
  })

  it('输入法组合期按 Enter 不发送（选词确认不该提交）', async () => {
    const wrapper = mountInput()
    const ta = wrapper.find('textarea')

    await ta.setValue('中文候选')
    await ta.trigger('keydown', { key: 'Enter', isComposing: true })
    expect(wrapper.emitted('send')).toBeUndefined()
  })

  it('发送后写入历史，重复内容只保留最近一条', async () => {
    const wrapper = mountInput()
    const ta = wrapper.find('textarea')

    for (const text of ['a', 'b', 'a']) {
      await ta.setValue(text)
      await ta.trigger('keydown', { key: 'Enter' })
    }
    expect(JSON.parse(localStorage.getItem(HISTORY_KEY) as string)).toEqual(['b', 'a'])
  })

  it('草稿按会话持久化并在切回时恢复', async () => {
    const wrapper = mountInput()
    const ta = wrapper.find('textarea')

    await ta.setValue('未发完的草稿')
    await new Promise(resolve => setTimeout(resolve, 350))   // 草稿写入有 300ms 防抖
    expect(localStorage.getItem('chiron:draft:s1')).toBe('未发完的草稿')

    await wrapper.setProps({ sessionId: 's2' })
    expect(valueOf(wrapper)).toBe('')

    await wrapper.setProps({ sessionId: 's1' })
    expect(valueOf(wrapper)).toBe('未发完的草稿')
  })
})

describe('ChatInput（运行中补充指令 steer）', () => {
  it('只在"正在生成 + 手上有草稿"时出现', async () => {
    const wrapper = mountInput()
    // 空闲：没有草稿、也没在跑
    expect(wrapper.find('.steer-btn').exists()).toBe(false)

    await wrapper.setProps({ loading: true })
    expect(wrapper.find('.steer-btn').exists()).toBe(false)      // 在跑但没内容可注入

    await wrapper.find('textarea').setValue('先别删那个表')
    expect(wrapper.find('.steer-btn').exists()).toBe(true)       // 两条都满足才出现

    // 空闲但有草稿：此时普通发送就是正确动作，不该出现 steer
    await wrapper.setProps({ loading: false })
    expect(wrapper.find('.steer-btn').exists()).toBe(false)
  })

  it('发出 steer 且清空输入，但不打断、不写提问历史', async () => {
    const wrapper = mountInput({ loading: true })
    await wrapper.find('textarea').setValue('先别删那个表')
    await wrapper.find('.steer-btn').trigger('click')

    expect(wrapper.emitted('steer')?.[0]?.[0]).toBe('先别删那个表')
    // 不打断：stop 一次都不该发（那是另一条语义）
    expect(wrapper.emitted('stop')).toBeUndefined()
    expect(wrapper.emitted('send')).toBeUndefined()
    expect(valueOf(wrapper)).toBe('')
    // 补充指令不是"我问过的问题"，不该进 ↑ 召回历史
    expect(localStorage.getItem(HISTORY_KEY)).toBeNull()
  })

  it('setDraft 把失败的原文还回输入框（不发送、不写历史）', async () => {
    const wrapper = mountInput()
    ;(wrapper.vm as any).setDraft('没送到的那句话')
    await new Promise(resolve => setTimeout(resolve, 0))   // 等 v-model 回写 DOM
    expect(valueOf(wrapper)).toBe('没送到的那句话')
    expect(wrapper.emitted('send')).toBeUndefined()
    expect(localStorage.getItem(HISTORY_KEY)).toBeNull()
  })
})

describe('ChatInput（未送达消息预览与取回）', () => {
  const items = [
    { id: 'o-1', preview: '第一条没送达的内容', attachmentCount: 2 },
    { id: 'o-2', preview: '第二条没送达的内容', attachmentCount: 0 },
  ]

  it('默认只显示条数；展开后才逐条列出内容', async () => {
    const wrapper = mountInput({ failedQueueCount: 2, outboxItems: items })
    expect(wrapper.find('.outbox-banner').exists()).toBe(true)
    expect(wrapper.find('.outbox-list').exists()).toBe(false)

    const toggle = wrapper.find('.outbox-toggle')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    await toggle.trigger('click')

    expect(wrapper.find('.outbox-list').exists()).toBe(true)
    const rows = wrapper.findAll('.outbox-item-text')
    // 预览必须是**内容本身**，否则用户没法判断这条还值不值得重发
    expect(rows.map(r => r.text())).toEqual(['第一条没送达的内容', '第二条没送达的内容'])
    // 附件数只在该条有附件时出现
    expect(wrapper.findAll('.outbox-item-atts').map(a => a.text())).toEqual(['2 个附件'])
  })

  it('没有预览数据时不给展开入口（只剩重发/丢弃）', async () => {
    const wrapper = mountInput({ failedQueueCount: 1 })
    expect(wrapper.find('.outbox-toggle').exists()).toBe(false)
    expect(wrapper.find('.outbox-retry').exists()).toBe(true)
    expect(wrapper.find('.outbox-list').exists()).toBe(false)
  })

  it('点某一条的「取回到输入框」⇒ 只上报那一条的 id（由父组件搬正文）', async () => {
    const wrapper = mountInput({ failedQueueCount: 2, outboxItems: items })
    await wrapper.find('.outbox-toggle').trigger('click')

    await wrapper.findAll('.outbox-retrieve')[1].trigger('click')
    expect(wrapper.emitted('retrieve-outbox')).toEqual([['o-2']])
    // 取回 ≠ 重发：不该顺手触发一次重试
    expect(wrapper.emitted('retry-outbox')).toBeUndefined()
  })

  it('restoreDraft 追加到已有草稿之后并带回附件（不覆盖用户正在写的）', async () => {
    const wrapper = mountInput()
    await wrapper.find('textarea').setValue('我正在写的新问题')

    ;(wrapper.vm as any).restoreDraft('以前没送达的话', [
      { id: 'a-1', name: '设计.png', url: 'blob:x', mimeType: 'image/png', isImage: true, size: 10 },
    ])
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(valueOf(wrapper)).toBe('我正在写的新问题\n\n以前没送达的话')
    expect(wrapper.findAll('.att-thumb-name, .att-thumb-img')).toHaveLength(1)
    expect(wrapper.emitted('send')).toBeUndefined()
  })

  it('restoreDraft 在草稿为空时直接落正文', async () => {
    const wrapper = mountInput()
    ;(wrapper.vm as any).restoreDraft('空着的时候取回')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(valueOf(wrapper)).toBe('空着的时候取回')
  })
})

describe('ChatInput（`@` 提及触发条件）', () => {
  it('中文紧邻 `@` 也打开面板（中文句子词间不写空格）', async () => {
    const wrapper = mountInput()
    await wrapper.find('textarea').setValue('看看@')
    expect(wrapper.find('.mention-menu').exists()).toBe(true)
  })

  it('邮箱形态的 `@` 不打开面板（ASCII 词字符紧邻）', async () => {
    const wrapper = mountInput()
    await wrapper.find('textarea').setValue('user@')
    expect(wrapper.find('.mention-menu').exists()).toBe(false)
  })

  it('查询里出现空格即视为提及结束，面板收起', async () => {
    const wrapper = mountInput()
    await wrapper.find('textarea').setValue('看看@文件 ')
    expect(wrapper.find('.mention-menu').exists()).toBe(false)
  })
})
