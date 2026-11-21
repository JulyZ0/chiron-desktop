<script setup lang="ts">
/**
 * 自助改密页（首登强制 / 主动修改）。
 *
 * 团队模式里初始密码由管理员一次性交付，必须能在这里换掉。未改密前后端
 * AuthMiddleware 会拦下除本页相关接口外的所有请求（403 password_change_required），
 * 路由守卫也会把用户送回这里。
 */
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Alert, Button, Card, Input, message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '../stores/auth'
import { serverErrorMessage } from '../utils/apiError'

const { t } = useI18n()
const router = useRouter()
const auth = useAuthStore()

const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const error = ref('')
const submitting = ref(false)

const forced = computed(() => auth.mustChangePassword)
const displayName = computed(() => auth.user?.nickname || auth.user?.name || auth.user?.email || '')
/** 本地 owner 的密码是随机值（password_set=false）：首次设置不需要旧密码。 */
const needsOldPassword = computed(() => auth.user?.password_set !== false)

async function submit() {
  if (submitting.value) return
  error.value = ''
  if ((needsOldPassword.value && !oldPassword.value) || !newPassword.value) {
    error.value = t('auth.change_password_missing')
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    error.value = t('auth.password_mismatch')
    return
  }
  if (newPassword.value.length < 8) {
    error.value = t('auth.password_too_short')
    return
  }
  submitting.value = true
  try {
    await auth.changePassword(oldPassword.value, newPassword.value)
    message.success(t('auth.change_password_success'))
    router.replace(forced.value ? '/' : '/profile')
  } catch (err) {
    error.value = serverErrorMessage(err, t('auth.change_password_failed'))
  } finally {
    submitting.value = false
  }
}

function leave() {
  if (forced.value) return
  router.back()
}

async function logout() {
  await auth.logout()
  router.replace('/login')
}
</script>

<template>
  <div class="password-page">
    <Card
      :bordered="false"
      class="password-card"
    >
      <h1 class="password-title">
        {{ forced ? t('auth.change_password_title_forced') : t('auth.change_password_title') }}
      </h1>
      <p
        v-if="displayName"
        class="password-subtitle"
      >
        {{ needsOldPassword
          ? t('auth.change_password_subtitle', { name: displayName })
          : t('auth.set_password_subtitle', { name: displayName }) }}
      </p>

      <form
        class="password-form"
        @submit.prevent="submit"
      >
        <template v-if="needsOldPassword">
          <label class="password-label">{{ t('auth.old_password') }}</label>
          <Input.Password
            v-model:value="oldPassword"
            size="large"
            autocomplete="current-password"
            data-test="old-password"
          />
        </template>

        <label class="password-label">{{ t('auth.newPassword') }}</label>
        <Input.Password
          v-model:value="newPassword"
          size="large"
          autocomplete="new-password"
          data-test="new-password"
        />

        <label class="password-label">{{ t('auth.confirmNewPassword') }}</label>
        <Input.Password
          v-model:value="confirmPassword"
          size="large"
          autocomplete="new-password"
          data-test="confirm-password"
          @press-enter="submit"
        />

        <Alert
          v-if="error"
          type="error"
          show-icon
          :message="error"
          class="password-error"
        />

        <Button
          type="primary"
          size="large"
          html-type="submit"
          block
          :loading="submitting"
          data-test="password-submit"
        >
          {{ t('auth.change_password_submit') }}
        </Button>
      </form>

      <div class="password-actions">
        <Button
          v-if="forced"
          type="link"
          @click="logout"
        >
          {{ t('auth.logout') }}
        </Button>
        <Button
          v-else
          type="link"
          @click="leave"
        >
          {{ t('common.back') }}
        </Button>
      </div>
    </Card>
  </div>
</template>

<style scoped>
.password-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--bg-body);
}

.password-card {
  width: 100%;
  max-width: 420px;
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-lg);
}

.password-title {
  margin: 0 0 8px;
  font-size: 22px;
  color: var(--text-primary);
}

.password-subtitle {
  margin: 0 0 20px;
  font-size: 13px;
  color: var(--text-secondary);
}

.password-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.password-label {
  font-size: 13px;
  font-weight: var(--fw-medium);
  color: var(--text-primary);
}

.password-error {
  margin: 4px 0;
}

.password-actions {
  margin-top: 12px;
  text-align: center;
}
</style>
