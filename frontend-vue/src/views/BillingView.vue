<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import dayjs from 'dayjs'
import { Card, Spin, Radio, Table, Button, InputNumber, Modal, message } from 'ant-design-vue'
import { useRouter } from 'vue-router'
import EmptyState from '../components/common/EmptyState.vue'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import { errorMessageText } from '../utils/apiError'
import {
  CreditCardOutlined, ThunderboltOutlined, BarChartOutlined, MoneyCollectOutlined,
} from '@ant-design/icons-vue'
import { api } from '../api'

import { useI18n } from 'vue-i18n'

const { t } = useI18n()
const router = useRouter()

// ── 类型 ──

interface DailyUsage {
  day: string
  tx_count: number
  credits_spent: number
  credits_added: number
  input_tokens?: number
  output_tokens?: number
  cached_tokens?: number
  zero_token_events?: number
  /** 当天「有 token 但没有单价」的记账行数（后端派生自 cost_cents IS NULL） */
  unpriced_calls?: number
  price_missing?: boolean
}

/** 非按天分组时的一行（后端 usage_events 聚合结果） */
interface BreakdownRow {
  bucket: string
  /**
   * 厂商归属（`usage_events.provider`；写入时缺失则后端按模型名回填）。
   * 空串 + `provider_missing` = 后端查不到，**不是**"没有厂商"。
   */
  provider?: string
  provider_missing?: boolean
  event_count: number
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  cost_cents: number
  /** 有单价、真实折算成金额的调用数 */
  priced_calls?: number
  /** 有 token、但取不到单价的调用数（金额里不含这部分） */
  unpriced_calls?: number
  /** 后端派生的缺价标记：unpriced_calls > 0 */
  price_missing?: boolean
}

/** 明细表里的一行：在契约字段之外补上"这一格怎么显示" */
interface BreakdownDisplayRow extends BreakdownRow {
  label: string
  /** 厂商列：查不到归属时显示 "—"（不能让空串渲染成一片空白，那看起来像 bug） */
  provider_display: string
  cost_display: string
  cost_tooltip?: string
}

interface UsageData {
  daily: DailyUsage[]
  /** 按天分组时才有意义；按模型/会话分组时后端返回空数组占位 */
  total_spent?: number
  period_days: number
  /** 以下字段仅在 groupBy != day 时出现 */
  group_by?: UsageGroup
  breakdown?: BreakdownRow[]
  total_input?: number
  total_output?: number
  total_cached?: number
  total_cost?: number
  /** 记账行数（一行 = 某个模型在一个回合里的调用；同模型同回合合并） */
  total_calls?: number
  /** 区间内 token 全为 0 的记账行数：升级前的历史行没有 token 明细 */
  zero_token_events?: number
  /** 区间内缺价（有 token、取不到单价）的记账行数：花费只覆盖已定价部分 */
  total_unpriced_calls?: number
  total_priced_calls?: number
  price_missing?: boolean
  /** 后端只返回花费最高的 limit 项；truncated=true 表示还有没返回的 bucket */
  truncated?: boolean
  /** 聚合后的 bucket 总数（截断前），用于提示"共 N 项" */
  total_buckets?: number
  limit?: number
  /** 按模型分组时：多少个模型的厂商归属查不到（后端标，不猜） */
  provider_missing_models?: number
}

/** 价格覆盖率（GET /v1/admin/billing/price-coverage） */
interface PriceCoverageUnpriced {
  model: string
  /** 厂商（后端按模型名反查；查不到为空串，界面不显示"未知厂商"这种编造值） */
  provider?: string
  calls: number
  token_calls: number
  input_tokens: number
  output_tokens: number
  last_seen: string
}

interface PriceCoverage {
  period_days: number
  total_models: number
  priced_models: number
  unpriced_models: number
  /** 0..1；分母只算"有 token 消耗"的模型 */
  coverage: number
  unpriced: PriceCoverageUnpriced[]
  prices_entry_url?: string
}

/** 与后端 usageGroupByWhitelist 一一对应；写死成联合类型，参数拼错在编译期就红 */
type UsageGroup = 'day' | 'model' | 'session'
type UsageRange = '7d' | '30d' | '90d'

// ── 状态 ──

const usage = ref<UsageData | null>(null)

// 用量视图的分组/区间：与后端 `groupBy` / `range` 白名单一一对应。
// 默认值必须与后端一致（day + 30d），否则首次加载会与"改造前"的口径不同。
const usageGroup = ref<UsageGroup>('day')
const usageRange = ref<UsageRange>('30d')
/** 切换分组/区间时的单独 loading，避免整页闪成骨架屏 */
const usageLoading = ref(false)
/** 明细表分页：90 天按会话分组可能上百行，必须分页而不是一次全渲染 */
const breakdownPage = ref(1)
const BREAKDOWN_PAGE_SIZE = 10

const GROUP_OPTIONS = computed<{ label: string; value: UsageGroup }[]>(() => [
  { label: t('billing.usage_group_day'), value: 'day' },
  { label: t('billing.usage_group_model'), value: 'model' },
  { label: t('billing.usage_group_session'), value: 'session' },
])

const RANGE_OPTIONS: { label: string; value: UsageRange; days: number }[] = [
  { label: t('billing.usage_range_days', { n: 7 }), value: '7d', days: 7 },
  { label: t('billing.usage_range_days', { n: 30 }), value: '30d', days: 30 },
  { label: t('billing.usage_range_days', { n: 90 }), value: '90d', days: 90 },
]

/** 区间天数：优先用后端回传的 period_days（是权威值），没有时按选择项推算 */
const rangeDays = computed(() =>
  usage.value?.period_days
  ?? RANGE_OPTIONS.find(o => o.value === usageRange.value)?.days
  ?? 30,
)

const loading = ref(true)

/**
 * 价格覆盖率面板（P1-9）。
 *
 * `null` 表示"这次拿不到"（非 admin 的 403 / 老后端 404 / 网络错误）—— 面板
 * 直接不显示，**不报错也不显示 0%**：把一次"没权限/没部署"渲染成"覆盖率 0%"
 * 会误导运营去补一堆其实已有价格的模型。
 */
const coverage = ref<PriceCoverage | null>(null)

/** 覆盖率百分数（整数）；无模型可定价时不显示 0%，由文案说明 */
const coveragePercent = computed(() => {
  if (!coverage.value || coverage.value.total_models <= 0) return ''
  return `${Math.round((coverage.value.coverage ?? 0) * 100)}%`
})

// ── 手工设置单价（PUT /v1/admin/model-prices/{model}）──
//
// 为什么要有这个对话框：价格表只有"多源交叉验证"的自动写入方，而它**默认关闭**，
// 所以自建 / 聚合网关 / 新发布的模型永远没有价 —— 计费页只能看着一串 0，没有任何
// 入口能补。这里就是那个入口：由人填，**预填 0**（绝不替用户猜一个数）。
const priceDialogOpen = ref(false)
/** 正在定价的模型名（同时是请求路径段，需 URL 编码） */
const priceModel = ref('')
const priceInput = ref<number>(0)
const priceOutput = ref<number>(0)
const priceSaving = ref(false)

function openPriceDialog(model: string) {
  priceModel.value = model
  // 预填 0 而不是某个"看起来合理"的默认价：猜错的价格会被当成事实长期留着。
  priceInput.value = 0
  priceOutput.value = 0
  priceDialogOpen.value = true
}

/**
 * 保存单价。成功后**立刻重取覆盖率与用量**：
 * 后端写完就清空单价缓存（下一次调用立即按新价折算），覆盖率按"模型有没有价"
 * 判定，所以刷新后该模型立即算已定价（不需要等缓存 TTL）。
 */
async function savePrice() {
  const model = priceModel.value
  if (!model || priceSaving.value) return
  const input = Number(priceInput.value ?? 0)
  const output = Number(priceOutput.value ?? 0)
  if (!(input > 0) && !(output > 0)) {
    // 0/0 后端会 400：0 不会被当成"免费"，而是"不知道"，保存了也不会有任何效果
    message.error(t('billing.price_dialog_zero'))
    return
  }
  priceSaving.value = true
  try {
    await api.put(`/v1/admin/model-prices/${encodeURIComponent(model)}`, {
      input_per_1k: input,
      output_per_1k: output,
      currency: 'USD',
    })
    message.success(t('billing.price_dialog_saved', { model }))
    priceDialogOpen.value = false
    await Promise.all([loadCoverage(), loadUsage()])
  } catch (error) {
    message.error(errorMessageText(error, t('errors.failed_to_load')))
  } finally {
    priceSaving.value = false
  }
}

// ── 计算属性 ──

const todaySpent = computed(() => {
  if (!usage.value?.daily?.length) return 0
  const today = dayjs().format('YYYY-MM-DD')
  return usage.value.daily.find(d => d.day === today)?.credits_spent ?? 0
})

/** 近 N 天消耗序列（升序，用于柱状图）；仅按天分组时有数据 */
const chartData = computed(() => {
  const daily = [...(usage.value?.daily ?? [])].reverse()
  return daily.map(d => ({
    day: d.day,
    label: dayjs(d.day).format('MM-DD'),
    spent: d.credits_spent,
  }))
})

const chartMax = computed(() => Math.max(1, ...chartData.value.map(d => d.spent)))

/** 是否处于"按天"视图：决定展示柱状图还是明细表 */
const isDayGroup = computed(() => usageGroup.value === 'day')

/**
 * 是否存在"没有 token 明细"的记账行（升级前的历史数据）。
 *
 * 为什么要单独提示：这些行是**真实发生过的调用**，但当时链路只写了"有一次调用"、
 * 没拿到 token —— 页面若只显示 0，用户会以为统计坏了；而回填是不可能的（等于编数据）。
 * 所以标注口径，而不是假装数据完整。
 */
const hasLegacyZeroTokenRows = computed(() => (usage.value?.zero_token_events ?? 0) > 0)

/** 区间内的总 token（输入 + 输出；缓存命中的输入已包含在输入里，不重复相加） */
const totalTokens = computed(() =>
  (usage.value?.total_input ?? 0) + (usage.value?.total_output ?? 0),
)

/**
 * 是否存在「有 token、但取不到单价」的记账行（P1-9 缺价派生）。
 *
 * 为什么必须单独提示：这些行的 `cost_cents` 是 NULL，合计里表现为 0 —— 用户会
 * 以为"这个模型免费"或"统计坏了"。事实是**价格表缺行**，金额不可知。历史零值
 * 不回填，但必须说清楚"这个合计不完整"。
 */
const hasUnpricedCalls = computed(() => (usage.value?.total_unpriced_calls ?? 0) > 0)

/**
 * 明细行的花费格：缺价显示 "—"（而不是 0），并给出 tooltip 说明原因。
 *
 * 三种形态：
 *  - 全部已定价：显示金额；
 *  - **部分**缺价：显示金额（只含已定价部分）+ tooltip 说明还差多少次调用，
 *    这样已知的钱不丢；
 *  - 全部缺价：显示 "—"，金额无从谈起（不能写 0，0 是"免费"）。
 */
function costCell(row: BreakdownRow): { cost_display: string; cost_tooltip?: string } {
  const unpriced = Number(row.unpriced_calls ?? 0)
  const priced = Number(
    row.priced_calls ?? (unpriced > 0 ? 0 : row.event_count ?? 0),
  )
  if (unpriced > 0 && priced === 0) {
    return { cost_display: '—', cost_tooltip: t('billing.usage_price_missing_tooltip') }
  }
  if (unpriced > 0) {
    return {
      cost_display: String(row.cost_cents ?? 0),
      cost_tooltip: t('billing.usage_price_partial_tooltip', {
        n: unpriced,
        cost: row.cost_cents ?? 0,
      }),
    }
  }
  return { cost_display: String(row.cost_cents ?? 0) }
}

/** 明细表的行：把后端的 bucket 翻译成可读文本（空值后端归为 'unknown'） */
const breakdownRows = computed<BreakdownDisplayRow[]>(() =>
  (usage.value?.breakdown ?? []).map(row => ({
    ...row,
    label: row.bucket === 'unknown'
      ? t('billing.usage_unknown_bucket')
      : row.bucket,
    // 厂商：后端查不到就给空串 + provider_missing，这里显示 "—" 而不是编一个名字
    provider_display: row.provider && row.provider !== ''
      ? row.provider
      : (row.provider_missing ? '—' : ''),
    ...costCell(row),
  })),
)

/**
 * 明细表列：只在分组维度上显示一列（day / model / session），
 * 而不是把三列都渲染出来 —— 另外两列必然是空的，用户会以为数据丢了。
 */
const breakdownColumns = computed(() => {
  const first = {
    day: { title: t('billing.usage_col_day'), key: 'label', dataIndex: 'label', width: 160 },
    model: { title: t('billing.usage_col_model'), key: 'label', dataIndex: 'label', width: 240 },
    session: { title: t('billing.usage_col_session'), key: 'label', dataIndex: 'label', width: 240 },
  }[usageGroup.value]
  // 厂商列只在按模型分组时出现：按天/按会话没有单一厂商，加一列只会显示一排 "—"。
  const providerColumn = usageGroup.value === 'model'
    ? [{
        title: t('billing.usage_col_provider'),
        key: 'provider',
        dataIndex: 'provider_display',
        width: 140,
        // 归属查不到时给出说明（不是"没有厂商"，而是"没记下来也查不到"）
        customCell: (record: BreakdownDisplayRow) => (
          record.provider_missing ? { title: t('billing.usage_provider_missing_tooltip') } : {}
        ),
      }]
    : []
  return [
    first,
    ...providerColumn,
    { title: t('billing.usage_col_events'), key: 'event_count', dataIndex: 'event_count', width: 100, align: 'right' as const },
    { title: t('billing.usage_col_input_tokens'), key: 'input_tokens', dataIndex: 'input_tokens', width: 130, align: 'right' as const },
    { title: t('billing.usage_col_output_tokens'), key: 'output_tokens', dataIndex: 'output_tokens', width: 130, align: 'right' as const },
    { title: t('billing.usage_col_cached_tokens'), key: 'cached_tokens', dataIndex: 'cached_tokens', width: 130, align: 'right' as const },
    {
      title: t('billing.usage_col_cost'),
      key: 'cost_cents',
      // 走算好的显示值：缺价要显示 "—" 而不是把 0 当成免费
      dataIndex: 'cost_display',
      width: 130,
      align: 'right' as const,
      // 原生 title 即 tooltip（无 JS 定位开销）；缺价行的说明靠它承载
      customCell: (record: BreakdownDisplayRow) => (
        record.cost_tooltip ? { title: record.cost_tooltip } : {}
      ),
    },
  ]
})

// ── 数据加载 ──

onMounted(async () => {
  // 覆盖率与用量互不依赖：并行发，任何一个失败都不影响另一个（覆盖率的失败
  // 只是"不显示面板"）。
  await Promise.all([loadUsage(true), loadCoverage()])
})

/**
 * 查询参数：**总是显式传** groupBy/range。
 * 虽然省略时后端默认 day+30d，但显式传参让"页面显示的区间"与"请求的区间"永远一致——
 * 默认值在前后端各写一份，任一改动都会导致界面标题与实际数据不符。
 */
function usageParams(): Record<string, string> {
  return { groupBy: usageGroup.value, range: usageRange.value }
}

/**
 * 加载用量。
 *
 * `initial=true` 时用整页骨架屏（首次进入），切换分组/区间时只转表格，
 * 避免每次切换都把整页打回骨架屏。
 */
async function loadUsage(initial = false) {
  try {
    if (initial) loading.value = true
    else usageLoading.value = true
    const resp = await api.get('/v1/billing/usage', { params: usageParams() })
    usage.value = (resp.data?.data as UsageData | undefined) ?? null
    breakdownPage.value = 1
  } catch (error) {
    message.error(errorMessageText(error, t('errors.failed_to_load')))
  } finally {
    loading.value = false
    usageLoading.value = false
  }
}

/**
 * 切换分组/区间时重新查询。
 * 「按天」分组用 credit_transactions（记账口径），其它分组用 usage_events（真实用量），
 * 两者不可混用，否则"按模型看花费"会拿到空数组。
 */
function onUsageQueryChange() {
  void loadUsage()
}

/**
 * 加载价格覆盖率（admin 端点；非 admin 会 403）。
 *
 * 失败时静默隐藏面板：这一块是**辅助信息**，不能因为它让整页报错。数据形状也
 * 校验一层 —— 老后端回 `{}` 时若当成覆盖率渲染，会显示"0 个模型 / 0%"。
 */
async function loadCoverage() {
  try {
    const resp = await api.get('/v1/admin/billing/price-coverage')
    const data = resp.data?.data as PriceCoverage | undefined
    coverage.value = data && typeof data.total_models === 'number' ? data : null
  } catch {
    coverage.value = null
  }
}
</script>

<template>
  <div class="billing-container">
    <div class="billing-header">
      <CreditCardOutlined style="font-size: 26px; color: var(--warning)" />
      <h1>{{ $t('billing.billing_management') }}</h1>
      <!-- 价格一览表：与"我用掉了多少"是两件事，独立成页，这里只放入口 -->
      <Button
        class="billing-header-action"
        size="small"
        @click="router.push('/billing/prices')"
      >
        {{ $t('billing.prices_title') }}
      </Button>
    </div>

    <div
      v-if="loading"
      class="loading-state"
    >
      <PageSkeleton
        variant="cards"
        :columns="1"
      />
    </div>

    <template v-else>
      <Card class="usage-card">
        <template #title>
          <BarChartOutlined />{{ $t('common.used_in_the_last_n_days', { n: rangeDays }) }}
        </template>
        <div class="usage-stats">
          <!-- 按天：记账口径；非按天：真实用量口径（token 维度）。
               本页只呈现用量，不再展示余额或充值口径的统计。 -->
          <template v-if="isDayGroup">
            <div class="stat-item">
              <span class="stat-label">{{ $t('common.consumed_today') }}</span>
              <span class="stat-value stat-spent">{{ todaySpent }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">{{ $t('common.total_consumed') }}</span>
              <span class="stat-value stat-spent">{{ usage?.total_spent ?? 0 }}</span>
            </div>
            <!-- token 在按天视图里也要显示：只看花费的话，"花费算不出来"（无单价）
                 会让整页看起来像"什么都没记"，而用量其实一直在。 -->
            <div class="stat-item">
              <span class="stat-label">{{ $t('billing.usage_col_input_tokens') }}</span>
              <span class="stat-value">{{ usage?.total_input ?? 0 }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">{{ $t('billing.usage_col_output_tokens') }}</span>
              <span class="stat-value">{{ usage?.total_output ?? 0 }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">{{ $t('billing.usage_col_cached_tokens') }}</span>
              <span class="stat-value">{{ usage?.total_cached ?? 0 }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">{{ $t('billing.usage_col_events') }}</span>
              <span class="stat-value">{{ usage?.total_calls ?? 0 }}</span>
            </div>
          </template>
          <template v-else>
            <div class="stat-item">
              <span class="stat-label">{{ $t('billing.usage_col_input_tokens') }}</span>
              <span class="stat-value">{{ usage?.total_input ?? 0 }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">{{ $t('billing.usage_col_output_tokens') }}</span>
              <span class="stat-value">{{ usage?.total_output ?? 0 }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">{{ $t('billing.usage_col_cached_tokens') }}</span>
              <span class="stat-value">{{ usage?.total_cached ?? 0 }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">{{ $t('billing.usage_col_cost') }}</span>
              <span class="stat-value stat-spent">{{ usage?.total_cost ?? 0 }}</span>
            </div>
            <div class="stat-item">
              <span class="stat-label">{{ $t('billing.usage_col_events') }}</span>
              <span class="stat-value">{{ usage?.total_calls ?? 0 }}</span>
            </div>
          </template>
        </div>
        <!-- 票据口径说明：调用次数 = 记账行数（一行 = 某模型在某回合的调用）。
             写清楚是为了让"调用次数和我数的不一样"有据可查，而不是靠猜。 -->
        <div class="breakdown-hint">
          {{ $t('billing.usage_count_basis', { rows: usage?.total_calls ?? 0, tokens: totalTokens }) }}
        </div>
        <!-- 历史数据标注：升级前的行只记了"调用了一次"，没有 token 明细。
             不回填（那等于编数据），但要明确告诉用户这段时间的数据不完整。 -->
        <div
          v-if="hasLegacyZeroTokenRows"
          class="breakdown-hint breakdown-truncated"
        >
          {{ $t('billing.usage_legacy_no_tokens', { n: usage?.zero_token_events ?? 0 }) }}
        </div>
        <!-- 缺价标注（P1-9）：有 token 但取不到单价的调用不计入花费。
             不写 0（0 是"免费"），而是说明"这个合计只覆盖已定价部分"。 -->
        <div
          v-if="hasUnpricedCalls"
          class="breakdown-hint breakdown-unpriced"
        >
          {{ $t('billing.usage_price_partial', { n: usage?.total_unpriced_calls ?? 0 }) }}
        </div>
      </Card>

      <!-- 价格覆盖率面板：回答"花费为什么算不出来"——不是统计坏了，是价格表缺行。
           只在拿得到数据时渲染（非 admin / 老后端时整块消失，不显示假的 0%）。 -->
      <Card
        v-if="coverage"
        class="coverage-card"
      >
        <template #title>
          <MoneyCollectOutlined /> {{ $t('billing.coverage_title') }}
        </template>
        <div class="coverage-row">
          <span
            v-if="coveragePercent"
            class="coverage-rate"
          >{{ coveragePercent }}</span>
          <span class="coverage-text">
            {{ coverage.total_models > 0
              ? $t('billing.coverage_hint', {
                days: coverage.period_days,
                models: coverage.total_models,
                priced: coverage.priced_models,
              })
              : $t('billing.coverage_empty', { days: coverage.period_days }) }}
          </span>
          <Button
            size="small"
            class="coverage-action"
            @click="router.push('/billing/prices')"
          >
            {{ $t('billing.coverage_action') }}
          </Button>
        </div>
        <!-- 未定价模型列表：按调用数降序（后端排好），决定补价优先级。
             每行都能直接设置单价 —— 此前只能跳去价格表看一眼，然后无路可走。 -->
        <ul
          v-if="coverage.unpriced.length"
          class="coverage-list"
        >
          <li
            v-for="item in coverage.unpriced"
            :key="item.model"
            class="coverage-item"
          >
            <span class="coverage-item-text">
              {{ $t('billing.coverage_unpriced_item', { model: item.model, calls: item.calls }) }}
            </span>
            <Button
              size="small"
              type="link"
              class="coverage-item-action"
              @click="openPriceDialog(item.model)"
            >
              {{ $t('billing.price_set_action') }}
            </Button>
          </li>
        </ul>
      </Card>

      <!-- 设置单价：预填 0，由人给出真实单价（后端不猜、不写死任何厂商价格） -->
      <Modal
        v-model:open="priceDialogOpen"
        :title="$t('billing.price_dialog_title', { model: priceModel })"
        :confirm-loading="priceSaving"
        :ok-text="$t('billing.price_dialog_save')"
        @ok="savePrice"
      >
        <p class="price-dialog-hint">
          {{ $t('billing.price_dialog_hint') }}
        </p>
        <div class="price-dialog-form">
          <label>{{ $t('billing.price_dialog_input') }}</label>
          <InputNumber
            v-model:value="priceInput"
            class="price-dialog-field"
            :min="0"
            :step="0.01"
            :precision="6"
            addon-after="USD / 1k"
          />
          <label>{{ $t('billing.price_dialog_output') }}</label>
          <InputNumber
            v-model:value="priceOutput"
            class="price-dialog-field"
            :min="0"
            :step="0.01"
            :precision="6"
            addon-after="USD / 1k"
          />
        </div>
      </Modal>

      <!-- 分组 / 区间切换：放在图表与明细之上，切换只重查用量 -->
      <div class="usage-filters">
        <span class="filter-label">{{ $t('billing.usage_group_by') }}</span>
        <Radio.Group
          v-model:value="usageGroup"
          :options="GROUP_OPTIONS"
          option-type="button"
          button-style="solid"
          @change="onUsageQueryChange"
        />
        <Radio.Group
          v-model:value="usageRange"
          :options="RANGE_OPTIONS"
          option-type="button"
          @change="onUsageQueryChange"
        />
      </div>

      <!-- 按天：消耗趋势；按模型/会话：明细表 -->
      <Card
        v-if="isDayGroup && chartData.length"
        class="chart-card"
        style="margin-top: 16px"
      >
        <template #title>
          <ThunderboltOutlined /> {{ $t('common.daily_consumption_trend') }}
        </template>
        <div class="bar-chart">
          <div
            v-for="d in chartData"
            :key="d.day"
            class="bar-col"
            :title="`${d.day}: ${d.spent} credits`"
          >
            <div class="bar-value">
              {{ d.spent > 0 ? d.spent : '' }}
            </div>
            <div
              class="bar"
              :style="{ height: Math.max(2, Math.round((d.spent / chartMax) * 120)) + 'px' }"
              :class="{ 'bar-zero': d.spent === 0 }"
            />
            <div class="bar-label">
              {{ d.label }}
            </div>
          </div>
        </div>
      </Card>

      <Card
        v-else-if="!isDayGroup"
        class="breakdown-card"
        style="margin-top: 16px"
      >
        <template #title>
          <ThunderboltOutlined /> {{ $t('billing.usage_breakdown_title') }}
        </template>
        <Spin :spinning="usageLoading">
          <Table
            v-if="breakdownRows.length"
            :columns="breakdownColumns"
            :data-source="breakdownRows"
            :row-key="(r: BreakdownRow) => r.bucket"
            size="small"
            :scroll="{ x: 860 }"
            :pagination="{
              current: breakdownPage,
              pageSize: BREAKDOWN_PAGE_SIZE,
              showSizeChanger: false,
              onChange: (p: number) => { breakdownPage = p },
              showTotal: (n: number) => $t('common.total_n', { n }),
            }"
          />
          <EmptyState
            v-else-if="!usageLoading"
            :description="$t('billing.usage_breakdown_empty')"
          />
          <div class="breakdown-hint">
            {{ $t('billing.usage_breakdown_hint') }}
          </div>
          <!-- 截断提示：后端只回花费最高的 limit 项。总量仍是全量合计，所以要说清
               "显示不全"而不是"数据不准"，否则用户会以为统计错了。 -->
          <div
            v-if="usage?.truncated"
            class="breakdown-hint breakdown-truncated"
          >
            {{ $t('billing.usage_breakdown_truncated', {
              shown: breakdownRows.length,
              total: usage.total_buckets ?? breakdownRows.length,
            }) }}
          </div>
          <!-- 厂商归属缺失：老行写入时拿不到 provider，且按模型名也查不到。
               不猜（不按名字前缀推断），只把"我们不知道"说清楚。 -->
          <div
            v-if="(usage?.provider_missing_models ?? 0) > 0 && usageGroup === 'model'"
            class="breakdown-hint"
          >
            {{ $t('billing.usage_provider_missing', { n: usage?.provider_missing_models ?? 0 }) }}
          </div>
        </Spin>
      </Card>
    </template>
  </div>
</template>

<style scoped>
.billing-container { padding: 24px; max-width: 1080px; margin: 0 auto; }
.billing-header { display: flex; align-items: center; gap: 12px; margin-bottom: 20px; }
.billing-header h1 { margin: 0; font-size: 24px; font-weight: var(--fw-semibold); color: var(--text-primary); }
/* 入口按钮推到最右：标题区左对齐、动作右对齐，窄屏折行时不挤压标题 */
.billing-header-action { margin-inline-start: auto; }
.loading-state { padding: 0; }

/* 使用统计：auto-fill 自适应列数（窄屏自动降列） */
.usage-stats { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 12px; }
.stat-item { text-align: center; padding: 14px 8px; background: var(--bg-secondary, rgba(0,0,0,0.02)); border-radius: var(--radius-lg); }
.stat-label { display: block; color: var(--text-tertiary); font-size: 12px; margin-bottom: 6px; }
.stat-value { font-size: 22px; font-weight: var(--fw-semibold); color: var(--text-primary); font-variant-numeric: tabular-nums; }
.stat-spent { color: var(--colorError, var(--error)); }

/* 分组/区间切换条：窄屏自动折行，不挤压卡片 */
.usage-filters { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 16px; }
.filter-label { font-size: 13px; color: var(--text-tertiary); }
.breakdown-hint { margin-top: 10px; font-size: 12px; color: var(--text-tertiary); }
/* 缺价标注用"提醒"色而不是错误色：数据是真的，只是金额不完整 */
.breakdown-unpriced { color: var(--colorWarning, var(--warning)); }

/* 价格覆盖率：一行三项，窄屏折行 */
.coverage-card { margin-top: 16px; }
.coverage-row { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.coverage-rate { font-size: 22px; font-weight: var(--fw-semibold); color: var(--text-primary); font-variant-numeric: tabular-nums; }
.coverage-text { flex: 1; min-width: 200px; font-size: 13px; color: var(--text-secondary); }
.coverage-list { margin: 10px 0 0; padding-inline-start: 18px; font-size: 12px; color: var(--text-tertiary); }
.coverage-list li { margin-top: 2px; }
/* 未定价行：文案在左、设置单价在右（窄屏自动折行，不挤成一列） */
.coverage-item { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.coverage-item-text { flex: 1; min-width: 160px; }
.coverage-item-action { padding: 0 4px; height: auto; }
.price-dialog-hint { margin: 0 0 12px; font-size: 12px; color: var(--text-tertiary); }
.price-dialog-form { display: flex; flex-direction: column; gap: 6px; }
.price-dialog-field { width: 100%; }

.bar-chart { display: flex; align-items: flex-end; gap: 6px; height: 170px; overflow-x: auto; padding-top: 8px; -webkit-overflow-scrolling: touch; }
.bar-col { flex: 1; min-width: 26px; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }
.bar-value { font-size: var(--fs-xs); color: var(--text-tertiary); margin-bottom: 2px; white-space: nowrap; }
.bar { width: 70%; border-radius: 3px 3px 0 0; background: linear-gradient(180deg, var(--colorWarning, var(--warning)), var(--colorWarning-bg, #fbbf24)); }
.bar-zero { background: var(--border-card, #e5e7eb); }
.bar-label { font-size: var(--fs-xs); color: var(--text-tertiary); margin-top: 4px; white-space: nowrap; transform: rotate(-30deg); transform-origin: top left; }

/* 窄屏（≤576px，与 .u-hide-sm 断点一致）：卡片贴边、iOS 防缩放 */
@media (max-width: 576px) {
  .billing-container { padding: 12px; }
  .billing-header h1 { font-size: 20px; }
  .stat-value { font-size: 18px; }
  .billing-container :deep(.ant-card-body) { padding: 16px; }
  .billing-container :deep(.ant-input) { font-size: 16px; }
}

@media (prefers-reduced-motion: reduce) {
  .bar { transition: none; }
}
</style>
