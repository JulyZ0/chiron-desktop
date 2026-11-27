<script setup lang="ts">
/**
 * 工具审批卡（单张）。
 *
 * 只负责展示与转发：审批队列、提交状态、编辑态归 ChatView / `useApprovalEdit`，
 * 组件不自己发请求，也不判断"能不能放行" —— 放行语义只能有一处实现。
 *
 * 三档状态：
 * - 等待：允许 / 拒绝 / 编辑参数；
 * - 编辑中：可"编辑后批准"，非法 JSON 禁用提交；
 * - 失败：票不摘，错误留在卡片上，按钮可重试。
 */
import { computed } from 'vue'
import type { ApprovalEditState, PendingApproval } from '../../composables/useApprovalEdit'

const props = defineProps<{
  approval: PendingApproval & { busy?: boolean; error?: string }
  /** 剩余秒数；0 = 已过期/未知，不展示倒计时 */
  remaining: number
  edit?: ApprovalEditState
}>()

const emit = defineEmits<{
  (e: 'approve'): void
  (e: 'reject'): void
  (e: 'toggle-edit'): void
  (e: 'edit-input', value: string): void
  (e: 'submit-edit'): void
}>()

const editing = computed(() => props.edit?.open === true)
</script>

<template>
  <div class="approval-card">
    <div class="approval-info">
      <span class="approval-tag">{{ $t('agent.tool_confirmation') }}</span>
      <span class="approval-name">{{ approval.toolName }}</span>
      <span
        v-if="remaining > 0"
        class="approval-countdown"
      >{{ $t('errors.auto_reject_in_n_s', { n: remaining }) }}</span>
    </div>
    <div class="approval-args">
      {{ approval.arguments }}
    </div>
    <div
      v-if="approval.error"
      class="approval-edit-error"
    >
      {{ approval.error }}
    </div>
    <div
      v-if="editing"
      class="approval-edit"
    >
      <textarea
        class="approval-edit-text"
        :value="edit?.text || ''"
        rows="5"
        spellcheck="false"
        :aria-label="$t('agent.edit_arguments')"
        @input="emit('edit-input', ($event.target as HTMLTextAreaElement).value)"
      />
      <div
        v-if="edit?.error"
        class="approval-edit-error"
      >
        {{ edit.error }}
      </div>
    </div>
    <div class="approval-actions">
      <button
        class="approval-btn danger"
        type="button"
        :disabled="!!approval.busy"
        @click="emit('reject')"
      >
        {{ $t('errors.reject') }}
      </button>
      <button
        class="approval-link"
        type="button"
        @click="emit('toggle-edit')"
      >
        {{ editing ? $t('common.cancel') : $t('agent.edit_arguments') }}
      </button>
      <button
        v-if="editing"
        class="approval-btn allow"
        type="button"
        :disabled="!!edit?.error"
        @click="emit('submit-edit')"
      >
        {{ $t('agent.approve_with_edited_arguments') }}
      </button>
      <button
        v-else
        class="approval-btn allow"
        type="button"
        :disabled="!!approval.busy"
        @click="emit('approve')"
      >
        {{ $t('common.allow_execution') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.approval-card {
  padding: 10px 14px;
  border: var(--hairline-width) solid var(--border);
  border-inline-start: 3px solid var(--primary);
  border-radius: var(--sig-radius-card);
  background: var(--bg-card);
}
.approval-info { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.approval-tag {
  padding: 2px 8px;
  border-radius: var(--sig-radius-card);
  background: var(--primary-bg);
  color: var(--primary);
  font-size: var(--fs-xs);
}
.approval-name { color: var(--text-primary); font-size: var(--fs-base); font-weight: var(--fw-semibold); }
.approval-countdown { margin-inline-start: auto; color: var(--warning); font-size: var(--fs-xs); }
.approval-args {
  margin-bottom: 8px;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  word-break: break-all;
}
.approval-edit { margin-bottom: 8px; }
.approval-edit-text {
  width: 100%;
  padding: 6px 8px;
  border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-subtle);
  color: var(--text-primary);
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  resize: vertical;
}
.approval-edit-error { margin-top: 4px; color: var(--danger); font-size: var(--fs-xs); line-height: 1.5; }
.approval-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.approval-btn {
  padding: 3px 12px;
  border: var(--hairline-width) solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  font-size: var(--fs-xs);
  cursor: pointer;
}
.approval-btn.danger { border-color: var(--danger); color: var(--danger); }
.approval-btn.allow { background: var(--primary); color: var(--on-solid); }
.approval-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.approval-btn:focus-visible,
.approval-link:focus-visible {
  outline: var(--focus-ring-width) solid var(--focus-ring-color);
  outline-offset: var(--focus-ring-offset);
}
.approval-link {
  border: 0;
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
}
.approval-link:hover { color: var(--primary); }
</style>
