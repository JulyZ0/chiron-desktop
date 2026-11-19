<script setup lang="ts">
/**
 * 后台「分享管理」。
 *
 * 与用户态 `/v1/shares` 的区别：那个按 `user_id` 过滤（"我分享出去的"），
 * 这个按租户列出**所有用户**的有效分享 —— 运营处置场景（会话里带了不该公开的
 * 内容、被举报、员工离职）需要看见并直接掐断，而不必先知道是哪个会话。
 *
 * 只列有效分享：已撤销 / 已过期的链接点开就是 410，停在列表里没有处置价值。
 */
import { computed, onMounted, ref } from 'vue'
import { Button, Card, Empty, Input, Table, Tag, message } from 'ant-design-vue'
import type { TableColumnsType } from 'ant-design-vue'
import { ReloadOutlined, StopOutlined, LinkOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import { listAdminShares, revokeAdminShare, type AdminShareItem } from '@/api/admin'
import { serverErrorMessage } from '../../utils/apiError'
import { copyTextOrThrow } from '../../utils/clipboard'

const { t } = useI18n()

const loading = ref(false)
const revokingId = ref('')
const items = ref<AdminShareItem[]>([])
const keyword = ref('')

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return items.value
  return items.value.filter(it =>
    [it.title, it.user_email, it.user_id, it.session_id, it.share_id]
      .some(field => (field ?? '').toLowerCase().includes(kw)))
})

function shareUrl(id: string): string {
  return `${window.location.origin}/share/${id}`
}

function formatTime(value: string): string {
  if (!value) return '—'
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString()
}

const columns: TableColumnsType = [
  { title: t('admin.share_col_title'), dataIndex: 'title', key: 'title', ellipsis: true },
  { title: t('admin.share_col_owner'), dataIndex: 'user_email', key: 'user_email', width: 220, ellipsis: true },
  { title: t('admin.share_col_messages'), dataIndex: 'message_count', key: 'message_count', width: 110 },
  { title: t('admin.share_col_created'), dataIndex: 'created_at', key: 'created_at', width: 190 },
  { title: t('admin.share_col_expires'), dataIndex: 'expires_at', key: 'expires_at', width: 190 },
  { title: t('admin.share_col_actions'), key: 'actions', width: 200, fixed: 'right' },
]

async function load() {
  loading.value = true
  try {
    items.value = await listAdminShares()
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.share_load_failed')))
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
    await copyTextOrThrow(shareUrl(item.share_id))
    message.success(t('common.share_link_copied'))
  } catch {
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
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.share_revoke_failed')))
  } finally {
    revokingId.value = ''
  }
}

onMounted(load)
</script>

<template>
  <div class="share-admin">
    <Card :bordered="false" class="share-card">
      <div class="share-toolbar">
        <div>
          <h2 class="share-title">{{ $t('admin.share_management') }}</h2>
          <p class="share-desc">{{ $t('admin.share_management_desc') }}</p>
        </div>
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
      </div>

      <Table
        :columns="columns"
        :data-source="filtered"
        :loading="loading"
        :pagination="{ pageSize: 20, showSizeChanger: false }"
        row-key="share_id"
        size="middle"
        :scroll="{ x: 1100 }"
      >
        <template #bodyCell="{ column, record }">
          <template v-if="column.key === 'title'">
            <span class="share-name">{{ record.title || $t('admin.share_untitled') }}</span>
            <Tag class="share-tag">{{ record.share_id }}</Tag>
          </template>
          <template v-else-if="column.key === 'user_email'">
            {{ record.user_email || record.user_id || '—' }}
          </template>
          <template v-else-if="column.key === 'created_at'">
            {{ formatTime(record.created_at) }}
          </template>
          <template v-else-if="column.key === 'expires_at'">
            {{ formatTime(record.expires_at) }}
          </template>
          <template v-else-if="column.key === 'actions'">
            <div class="share-actions">
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
          <Empty :description="$t('admin.share_empty')" />
        </template>
      </Table>
    </Card>
  </div>
</template>

<style scoped>
.share-admin { width: 100%; }
.share-card { border-radius: var(--radius-lg); }
.share-toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.share-title { margin: 0; font-size: var(--fs-lg); font-weight: var(--fw-semibold); color: var(--text-primary); }
.share-desc { margin: 4px 0 0; font-size: var(--fs-sm); color: var(--text-secondary); }
.share-tools { display: flex; align-items: center; gap: 8px; }
.share-search { width: 240px; }
.share-name { color: var(--text-primary); margin-inline-end: 8px; }
.share-tag {
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  color: var(--text-tertiary);
}
.share-actions { display: flex; gap: 4px; }
</style>
