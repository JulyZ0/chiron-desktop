<script setup lang="ts">
/**
 * 后台「分享管理」。
 *
 * 与用户态 `/v1/shares` 的区别：那个按 `user_id` 过滤（"我分享出去的"），
 * 这个按租户列出**所有用户**的有效分享 —— 运营处置场景（会话里带了不该公开的
 * 内容、被举报、员工离职）需要看见并直接掐断，而不必先知道是哪个会话。
 *
 * 只列有效分享：已撤销 / 已过期的链接点开就是 410，停在列表里没有处置价值。
 *
 * 本页新增两件事（P1-8 分享运营）：
 *  1. **访问统计**：24h / 累计 / 最近访问 + 详情面板里的 7 天迷你趋势图。
 *     趋势图用内联 SVG polyline 画，不引图表库 —— 为一张 40px 高的折线引入
 *     一个几百 KB 的依赖不划算，且它的坐标全是纯计算，没有交互需求。
 *  2. **批量撤销**：多选 + 就地二次确认（不是一行一个的 Modal 轰炸）。撤销一批
 *     链接是一次处置动作，服务端也只记一条汇总审计。
 */
import { computed, onMounted, ref } from 'vue'
import { Button, Checkbox, Empty, Input, Tag, message } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import { ReloadOutlined, StopOutlined, LinkOutlined, EyeOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import {
  listAdminShares,
  revokeAdminShare,
  revokeAdminShares,
  type AdminShareItem,
} from '@/api/admin'
import AdminPage from '@/components/admin/AdminPage.vue'
import AdminTable from '@/components/admin/AdminTable.vue'
import AdminErrorState from '@/components/admin/AdminErrorState.vue'
import { serverErrorMessage } from '../../utils/apiError'
import { copyTextOrThrow } from '../../utils/clipboard'
import { formatDateTime } from '../../utils/datetime'
import { resolveShareUrl, ShareLinkUnavailableError } from '../../utils/shareLink'

const { t } = useI18n()

const loading = ref(false)
const loadError = ref('')
const revokingId = ref('')
const items = ref<AdminShareItem[]>([])
const keyword = ref('')

/** 批量撤销：选中项 + 二次确认 + 执行中。 */
const selectedKeys = ref<string[]>([])
const bulkConfirming = ref(false)
const bulkRunning = ref(false)

/** 详情面板（侧滑）。null = 关闭。 */
const detail = ref<AdminShareItem | null>(null)

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return items.value
  return items.value.filter(it =>
    [it.title, it.user_email, it.user_id, it.session_id, it.share_id]
      .some(field => (field ?? '').toLowerCase().includes(kw)))
})

const emptyText = computed(() =>
  filtered.value.length === 0 && keyword.value.trim()
    ? t('admin.share_no_results')
    : t('admin.share_empty'))

/**
 * 选中项里**仍然存在**的那部分。
 *
 * 列表会因刷新/撤销而变短，antd 的 rowSelection 不会自动裁掉已消失的 key ——
 * 不裁剪的话"已选 5 项"里可能混着 3 条已经不存在的分享，批量撤销会白白失败。
 */
const selectedVisible = computed(() => {
  const alive = new Set(items.value.map(it => it.share_id))
  return selectedKeys.value.filter(id => alive.has(id))
})

const rowSelection = computed(() => ({
  selectedRowKeys: selectedVisible.value,
  onChange: (keys: (string | number)[]) => {
    selectedKeys.value = keys.map(String)
  },
}))

function shareUrl(item: AdminShareItem): string {
  // 服务端的 url 是权威值（桌面端 file:// 下 origin 不可用）；拿不到时按 origin 兜底。
  return resolveShareUrl(item.url, window.location.origin, item.share_id)
}

/** 时间列走统一入口：`toLocaleString()` 跟的是操作系统 locale，不是界面语言。 */
function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  return formatDateTime(value, '—')
}

/**
 * 迷你趋势图的 polyline 坐标。
 *
 * 固定 viewBox（不按容器宽度重算）：面板宽度会变，按像素画反而会让折线在小屏上
 * 失真；这里用 160×36 的逻辑坐标 + `preserveAspectRatio="none"` 拉伸。
 * 全零时给一条贴底的直线（而不是空白）——"这 7 天没人看"本身是信息。
 */
function sparkPoints(values: number[], width = 160, height = 36, pad = 3): string {
  if (!values.length) return ''
  const max = Math.max(1, ...values)
  const step = values.length > 1 ? (width - pad * 2) / (values.length - 1) : 0
  return values
    .map((v, i) => {
      const x = pad + step * i
      const y = height - pad - ((height - pad * 2) * v) / max
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}

const detailDaily = computed(() => detail.value?.daily ?? [])

// 必须 computed：`t()` 在 setup 顶层只求值一次，切语言后表头会停在旧语言。
const columns = computed<TableColumnsType>(() => [
  { title: t('admin.share_col_title'), dataIndex: 'title', key: 'title', ellipsis: true },
  { title: t('admin.share_col_owner'), dataIndex: 'user_email', key: 'user_email', width: 200, ellipsis: true },
  { title: t('admin.share_col_messages'), dataIndex: 'message_count', key: 'message_count', width: 90 },
  { title: t('admin.share_col_views_24h'), dataIndex: 'views_24h', key: 'views_24h', width: 110 },
  { title: t('admin.share_col_views_total'), dataIndex: 'views_total', key: 'views_total', width: 100 },
  { title: t('admin.share_col_last_viewed'), dataIndex: 'last_viewed_at', key: 'last_viewed_at', width: 180 },
  { title: t('admin.share_col_created'), dataIndex: 'created_at', key: 'created_at', width: 180 },
  { title: t('admin.share_col_expires'), dataIndex: 'expires_at', key: 'expires_at', width: 180 },
  { title: t('admin.share_col_actions'), key: 'actions', width: 240, fixed: 'right' },
])

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    items.value = await listAdminShares()
    // 选中集合以列表为准：刷新后不存在的 id 不该继续占着"已选 N 项"
    selectedKeys.value = selectedVisible.value
  } catch (err) {
    loadError.value = serverErrorMessage(err, t('admin.share_load_failed'))
    message.error(loadError.value)
  } finally {
    loading.value = false
  }
}

/**
 * Table 的 `#bodyCell` 把每行交成 `Record<string, any>`，不是我们的行类型。
 * 这里收敛成最小断言，免得在两个 handler 里各写一遍 as。
 */
function asShare(record: Record<string, unknown>): AdminShareItem {
  return record as unknown as AdminShareItem
}

async function copyLink(record: Record<string, unknown>) {
  const item = asShare(record)
  try {
    await copyTextOrThrow(shareUrl(item))
    message.success(t('common.share_link_copied'))
  } catch (e) {
    if (e instanceof ShareLinkUnavailableError) {
      message.error(t('errors.share_link_unavailable_please_configure_public_base_url'))
      return
    }
    message.warning(t('errors.copy_failed_please_copy_manually'))
  }
}

async function revoke(record: Record<string, unknown>) {
  const item = asShare(record)
  revokingId.value = item.share_id
  try {
    await revokeAdminShare(item.share_id)
    message.success(t('common.sharing_canceled'))
    // 本地摘掉即可：重新拉全表会让"刚撤掉的那条"有一瞬间还在，
    // 也白白多一次跨表查询。
    items.value = items.value.filter(it => it.share_id !== item.share_id)
    selectedKeys.value = selectedVisible.value
    if (detail.value?.share_id === item.share_id) detail.value = null
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.share_revoke_failed')))
  } finally {
    revokingId.value = ''
  }
}

function openDetail(record: Record<string, unknown>) {
  detail.value = asShare(record)
}

function closeDetail() {
  detail.value = null
}

/** 第一步：打开就地确认（不是立刻发请求）。 */
function askBulkRevoke() {
  if (!selectedVisible.value.length || bulkRunning.value) return
  bulkConfirming.value = true
}

function cancelBulkRevoke() {
  bulkConfirming.value = false
}

/**
 * 第二步：确认后执行。
 *
 * 逐条的结果都在响应里：`revoked_ids` 从列表摘掉，`failed` 只提示数量 —— 失败的
 * 原因（不存在 / 不属于本租户 / 已被撤销）对运营是同一种处置结论，无需逐个展示。
 */
async function confirmBulkRevoke() {
  const ids = [...selectedVisible.value]
  if (!ids.length || bulkRunning.value) {
    bulkConfirming.value = false
    return
  }
  bulkRunning.value = true
  try {
    const res = await revokeAdminShares(ids)
    const revoked = new Set(res.revoked_ids ?? [])
    items.value = items.value.filter(it => !revoked.has(it.share_id))
    selectedKeys.value = []
    bulkConfirming.value = false
    if (res.failed?.length) {
      // 部分失败不是"操作失败"：已经撤掉的那些照样生效，只是有 n 条没撤成
      message.warning(t('admin.share_bulk_partial', { n: res.failed.length }))
    } else {
      message.success(t('admin.share_bulk_done', { n: res.revoked }))
    }
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.share_bulk_failed')))
  } finally {
    bulkRunning.value = false
  }
}

onMounted(load)
</script>

<template>
  <AdminPage
    :title="$t('admin.share_management')"
    :description="$t('admin.share_management_desc')"
  >
    <template #actions>
      <div class="share-tools">
        <Input
          v-model:value="keyword"
          class="share-search"
          allow-clear
          :placeholder="$t('admin.share_search_placeholder')"
        />
        <Button :loading="loading" @click="load">
          <template #icon><ReloadOutlined /></template>
          {{ $t('common.refresh') }}
        </Button>
      </div>
    </template>

      <!-- 批量操作条：只在有选中时出现，避免常驻一个禁用的按钮干扰视线 -->
      <div
        v-if="selectedVisible.length"
        class="share-bulk"
      >
        <span class="share-bulk-count">{{ $t('admin.share_bulk_selected', { n: selectedVisible.length }) }}</span>
        <template v-if="!bulkConfirming">
          <Button
            danger
            size="small"
            class="share-bulk-ask"
            @click="askBulkRevoke"
          >
            <template #icon><StopOutlined /></template>
            {{ $t('admin.share_bulk_revoke') }}
          </Button>
        </template>
        <!-- 二次确认就地展开：撤销会让已发出的公开链接立刻 410，一步误点代价太高 -->
        <template v-else>
          <span class="share-bulk-confirm-text">
            {{ $t('admin.share_bulk_confirm_text', { n: selectedVisible.length }) }}
          </span>
          <Button
            danger
            size="small"
            class="share-bulk-confirm"
            :loading="bulkRunning"
            @click="confirmBulkRevoke"
          >
            {{ $t('admin.share_bulk_confirm_ok') }}
          </Button>
          <Button
            size="small"
            class="share-bulk-cancel"
            :disabled="bulkRunning"
            @click="cancelBulkRevoke"
          >
            {{ $t('admin.share_bulk_cancel') }}
          </Button>
        </template>
      </div>

      <AdminErrorState
        v-if="loadError"
        :message="loadError"
        :retrying="loading"
        @retry="load"
      />

      <AdminTable
        v-else
        :columns="columns"
        :data-source="filtered"
        :loading="loading"
        :pagination="{ pageSize: 20, showSizeChanger: false }"
        :row-selection="rowSelection"
        row-key="share_id"
        :scroll="{ x: 1500 }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'title'">
            <span class="share-name">{{ record.title || $t('admin.share_untitled') }}</span>
            <Tag class="share-tag">{{ record.share_id }}</Tag>
          </template>
          <template v-else-if="column.key === 'user_email'">
            {{ record.user_email || record.user_id || '—' }}
          </template>
          <template v-else-if="column.key === 'views_24h'">
            <span :class="{ 'share-views-hot': (record.views_24h ?? 0) > 0 }">
              {{ record.views_24h ?? 0 }}
            </span>
          </template>
          <template v-else-if="column.key === 'views_total'">
            {{ record.views_total ?? 0 }}
          </template>
          <template v-else-if="column.key === 'last_viewed_at'">
            {{ formatTime(record.last_viewed_at) }}
          </template>
          <template v-else-if="column.key === 'created_at'">
            {{ formatTime(record.created_at) }}
          </template>
          <template v-else-if="column.key === 'expires_at'">
            {{ formatTime(record.expires_at) }}
          </template>
          <template v-else-if="column.key === 'actions'">
            <div class="share-actions">
              <Button type="link" size="small" class="share-detail-btn" @click="openDetail(record)">
                <template #icon><EyeOutlined /></template>
                {{ $t('admin.share_detail') }}
              </Button>
              <Button type="link" size="small" @click="copyLink(record)">
                <template #icon><LinkOutlined /></template>
                {{ $t('common.copy_link') }}
              </Button>
              <Button
                type="link"
                size="small"
                danger
                :loading="revokingId === record.share_id"
                @click="revoke(record)"
              >
                <template #icon><StopOutlined /></template>
                {{ $t('admin.share_revoke') }}
              </Button>
            </div>
          </template>
        </template>
        <template #emptyText>
          <Empty :description="emptyText" />
        </template>
      </AdminTable>

    <!-- 详情：自绘侧滑面板（不用 antd Drawer 是为了单测能直接找到节点，也少一层 teleport） -->
    <div
      v-if="detail"
      class="share-detail-mask"
      @click.self="closeDetail"
    >
      <aside
        class="share-detail"
        role="dialog"
        aria-modal="true"
      >
        <header class="share-detail-head">
          <h3 class="share-detail-title">
            {{ $t('admin.share_detail_title') }}
          </h3>
          <Button size="small" class="share-detail-close" @click="closeDetail">
            {{ $t('admin.share_detail_close') }}
          </Button>
        </header>

        <dl class="share-detail-list">
          <dt>{{ $t('admin.share_col_title') }}</dt>
          <dd>{{ detail.title || $t('admin.share_untitled') }}</dd>
          <dt>{{ $t('admin.share_col_owner') }}</dt>
          <dd>{{ detail.user_email || detail.user_id || '—' }}</dd>
          <dt>{{ $t('admin.share_col_views_24h') }}</dt>
          <dd>{{ detail.views_24h ?? 0 }}</dd>
          <dt>{{ $t('admin.share_col_views_total') }}</dt>
          <dd>{{ detail.views_total ?? 0 }}</dd>
          <dt>{{ $t('admin.share_col_last_viewed') }}</dt>
          <dd>{{ detail.last_viewed_at ? formatTime(detail.last_viewed_at) : $t('admin.share_never_viewed') }}</dd>
        </dl>

        <section class="share-trend">
          <div class="share-trend-head">{{ $t('admin.share_trend_7d') }}</div>
          <svg
            class="share-spark"
            viewBox="0 0 160 36"
            preserveAspectRatio="none"
            role="img"
            :aria-label="$t('admin.share_trend_7d')"
          >
            <polyline
              class="share-spark-line"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linejoin="round"
              stroke-linecap="round"
              :points="sparkPoints(detailDaily.map(d => d.count))"
            />
          </svg>
          <div class="share-trend-days">
            <span
              v-for="d in detailDaily"
              :key="d.date"
              class="share-trend-day"
              :title="`${d.date}: ${d.count}`"
            >{{ d.count }}</span>
          </div>
        </section>
      </aside>
    </div>
  </AdminPage>
</template>

<style scoped>
.share-tools { display: flex; align-items: center; gap: 8px; }
.share-search { width: 240px; }
.share-name { color: var(--text-primary); margin-inline-end: 8px; }
.share-tag {
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
}
.share-actions { display: flex; gap: 4px; }
.share-views-hot { color: var(--primary); font-weight: var(--fw-semibold); }

.share-bulk {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  padding: 8px 12px;
  background: var(--bg-secondary);
  border-radius: var(--radius-md);
  flex-wrap: wrap;
}
.share-bulk-count { font-size: var(--fs-sm); color: var(--text-secondary); }
.share-bulk-confirm-text { font-size: var(--fs-sm); color: var(--text-primary); }

.share-detail-mask {
  position: fixed;
  inset: 0;
  background: var(--bg-overlay);
  display: flex;
  justify-content: flex-end;
  z-index: var(--z-drawer);
}
.share-detail {
  width: min(360px, 92vw);
  height: 100%;
  overflow-y: auto;
  padding: 16px 20px;
  background: var(--bg-card);
  border-inline-start: var(--hairline-width) solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.share-detail-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.share-detail-title { margin: 0; font-size: var(--fs-md); font-weight: var(--fw-semibold); color: var(--text-primary); }
.share-detail-list { display: grid; grid-template-columns: auto 1fr; gap: 6px 12px; margin: 0; }
.share-detail-list dt { font-size: var(--fs-sm); color: var(--text-tertiary); }
.share-detail-list dd { margin: 0; font-size: var(--fs-sm); color: var(--text-primary); word-break: break-word; }
.share-trend { display: flex; flex-direction: column; gap: 6px; }
.share-trend-head { font-size: var(--fs-sm); color: var(--text-secondary); }
.share-spark { width: 100%; height: 48px; color: var(--primary); }
.share-trend-days { display: flex; justify-content: space-between; font-size: var(--fs-xs); color: var(--text-muted); }
.share-trend-day { min-width: 12px; text-align: center; }
</style>
