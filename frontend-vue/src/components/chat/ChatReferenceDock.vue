<script setup lang="ts">
/**
 * 右侧对照 Dock：参考会话与子 Agent 共用一栏。
 *
 * 两者都是“并排读”的对照物，同时打开时不再各自占一条分割线、把主区挤窄；
 * 改为同一容器内的 tab。宽度仍由各自的 `useSplitWidth` 管理，切换 tab 时保留
 * 各自拖过的宽度。
 */
import { computed } from 'vue'
import type { ChatSession } from './chat-types'
import type { SubagentEvent } from '../../api/subagent'
import SessionPreviewPane from './SessionPreviewPane.vue'
import SubAgentPane from './SubAgentPane.vue'

const props = defineProps<{
  referenceOpen: boolean
  subagentOpen: boolean
  sessionId: string
  referenceSessionId: string
  referenceTitle: string
  sessions: ChatSession[]
  excludeSessionId: string
  handleSide: 'left' | 'right'
  liveEvents: SubagentEvent[]
  focusRunId: string
  focusSeq: number
}>()

const active = defineModel<'reference' | 'subagent'>('active', { default: 'reference' })

const emit = defineEmits<{
  (e: 'update:session-id', id: string): void
  (e: 'close-reference'): void
  (e: 'close-subagent'): void
  (e: 'retry-run', payload: { runId: string; text: string }): void
}>()

const both = computed(() => props.referenceOpen && props.subagentOpen)
const showReference = computed(
  () => props.referenceOpen && (!props.subagentOpen || active.value === 'reference'),
)
const showSubagent = computed(
  () => props.subagentOpen && (!props.referenceOpen || active.value === 'subagent'),
)
</script>

<template>
  <section class="reference-dock">
    <div
      v-if="both"
      class="dock-tabs"
      role="tablist"
      :aria-label="$t('common.split_view')"
    >
      <button
        type="button"
        role="tab"
        class="dock-tab"
        :class="{ active: active === 'reference' }"
        :aria-selected="active === 'reference'"
        @click="active = 'reference'"
      >
        {{ $t('chat.reference_session') }}
      </button>
      <button
        type="button"
        role="tab"
        class="dock-tab"
        :class="{ active: active === 'subagent' }"
        :aria-selected="active === 'subagent'"
        @click="active = 'subagent'"
      >
        {{ $t('agent.sub_agent') }}
      </button>
    </div>
    <SessionPreviewPane
      v-if="referenceOpen"
      v-show="showReference"
      :session-id="referenceSessionId"
      :title="referenceTitle"
      :sessions="sessions"
      :exclude-session-id="excludeSessionId"
      :handle-side="handleSide"
      @update:session-id="emit('update:session-id', $event)"
    />
    <SubAgentPane
      v-if="subagentOpen"
      v-show="showSubagent"
      :session-id="sessionId"
      :live-events="liveEvents"
      :handle-side="handleSide"
      :focus-run-id="focusRunId"
      :focus-seq="focusSeq"
      @retry-run="emit('retry-run', $event)"
      @close="emit('close-subagent')"
    />
  </section>
</template>

<style scoped>
.reference-dock {
  display: flex;
  flex-direction: column;
  min-width: 0;
  height: 100%;
  overflow: hidden;
}
.dock-tabs {
  flex: none;
  display: flex;
  gap: 2px;
  padding: 4px 8px;
  border-bottom: var(--hairline-width) solid var(--border-subtle);
  background: var(--bg-secondary);
}
.dock-tab {
  padding: 3px 10px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--fs-xs);
  cursor: pointer;
}
.dock-tab:hover { background: var(--bg-hover); color: var(--text-primary); }
.dock-tab.active { background: var(--primary-bg); color: var(--primary); }
/* 子面板自己的宽度/手柄继续生效；在 host 内改为纵向填满剩余高度。 */
.reference-dock :deep(> .preview-pane),
.reference-dock :deep(> .subagent-pane) {
  flex: 1 1 auto;
  min-height: 0;
  height: auto;
}
</style>
