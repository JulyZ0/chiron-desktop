<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Card, Input, Table, message, Button, Modal } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import { DollarOutlined, SearchOutlined, UploadOutlined } from '@ant-design/icons-vue'
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

/** 导入结果：与 `internal/api/billing_price_import.go` 的响应一一对应 */
interface PriceImportSkip {
  /** 文件里的行号：CSV 表头是 1，所以第一条数据是 2 */
  row: number
  reason: string
}

interface PriceImportResult {
  imported: number
  total: number
  skipped: PriceImportSkip[]
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

// ── 批量导入价格（POST /v1/admin/billing/model-prices/import）──
//
// 为什么要有它：自动同步依赖外部价格源，源挂了（实测 pricetoken 超时）就一行都
// 没有；手工设置一次只能填一个模型。批量导入让"整张表缺价"可以在一次操作里补齐。
// 后端逐行校验并回 `{imported, skipped:[{row,reason}]}`，所以这里必须把**跳过原因**
// 显示出来 —— 只说"导入成功 12 行"而藏起 3 行失败，用户永远不知道自己少填了什么。

const importOpen = ref(false)
const importing = ref(false)
/** 粘贴的 CSV/JSON 文本 */
const importText = ref('')
/** 用户选择的文件（优先于粘贴文本，走 multipart） */
const importFile = ref<File | null>(null)
const importFileName = ref('')
/** 上一次导入的结果摘要（弹窗内展示，不清空直到再次导入/关闭） */
const importResult = ref<PriceImportResult | null>(null)
const fileInputKey = ref(0)

/** 跳过原因最多展示 20 条：再多会把弹窗撑到看不完，也更该按行号回去改文件。 */
const IMPORT_SKIP_PREVIEW = 20

const visibleSkips = computed(() => (importResult.value?.skipped ?? []).slice(0, IMPORT_SKIP_PREVIEW))
const hiddenSkipCount = computed(() =>
  Math.max((importResult.value?.skipped?.length ?? 0) - IMPORT_SKIP_PREVIEW, 0),
)

function openImportDialog() {
  importOpen.value = true
  importText.value = ''
  importFile.value = null
  importFileName.value = ''
  importResult.value = null
  fileInputKey.value += 1
}

function onImportFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  importFile.value = file
  importFileName.value = file?.name ?? ''
  // 换文件等于换输入：旧的摘要不再对应当前内容
  importResult.value = null
}

function clearImportFile() {
  importFile.value = null
  importFileName.value = ''
  fileInputKey.value += 1
}

/** 粘贴内容按首个非空白字符判断格式（JSON 数组/对象 vs CSV）。 */
function detectFormat(text: string): 'json' | 'csv' {
  const trimmed = text.trim()
  return trimmed.startsWith('[') || trimmed.startsWith('{') ? 'json' : 'csv'
}

async function submitImport() {
  if (importing.value) return
  if (!importFile.value && !importText.value.trim()) {
    // 空输入不发请求：后端的 400 是"data is required"，但用户看到"导入失败"会更困惑
    message.error(t('billing.prices_import_empty'))
    return
  }
  importing.value = true
  importResult.value = null
  try {
    let resp
    if (importFile.value) {
      const form = new FormData()
      form.append('file', importFile.value)
      resp = await api.post('/v1/admin/billing/model-prices/import', form)
    } else {
      resp = await api.post('/v1/admin/billing/model-prices/import', {
        format: detectFormat(importText.value),
        data: importText.value,
      })
    }
    const data = resp.data?.data as Partial<PriceImportResult> | undefined
    const result: PriceImportResult = {
      imported: data?.imported ?? 0,
      total: data?.total ?? 0,
      skipped: data?.skipped ?? [],
    }
    importResult.value = result
    if (result.imported > 0) {
      message.success(t('billing.prices_import_done', { n: result.imported }))
      // 表里立即能看到新价：后端写完就清缓存，取价下一次调用即生效
      await load()
    } else {
      message.warning(t('billing.prices_import_all_failed'))
    }
  } catch (error) {
    message.error(errorMessageText(error, t('errors.request_failed')))
  } finally {
    importing.value = false
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
      <!-- 导入入口与"立即同步"并列：源挂了的时候，同步按钮只会再失败一次 -->
      <Button
        size="small"
        class="prices-import-action"
        @click="openImportDialog"
      >
        <template #icon>
          <UploadOutlined />
        </template>
        {{ $t('billing.prices_import_action') }}
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

    <!-- 导入弹窗：粘贴 CSV/JSON 或选择文件，结果摘要留在弹窗里直到关闭 -->
    <Modal
      v-model:open="importOpen"
      :title="$t('billing.prices_import_title')"
      :confirm-loading="importing"
      :ok-text="$t('billing.prices_import_submit')"
      @ok="submitImport"
    >
      <p class="prices-import-hint">
        {{ $t('billing.prices_import_hint') }}
      </p>

      <label class="prices-import-label">{{ $t('billing.prices_import_paste_label') }}</label>
      <Input.TextArea
        v-model:value="importText"
        class="prices-import-textarea"
        :rows="8"
        :placeholder="$t('billing.prices_import_placeholder')"
        :disabled="!!importFile"
      />

      <div class="prices-import-file-row">
        <label class="prices-import-label">{{ $t('billing.prices_import_file_label') }}</label>
        <input
          :key="fileInputKey"
          type="file"
          accept=".csv,.json,text/csv,application/json"
          class="prices-import-file"
          data-testid="price-import-file"
          @change="onImportFileChange"
        >
        <span
          v-if="importFileName"
          class="prices-import-filename"
        >{{ importFileName }}</span>
        <Button
          v-if="importFile"
          size="small"
          type="link"
          @click="clearImportFile"
        >
          {{ $t('billing.prices_import_clear_file') }}
        </Button>
      </div>
      <p
        v-if="importFile"
        class="prices-import-hint"
      >
        {{ $t('billing.prices_import_file_wins') }}
      </p>

      <!-- 结果摘要：imported/total + 逐行跳过原因（后端给的 row 是文件行号） -->
      <div
        v-if="importResult"
        class="prices-import-result"
      >
        <div class="prices-import-summary">
          {{ $t('billing.prices_import_summary', {
            total: importResult.total,
            imported: importResult.imported,
            skipped: importResult.skipped.length,
          }) }}
        </div>
        <p
          v-if="importResult.imported === 0"
          class="prices-import-all-failed"
        >
          {{ $t('billing.prices_import_all_failed_hint') }}
        </p>
        <ul
          v-if="visibleSkips.length"
          class="prices-import-skips"
        >
          <li
            v-for="skip in visibleSkips"
            :key="`${skip.row}-${skip.reason}`"
            class="prices-import-skip"
          >
            {{ $t('billing.prices_import_skipped_row', { row: skip.row, reason: skip.reason }) }}
          </li>
        </ul>
        <p
          v-if="hiddenSkipCount"
          class="prices-import-hint"
        >
          {{ $t('billing.prices_import_skipped_more', { n: hiddenSkipCount }) }}
        </p>
      </div>
    </Modal>
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

/* 导入弹窗 */
.prices-import-hint { margin: 0 0 10px; font-size: 12px; color: var(--text-tertiary); line-height: 1.6; }
.prices-import-label { display: block; margin: 8px 0 4px; font-size: 12px; color: var(--text-secondary); }
.prices-import-textarea { font-family: var(--font-mono, monospace); font-size: 12px; }
.prices-import-file-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 8px; }
.prices-import-file { font-size: 12px; }
.prices-import-filename { font-size: 12px; color: var(--text-secondary); }
.prices-import-result { margin-top: 12px; padding: 10px; border-radius: 6px; background: var(--bg-secondary, rgba(0, 0, 0, 0.03)); }
.prices-import-summary { font-size: 13px; color: var(--text-primary); font-variant-numeric: tabular-nums; }
.prices-import-all-failed { margin: 8px 0 0; font-size: 12px; color: var(--colorWarning, var(--warning)); }
.prices-import-skips { margin: 8px 0 0; padding-inline-start: 18px; max-height: 180px; overflow-y: auto; }
.prices-import-skip { font-size: 12px; color: var(--text-secondary); line-height: 1.6; }

@media (max-width: 576px) {
  .prices-container { padding: 12px; }
  .prices-header h1 { font-size: 20px; }
  .prices-search { max-width: none; width: 100%; }
  .prices-container :deep(.ant-input) { font-size: 16px; }
}
</style>
