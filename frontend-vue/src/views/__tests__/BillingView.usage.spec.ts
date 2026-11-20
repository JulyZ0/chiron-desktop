import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, DOMWrapper } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'

/**
 * 计费页「用量分摊」（P0-1）的前端接线。
 *
 * 为什么值得钉住：
 *  - 后端 `GetUsage` 有两个数据源：`groupBy=day` 走 credit_transactions（记账口径，含充值），
 *    其它分组走 usage_events（真实 token 用量）。**前端一旦不传参数，后端默认 day**，
 *    用户点了"按模型"却看到按日流水 —— 界面不报错，但数据是错的，这比报错更难发现。
 *  - 切换分组/区间只应重查用量（`/v1/billing/usage`），不该把整页打回骨架屏（余额卡片闪一次）。
 *  - 明细表的 bucket 为空时后端给 'unknown'，前端要翻译成可读文案而不是直接显示英文标识符。
 */

const apiMocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  // 补价（PUT /v1/admin/model-prices/{model}）走 put
  put: vi.fn(),
}))

// antd 的 Table 走 useBreakpoint → responsiveObserve，jsdom 没有 matchMedia。
// 不补这一层，每个用例结束时都会抛 Unhandled Rejection，把通过数也带塌。
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

vi.mock('../../api', () => ({
  api: { get: apiMocks.get, post: apiMocks.post, put: apiMocks.put },
}))

import BillingView from '../BillingView.vue'

const router = createRouter({
  history: createMemoryHistory(),
  routes: [
    { path: '/billing', component: { template: '<div />' } },
    // 补价入口的目标页：缺价面板要能真的跳过去
    { path: '/billing/prices', component: { template: '<div />' } },
  ],
})

const BALANCE = { balance: 1200, daily_free_limit: 10, daily_free_used: 2, daily_free_remaining: 8 }

const DAY_USAGE = {
  daily: [{ day: '2026-10-28', tx_count: 3, credits_spent: 42, credits_added: 0 }],
  total_spent: 42,
  total_added: 0,
  period_days: 30,
}

const MODEL_USAGE = {
  group_by: 'model',
  period_days: 30,
  breakdown: [
    { bucket: 'gpt-4o', event_count: 3, input_tokens: 100, output_tokens: 50, cached_tokens: 10, cost_cents: 12 },
    // 空模型名后端归入 'unknown'，前端需翻译
    { bucket: 'unknown', event_count: 1, input_tokens: 7, output_tokens: 3, cached_tokens: 0, cost_cents: 1 },
  ],
  total_input: 107,
  total_output: 53,
  total_cached: 10,
  total_cost: 13,
  daily: [],
}

/**
 * 被后端截断的响应：只回 Top 100，但 total_* 仍是**全量**合计。
 * 这两个字段的一致性是最容易被改坏的 —— 若有人把 total_* 改成"返回行之和"，
 * 用户拿明细永远对不上账，而且界面上不会有任何报错。
 */
const TRUNCATED_USAGE = {
  group_by: 'model',
  period_days: 90,
  breakdown: [
    { bucket: 'gpt-4o', event_count: 3, input_tokens: 100, output_tokens: 50, cached_tokens: 10, cost_cents: 12 },
  ],
  total_input: 999,
  total_output: 999,
  total_cached: 0,
  total_cost: 500,
  truncated: true,
  total_buckets: 240,
  limit: 100,
  daily: [],
}

/** 按 URL 返回替身数据，并记录每次请求的 params */
function stubApi() {
  apiMocks.get.mockImplementation((url: string) => {
    if (url === '/v1/billing/balance') return Promise.resolve({ data: { data: BALANCE } })
    if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: DAY_USAGE } })
    if (url === '/v1/billing/history') return Promise.resolve({ data: { data: { history: [] } } })
    return Promise.resolve({ data: { data: {} } })
  })
}

/** 取 usage 端点最近一次请求带的 params */
function lastUsageParams() {
  const calls = apiMocks.get.mock.calls.filter((c) => c[0] === '/v1/billing/usage')
  return calls.length ? calls[calls.length - 1][1]?.params : undefined
}

/** antd 的 Radio.Button 渲染成 <input type=radio value=...>，勾选它即可触发 @change */
async function chooseRadio(wrapper: ReturnType<typeof mount>, value: string) {
  const input = wrapper.findAll('input[type="radio"]').find(i => (i.element as HTMLInputElement).value === value)
  if (!input) {
    throw new Error(`找不到 value=${value} 的单选，现有：${wrapper.findAll('input[type="radio"]').map(i => (i.element as HTMLInputElement).value).join(' | ')}`)
  }
  await input.setValue(true)
  await flushPromises()
}

async function mountView() {
  const wrapper = mount(BillingView, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}


/**
 * 非零 token 的显示：计费页"消耗全为 0"的用户可见形态。
 *
 * 后端修好之后（按天视图也会 SUM token），页面必须把这些数字**真的显示出来**：
 *  - 按天视图也要有 input/output/cached 与记账行数，不能只有花费；
 *  - 花费算不出来（无单价）时，token 仍然是自证"用量被记下来了"的唯一证据；
 *  - 历史行缺 token 明细时给出标注，但不凭空补数。
 */
const DAY_USAGE_WITH_TOKENS = {
  daily: [
    { day: '2026-11-19', tx_count: 3, credits_spent: 0, credits_added: 0, input_tokens: 1200, output_tokens: 300, cached_tokens: 128, zero_token_events: 0 },
  ],
  total_spent: 0,
  period_days: 90,
  total_input: 1200,
  total_output: 300,
  total_cached: 128,
  total_calls: 3,
  zero_token_events: 0,
  count_basis: 'usage_rows',
}

const DAY_USAGE_WITH_LEGACY_ROWS = {
  daily: [
    { day: '2026-08-01', tx_count: 10, credits_spent: 0, credits_added: 0, input_tokens: 0, output_tokens: 0, cached_tokens: 0, zero_token_events: 10 },
  ],
  total_spent: 0,
  period_days: 90,
  total_input: 0,
  total_output: 0,
  total_cached: 0,
  total_calls: 10,
  zero_token_events: 10,
  count_basis: 'usage_rows',
}

describe('BillingView 用量分摊', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    stubApi()
  })

  it('首次加载显式带上默认的 groupBy=day & range=30d', async () => {
    await mountView()
    // 显式传参：默认值前后端各有一份，省略参数会让"界面标题"与"实际查询"可能不一致
    expect(lastUsageParams()).toEqual({ groupBy: 'day', range: '30d' })
  })

  it('切到「按模型」只重查用量，并按明细渲染', async () => {
    const wrapper = await mountView()
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: MODEL_USAGE } })
      return Promise.resolve({ data: { data: BALANCE } })
    })

    await chooseRadio(wrapper, 'model')

    expect(lastUsageParams()).toEqual({ groupBy: 'model', range: '30d' })
    // 本页只保留用量，已不再请求余额端点：切分组更不该触发任何额外请求
    const balanceCalls = apiMocks.get.mock.calls.filter(c => c[0] === '/v1/billing/balance')
    expect(balanceCalls).toHaveLength(0)

    const text = wrapper.text()
    expect(text).toContain('gpt-4o')
    // 'unknown' 必须翻译成可读文案，而不是原样显示
    expect(text).toContain('未知')
    expect(text).not.toContain('unknown')
  })

  it('切到 90 天时把 range 带进请求', async () => {
    const wrapper = await mountView()
    await chooseRadio(wrapper, '90d')
    expect(lastUsageParams()).toEqual({ groupBy: 'day', range: '90d' })
  })

  it('切回「按天」恢复趋势视图，不报错', async () => {    const wrapper = await mountView()
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: DAY_USAGE } })
      return Promise.resolve({ data: { data: BALANCE } })
    })
    await chooseRadio(wrapper, 'session')
    await chooseRadio(wrapper, 'day')

    expect(lastUsageParams()).toEqual({ groupBy: 'day', range: '30d' })
    expect(wrapper.text()).toContain('每日消耗趋势')
  })

  it('明细被截断时提示"仅显示前 N 项"，且合计数不被截断', async () => {
    const wrapper = await mountView()
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: TRUNCATED_USAGE } })
      return Promise.resolve({ data: { data: BALANCE } })
    })

    await chooseRadio(wrapper, 'model')
    await flushPromises()

    const text = wrapper.text()
    // 用户必须知道"这里显示不全"，否则会以为明细与合计对不上是数据错了
    expect(text).toContain('仅显示花费最高的')
    expect(text).toContain('240')

    // 合计来自后端全量汇总（999/500），不是 1 行明细之和（100/12）
    expect(text).toContain('999')
    expect(text).toContain('500')
  })

  it('未被截断时不显示截断提示', async () => {
    const wrapper = await mountView()
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: MODEL_USAGE } })
      return Promise.resolve({ data: { data: BALANCE } })
    })

    await chooseRadio(wrapper, 'model')
    await flushPromises()

    // truncated 缺省（老后端）不能误报"显示不全"
    expect(wrapper.text()).not.toContain('仅显示花费最高的')
  })
  it('按天视图显示真实 token 与记账行数（花费为 0 也不掩盖用量）', async () => {
    const wrapper = await mountView()
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: DAY_USAGE_WITH_TOKENS } })
      return Promise.resolve({ data: { data: BALANCE } })
    })
    await chooseRadio(wrapper, '90d')
    await flushPromises()

    const text = wrapper.text()
    // 1200 / 300 / 128：三个 token 维度都必须渲染出来
    expect(text).toContain('1200')
    expect(text).toContain('300')
    expect(text).toContain('128')
    // 花费为 0 是"算不出来"，不该让 token 也一起消失
    expect(text).toContain('每日消耗趋势')
    // 调用次数口径必须可见，否则用户无从解释"调用次数和我数的不一样"
    expect(text).toContain('记账行')
  })

  it('存在无 token 明细的历史行时给出标注', async () => {
    const wrapper = await mountView()
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: DAY_USAGE_WITH_LEGACY_ROWS } })
      return Promise.resolve({ data: { data: BALANCE } })
    })
    await chooseRadio(wrapper, '90d')
    await flushPromises()

    // 不回填，但要说清"这段时间没有 token 明细"
    expect(wrapper.text()).toContain('升级前')
  })

  it('没有历史零 token 行时不显示该标注', async () => {
    const wrapper = await mountView()
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: DAY_USAGE_WITH_TOKENS } })
      return Promise.resolve({ data: { data: BALANCE } })
    })
    await chooseRadio(wrapper, '90d')
    await flushPromises()

    expect(wrapper.text()).not.toContain('升级前')
  })
})

// ── P1-9：缺价显示「未知」而不是 0 ─────────────────────────────────

/** 缺价行：后端派生 price_missing（cost_cents IS NULL ⇒ 全部未定价） */
const MODEL_USAGE_WITH_UNPRICED = {
  group_by: 'model',
  period_days: 30,
  breakdown: [
    // 全部已定价：显示数字，不能出现 "—"
    { bucket: 'gpt-4o', event_count: 2, input_tokens: 100, output_tokens: 10, cached_tokens: 0, cost_cents: 12, priced_calls: 2, unpriced_calls: 0, price_missing: false },
    // 全部缺价：显示 "—"（0 会被读成"免费"）
    { bucket: 'mystery-model', event_count: 3, input_tokens: 900, output_tokens: 90, cached_tokens: 0, cost_cents: 0, priced_calls: 0, unpriced_calls: 3, price_missing: true },
    // 部分缺价：金额是"已定价部分"，tooltip 说明还差几次
    { bucket: 'half-priced', event_count: 4, input_tokens: 50, output_tokens: 5, cached_tokens: 0, cost_cents: 3, priced_calls: 1, unpriced_calls: 3, price_missing: true },
  ],
  total_input: 1050,
  total_output: 105,
  total_cached: 0,
  total_cost: 15,
  total_calls: 9,
  total_priced_calls: 3,
  total_unpriced_calls: 6,
  price_missing: true,
  zero_token_events: 0,
  daily: [],
}

describe('BillingView 缺价与价格覆盖率（P1-9）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    stubApi()
  })

  it('缺价的明细行显示 "—" 与 tooltip，不用 0 冒充免费', async () => {
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: MODEL_USAGE_WITH_UNPRICED } })
      return Promise.resolve({ data: { data: BALANCE } })
    })
    const wrapper = await mountView()
    await chooseRadio(wrapper, 'model')
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('mystery-model')
    expect(text).toContain('—')

    // tooltip 挂在单元格的 title 上（原生 title 即悬浮说明）
    const tooltips = wrapper.findAll('td[title]').map(c => c.attributes('title') ?? '')
    expect(tooltips.some(t => t.includes('没有可用单价'))).toBe(true)
    // 部分缺价行：金额仍显示（已知的钱不能丢），并说明只覆盖已定价部分
    expect(tooltips.some(t => t.includes('只统计已定价部分') || t.includes('只含已定价部分') || t.includes('已定价'))).toBe(true)
    expect(text).toContain('3')
  })

  it('汇总卡标注"部分未定价"，不把不完整的合计当作全部花费', async () => {
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: MODEL_USAGE_WITH_UNPRICED } })
      return Promise.resolve({ data: { data: BALANCE } })
    })
    const wrapper = await mountView()
    await chooseRadio(wrapper, 'model')
    await flushPromises()

    expect(wrapper.text()).toContain('部分未定价')
    expect(wrapper.text()).toContain('6')
  })

  it('全部有价时不显示缺价标注', async () => {
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: MODEL_USAGE } })
      return Promise.resolve({ data: { data: BALANCE } })
    })
    const wrapper = await mountView()
    await chooseRadio(wrapper, 'model')
    await flushPromises()

    expect(wrapper.text()).not.toContain('部分未定价')
    expect(wrapper.text()).not.toContain('—')
  })

  it('覆盖率卡片显示百分比、未定价模型与补价入口', async () => {
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: MODEL_USAGE } })
      if (url === '/v1/admin/billing/price-coverage') {
        return Promise.resolve({
          data: {
            data: {
              period_days: 90,
              total_models: 4,
              priced_models: 3,
              unpriced_models: 1,
              coverage: 0.75,
              unpriced: [
                { model: 'mystery-model', calls: 5, token_calls: 5, input_tokens: 100, output_tokens: 10, last_seen: '2026-11-19' },
              ],
              prices_entry_url: '/billing/prices',
            },
          },
        })
      }
      return Promise.resolve({ data: { data: BALANCE } })
    })
    const wrapper = await mountView()
    await chooseRadio(wrapper, 'model')
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('价格覆盖率')
    expect(text).toContain('75%')
    // 未定价模型要可读（补价优先级由调用数决定）
    expect(text).toContain('mystery-model')
    expect(text).toContain('5')

    // 补价入口：点一下必须真的跳到 /billing/prices
    const action = wrapper.findAll('button').find(b => b.text().includes('去补价格'))
    expect(action).toBeTruthy()
    await action!.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/billing/prices')
  })

  it('覆盖率接口失败（非 admin / 老后端）时整块隐藏且不报错', async () => {
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: MODEL_USAGE } })
      if (url === '/v1/admin/billing/price-coverage') return Promise.reject(new Error('403'))
      return Promise.resolve({ data: { data: BALANCE } })
    })
    const wrapper = await mountView()
    await chooseRadio(wrapper, 'model')
    await flushPromises()

    const text = wrapper.text()
    expect(text).not.toContain('价格覆盖率')
    // 也不能渲染成 0%（那会把"读不到"说成"全都没价"）
    expect(text).not.toContain('0%')
    // 用量明细不受影响
    expect(text).toContain('gpt-4o')
  })
})

// ── 手工设置单价（PUT /v1/admin/model-prices/{model}）────────────────

/**
 * 用户可见的缺口：价格表只有"多源交叉验证"的自动写入方，而它默认关闭；于是
 * 未定价模型在界面上只出现一行文字，**没有任何入口能补价**，计费页的花费永远是 0。
 *
 * 这里钉的是那条闭环：
 *  1. 未定价行必须带「设置单价」按钮；
 *  2. 对话框**预填 0**（不替用户猜价）；
 *  3. 保存走 `PUT /v1/admin/model-prices/{model}`，路径段要 URL 编码；
 *  4. 保存后**重新拉覆盖率与用量** —— 不重取的话用户会以为没生效。
 */
const COVERAGE_UNPRICED = {
  period_days: 90,
  total_models: 2,
  priced_models: 1,
  unpriced_models: 1,
  coverage: 0.5,
  unpriced: [
    // 名字里带 `/`：路径段必须编码，否则请求会打到另一个路由上
    { model: 'meta-llama/Llama-3', calls: 7, token_calls: 7, input_tokens: 700, output_tokens: 70, last_seen: '2026-11-19' },
  ],
  prices_entry_url: '/billing/prices',
}

/** Modal 默认 teleport 到 body：断言与操作都得在 document.body 里找 */
function bodyFindAll(selector: string): HTMLElement[] {
  return Array.from(document.body.querySelectorAll(selector)) as HTMLElement[]
}

/**
 * 按文案找按钮。**必须去掉空白再比**：antd 会给两个汉字的按钮自动插入空格
 * （"保存" 渲染成 "保 存"），按原文匹配会全部落空 —— 这种失败看起来像"按钮不存在"。
 */
function bodyButtonByText(text: string): HTMLElement | undefined {
  const want = text.replace(/\s+/g, '')
  return bodyFindAll('button').find(b => (b.textContent ?? '').replace(/\s+/g, '') === want)
}

/**
 * 挂到真实 document.body 上：Modal 的内容是 teleport 出去的，没挂载时
 * `document.body.querySelector` 看不到任何东西（弹窗断言会全部落空）。
 */
async function mountAttached() {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const wrapper = mount(BillingView, { global: { plugins: [router] }, attachTo: host })
  await flushPromises()
  return wrapper
}

describe('BillingView 手工设置单价', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
    stubApi()
  })

  it('未定价行提供「设置单价」，保存时按 URL 编码写上模型并刷新覆盖率', async () => {
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: MODEL_USAGE } })
      if (url === '/v1/admin/billing/price-coverage') return Promise.resolve({ data: { data: COVERAGE_UNPRICED } })
      return Promise.resolve({ data: { data: BALANCE } })
    })
    // 第二次拉覆盖率时模型已定价 ⇒ 覆盖率达到 100%（对应后端"保存即生效"）
    let coverageCalls = 0
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: MODEL_USAGE } })
      if (url === '/v1/admin/billing/price-coverage') {
        coverageCalls++
        if (coverageCalls === 1) return Promise.resolve({ data: { data: COVERAGE_UNPRICED } })
        return Promise.resolve({
          data: {
            data: { ...COVERAGE_UNPRICED, total_models: 2, priced_models: 2, unpriced_models: 0, coverage: 1, unpriced: [] },
          },
        })
      }
      return Promise.resolve({ data: { data: BALANCE } })
    })
    apiMocks.put.mockResolvedValue({ data: { success: true } })

    const wrapper = await mountAttached()
    await chooseRadio(wrapper, 'model')
    await flushPromises()

    // ① 未定价行有入口
    const setPrice = bodyButtonByText('设置单价')
    expect(setPrice).toBeTruthy()
    setPrice!.click()
    await flushPromises()

    // ② 对话框预填 0（绝不替用户猜一个价）
    const inputs = bodyFindAll('.price-dialog-form input')
    expect(inputs.length).toBe(2)
    // InputNumber 带 precision=6：预填 0 显示成 0.000000，数值上必须是 0
    expect(Number((inputs[0] as HTMLInputElement).value)).toBe(0)
    expect(Number((inputs[1] as HTMLInputElement).value)).toBe(0)

    // ③ 填真实单价后保存
    await new DOMWrapper(inputs[0]).setValue('0.27')
    await new DOMWrapper(inputs[1]).setValue('1.1')
    await flushPromises()

    const ok = bodyButtonByText('保存')
    expect(ok).toBeTruthy()
    ok!.click()
    await flushPromises()

    expect(apiMocks.put).toHaveBeenCalledTimes(1)
    const [path, payload] = apiMocks.put.mock.calls[0]
    // 路径段必须编码：`/` 不编码会落到别的路由上（后端同一个通配段匹配不到）
    expect(path).toBe(`/v1/admin/model-prices/${encodeURIComponent('meta-llama/Llama-3')}`)
    expect(payload).toEqual({ input_per_1k: 0.27, output_per_1k: 1.1, currency: 'USD' })

    // ④ 保存后必须重取覆盖率（否则用户会以为没生效）
    expect(coverageCalls).toBeGreaterThanOrEqual(2)
    await flushPromises()
    expect(wrapper.text()).toContain('100%')

    wrapper.unmount()
  })

  it('两个单价都是 0 时不发请求：0 表示「不知道」而不是「免费」', async () => {
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: MODEL_USAGE } })
      if (url === '/v1/admin/billing/price-coverage') return Promise.resolve({ data: { data: COVERAGE_UNPRICED } })
      return Promise.resolve({ data: { data: BALANCE } })
    })

    const wrapper = await mountAttached()
    await chooseRadio(wrapper, 'model')
    await flushPromises()

    bodyButtonByText('设置单价')!.click()
    await flushPromises()
    bodyButtonByText('保存')!.click()
    await flushPromises()

    expect(apiMocks.put).not.toHaveBeenCalled()

    wrapper.unmount()
  })
})

// ── 价格源状态（GET /v1/admin/billing/price-sources）────────────────

/**
 * 覆盖率只说"缺多少价"，不说"为什么缺"。实测 pricetoken 超时时，用户只能看到
 * 价格表空了，没有任何地方指出是某个源失败。这里钉住那条可见性：
 *  - 失败源必须显示状态与**脱敏后的原因**；
 *  - `empty`（源活着但对不上模型名）不能显示成失败；
 *  - 接口失败时整块隐藏，不能渲染成"所有源正常"。
 */
const PRICE_SOURCES_FAILED = {
  sources: [
    {
      source: 'pricetoken',
      parser: 'pricetoken',
      provider: '',
      url_host: 'pricetoken.ai',
      status: 'failed',
      rows: 0,
      duration_ms: 812,
      error: 'ReadTimeout: 请求超时',
      checked_at: '2026-11-20T10:00:00Z',
      age_seconds: 60,
      stale: false,
    },
    {
      source: 'llmrates',
      parser: 'llmrates',
      provider: '',
      url_host: 'www.llmrates.ai',
      status: 'empty',
      rows: 0,
      duration_ms: 90,
      error: '',
      checked_at: '2026-11-20T10:00:00Z',
      age_seconds: 60,
      stale: false,
    },
  ],
  summary: {
    total: 2, ok: 0, empty: 1, failed: 1, stale: false, stale_count: 0,
    rows_total: 0, last_run_at: '2026-11-20T10:00:00Z',
  },
}

describe('BillingView 价格源状态', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    stubApi()
  })

  it('在覆盖率卡片里显示每个源的状态与失败原因', async () => {
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: MODEL_USAGE } })
      if (url === '/v1/admin/billing/price-coverage') {
        return Promise.resolve({ data: { data: COVERAGE_UNPRICED } })
      }
      if (url === '/v1/admin/billing/price-sources') {
        return Promise.resolve({ data: { data: PRICE_SOURCES_FAILED } })
      }
      return Promise.resolve({ data: { data: BALANCE } })
    })

    const wrapper = await mountAttached()
    await chooseRadio(wrapper, 'model')
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('价格源状态')
    expect(text).toContain('pricetoken')
    // 失败原因必须可见（否则"价格表为什么空"永远无从判断）
    expect(text).toContain('ReadTimeout')
    // 三态分开：llmrates 是空结果，不是失败
    expect(text).toContain('空结果')
    expect(text).toContain('失败')

    wrapper.unmount()
  })

  it('源状态接口失败（非 admin / 老后端）时整块隐藏且不报错', async () => {
    apiMocks.get.mockImplementation((url: string) => {
      if (url === '/v1/billing/usage') return Promise.resolve({ data: { data: MODEL_USAGE } })
      if (url === '/v1/admin/billing/price-coverage') {
        return Promise.resolve({ data: { data: COVERAGE_UNPRICED } })
      }
      if (url === '/v1/admin/billing/price-sources') return Promise.reject(new Error('403'))
      return Promise.resolve({ data: { data: BALANCE } })
    })

    const wrapper = await mountAttached()
    await chooseRadio(wrapper, 'model')
    await flushPromises()

    const text = wrapper.text()
    expect(text).not.toContain('价格源状态')
    // 覆盖率面板本身仍在
    expect(text).toContain('价格覆盖率')

    wrapper.unmount()
  })
})
