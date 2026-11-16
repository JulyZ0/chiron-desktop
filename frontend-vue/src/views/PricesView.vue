<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Card, Input, Table, Tag, message, Button } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import { DollarOutlined, SearchOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import EmptyState from '../components/common/EmptyState.vue'
import { api } from '../api'
import { errorMessageText } from '../utils/apiError'

const { t } = useI18n()

/**
 * 一行价格。字段与 `internal/api/billing_prices.go::billablePrice` 一一对应。
 *
 * `input_per_1k` / `output_per_1k` 的单位是 **USD / 1k tokens** —— 数值很小
 * （gpt-4o 是 0.0025），直接显示会被误读成"几乎免费"，所以统一按
 * **每百万 token** 展示（×1000），与各家官网的报价口径一致。
 */
interface PriceRow {
  model: string
  provider: string
  input_per_1k: number
  output_per_1k: number
  currency: string
  sources: string[]
  source_count: number
  verified_at: string
  updated_at: string
}

interface PriceResponse {
  prices: PriceRow[]
  total: number
  limit: number
  query: string
  unit: string
}

const rows = ref<PriceRow[]>([])
const total = ref(0)
const loading = ref(false)
const search = ref('')
const loaded = ref(false)

/**
 * 每百万 token 的美元值。
 *
 * 为什么不在后端换算：接口的 `input_per_1k` 是**数据本来的单位**（写库时的
 * USD/1k），换算成展示单位是纯呈现问题；放在视图层后，将来要按 1k 展示也
 * 只改这里一行，不必动接口契约。
 */
function perMillion(per1k: number): string {
  const value = per1k * 1000
  if (!Number.isFinite(value) || value <= 0) return '—'
  // 小于 1 美元时保留 3 位（便宜模型差一个量级就看不出来了）
  return value < 1 ? `$${value.toFixed(3)}` : `$${value.toFixed(2)}`
}

const columns = computed<TableColumnsType>(() => [
  {
    title: t('billing.prices_col_model'),
    dataIndex: 'model',
    key: 'model',
    ellipsis: true,
  },
  {
    title: t('billing.prices_col_provider'),
    dataIndex: 'provider',
    key: 'provider',
    width: 140,
  },
  {
    title: t('billing.prices_col_input'),
    key: 'input',
    width: 150,
    align: 'right',
    customRender: ({ record }: { record: PriceRow }) => perMillion(record.input_per_1k),
  },
  {
    title: t('billing.prices_col_output'),
    key: 'output',
    width: 150,
    align: 'right',
    // 输出价是用户最容易直接比较的一列，用等宽数字对齐
    customRender: ({ record }: { record: PriceRow }) => perMillion(record.output_per_1k),
  },
  {
    title: t('billing.prices_col_sources'),
    key: 'sources',
    width: 210,
    customRender: ({ record }: { record: PriceRow }) => record.sources.join(', ') || '—',
  },
  {
    title: t('billing.prices_col_confidence'),
    key: 'confidence',
    width: 130,
    customRender: ({ record }: { record: PriceRow }) =>
      record.source_count >= 2
        ? t('billing.prices_verified_multi', { n: record.source_count })
        : t('billing.prices_verified_single'),
  },
  {
    title: t('billing.prices_col_verified'),
    dataIndex: 'verified_at',
    key: 'verified_at',
    width: 130,
    customRender: ({ record }: { record: PriceRow }) => record.verified_at || '—',
  },
])

/** 最新一条 updated_at：页脚用来说明"这批价格是什么时候进来的"。 */
const latestUpdate = computed(() => {
  let latest = ''
  for (const row of rows.value) {
    if (row.updated_at > latest) latest = row.updated_at
  }
  return latest
})

/**
 * 搜索防抖：输入停下 300ms 才查。
 *
 * 不打防抖就要么每敲一个字符发一次请求（表小、但没必要），要么必须按回车才生效
 * （用户以为搜索框坏了）。300ms 是"打字间隙"与"响应及时"的常见折中。
 */
let searchTimer: ReturnType<typeof setTimeout> | undefined

function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    void load()
  }, 300)
}

onUnmounted(() => {
  if (searchTimer) clearTimeout(searchTimer)
})

async function load() {
  loading.value = true
  try {
    const resp = await api.get('/v1/billing/prices', {
      params: { q: search.value.trim() || undefined, limit: 500 },
    })
    const data = (resp.data?.data as PriceResponse | undefined)
    rows.value = data?.prices ?? []
    total.value = data?.total ?? rows.value.length
    loaded.value = true
  } catch (error) {
    message.error(errorMessageText(error, t('errors.failed_to_load')))
  } finally {
    loading.value = false
  }
}

onMounted(load)

/**
 * 手动触发一轮价格同步。
 *
 * 后台循环 30min 一轮，新装或换模型后不该干等；点一下就跑同一套多源交叉验证
 * （后端复用 `sync_once()`），完成后重新加载表，让用户立刻看到结果。
 * `written === 0` 不当作成功：可能是网络不可达或只有一个源应答，据实提示。
 */
const syncing = ref(false)

async function refreshPrices() {
  syncing.value = true
  try {
    const resp = await api.post('/v1/billing/prices/sync', {})
    const data = resp.data?.data as { ok?: boolean; written?: number; reason?: string } | undefined
    if (data?.ok === false) {
      message.warning(data.reason || t('billing.prices_sync_disabled'))
    } else if ((data?.written ?? 0) > 0) {
      message.success(t('billing.prices_sync_done', { n: data?.written ?? 0 }))
    } else {
      message.warning(t('billing.prices_sync_empty'))
    }
    await load()
  } catch (error) {
    message.error(errorMessageText(error, t('errors.request_failed')))
  } finally {
    syncing.value = false
  }
}
</script>

<template>
  <div class="prices-container">
    <div class="prices-header">
      <DollarOutlined class="prices-header-icon" />
      <h1>{{ $t('billing.prices_title') }}</h1>
    </div>

    <p class="prices-hint">
      {{ $t('billing.prices_hint') }}
    </p>

    <div class="prices-filters">
      <Input
        v-model:value="search"
        :placeholder="$t('billing.prices_search_placeholder')"
        allow-clear
        class="prices-search"
        @update:value="onSearchInput"
      >
        <template #prefix>
          <SearchOutlined />
        </template>
      </Input>
      <span class="prices-count">{{ $t('billing.prices_total', { n: total }) }}</span>
      <Button
        size="small"
        :loading="syncing"
        @click="refreshPrices"
      >
        {{ $t('billing.prices_sync_now') }}
      </Button>
    </div>

    <Card class="prices-card">
      <Table
        v-if="rows.length"
        :columns="columns"
        :data-source="rows"
        :row-key="(r: PriceRow) => `${r.provider}/${r.model}`"
        :loading="loading"
        size="small"
        :scroll="{ x: 1000 }"
        :pagination="{ pageSize: 50, showSizeChanger: false }"
      />
      <EmptyState
        v-else-if="loaded && !loading"
        :description="$t('billing.prices_empty')"
        :hint="$t('billing.prices_empty_hint')"
      />

      <p
        v-if="latestUpdate"
        class="prices-footer"
      >
        {{ $t('billing.prices_updated', { date: latestUpdate }) }}
      </p>
    </Card>
  </div>
</template>

<style scoped>
.prices-container { padding: 24px; max-width: 1180px; margin: 0 auto; }
.prices-header { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
.prices-header h1 { margin: 0; font-size: 24px; font-weight: var(--fw-semibold); color: var(--text-primary); }
.prices-header-icon { font-size: 26px; color: var(--warning); }
.prices-hint { margin: 0 0 16px; font-size: 13px; color: var(--text-tertiary); line-height: 1.6; }

.prices-filters { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-bottom: 12px; }
.prices-search { max-width: 280px; }
.prices-count { font-size: 13px; color: var(--text-tertiary); font-variant-numeric: tabular-nums; }

.prices-footer { margin: 12px 0 0; font-size: 12px; color: var(--text-tertiary); }

/* 数字列等宽：价格纵向比较时数字必须对齐，否则一眼看不出量级差 */
.prices-card :deep(td) { font-variant-numeric: tabular-nums; }

@media (max-width: 576px) {
  .prices-container { padding: 12px; }
  .prices-header h1 { font-size: 20px; }
  .prices-search { max-width: none; width: 100%; }
  .prices-container :deep(.ant-input) { font-size: 16px; }
}
</style>
