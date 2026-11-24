<script setup lang="ts">
import { Alert, Button } from 'ant-design-vue'

defineProps<{
  message: string
  retrying?: boolean
}>()

defineEmits<{
  retry: []
}>()
</script>

<template>
  <Alert
    type="error"
    show-icon
    role="alert"
    class="admin-error-state"
    :message="message"
    :description="$t('common.please_check_if_the_backend_service_is_normal_or_retry_later')"
  >
    <template #action>
      <Button
        type="primary"
        size="small"
        :loading="retrying"
        @click="$emit('retry')"
      >
        {{ $t('common.retry') }}
      </Button>
    </template>
  </Alert>
</template>
