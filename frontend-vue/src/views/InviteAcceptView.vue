<script setup lang="ts">
/**
 * 受邀人设置密码（一次性邀请链接）。
 *
 * 公开页面：凭链接里的 token 换一次"设置自己的密码"，成功后跳到登录页。
 * 失败语义要分清：链接无效/过期与密码不合规都来自后端，页面就地展示。
 */
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Alert, Button, Card, Input } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { acceptInvite } from '../api/auth'
import { serverErrorMessage } from '../utils/apiError'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

const password = ref('')
const confirm = ref('')
const error = ref('')
const submitting = ref(false)

async function submit() {
  if (submitting.value) return
  error.value = ''
  const token = String(route.params.token || '')
  if (!token) {
    error.value = t('auth.invite_invalid')
    return
  }
  if (!password.value) {
    error.value = t('auth.invite_password_required')
    return
  }
  if (password.value !== confirm.value) {
    error.value = t('auth.invite_mismatch')
    return
  }
  submitting.value = true
  try {
    await acceptInvite(token, password.value)
    await router.replace({ path: '/login' })
  } catch (err) {
    error.value = serverErrorMessage(err, t('auth.invite_invalid'))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="invite-page">
    <Card :bordered="false" class="invite-card">
      <h1 class="invite-title">{{ t('auth.invite_title') }}</h1>
      <p class="invite-hint">{{ t('auth.invite_hint') }}</p>

      <form class="invite-form" @submit.prevent="submit">
        <label class="invite-label">{{ t('auth.invite_password') }}</label>
        <Input.Password
          v-model:value="password"
          size="large"
          autocomplete="new-password"
        />
        <label class="invite-label">{{ t('auth.invite_confirm') }}</label>
        <Input.Password
          v-model:value="confirm"
          size="large"
          autocomplete="new-password"
          @press-enter="submit"
        />
        <Alert v-if="error" type="error" show-icon :message="error" />
        <Button type="primary" size="large" html-type="submit" block :loading="submitting">
          {{ t('auth.invite_submit') }}
        </Button>
      </form>
    </Card>
  </div>
</template>

<style scoped>
.invite-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--bg-body);
}
.invite-card {
  width: 100%;
  max-width: 400px;
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-lg);
}
.invite-title {
  margin: 0 0 8px;
  font-size: 22px;
  color: var(--text-primary);
  text-align: center;
}
.invite-hint {
  margin: 0 0 20px;
  font-size: 13px;
  color: var(--text-secondary);
  text-align: center;
}
.invite-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.invite-label {
  font-size: 13px;
  font-weight: var(--fw-medium);
  color: var(--text-primary);
}
</style>
