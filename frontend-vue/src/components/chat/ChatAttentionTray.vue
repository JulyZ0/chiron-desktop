<script setup lang="ts">
/**
 * “需要你处理”的票据区：审批 + 结构化提问。
 *
 * 刻意只聚合这两类**会阻塞执行链**的票据；待办清单和跨进程收件箱仍由各自组件
 * 负责（前者是任务进度，后者是历史对账，紧急度与交互都不同）。
 *
 * 组件只做展示与事件转发：票据归属、提交状态、倒计时、编辑态都由 ChatView 持有，
 * 避免第二份“能不能放行”的判断。
 */
import type { ApprovalEditState } from '../../composables/useApprovalEdit'
import type { ScopedApproval, ScopedQuestion } from './interactionScope'
import ApprovalCard from './ApprovalCard.vue'
import AskCard from './AskCard.vue'

const props = defineProps<{
  approvals: readonly ScopedApproval[]
  questions: readonly ScopedQuestion[]
  /** 剩余秒数（父组件的单一时钟） */
  remainingOf: (approval: ScopedApproval) => number
  /** 该票的编辑态；未打开编辑时为 undefined */
  editOf: (approval: ScopedApproval) => ApprovalEditState | undefined
}>()

const emit = defineEmits<{
  (e: 'approve', approval: ScopedApproval): void
  (e: 'reject', approval: ScopedApproval): void
  (e: 'toggle-edit', approval: ScopedApproval): void
  (e: 'edit-input', approval: ScopedApproval, value: string): void
  (e: 'submit-edit', approval: ScopedApproval): void
  (e: 'answer', question: ScopedQuestion, value: string): void
  (e: 'decline', question: ScopedQuestion): void
  (e: 'drafting', question: ScopedQuestion, active: boolean): void
}>()
</script>

<template>
  <div
    v-if="approvals.length || questions.length"
    class="attention-tray"
  >
    <div
      v-if="approvals.length"
      class="attention-zone"
    >
      <ApprovalCard
        v-for="a in approvals"
        :key="a.id"
        :approval="a"
        :remaining="remainingOf(a)"
        :edit="editOf(a)"
        @approve="emit('approve', a)"
        @reject="emit('reject', a)"
        @toggle-edit="emit('toggle-edit', a)"
        @edit-input="emit('edit-input', a, $event)"
        @submit-edit="emit('submit-edit', a)"
      />
    </div>
    <div
      v-if="questions.length"
      class="attention-zone"
    >
      <AskCard
        v-for="q in questions"
        :key="q.id"
        :question="q.question"
        :options="q.options"
        :allow-free-text="q.allowFreeText"
        :expired="!!q.expired"
        @answer="(value: string) => emit('answer', q, value)"
        @decline="emit('decline', q)"
        @drafting="(active: boolean) => emit('drafting', q, active)"
      />
    </div>
  </div>
</template>

<style scoped>
.attention-tray {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0 20px 8px;
}
.attention-zone {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
@media (max-width: 768px) {
  .attention-tray { padding: 0 12px 8px; }
}
</style>
