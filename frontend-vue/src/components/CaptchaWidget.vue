<script setup lang="ts">
/**
 * CaptchaWidget — 人机验证组件
 *
 * 按后端下发的 provider 动态加载对应官方脚本：
 * - turnstile（Cloudflare）/ recaptcha（Google）/ hcaptcha：显式 render 容器
 * - tencent（腾讯防水墙）：弹出式，点击触发
 * - custom：无标准组件，展示部署方接入提示（fail-loud，不伪装已验证）
 *
 * 验证成功后 emit('verified', { token, randstr })；
 * 提交失败后由父组件调用 reset() 重置。
 *
 * **启用开关与 CSP 一致性（PRD H74）**：只有部署方在 `VITE_CAPTCHA_PROVIDERS` 里
 * 声明启用的 provider 才会加载脚本 —— 未启用时**一个第三方请求都不发**（不插
 * `<script>`、不建 iframe），而不是"先加载再看 CSP 拦不拦"。脚本 URL 与需要放行的
 * 域名都来自 `utils/captchaProviders.ts`（唯一事实来源），部署侧
 * `security-headers.conf` 的 CSP 与它逐源一致（由 spec 强制）。
 */
import { ref, onMounted, onBeforeUnmount, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  CAPTCHA_PROVIDERS,
  isCaptchaProviderEnabled,
} from '../utils/captchaProviders'

const props = defineProps<{
  provider: string
  siteKey: string
  verifyUrl?: string
}>()

const emit = defineEmits<{
  (e: 'verified', payload: { token: string; randstr?: string }): void
  (e: 'expired'): void
}>()

const { t } = useI18n()

/**
 * 三家「显式渲染」式验证码的公共形状。
 *
 * turnstile / grecaptcha / hcaptcha 的 `render` 参数与 `reset` 语义一致，只有返回值类型
 * 不同（turnstile/hcaptcha 给字符串 id，reCAPTCHA 给数字），故 id 统一成 `CaptchaWidgetId`。
 * 这些脚本只有 CDN 版、没有官方类型，形状按各家文档声明在此。
 */
type CaptchaWidgetId = string | number
interface CaptchaRenderer {
  render(
    el: HTMLElement,
    opts: {
      sitekey: string
      callback?: (token: string) => void
      'expired-callback'?: () => void
      'error-callback'?: () => void
    },
  ): CaptchaWidgetId
  reset(id?: CaptchaWidgetId): void
  remove?(id: CaptchaWidgetId): void
}

/** 腾讯防水墙：弹出式，构造后 show() 触发（与其它三家的「容器内渲染」模型不同） */
interface TencentCaptchaInstance {
  show(): void
}
interface TencentCaptchaResult {
  ret?: number
  ticket?: string
  randstr?: string
}
type TencentCaptchaCtor = new (
  siteKey: string,
  callback: (res: TencentCaptchaResult) => void,
) => TencentCaptchaInstance

/** 第三方脚本挂在 window 上的全局；全部可选，取用前必须判存在 */
interface CaptchaGlobals {
  turnstile?: CaptchaRenderer
  grecaptcha?: CaptchaRenderer
  hcaptcha?: CaptchaRenderer
  TencentCaptcha?: TencentCaptchaCtor
}
const captchaGlobal = (): CaptchaGlobals => window as unknown as CaptchaGlobals

const container = ref<HTMLElement>()
const loading = ref(false)
const loadError = ref('')
const tencentCaptcha = ref<TencentCaptchaInstance | null>(null)

const providerLabel = computed(() => {
  const map: Record<string, string> = {
    turnstile: 'Cloudflare Turnstile',
    recaptcha: 'Google reCAPTCHA',
    hcaptcha: 'hCaptcha',
    tencent: t('common.tencent_waterproof_wall'),
    custom: t('auth.custom_verification'),
  }
  return map[props.provider] || props.provider
})

// ── 脚本加载（去重，避免重复 <script>） ──

const loadedScripts = new Set<string>()

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    if (loadedScripts.has(src)) return resolve()
    const existing = document.querySelector(`script[src="${src}"]`) as HTMLScriptElement | null
    if (existing) {
      if (existing.dataset.loaded === '1') return resolve()
      existing.addEventListener('load', () => resolve())
      existing.addEventListener('error', () => reject(new Error('script load failed')))
      return
    }
    const s = document.createElement('script')
    s.src = src
    s.async = true
    s.defer = true
    s.addEventListener('load', () => {
      s.dataset.loaded = '1'
      loadedScripts.add(src)
      resolve()
    })
    s.addEventListener('error', () => reject(new Error('script load failed')))
    document.head.appendChild(s)
  })
}

function waitFor<T>(getter: () => T | undefined, timeoutMs = 8000): Promise<T> {
  return new Promise((resolve, reject) => {
    const start = Date.now()
    const timer = setInterval(() => {
      const v = getter()
      if (v) {
        clearInterval(timer)
        resolve(v)
      } else if (Date.now() - start > timeoutMs) {
        clearInterval(timer)
        reject(new Error('captcha script timeout'))
      }
    }, 100)
  })
}

// ── 各 provider 渲染 ──

let widgetId: CaptchaWidgetId | null = null

/** 该 provider 的官方脚本地址（白名单里的唯一一条；未知 provider 抛错由调用方兜底） */
function providerScriptUrl(provider: string): string {
  const spec = CAPTCHA_PROVIDERS[provider]
  const url = spec?.scriptUrls[0]
  if (!url) throw new Error(`no script url for captcha provider: ${provider}`)
  return url
}

async function renderWidget() {
  if (!container.value) return
  // 部署方没启用这个 provider：不加载任何第三方脚本，直接如实报出来。
  // 顺序很重要 —— 先判启用、再 `loadScript`，否则"加载了但被 CSP 拦住"会成为
  // 最难查的那类线上问题（控制台只有一条 CSP violation）。
  if (!isCaptchaProviderEnabled(props.provider)) {
    loadError.value = t('auth.captcha_provider_not_enabled', { provider: providerLabel.value })
    return
  }
  loading.value = true
  loadError.value = ''
  try {
    switch (props.provider) {
      case 'turnstile': {
        await loadScript(providerScriptUrl('turnstile'))
        const ts = await waitFor<CaptchaRenderer>(() => captchaGlobal().turnstile)
        widgetId = ts.render(container.value, {
          sitekey: props.siteKey,
          callback: (token: string) => emit('verified', { token }),
          'expired-callback': () => emit('expired'),
          'error-callback': () => { loadError.value = t('auth.verification_component_failed_to_load_refresh_to_retry') },
        })
        break
      }
      case 'recaptcha': {
        await loadScript(providerScriptUrl('recaptcha'))
        const g = await waitFor<CaptchaRenderer>(() => {
          const g = captchaGlobal().grecaptcha
          return g?.render ? g : undefined
        })
        widgetId = g.render(container.value, {
          sitekey: props.siteKey,
          callback: (token: string) => emit('verified', { token }),
          'expired-callback': () => emit('expired'),
        })
        break
      }
      case 'hcaptcha': {
        await loadScript(providerScriptUrl('hcaptcha'))
        const h = await waitFor<CaptchaRenderer>(() => captchaGlobal().hcaptcha)
        widgetId = h.render(container.value, {
          sitekey: props.siteKey,
          callback: (token: string) => emit('verified', { token }),
          'expired-callback': () => emit('expired'),
        })
        break
      }
      case 'tencent': {
        await loadScript(providerScriptUrl('tencent'))
        const TC = await waitFor<TencentCaptchaCtor>(() => captchaGlobal().TencentCaptcha)
        // 弹出式：点击按钮时 show() 触发
        tencentCaptcha.value = new TC(props.siteKey, (res: TencentCaptchaResult) => {
          if (res?.ret === 0 && res.ticket) {
            emit('verified', { token: res.ticket, randstr: res.randstr })
          } else {
            // 用户关闭弹窗 → 已验证状态作废
            emit('expired')
          }
        })
        break
      }
      case 'custom':
        // custom 由部署方按 verify_url 契约自行接入前端组件，此处仅提示
        break
      default:
        loadError.value = t('auth.unknown_captcha_type_provider', { provider: props.provider })
    }
  } catch {
    loadError.value = t('auth.captcha_component_failed_to_load_check_your_network_and_refresh_to_retry')
  } finally {
    loading.value = false
  }
}

function showTencent() {
  tencentCaptcha.value?.show()
}

/** 重置验证状态（提交失败后由父组件调用） */
function reset() {
  if (widgetId !== null) {
    try {
      const w = captchaGlobal()
      if (props.provider === 'turnstile' && w.turnstile) w.turnstile.reset(widgetId)
      else if (props.provider === 'recaptcha' && w.grecaptcha) w.grecaptcha.reset(widgetId)
      else if (props.provider === 'hcaptcha' && w.hcaptcha) w.hcaptcha.reset(widgetId)
    } catch { /* 组件未就绪时忽略 */ }
  }
}

onMounted(renderWidget)
onBeforeUnmount(() => {
  try {
    const w = captchaGlobal()
    if (widgetId !== null) {
      if (props.provider === 'turnstile' && w.turnstile?.remove) w.turnstile.remove(widgetId)
      else if (props.provider === 'hcaptcha' && w.hcaptcha?.reset) w.hcaptcha.reset(widgetId)
    }
  } catch { /* 卸载阶段忽略 */ }
})

defineExpose({ reset })
</script>

<template>
  <div class="captcha-widget">
    <template v-if="provider === 'tencent'">
      <button
        type="button"
        class="tencent-trigger"
        :title="$t('auth.click_to_complete_human_verification')"
        :disabled="loading"
        @click="showTencent"
      >
        {{ loading ? $t('common.loading') : $t('auth.click_to_complete_human_verification') }}
      </button>
    </template>
    <template v-else-if="provider === 'custom'">
      <div class="custom-hint">
        {{ $t('auth.this_site_uses_a_custom_human_verification_url_please_complete_it_per_the_deployer_s_setup_guide_before_submitting', { url: verifyUrl || $t('common.custom_endpoint') }) }}
      </div>
    </template>
    <template v-else>
      <div
        ref="container"
        class="captcha-container"
      />
    </template>
    <div
      v-if="loading && provider !== 'tencent'"
      class="captcha-loading"
    >
      {{ $t('auth.loading_verification_component') }}
    </div>
    <div
      v-if="loadError"
      class="captcha-error"
    >
      {{ loadError }}
    </div>
  </div>
</template>

<style scoped>
.captcha-widget {
  margin-bottom: 8px;
}

.captcha-container {
  min-height: 44px;
  display: flex;
  justify-content: flex-start;
  max-width: 100%;
  /* 第三方组件（如 Turnstile 300px 宽）在极窄屏防页面横向溢出 */
  overflow-x: auto;
}

.tencent-trigger {
  width: 100%;
  min-height: 40px; /* 触控目标 ≥ 40px */
  padding: 10px 12px;
  border: var(--hairline-width) solid var(--border-card, #d9d9d9);
  border-radius: var(--radius-lg);
  background: var(--bg-card, var(--on-solid));
  color: var(--text-primary, rgba(0, 0, 0, 0.88));
  font-size: var(--fs-base);
  cursor: pointer;
  transition: border-color var(--dur-normal);
}

.tencent-trigger:hover:not(:disabled) {
  border-color: var(--primary, #1677ff);
}

.tencent-trigger:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.custom-hint {
  padding: 10px 12px;
  border: 1px dashed var(--border-card, #d9d9d9);
  border-radius: var(--radius-lg);
  font-size: var(--fs-sm);
  color: var(--text-tertiary, var(--bg-overlay));
  line-height: 1.6;
}

.captcha-loading,
.captcha-error {
  margin-top: 6px;
  font-size: var(--fs-sm);
  color: var(--text-tertiary, var(--bg-overlay));
}

.captcha-error {
  color: var(--error);
}
</style>
