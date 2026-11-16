<template>
  <div class="diagnostics-view">
    <div class="page-header">
      <div>
        <h2 class="page-title page-title-sm">
          {{ $t('admin.diagnostics') }}
        </h2>
        <p class="page-desc">
          {{ $t('admin.diagnostics_desc') }}
        </p>
      </div>
      <Button
        :loading="loading"
        @click="load"
      >
        <template #icon>
          <ReloadOutlined />
        </template>
        {{ $t('common.refresh') }}
      </Button>
    </div>

    <Alert
      v-if="report"
      class="summary"
      :type="summaryAlertType"
      show-icon
      :message="summaryMessage"
    />

    <Spin :spinning="loading">
      <div
        v-for="group in groups"
        :key="group.category"
        class="group"
      >
        <h3 class="group-title">
          {{ group.label }}
        </h3>
        <div
          v-for="c in group.checks"
          :key="c.id"
          class="check-row"
          :class="`check-row--${c.status}`"
        >
          <span class="check-status">{{ statusIcon(c.status) }}</span>
          <div class="check-body">
            <div class="check-head">
              <span class="check-title">{{ c.title }}</span>
              <Tag :color="statusColor(c.status)">
                {{ $t(`admin.diag_status_${c.status}`) }}
              </Tag>
            </div>
            <div class="check-detail">
              {{ c.detail }}
            </div>
            <!-- 补救建议只在"需要处理"时出现：ok 的行不需要噪音（与后端一致：ok 时为空）。 -->
            <div
              v-if="c.remediation"
              class="check-fix"
            >
              <BulbOutlined />
              <span>{{ c.remediation }}</span>
            </div>
          </div>
        </div>
      </div>

      <Empty
        v-if="report && !report.checks.length"
        :description="$t('admin.diagnostics_empty')"
      />

      <!-- LLM 熔断：诊断回答"配了但不生效"，这里回答"生效了但被打断了"。
           熔断是自动恢复的，但恢复窗口由 recovery_timeout 决定；provider 已修好时
           只能干等，所以需要一个手动入口。 -->
      <div class="group">
        <h3 class="group-title">
          {{ $t('admin.breaker_title') }}
        </h3>
        <p class="group-desc">
          {{ $t('admin.breaker_desc') }}
        </p>

        <Alert
          v-if="breakersUnavailable"
          type="warning"
          show-icon
          :message="$t('admin.breaker_unavailable')"
        />
        <Empty
          v-else-if="!breakers.length"
          :description="$t('admin.breaker_empty')"
        />
        <template v-else>
          <div class="breaker-row breaker-row--head">
            <span class="breaker-provider">{{ $t('admin.breaker_col_provider') }}</span>
            <span class="breaker-state">{{ $t('admin.breaker_col_chat') }}</span>
            <span class="breaker-state">{{ $t('admin.breaker_col_embed') }}</span>
            <span class="breaker-latency">{{ $t('admin.breaker_col_latency') }}</span>
            <span class="breaker-action" />
          </div>
          <div
            v-for="b in breakers"
            :key="b.provider"
            class="breaker-row"
          >
            <span class="breaker-provider">{{ b.provider }}</span>
            <span class="breaker-state">
              <Tag :color="breakerColor(b.chat)">
                {{ breakerLabel(b.chat) }}
              </Tag>
            </span>
            <span class="breaker-state">
              <Tag :color="breakerColor(b.embed)">
                {{ breakerLabel(b.embed) }}
              </Tag>
            </span>
            <span class="breaker-latency">{{ formatLatency(b.avg_latency_ms) }}</span>
            <span class="breaker-action">
              <Popconfirm
                :title="t('admin.breaker_reset_confirm', { provider: b.provider })"
                :description="t('admin.breaker_reset_confirm_hint')"
                :ok-text="t('common.confirm')"
                :cancel-text="t('common.cancel')"
                @confirm="resetBreaker(b.provider)"
              >
                <Button
                  size="small"
                  :loading="resetting === b.provider"
                >
                  {{ $t('admin.breaker_reset') }}
                </Button>
              </Popconfirm>
            </span>
          </div>
        </template>
      </div>
    </Spin>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Alert, Button, Empty, Popconfirm, Spin, Tag, message } from 'ant-design-vue'
import { BulbOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import {
  getDiagnostics,
  getGatewayBreakers,
  resetGatewayBreaker,
  type DiagnosticCheck,
  type DiagnosticStatus,
  type DiagnosticsReport,
  type GatewayBreaker,
} from '../../api/admin'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const loading = ref(false)
const report = ref<DiagnosticsReport | null>(null)

const breakers = ref<GatewayBreaker[]>([])
// 与"没有熔断"区分开：读不到时不能渲染成空列表 —— 排障页上"没有异常"和
// "看不见"的处置正好相反，后者会让人误以为链路健康。
const breakersUnavailable = ref(false)
const resetting = ref('')

const CATEGORY_LABEL: Record<string, string> = {
  dependency: 'admin.diag_category_dependency',
  config: 'admin.diag_category_config',
  capability: 'admin.diag_category_capability',
}

// 后端已按"依赖 → 配置 → 能力"排好序；这里只做分组，**不重排** ——
// 顺序本身是"先看链路通不通、再看配置对不对、最后看功能是否可用"的排障次序。
const groups = computed(() => {
  const out: { category: string; label: string; checks: DiagnosticCheck[] }[] = []
  for (const c of report.value?.checks ?? []) {
    let g = out.find(x => x.category === c.category)
    if (!g) {
      g = { category: c.category, label: t(CATEGORY_LABEL[c.category] || 'admin.diag_category_other'), checks: [] }
      out.push(g)
    }
    g.checks.push(c)
  }
  return out
})

const summaryAlertType = computed(() => {
  switch (report.value?.status) {
    case 'fail': return 'error' as const
    case 'warn': return 'warning' as const
    case 'skipped': return 'info' as const
    default: return 'success' as const
  }
})

const summaryMessage = computed(() => {
  const r = report.value
  if (!r) return ''
  const c = r.counts || {}
  return t('admin.diag_summary', {
    status: t(`admin.diag_status_${r.status}`),
    ok: c.ok ?? 0,
    warn: c.warn ?? 0,
    fail: c.fail ?? 0,
    skipped: c.skipped ?? 0,
  })
})

function statusIcon(s: DiagnosticStatus): string {
  switch (s) {
    case 'ok': return '✓'
    case 'warn': return '!'
    case 'fail': return '✕'
    default: return '–'
  }
}

function statusColor(s: DiagnosticStatus): string {
  switch (s) {
    case 'ok': return 'success'
    case 'warn': return 'warning'
    case 'fail': return 'error'
    default: return 'default'
  }
}

async function load() {
  loading.value = true
  try {
    report.value = await getDiagnostics()
  } finally {
    loading.value = false
  }
  await loadBreakers()
}

/**
 * 熔断读数单独拉取：诊断报告与它来自不同端点，一个挂了不该让另一个空白。
 * 失败时置 breakersUnavailable，而不是留一个空数组冒充"没有熔断"。
 */
async function loadBreakers() {
  try {
    breakers.value = await getGatewayBreakers()
    breakersUnavailable.value = false
  } catch {
    breakers.value = []
    breakersUnavailable.value = true
  }
}

function breakerLabel(state: string): string {
  // 空串是"引擎没报这张表"，不是"正常" —— 两者在排障时结论相反，必须分开显示。
  if (!state) return t('admin.breaker_state_unknown')
  return t(`admin.breaker_state_${state}`)
}

function breakerColor(state: string): string {
  switch (state) {
    case 'closed': return 'success'
    case 'half_open': return 'warning'
    case 'open': return 'error'
    default: return 'default'
  }
}

function formatLatency(ms: number): string {
  // 未采样时引擎给 0：显示 0ms 会被读成"极快"，实际是"没有样本"。
  if (!Number.isFinite(ms) || ms <= 0) return t('admin.breaker_no_avg')
  return `${ms.toFixed(1)} ms`
}

async function resetBreaker(provider: string) {
  resetting.value = provider
  try {
    await resetGatewayBreaker(provider)
    message.success(t('admin.breaker_reset_done', { provider }))
    // 重新拉读数而不是就地改本地状态：手册上"已解除"不代表引擎真的清了
    // （provider 可能已从注册表移除），以引擎回报为准。
    await loadBreakers()
  } catch {
    message.error(t('admin.breaker_reset_failed'))
  } finally {
    resetting.value = ''
  }
}

onMounted(load)
</script>

<style scoped>
.diagnostics-view { padding: 4px; }
.page-desc { margin: 4px 0 0; font-size: 12px; color: var(--text-secondary); }
.summary { margin-bottom: 16px; }
.group { margin-bottom: 18px; }
.group-title { font-size: 13px; font-weight: var(--fw-semibold); margin: 0 0 8px; color: var(--text-secondary); }
.check-row { display: flex; gap: 10px; padding: 10px 12px; border: var(--hairline-width) solid var(--border); border-radius: var(--radius-lg); margin-bottom: 8px; background: var(--bg-card); }
.check-status { flex: 0 0 auto; width: 18px; text-align: center; font-weight: var(--fw-bold); line-height: 20px; }
.check-row--ok .check-status { color: var(--success); }
.check-row--warn .check-status { color: var(--warning); }
.check-row--fail .check-status { color: var(--error); }
.check-body { flex: 1 1 auto; min-width: 0; }
.check-head { display: flex; align-items: center; gap: 8px; }
.check-title { font-weight: var(--fw-medium); }
.check-detail { font-size: 12px; color: var(--text-secondary); margin-top: 2px; word-break: break-word; }
.check-fix { display: flex; gap: 6px; font-size: 12px; margin-top: 6px; padding: 6px 8px; border-radius: var(--radius-md); background: var(--bg-subtle, transparent); border: 1px dashed var(--border); }
.group-desc { margin: 0 0 8px; font-size: 12px; color: var(--text-secondary); }
/* 五行定宽用 grid：各行的状态 Tag 才能上下对齐（flex + 不等宽内容会错位）。
   `text-align: end` 等逻辑值在 RTL 下自动翻转，不用写 right/left。 */
.breaker-row {
  display: grid;
  grid-template-columns: minmax(120px, 1fr) 96px 96px 96px minmax(112px, auto);
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-lg);
  margin-bottom: 8px;
  background: var(--bg-card);
}
.breaker-row--head {
  border-color: transparent;
  background: transparent;
  margin-bottom: 4px;
  font-size: 12px;
  color: var(--text-secondary);
}
.breaker-provider { font-weight: var(--fw-medium); word-break: break-all; }
.breaker-latency { font-size: 12px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.breaker-action { text-align: end; }
</style>
