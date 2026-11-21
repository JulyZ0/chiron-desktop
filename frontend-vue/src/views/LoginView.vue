<script setup lang="ts">
/**
 * Web 登录页（团队服务模式）。
 *
 * 桌面本地模式不会到这里：身份由网关按 OS 账户免口令下发。同一套产物被网关
 * 托管给浏览器时，这条路由就是唯一入口 —— 没有它，未登录访客只能看到一堆 401。
 *
 * 三个边界：
 *   · 不提供注册/找回入口（账号由管理员创建）；
 *   · 登录失败用页面内 Alert，不依赖全局 toast（全局拦截器会把 401 当"会话过期"）；
 *   · `redirect` 只接受站内相对路径，避免被拼成开放重定向。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Alert, Button, Card, Input } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '../stores/auth'
import { serverErrorMessage } from '../utils/apiError'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const identifier = ref('')
const password = ref('')
const error = ref('')
const submitting = ref(false)

/** 只接受站内绝对路径：`//evil.com` 与完整 URL 一律忽略。 */
const redirect = computed(() => {
  const raw = route.query.redirect
  if (typeof raw === 'string' && raw.startsWith('/') && !raw.startsWith('//')) return raw
  return '/'
})

function goNext() {
  router.replace(auth.mustChangePassword ? '/change-password' : redirect.value)
}

async function submit() {
  if (submitting.value) return
  const ident = identifier.value.trim()
  if (!ident || !password.value) {
    error.value = t('auth.login_missing_fields')
    return
  }
  submitting.value = true
  error.value = ''
  try {
    await auth.login(ident, password.value)
    goNext()
  } catch (err) {
    const status = (err as { response?: { status?: number } })?.response?.status
    if (status === 401) error.value = t('auth.login_invalid_credentials')
    else if (status === 429) error.value = t('auth.login_locked')
    else error.value = serverErrorMessage(err, t('auth.login_failed'))
  } finally {
    submitting.value = false
  }
}

onMounted(async () => {
  // 已有 cookie（刷新/新标签页）时不留在登录页。
  if (await auth.bootstrapSession()) goNext()
})
</script>

<template>
  <div class="login-page">
    <Card
      :bordered="false"
      class="login-card"
    >
      <div class="login-head">
        <h1 class="login-title">
          {{ t('auth.login_title') }}
        </h1>
        <p class="login-subtitle">
          {{ t('auth.login_subtitle') }}
        </p>
      </div>

      <form
        class="login-form"
        @submit.prevent="submit"
      >
        <label class="login-label">{{ t('auth.login_identifier') }}</label>
        <Input
          v-model:value="identifier"
          size="large"
          autocomplete="username"
          :placeholder="t('auth.login_identifier_placeholder')"
          data-test="login-identifier"
        />

        <label class="login-label">{{ t('auth.password') }}</label>
        <Input.Password
          v-model:value="password"
          size="large"
          autocomplete="current-password"
          data-test="login-password"
          @press-enter="submit"
        />

        <Alert
          v-if="error"
          type="error"
          show-icon
          :message="error"
          class="login-error"
        />

        <Button
          type="primary"
          size="large"
          html-type="submit"
          block
          :loading="submitting"
          data-test="login-submit"
        >
          {{ t('auth.login') }}
        </Button>
      </form>

      <p class="login-hint">
        {{ t('auth.login_no_account_hint') }}
      </p>
    </Card>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--bg-body);
}

.login-card {
  width: 100%;
  max-width: 400px;
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-lg);
}

.login-head {
  margin-bottom: 24px;
  text-align: center;
}

.login-title {
  margin: 0 0 8px;
  font-size: 24px;
  color: var(--text-primary);
}

.login-subtitle {
  margin: 0;
  font-size: 13px;
  color: var(--text-secondary);
}

.login-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.login-label {
  font-size: 13px;
  font-weight: var(--fw-medium);
  color: var(--text-primary);
}

.login-error {
  margin: 4px 0;
}

.login-hint {
  margin: 16px 0 0;
  text-align: center;
  font-size: 12px;
  color: var(--text-tertiary);
}
</style>
