<script setup lang="ts">
/**
 * schema 漂移页（`/schema-mismatch`）。
 *
 * ## 它替代的是什么
 *
 * 迁移没跑时，旧行为是网关于启动期 `FATAL` 退出：端口上没有监听者 → Electron 壳的
 * `/health` 探测失败 → 用户看到白屏/转圈，日志里那句 "expected X actual Y" 永远到不了
 * 用户面前。现在网关漂移时仍起一个最小服务（`/healthz` 200 + 业务路径 503），这个页面
 * 就是"把那段日志翻译成一页可操作的东西"：
 *
 *   - 期望/实际的迁移版本（用户能判断是"没升级"还是"超前"）；
 *   - 能照抄的修复命令 + 一键复制；
 *   - 重试：迁移跑完后点一下，页面自己探一次——恢复就回首页，仍是漂移就原地更新。
 *
 * ## 数据来源
 *
 * 首屏数据来自**查询参数**（路由跳转器带上，见 src/router/schemaMismatch.ts），
 * 因此不依赖任何业务接口——业务接口此刻全是 503。页面挂载后再探一次 `/healthz` 校正
 * （用户可能是直接输入地址进来的，没有查询参数）。
 */
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Button, message } from 'ant-design-vue'
import { DEFAULT_SCHEMA_FIX, probeSchemaMismatch, type SchemaMismatchInfo } from '../api/schemaMismatch'
import { copyText } from '../utils/clipboard'
import { t } from '../i18n'

const route = useRoute()
const router = useRouter()

/** 从查询参数恢复首屏状态：没有参数时给空串，界面据空值隐藏对应行。 */
function fromQuery(): SchemaMismatchInfo {
  const raw = route.query
  const expected = typeof raw.expected === 'string' ? raw.expected : ''
  const actual = typeof raw.actual === 'string' ? raw.actual : ''
  const reason = typeof raw.reason === 'string' && raw.reason ? raw.reason : undefined
  const missingRaw = typeof raw.missing === 'string' && raw.missing ? raw.missing.split(',') : []
  const fix = typeof raw.fix === 'string' && raw.fix ? raw.fix : DEFAULT_SCHEMA_FIX
  return {
    expected,
    actual,
    reason,
    missing: missingRaw.length > 0 ? missingRaw : undefined,
    fix,
  }
}

const info = ref<SchemaMismatchInfo>(fromQuery())
const checking = ref(false)
/** 重试后仍是漂移 / 网关没响应的一句话说明（空 = 没有额外信息） */
const statusNote = ref('')

/** 首屏校正：直接进页面（无查询参数）时也能拿到 expected/actual。 */
onMounted(async () => {
  if (info.value.expected || info.value.actual) return
  const probed = await probeSchemaMismatch()
  if (probed) info.value = probed
})

async function copyFix(): Promise<void> {
  const ok = await copyText(info.value.fix)
  if (ok) message.success(t('errors.schema_mismatch_copied'))
  else message.error(t('errors.schema_mismatch_copy_failed'))
}

/**
 * 重试：迁移跑完后由用户触发。
 *
 * 三种结果都要可区分——这正是本页存在的意义（旧行为只有"白屏"一种表现）：
 *   恢复（null 之外的正常态）→ 回首页；仍是漂移 → 原地更新版本号；探不到 → 说明网关
 *   没在听（可能正在重启），不要谎称"已修复"。
 */
async function retry(): Promise<void> {
  if (checking.value) return
  checking.value = true
  statusNote.value = ''
  try {
    const probed = await probeSchemaMismatch()
    if (probed) {
      info.value = probed
      statusNote.value = t('errors.schema_mismatch_still_mismatched')
      return
    }
    // 探测返回 null：要么已恢复（正常网关的 /healthz 不是 200 schema_mismatch），
    // 要么网关没在听。用一次业务健康探测区分这两者。
    const alive = await probeGatewayAlive()
    if (alive) {
      await router.replace('/')
      return
    }
    statusNote.value = t('errors.schema_mismatch_gateway_unreachable')
  } finally {
    checking.value = false
  }
}

/** 网关是否已经恢复成"业务可用"状态。只读一次 `/ready`，失败即视为未恢复。 */
async function probeGatewayAlive(): Promise<boolean> {
  try {
    const res = await fetch(`${gatewayOrigin()}/ready`, { method: 'GET' })
    return res.ok
  } catch {
    return false
  }
}

/** `/ready` 的 origin：与 axios 的 baseURL 同源（见 src/api/index.ts 的 API_URL）。 */
function gatewayOrigin(): string {
  const raw = String((import.meta.env.VITE_API_URL as string | undefined) || '')
  if (!raw) return ''
  try {
    return new URL(raw, window.location.href).origin
  } catch {
    return ''
  }
}

/** 整页重载：迁移已修好但路由状态没刷新时的最省事做法（壳层重启后也是同一条路）。 */
function reloadApp(): void {
  window.location.reload()
}
</script>

<template>
  <div class="sm-page">
    <div class="sm-card">
      <div class="sm-badge">
        {{ $t('errors.schema_mismatch_badge') }}
      </div>
      <h1 class="sm-title">
        {{ $t('errors.schema_mismatch_title') }}
      </h1>
      <p class="sm-desc">
        {{ $t('errors.schema_mismatch_desc') }}
      </p>

      <dl class="sm-versions">
        <div class="sm-row">
          <dt>{{ $t('errors.schema_mismatch_expected') }}</dt>
          <dd>
            <code data-testid="sm-expected">{{ info.expected || '—' }}</code>
          </dd>
        </div>
        <div class="sm-row">
          <dt>{{ $t('errors.schema_mismatch_actual') }}</dt>
          <dd>
            <code data-testid="sm-actual">{{ info.actual || $t('errors.schema_mismatch_actual_empty') }}</code>
          </dd>
        </div>
        <div
          v-if="info.missing && info.missing.length > 0"
          class="sm-row"
        >
          <dt>{{ $t('errors.schema_mismatch_missing_tables') }}</dt>
          <dd>
            <code data-testid="sm-missing">{{ info.missing.join(', ') }}</code>
          </dd>
        </div>
      </dl>

      <div class="sm-fix">
        <div class="sm-fix-label">
          {{ $t('errors.schema_mismatch_fix_label') }}
        </div>
        <div class="sm-fix-row">
          <code
            class="sm-fix-cmd"
            data-testid="sm-fix"
          >{{ info.fix }}</code>
          <Button
            size="small"
            data-testid="sm-copy"
            @click="copyFix"
          >
            {{ $t('errors.schema_mismatch_copy') }}
          </Button>
        </div>
      </div>

      <p
        v-if="statusNote"
        class="sm-note"
        data-testid="sm-note"
      >
        {{ statusNote }}
      </p>

      <div class="sm-actions">
        <Button
          type="primary"
          :loading="checking"
          data-testid="sm-retry"
          @click="retry"
        >
          {{ $t('errors.schema_mismatch_retry') }}
        </Button>
        <Button
          :disabled="checking"
          data-testid="sm-reload"
          @click="reloadApp"
        >
          {{ $t('errors.schema_mismatch_reload') }}
        </Button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sm-page {
  min-height: var(--app-height);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--bg-page);
}
.sm-card {
  width: min(560px, 100%);
  padding: 28px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
}
.sm-badge {
  display: inline-block;
  padding: 2px 8px;
  margin-bottom: 10px;
  font-size: 12px;
  font-weight: var(--fw-medium);
  color: var(--warning);
  background: var(--warning-bg);
  border-radius: var(--radius-sm);
}
.sm-title {
  margin: 0 0 8px;
  font-size: 20px;
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
}
.sm-desc {
  margin: 0 0 20px;
  font-size: 14px;
  line-height: 1.6;
  color: var(--text-secondary);
}
.sm-versions {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0 0 20px;
}
.sm-row {
  display: flex;
  gap: 12px;
  align-items: baseline;
}
.sm-row dt {
  flex: 0 0 96px;
  font-size: 13px;
  color: var(--text-tertiary);
}
.sm-row dd {
  flex: 1 1 auto;
  margin: 0;
  min-width: 0;
}
.sm-row code,
.sm-fix-cmd {
  display: block;
  padding: 6px 10px;
  font: 12px/1.5 ui-monospace, "Cascadia Code", Consolas, monospace;
  color: var(--text-primary);
  background: var(--bg-page);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  white-space: pre-wrap;
  word-break: break-all;
}
.sm-fix {
  margin-bottom: 18px;
}
.sm-fix-label {
  margin-bottom: 6px;
  font-size: 13px;
  color: var(--text-tertiary);
}
.sm-fix-row {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}
.sm-fix-cmd {
  flex: 1 1 auto;
  min-width: 0;
}
.sm-note {
  margin: 0 0 16px;
  font-size: 13px;
  color: var(--warning);
}
.sm-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
@media (max-width: 576px) {
  .sm-row { flex-direction: column; gap: 4px; }
  .sm-row dt { flex: none; }
  .sm-actions :deep(.ant-btn) { min-height: 40px; }
}
</style>
