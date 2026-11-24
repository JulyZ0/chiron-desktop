<script setup lang="ts">
/**
 * 两步验证（TOTP，可选）—— 设置页面板。
 *
 * ## 它解决什么
 *
 * 本地模式解锁只需 Windows 账户密码。共用工作站上，「你登录了 Windows、人走开、
 * 屏幕没锁」时旁人可以直接解锁。开启后解锁还要求一个验证器应用生成的 6 位码。
 *
 * ## 为什么默认关闭
 *
 * 单人独占的家用机上它是纯摩擦（每次解锁都要掏手机）。所以由用户显式开启，
 * 而不是替所有人做这个决定。
 *
 * ## 三条必须讲清楚的 UI 事实（都是"事后才发现就晚了"的）
 *
 * 1. **恢复码只显示一次**。验证器丢失时它是唯一出路，所以启用成功后必须停下来
 *    让用户抄写 —— 不能用一个 toast 打发。
 * 2. **密钥不回显、不可再查看**。它由主进程用 safeStorage 加密落盘，这里只拿得到
 *    "开没开 / 还剩几个恢复码"。
 * 3. **停用/重新生成要验密码**。否则旁人拿到未锁的窗口就能把第二因素关掉 ——
 *    那样这道防线等于不存在。
 */
import { copyTextOrThrow } from '../../utils/clipboard'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { message } from 'ant-design-vue'
import { SafetyCertificateOutlined } from '@ant-design/icons-vue'
import QRCode from 'qrcode'
import { useI18n } from 'vue-i18n'
import { useDeviceStore } from '../../stores/device'
import type { TwoFactorStatus } from '../../stores/device'

const { t } = useI18n()
const device = useDeviceStore()

const loading = ref(false)
const busy = ref(false)
const enrollConfirming = ref(false)
const status = ref<TwoFactorStatus | null>(null)
/** 外壳不支持这些 IPC（老 preload） */
const unsupported = ref(false)

// ── 启用流程：begin → 扫码/抄密钥 → 回填验证码 → confirm → 显示恢复码 ──

const enrollOpen = ref(false)
const enrollSecret = ref('')
const enrollQr = ref('')
const enrollToken = ref('')
const enrollError = ref('')

/** 启用成功后展示的恢复码（唯一一次） */
const recoveryCodes = ref<string[]>([])
const recoveryOpen = ref(false)
const copied = ref(false)

// ── 停用 / 重新生成：都要密码 ──

const passwordOpen = ref(false)
/** 'disable' | 'regenerate' */
const passwordAction = ref<'disable' | 'regenerate'>('disable')
const passwordInput = ref('')
const passwordError = ref('')
let mounted = true
let enrollGeneration = 0

function clearEnrollment() {
  enrollGeneration++
  enrollSecret.value = ''
  enrollQr.value = ''
  enrollToken.value = ''
  enrollError.value = ''
}

function clearRecovery() {
  recoveryCodes.value = []
  copied.value = false
}

function clearPassword() {
  passwordInput.value = ''
  passwordError.value = ''
}

watch(enrollOpen, open => {
  if (open) return
  if (enrollConfirming.value) {
    enrollOpen.value = true
    return
  }
  clearEnrollment()
})
watch(recoveryOpen, open => { if (!open) clearRecovery() })
watch(passwordOpen, open => { if (!open) clearPassword() })

onUnmounted(() => {
  mounted = false
  clearEnrollment()
  clearRecovery()
  clearPassword()
})

const enabled = computed(() => status.value?.enabled === true)

async function load() {
  loading.value = true
  try {
    const next = await device.getTwoFactorStatus()
    if (next === null) {
      unsupported.value = true
      return
    }
    status.value = next
  } finally {
    loading.value = false
  }
}

async function startEnroll() {
  busy.value = true
  clearEnrollment()
  const generation = enrollGeneration
  try {
    const started = await device.beginTwoFactor()
    if (!mounted || generation !== enrollGeneration) return
    if (!started.ok) {
      if (started.reason === 'unsupported') unsupported.value = true
      else enrollError.value = started.message || started.reason || ''
      if (!started.secret) return
    }
    enrollSecret.value = started.secret || ''
    // 二维码只是"少打字"的便利：画不出来就退化成手动输入密钥，不影响功能
    enrollQr.value = ''
    if (started.otpauth) {
      try {
        // 颜色在**生成时**指定，而不是靠外层 CSS 兜白底：二维码的深/浅对比是扫码的
        // 物理前提，深色主题下若让背景跟随 `--bg-card`，扫码会直接失败。让 SVG 自带
        // 白底 + 纯黑码，任何主题下都保持同样的可扫性。
        enrollQr.value = await QRCode.toString(started.otpauth, {
          type: 'svg',
          margin: 1,
          width: 200,
          color: { dark: '#000000', light: '#ffffff' },
        })
      } catch {
        /* 画不出来就走手动输入 */
      }
    }
    if (!mounted || generation !== enrollGeneration) {
      enrollSecret.value = ''
      enrollQr.value = ''
      return
    }
    enrollToken.value = ''
    enrollOpen.value = true
  } finally {
    if (mounted) busy.value = false
  }
}

async function confirmEnroll() {
  const token = enrollToken.value.trim()
  if (!token || busy.value) return
  busy.value = true
  enrollConfirming.value = true
  enrollError.value = ''
  const generation = enrollGeneration
  try {
    const result = await device.confirmTwoFactor(token)
    if (!mounted || generation !== enrollGeneration) return
    if (!result.ok) {
      enrollToken.value = ''
      enrollError.value = t('settings.two_factor_confirm_failed')
      return
    }
    clearEnrollment()
    enrollConfirming.value = false
    enrollOpen.value = false
    recoveryCodes.value = result.recoveryCodes || []
    recoveryOpen.value = true
    message.success(t('settings.two_factor_enabled_ok'))
    await load()
  } finally {
    enrollConfirming.value = false
    if (mounted) busy.value = false
  }
}

async function copyRecovery() {
  try {
    await copyTextOrThrow(recoveryCodes.value.join('\n'))
    copied.value = true
    message.success(t('settings.two_factor_recovery_copied'))
  } catch {
    // 剪贴板不可用（权限被拒）：让用户手动选中抄写，不算失败
    message.warning(t('settings.two_factor_recovery_copy'))
  }
}

function closeRecovery() {
  recoveryOpen.value = false
  clearRecovery()
}

/** 停用 / 重新生成都先要密码 —— 见模块头第 3 条 */
function askPassword(action: 'disable' | 'regenerate') {
  passwordAction.value = action
  clearPassword()
  passwordOpen.value = true
}

async function submitPassword() {
  const password = passwordInput.value
  if (!password || busy.value) return
  busy.value = true
  passwordError.value = ''
  try {
    if (passwordAction.value === 'disable') {
      const result = await device.disableTwoFactor(password)
      if (!mounted) return
      if (!result.ok) {
        passwordError.value =
          result.reason === 'bad-credential'
            ? t('auth.lock_bad_credential')
            : result.message || t('settings.two_factor_password_required')
        return
      }
      passwordOpen.value = false
      message.success(t('settings.two_factor_disabled_ok'))
      await load()
      return
    }
    const result = await device.regenerateRecoveryCodes(password)
    if (!mounted) return
    if (!result.ok) {
      passwordError.value =
        result.reason === 'bad-credential'
          ? t('auth.lock_bad_credential')
          : result.message || t('settings.two_factor_password_required')
      return
    }
    passwordOpen.value = false
    recoveryCodes.value = result.recoveryCodes || []
    recoveryOpen.value = true
    copied.value = false
    message.success(t('settings.two_factor_regenerated_ok'))
    await load()
  } finally {
    passwordInput.value = ''
    if (mounted) busy.value = false
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="tfa-block">
    <div class="tfa-head">
      <SafetyCertificateOutlined class="tfa-icon" />
      <div>
        <div class="tfa-title">
          {{ $t('settings.two_factor_title') }}
        </div>
        <p class="tfa-intro">
          {{ $t('settings.two_factor_intro') }}
        </p>
      </div>
    </div>

    <a-alert
      v-if="unsupported"
      type="warning"
      show-icon
      :message="$t('settings.two_factor_unsupported')"
    />

    <a-spin
      v-else
      :spinning="loading"
    >
      <a-alert
        v-if="status && status.unreadable"
        type="warning"
        show-icon
        class="tfa-alert"
        :message="$t('settings.two_factor_unreadable')"
      />

      <div class="tfa-status">
        <a-tag :color="enabled ? 'success' : 'default'">
          {{ enabled ? $t('settings.two_factor_status_on') : $t('settings.two_factor_status_off') }}
        </a-tag>
        <span
          v-if="enabled && status?.enabledAt"
          class="tfa-meta"
        >{{ $t('settings.two_factor_enabled_at', { at: new Date(status.enabledAt).toLocaleString() }) }}</span>
        <span
          v-if="enabled && status"
          class="tfa-meta"
        >{{ $t('settings.two_factor_recovery_left', { n: status.recoveryCodesLeft }) }}</span>
      </div>

      <!-- 恢复码用完是**必须提醒**的状态：验证器一丢就再也进不来 -->
      <a-alert
        v-if="enabled && status && status.recoveryCodesLeft === 0"
        type="error"
        show-icon
        class="tfa-alert"
        :message="$t('settings.two_factor_recovery_empty_warn')"
      />

      <div class="tfa-actions">
        <a-button
          v-if="!enabled"
          type="primary"
          :loading="busy"
          data-test="tfa-enable"
          @click="startEnroll"
        >
          {{ $t('settings.two_factor_enable') }}
        </a-button>
        <template v-else>
          <a-button
            :loading="busy"
            data-test="tfa-regenerate"
            @click="askPassword('regenerate')"
          >
            {{ $t('settings.two_factor_regenerate') }}
          </a-button>
          <a-button
            danger
            :loading="busy"
            data-test="tfa-disable"
            @click="askPassword('disable')"
          >
            {{ $t('settings.two_factor_disable') }}
          </a-button>
        </template>
      </div>
    </a-spin>

    <!-- 启用：二维码 + 手动密钥 + 回填验证码 -->
    <a-modal
      v-model:open="enrollOpen"
      :title="$t('settings.two_factor_enable')"
      :footer="null"
      :mask-closable="false"
      :closable="!enrollConfirming"
      :keyboard="!enrollConfirming"
      :width="420"
      data-test="tfa-enroll-modal"
    >
      <p class="tfa-modal-hint">
        {{ $t('settings.two_factor_scan_hint') }}
      </p>
      <!-- 二维码是便利而非前提：svg 来自本地生成，不含外部资源 -->
      <div
        v-if="enrollQr"
        class="tfa-qr"
        v-html="enrollQr"
      />
      <div class="tfa-secret">
        <span class="tfa-secret-label">{{ $t('settings.two_factor_secret_label') }}</span>
        <code
          class="tfa-secret-value"
          data-test="tfa-secret"
        >{{ enrollSecret }}</code>
      </div>
      <p class="tfa-modal-hint tfa-modal-hint--muted">
        {{ $t('settings.two_factor_manual_hint') }}
      </p>

      <label
        class="tfa-field"
        for="tfa-confirm-token"
      >
        <span class="tfa-field-label">{{ $t('settings.two_factor_confirm_label') }}</span>
        <a-input
          id="tfa-confirm-token"
          v-model:value="enrollToken"
          :maxlength="9"
          autocomplete="one-time-code"
          data-test="tfa-confirm-input"
          @press-enter="confirmEnroll"
        />
      </label>

      <p
        v-if="enrollError"
        class="tfa-error"
        role="alert"
      >
        {{ enrollError }}
      </p>

      <div class="tfa-modal-actions">
        <a-button
          data-test="tfa-enroll-cancel"
          :disabled="enrollConfirming"
          @click="enrollOpen = false"
        >
          {{ $t('common.cancel') }}
        </a-button>
        <a-button
          type="primary"
          :loading="busy"
          :disabled="!enrollToken.trim()"
          data-test="tfa-confirm"
          @click="confirmEnroll"
        >
          {{ $t('settings.two_factor_confirm') }}
        </a-button>
      </div>
    </a-modal>

    <!-- 恢复码：只显示这一次 -->
    <a-modal
      v-model:open="recoveryOpen"
      :title="$t('settings.two_factor_recovery_title')"
      :footer="null"
      :mask-closable="false"
      :width="420"
      data-test="tfa-recovery-modal"
    >
      <a-alert
        type="warning"
        show-icon
        :message="$t('settings.two_factor_recovery_hint')"
      />
      <ul
        class="tfa-codes"
        data-test="tfa-recovery-codes"
      >
        <li
          v-for="code in recoveryCodes"
          :key="code"
          class="tfa-code"
        >
          <code>{{ code }}</code>
        </li>
      </ul>
      <div class="tfa-modal-actions">
        <a-button @click="copyRecovery">
          {{ copied ? $t('settings.two_factor_recovery_copied') : $t('settings.two_factor_recovery_copy') }}
        </a-button>
        <a-button
          type="primary"
          data-test="tfa-recovery-done"
          @click="closeRecovery"
        >
          {{ $t('settings.two_factor_done') }}
        </a-button>
      </div>
    </a-modal>

    <!-- 停用 / 重新生成：验密码 -->
    <a-modal
      v-model:open="passwordOpen"
      :title="$t('settings.two_factor_password_prompt')"
      :footer="null"
      :mask-closable="false"
      :width="400"
      data-test="tfa-password-modal"
    >
      <a-input-password
        v-model:value="passwordInput"
        :placeholder="$t('auth.password')"
        autocomplete="current-password"
        data-test="tfa-password-input"
        @press-enter="submitPassword"
      />
      <p
        v-if="passwordError"
        class="tfa-error"
        role="alert"
      >
        {{ passwordError }}
      </p>
      <div class="tfa-modal-actions">
        <a-button @click="passwordOpen = false">
          {{ $t('common.cancel') }}
        </a-button>
        <a-button
          type="primary"
          :loading="busy"
          :disabled="!passwordInput"
          data-test="tfa-password-submit"
          @click="submitPassword"
        >
          {{ passwordAction === 'disable' ? $t('settings.two_factor_disable') : $t('settings.two_factor_regenerate') }}
        </a-button>
      </div>
    </a-modal>
  </div>
</template>

<style scoped>
.tfa-block { padding: 4px 0 8px; }

.tfa-head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  margin-bottom: 12px;
}

.tfa-icon {
  font-size: 20px;
  color: var(--primary);
  margin-block-start: 2px;
}

.tfa-title {
  font-size: 14px;
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
}

.tfa-intro {
  margin: 4px 0 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.tfa-alert { margin-bottom: 12px; }

.tfa-status {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
}

.tfa-meta {
  font-size: 12px;
  color: var(--text-tertiary);
  font-variant-numeric: tabular-nums;
}

.tfa-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.tfa-modal-hint {
  margin: 0 0 10px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.tfa-modal-hint--muted { color: var(--text-tertiary); }

/* 二维码：本地生成的 svg，限宽避免弹窗被撑开 */
.tfa-qr {
  display: flex;
  justify-content: center;
  margin-bottom: 12px;
}

.tfa-qr :deep(svg) {
  width: 180px;
  height: 180px;
  border-radius: var(--radius-sm);
  /* 白底由 SVG 自带（生成时指定配色，见 startEnroll）——不在这里写死色值：
     那会同时违反主题 token 契约，也会把"扫码对比度"这件事变成一个可被主题覆盖的样式。 */
}

.tfa-secret {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.tfa-secret-label {
  font-size: 12px;
  color: var(--text-secondary);
}

.tfa-secret-value {
  font-family: var(--font-mono);
  font-size: 12px;
  word-break: break-all;
  color: var(--text-primary);
}

.tfa-field {
  display: block;
  margin-bottom: 12px;
}

.tfa-field-label {
  display: block;
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}

.tfa-error {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--warning);
}

.tfa-modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-block-start: 16px;
}

.tfa-codes {
  list-style: none;
  margin: 12px 0 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
}

.tfa-code {
  font-family: var(--font-mono);
  font-size: 13px;
  padding: 6px 8px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-secondary);
  color: var(--text-primary);
  text-align: center;
}
</style>
