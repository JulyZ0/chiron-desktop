<script setup lang="ts">
/**
 * 操作审计：按租户分页查询 + 过滤 + CSV 导出。
 *
 * 数据来自 `GET /v1/admin/audit-logs`（权限 `audit:read`）。审计是**事后追查**的
 * 最后一手，所以这里只读：不做删除/改写入口，也不在页面里加载详情全文以外的资源。
 */
import { computed, onMounted, ref } from 'vue'
import { Button, Input, message } from 'ant-design-vue'
import AdminPage from '@/components/admin/AdminPage.vue'
import AdminTable from '@/components/admin/AdminTable.vue'
import { useI18n } from 'vue-i18n'
import {
  downloadAuditLogs,
  listAuditLogs,
  type AuditLogQuery,
  type AuditLogRow,
} from '../../api/admin'

const { t } = useI18n()

const rows = ref<AuditLogRow[]>([])
const total = ref(0)
const page = ref(1)
const perPage = ref(20)
const loading = ref(false)
const exporting = ref(false)

const action = ref('')
const userId = ref('')
const resourceType = ref('')
const from = ref('')
const to = ref('')

const columns = computed(() => [
  { title: t('common.time'), key: 'created_at', width: 180 },
  { title: t('admin.audit_filter_user'), key: 'user_id', ellipsis: true },
  { title: t('common.action'), key: 'action', ellipsis: true },
  { title: t('admin.audit_col_resource'), key: 'resource', ellipsis: true },
  { title: t('admin.audit_col_ip'), key: 'ip_address', width: 140 },
  { title: t('common.details'), key: 'details', ellipsis: true },
])

function query(): AuditLogQuery {
  return {
    page: page.value,
    per_page: perPage.value,
    action: action.value.trim() || undefined,
    user_id: userId.value.trim() || undefined,
    resource_type: resourceType.value.trim() || undefined,
    from: from.value || undefined,
    to: to.value || undefined,
  }
}

async function load() {
  loading.value = true
  try {
    const data = await listAuditLogs(query())
    rows.value = data.items || []
    total.value = data.total || 0
  } catch {
    rows.value = []
    total.value = 0
    message.error(t('errors.failed_to_load'))
  } finally {
    loading.value = false
  }
}

function search() {
  page.value = 1
  void load()
}

function reset() {
  action.value = ''
  userId.value = ''
  resourceType.value = ''
  from.value = ''
  to.value = ''
  page.value = 1
  void load()
}

function handleTableChange(pagination: { current?: number; pageSize?: number }) {
  page.value = pagination.current || 1
  perPage.value = pagination.pageSize || 20
  void load()
}

async function exportCsv() {
  if (exporting.value) return
  exporting.value = true
  try {
    // 导出使用当前过滤器，但不带分页：要的是「这一批记录」而不是「这一页」。
    const { page: _page, per_page: _perPage, ...filters } = query()
    void _page
    void _perPage
    await downloadAuditLogs(filters)
  } catch {
    message.error(t('admin.audit_export_failed'))
  } finally {
    exporting.value = false
  }
}

function formatTime(value?: string): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString()
}

function formatDetails(value: unknown): string {
  if (value == null) return ''
  try {
    return typeof value === 'string' ? value : JSON.stringify(value)
  } catch {
    return String(value)
  }
}

onMounted(load)
</script>

<template>
  <AdminPage :title="$t('admin.operation_audit')">
    <div class="audit-toolbar">
      <Input
        v-model:value="action"
        :placeholder="$t('admin.audit_filter_action')"
        allow-clear
        class="audit-field"
        @press-enter="search"
      />
      <Input
        v-model:value="userId"
        :placeholder="$t('admin.audit_filter_user')"
        allow-clear
        class="audit-field"
        @press-enter="search"
      />
      <Input
        v-model:value="resourceType"
        :placeholder="$t('admin.audit_filter_resource')"
        allow-clear
        class="audit-field"
        @press-enter="search"
      />
      <input
        v-model="from"
        type="date"
        class="audit-date"
        :aria-label="$t('admin.audit_filter_from')"
      >
      <input
        v-model="to"
        type="date"
        class="audit-date"
        :aria-label="$t('admin.audit_filter_to')"
      >
      <Button type="primary" @click="search">
        {{ $t('common.search') }}
      </Button>
      <Button @click="reset">
        {{ $t('admin.audit_filter_reset') }}
      </Button>
      <Button :loading="exporting" @click="exportCsv">
        {{ $t('admin.audit_export') }}
      </Button>
    </div>

    <AdminTable
      row-key="id"
      :columns="columns"
      :data-source="rows"
      :loading="loading"
      :scroll="{ x: 900 }"
      :pagination="{
        current: page,
        pageSize: perPage,
        total,
        showSizeChanger: false,
        showTotal: (n: number) => `${n}`,
      }"
      @change="handleTableChange"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'created_at'">
          {{ formatTime(record.created_at) }}
        </template>
        <template v-else-if="column.key === 'resource'">
          {{ record.resource_type || '—' }}<span v-if="record.resource_id"> / {{ record.resource_id }}</span>
        </template>
        <template v-else-if="column.key === 'details'">
          <span class="audit-details" :title="formatDetails(record.details)">
            {{ formatDetails(record.details) || '—' }}
          </span>
        </template>
        <template v-else-if="column.key === 'user_id'">
          {{ record.user_id || '—' }}
        </template>
        <template v-else-if="column.key === 'action'">
          {{ record.action || '—' }}
        </template>
        <template v-else-if="column.key === 'ip_address'">
          {{ record.ip_address || '—' }}
        </template>
      </template>
      <template #emptyText>
        {{ $t('admin.audit_empty') }}
      </template>
    </AdminTable>
  </AdminPage>
</template>

<style scoped>
.audit-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.audit-field { width: 200px; }
.audit-date {
  height: 32px;
  padding-inline: 10px;
  font-family: inherit;
  font-size: 13px;
  color: var(--text-primary);
  background: var(--bg-card);
  border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-md);
}
.audit-details {
  display: inline-block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-secondary);
}
</style>
