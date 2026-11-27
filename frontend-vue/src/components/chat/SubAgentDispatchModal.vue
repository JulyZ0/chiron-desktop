<script setup lang="ts">
/** 直接委派子 Agent 的表单（与模型自己调 subagent 同一条后台路径）。 */
import { Checkbox, Input, Modal } from 'ant-design-vue'

const open = defineModel<boolean>('open', { default: false })
const task = defineModel<string>('task', { default: '' })
const profile = defineModel<string>('profile', { default: '' })
const allowWrite = defineModel<boolean>('allowWrite', { default: false })

defineProps<{ dispatching: boolean }>()
const emit = defineEmits<{ (e: 'submit'): void }>()
</script>

<template>
  <Modal
    v-model:open="open"
    :title="$t('agent.subagent_dispatch')"
    :ok-text="$t('chat.input.send')"
    :cancel-text="$t('common.cancel')"
    :confirm-loading="dispatching"
    :ok-button-props="{ disabled: !task.trim() }"
    @ok="emit('submit')"
  >
    <div class="dispatch-form">
      <Input.TextArea
        v-model:value="task"
        :rows="4"
        :maxlength="4000"
        :placeholder="$t('agent.subagent_dispatch_placeholder')"
      />
      <Input
        v-model:value="profile"
        :placeholder="$t('agent.subagent_dispatch_profile')"
      />
      <Checkbox v-model:checked="allowWrite">
        {{ $t('agent.subagent_dispatch_allow_write') }}
      </Checkbox>
      <p class="dispatch-hint">
        {{ $t('agent.subagent_dispatch_hint') }}
      </p>
    </div>
  </Modal>
</template>

<style scoped>
.dispatch-form { display: flex; flex-direction: column; gap: 8px; }
.dispatch-hint { margin: 0; font-size: var(--fs-xs); color: var(--text-tertiary); }
</style>
