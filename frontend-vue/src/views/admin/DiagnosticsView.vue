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

    <!-- P0-1：聚合清单。放在最顶部，因为它是"先看这一屏就知道哪几件事没生效"的那一屏；
         逐项体检（下面的分组）是"具体是哪条配置"的第二层。 -->
    <div class="summary-section">
      <div class="summary-head">
        <h3 class="group-title">
          {{ $t('admin.diag_summary_title') }}
        </h3>
        <span class="summary-desc">{{ $t('admin.diag_summary_desc') }}</span>
      </div>

      <!-- 清单拉取失败单独提示：不能渲染成"没有异常"（那是相反结论）。 -->
      <Alert
        v-if="summaryError"
        class="summary"
        type="error"
        show-icon
        :message="$t('admin.diag_summary_unavailable')"
        :description="summaryError"
      />
      <Spin
        v-else
        :spinning="summaryLoading"
      >
        <div class="summary-grid">
          <div
            v-for="item in summaryChecks"
            :key="item.id"
            class="summary-card"
            :class="`summary-card--${item.status}`"
            data-test="diag-summary-card"
          >
            <div class="summary-card-head">
              <span class="summary-card-title">{{ summaryTitle(item) }}</span>
              <Tag :color="summaryColor(item.status)">
                {{ $t(`admin.diag_sum_status_${item.status}`) }}
              </Tag>
            </div>
            <div class="summary-card-detail">
              {{ item.detail }}
            </div>
            <!-- 修复指引只在"需要处理"时出现：ok 的行不需要噪音。 -->
            <div
              v-if="item.fix"
              class="summary-card-fix"
            >
              <BulbOutlined />
              <span>{{ item.fix }}</span>
            </div>
          </div>
        </div>
        <Empty
          v-if="!summaryLoading && !summaryChecks.length"
          :description="$t('admin.diag_summary_empty')"
        />
      </Spin>
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
  getDiagnosticsSummary,
  getGatewayBreakers,
  resetGatewayBreaker,
  type DiagnosticCheck,
  type DiagnosticStatus,
  type DiagnosticsReport,
  type DiagSummaryCheck,
  type DiagSummaryStatus,
  type GatewayBreaker,
} from '../../api/admin'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const loading = ref(false)
const report = ref<DiagnosticsReport | null>(null)

// ── P0-1：聚合清单 ──
const summaryLoading = ref(false)
// 拉取失败与"清单里有 unknown 项"是两件事：前者是这份清单拿不到（网络/鉴权），
// 后者是拿到了、但某一项看不见。混成一个空数组会让页面显示"一切正常"。
const summaryError = ref('')
const summaryChecks = ref<DiagSummaryCheck[]>([])

const SUMMARY_KEY: Record<string, string> = {
  schema: 'admin.diag_sum_key_schema',
  tables: 'admin.diag_sum_key_tables',
  knowledge: 'admin.diag_sum_key_knowledge',
  checkpoints: 'admin.diag_sum_key_checkpoints',
  deps: 'admin.diag_sum_key_deps',
}

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

// ── 聚合清单的展示辅助 ──

/** 标题优先走 i18n（三语），缺键时回退后端的 title（那是最新事实，不该被旧翻译盖住）。 */
function summaryTitle(item: DiagSummaryCheck): string {
  const key = SUMMARY_KEY[item.id]
  if (!key) return item.title
  const translated = t(key)
  return translated === key ? item.title : translated
}

function summaryColor(s: DiagSummaryStatus): string {
  switch (s) {
    case 'ok': return 'success'
    case 'warn': return 'warning'
    case 'error': return 'error'
    default: return 'default'
  }
}

async function loadSummary() {
  summaryLoading.value = true
  try {
    const res = await getDiagnosticsSummary()
    summaryChecks.value = res.checks ?? []
    summaryError.value = ''
  } catch (e) {
    // 失败必须清空清单：否则上一次的"全绿"会留在屏幕上，比什么都不显示更误导。
    summaryChecks.value = []
    summaryError.value = e instanceof Error ? e.message : String(e)
  } finally {
    summaryLoading.value = false
  }
}

async function load() {
  loading.value = true
  try {
    report.value = await getDiagnostics()
  } finally {
    loading.value = false
  }
  // 三段串行拉取但彼此隔离：清单 / 体检 / 熔断任一失败都不该让另外两块空白。
  await loadSummary()
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
.summary-section { margin-bottom: 18px; }
.summary-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.summary-desc { font-size: 12px; color: var(--text-secondary); }
/* auto-fit + minmax：窄屏自动降成单列，不需要媒体查询；RTL 下 grid 顺序自动镜像。 */
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 8px;
}
.summary-card {
  padding: 10px 12px;
  border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  /* 左侧色条用 border-inline-start：RTL 下自动翻到右侧，不用写 left/right。 */
  border-inline-start-width: 3px;
  border-inline-start-style: solid;
  border-inline-start-color: var(--border);
}
.summary-card--ok { border-inline-start-color: var(--success); }
.summary-card--warn { border-inline-start-color: var(--warning); }
.summary-card--error { border-inline-start-color: var(--error); }
.summary-card--unknown { border-inline-start-color: var(--text-secondary); }
.summary-card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.summary-card-title { font-weight: var(--fw-medium); }
.summary-card-detail { font-size: 12px; color: var(--text-secondary); margin-top: 4px; word-break: break-word; }
.summary-card-fix { display: flex; gap: 6px; font-size: 12px; margin-top: 6px; padding: 6px 8px; border-radius: var(--radius-md); border: 1px dashed var(--border); }
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
