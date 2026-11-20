import { describe, expect, it } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import MessageItem from '../MessageItem.vue'
import type { ChatItem, KnowledgeRetrievalItem } from '../chat-types'

/**
 * 知识库检索徽标（P1-6）的**渲染**。
 *
 * 引擎在注入上下文的同时发一条 `knowledge_retrieval` 事件（结构化字段，不是文案），
 * 前端把它落成一条 `kind: 'knowledge'` 的 item。此前用户面对"回答里没有库内容"
 * 完全无从判断：是没选库、库停用了、服务报错、还是确实没命中 —— 四种处置完全相反。
 *
 * 这里钉住四点：
 *  1. 正常态显示"检索 N 个库 · 命中 M 条"；
 *  2. 零命中显示**建议**（换关键词 / 确认索引），不是只说"没命中"；
 *  3. 失败态报分类（前端据此给动作），且**优先于**零命中（有错先修错）；
 *  4. `showKnowledgeBadge=false` 时整行不渲染 —— 只读/分享面不该看到库的权限细节。
 */

const kbItem = (patch: Partial<KnowledgeRetrievalItem> = {}): ChatItem => ({
  kind: 'knowledge',
  id: 'kb1',
  kbIds: ['kb-a', 'kb-b'],
  queried: 2,
  hits: 3,
  citations: 4,
  zeroHit: false,
  errors: [],
  ...patch,
})

const mountItem = (item: ChatItem, props: Record<string, unknown> = {}) =>
  mount(MessageItem, { props: { item, ...props } })

describe('知识库检索徽标', () => {
  it('正常态：显示检索库数与命中数', async () => {
    const wrapper = mountItem(kbItem())
    await flushPromises()
    const badge = wrapper.find('.kb-badge')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toContain('检索 2 个库')
    expect(badge.text()).toContain('命中 3 条')
    expect(badge.attributes('role')).toBe('status')
    expect(wrapper.find('.kb-badge-hint').exists()).toBe(false)
  })

  it('★ 零命中：给出可执行建议，而不是只说未命中', async () => {
    const wrapper = mountItem(kbItem({ hits: 0, citations: 0, zeroHit: true }))
    await flushPromises()
    expect(wrapper.find('.kb-badge').classes()).toContain('zero-hit')
    const hint = wrapper.find('.kb-badge-hint')
    expect(hint.exists()).toBe(true)
    expect(hint.text()).toContain('换个关键词')
  })

  it('★ 失败优先于零命中：报分类而不是"未命中，换关键词"', async () => {
    const wrapper = mountItem(
      kbItem({
        zeroHit: false,
        errors: [{ kbId: 'kb-a', category: 'provider_unavailable', message: '服务不可达' }],
      }),
    )
    await flushPromises()
    const badge = wrapper.find('.kb-badge')
    expect(badge.classes()).toContain('error')
    expect(badge.text()).toContain('1 个库失败')
    const hint = wrapper.find('.kb-badge-hint')
    expect(hint.text()).toContain('provider_unavailable')
    expect(hint.text()).toContain('服务不可达')
  })

  it('showKnowledgeBadge=false：整行不渲染（只读/分享降级）', async () => {
    const wrapper = mountItem(kbItem(), { showKnowledgeBadge: false })
    await flushPromises()
    expect(wrapper.find('.kb-badge-row').exists()).toBe(false)
  })
})
