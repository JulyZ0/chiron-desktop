<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import dayjs from 'dayjs'
import { Card, Spin, Radio, Table, Button, message } from 'ant-design-vue'
import { useRouter } from 'vue-router'
import EmptyState from '../components/common/EmptyState.vue'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import { errorMessageText } from '../utils/apiError'
import {
  CreditCardOutlined, ThunderboltOutlined, BarChartOutlined,
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
}

/** 非按天分组时的一行（后端 usage_events 聚合结果） */
interface BreakdownRow {
  bucket: string
  event_count: number
  input_tokens: number
  output_tokens: number
  cached_tokens: number
  cost_cents: number
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
  /** 后端只返回花费最高的 limit 项；truncated=true 表示还有没返回的 bucket */
  truncated?: boolean
  /** 聚合后的 bucket 总数（截断前），用于提示"共 N 项" */
  total_buckets?: number
  limit?: number
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

/** 明细表的行：把后端的 bucket 翻译成可读文本（空值后端归为 'unknown'） */
const breakdownRows = computed(() =>
  (usage.value?.breakdown ?? []).map(row => ({
    ...row,
    label: row.bucket === 'unknown'
      ? t('billing.usage_unknown_bucket')
      : row.bucket,
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
  return [
    first,
    { title: t('billing.usage_col_events'), key: 'event_count', dataIndex: 'event_count', width: 100, align: 'right' as const },
    { title: t('billing.usage_col_input_tokens'), key: 'input_tokens', dataIndex: 'input_tokens', width: 130, align: 'right' as const },
    { title: t('billing.usage_col_output_tokens'), key: 'output_tokens', dataIndex: 'output_tokens', width: 130, align: 'right' as const },
    { title: t('billing.usage_col_cached_tokens'), key: 'cached_tokens', dataIndex: 'cached_tokens', width: 130, align: 'right' as const },
    { title: t('billing.usage_col_cost'), key: 'cost_cents', dataIndex: 'cost_cents', width: 130, align: 'right' as const },
  ]
})

// ── 数据加载 ──

onMounted(async () => {
  await loadUsage(true)
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
          </template>
        </div>
      </Card>

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
