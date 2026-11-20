import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises, DOMWrapper } from '@vue/test-utils'

/**
 * 价格表「批量导入」的前端接线（P0-4）。
 *
 * 为什么要钉：
 *  - 自动同步依赖外部价格源，源挂了就一行都没有（实测 pricetoken 超时）；
 *    手工单价一次只能填一个模型 —— 导入是整张表缺价时唯一可用的入口。
 *  - 后端逐行校验并回 `{imported, skipped:[{row,reason}]}`。只显示"导入成功 N 行"
 *    而藏起跳过的行，用户永远不知道自己少填了什么，界面上也不会有任何报错。
 *  - 选择文件必须走 multipart（不是把文件内容当 JSON 发）：否则大文件会被
 *    JSON 转义放大，且后端的文件路径分支永远得不到验证。
 */

const apiMocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }))

// antd 的 Table 走 useBreakpoint → responsiveObserve，jsdom 没有 matchMedia。
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

vi.mock('../../api', () => ({
  api: { get: apiMocks.get, post: apiMocks.post },
}))

import PricesView from '../PricesView.vue'

const PRICES = {
  prices: [
    {
      model: 'deepseek-v4.1-flash',
      provider: 'deepseek',
      input_per_1k: 0.27,
      output_per_1k: 1.1,
      currency: 'USD',
      sources: ['import'],
      source_count: 1,
      verified_at: '2026-11-20',
      updated_at: '2026-11-20',
    },
  ],
  total: 1,
  limit: 500,
  query: '',
  unit: 'USD/1k tokens',
}

const IMPORT_OK = {
  imported: 2,
  total: 3,
  skipped: [{ row: 4, reason: 'only USD is supported' }],
  format: 'csv',
  effective: 'next_call',
}

/** Modal 默认 teleport 到 body：断言与操作都得在 document.body 里找 */
function bodyFindAll(selector: string): HTMLElement[] {
  return Array.from(document.body.querySelectorAll(selector)) as HTMLElement[]
}

/** antd 会给两个汉字的按钮自动插空格，按文案找按钮必须先去掉空白 */
function bodyButtonByText(text: string): HTMLElement | undefined {
  const want = text.replace(/\s+/g, '')
  return bodyFindAll('button').find(b => (b.textContent ?? '').replace(/\s+/g, '') === want)
}

async function mountAttached() {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const wrapper = mount(PricesView, { attachTo: host })
  await flushPromises()
  return wrapper
}

/** 挂载并打开导入弹窗 */
async function openImport(wrapper: ReturnType<typeof mount>) {
  const action = wrapper.findAll('button').find(b => b.text().includes('导入价格'))
  expect(action).toBeTruthy()
  await action!.trigger('click')
  await flushPromises()
}

describe('PricesView 批量导入', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
    apiMocks.get.mockResolvedValue({ data: { data: PRICES } })
  })

  it('粘贴 CSV 时按 json body 提交，并渲染 imported/跳过原因', async () => {
    apiMocks.post.mockResolvedValue({ data: { data: IMPORT_OK } })

    const wrapper = await mountAttached()
    await openImport(wrapper)

    const textarea = bodyFindAll('textarea')[0]
    expect(textarea).toBeTruthy()
    await new DOMWrapper(textarea).setValue(
      'model,input_per_1k,output_per_1k,currency\ndeepseek-v4.1-flash,0.27,1.10,USD\nm,1,2,CNY\n',
    )
    await flushPromises()

    const submit = bodyButtonByText('开始导入')
    expect(submit).toBeTruthy()
    submit!.click()
    await flushPromises()

    expect(apiMocks.post).toHaveBeenCalledTimes(1)
    const [path, payload] = apiMocks.post.mock.calls[0]
    expect(path).toBe('/v1/admin/billing/model-prices/import')
    // 粘贴文本走 JSON：format 按首个非空白字符推断（这里是 model,... ⇒ csv）
    expect(payload.format).toBe('csv')
    expect(payload.data).toContain('deepseek-v4.1-flash')

    // 结果摘要必须把"跳过了哪些行、为什么"显示出来
    const text = document.body.textContent ?? ''
    expect(text).toContain('共 3 行：导入 2 行，跳过 1 行')
    expect(text).toContain('第 4 行')
    expect(text).toContain('only USD is supported')

    // 导入成功后重新拉表：用户必须立刻看到新价
    const priceCalls = apiMocks.get.mock.calls.filter(c => c[0] === '/v1/billing/prices')
    expect(priceCalls.length).toBeGreaterThanOrEqual(2)

    wrapper.unmount()
  })

  it('选择文件时走 multipart，而不是把文件内容塞进 JSON', async () => {
    apiMocks.post.mockResolvedValue({
      data: { data: { imported: 1, total: 1, skipped: [] } },
    })

    const wrapper = await mountAttached()
    await openImport(wrapper)

    const input = bodyFindAll('input[type="file"]')[0] as HTMLInputElement
    expect(input).toBeTruthy()
    const file = new File(['model,input_per_1k,output_per_1k,currency\nm,1,2,USD\n'], 'prices.csv', {
      type: 'text/csv',
    })
    Object.defineProperty(input, 'files', { value: [file] })
    await new DOMWrapper(input).trigger('change')
    await flushPromises()

    expect(document.body.textContent).toContain('prices.csv')

    bodyButtonByText('开始导入')!.click()
    await flushPromises()

    expect(apiMocks.post).toHaveBeenCalledTimes(1)
    const [path, payload] = apiMocks.post.mock.calls[0]
    expect(path).toBe('/v1/admin/billing/model-prices/import')
    expect(payload).toBeInstanceOf(FormData)
    expect((payload as FormData).get('file')).toBeInstanceOf(File)

    wrapper.unmount()
  })

  it('空输入不发请求：先让用户补内容，而不是甩一个 400', async () => {
    const wrapper = await mountAttached()
    await openImport(wrapper)

    bodyButtonByText('开始导入')!.click()
    await flushPromises()

    expect(apiMocks.post).not.toHaveBeenCalled()

    wrapper.unmount()
  })

  it('全部行非法时明确说明"未写入任何价格"，且不重新拉表', async () => {
    apiMocks.post.mockResolvedValue({
      data: {
        data: {
          imported: 0,
          total: 2,
          skipped: [
            { row: 2, reason: 'model is required' },
            { row: 3, reason: 'input_per_1k and output_per_1k cannot both be 0' },
          ],
        },
      },
    })

    const wrapper = await mountAttached()
    const initialPriceCalls = apiMocks.get.mock.calls.filter(c => c[0] === '/v1/billing/prices').length
    await openImport(wrapper)

    await new DOMWrapper(bodyFindAll('textarea')[0]).setValue('model,input_per_1k,output_per_1k,currency\n,1,2,USD\nm,0,0,USD\n')
    await flushPromises()
    bodyButtonByText('开始导入')!.click()
    await flushPromises()

    const text = document.body.textContent ?? ''
    expect(text).toContain('共 2 行：导入 0 行，跳过 2 行')
    // 必须说清"数据库没动"，否则用户会以为部分写入成功了
    expect(text).toContain('数据库没有任何改动')

    const priceCallsAfter = apiMocks.get.mock.calls.filter(c => c[0] === '/v1/billing/prices').length
    expect(priceCallsAfter).toBe(initialPriceCalls)

    wrapper.unmount()
  })

  it('跳过行超过 20 条时折叠并提示还有多少条', async () => {
    const skipped = Array.from({ length: 25 }, (_, i) => ({
      row: i + 2,
      reason: 'model is required',
    }))
    apiMocks.post.mockResolvedValue({ data: { data: { imported: 0, total: 25, skipped } } })

    const wrapper = await mountAttached()
    await openImport(wrapper)
    await new DOMWrapper(bodyFindAll('textarea')[0]).setValue('anything')
    await flushPromises()
    bodyButtonByText('开始导入')!.click()
    await flushPromises()

    const text = document.body.textContent ?? ''
    expect(text).toContain('另有 5 条跳过未显示')

    wrapper.unmount()
  })
})
