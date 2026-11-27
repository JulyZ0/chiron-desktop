<script setup lang="ts">
/** 子 Agent 看板列视图（运行中 / 完成 / 失败），数据与列表完全同源。 */
import { Tag } from 'ant-design-vue'
import type { SubagentRunView } from '../../api/subagent'

export interface BoardColumn {
  id: string
  label: string
  runs: SubagentRunView[]
}

defineProps<{
  columns: BoardColumn[]
  cancelling: Set<string>
  rerunning: Set<string>
}>()

const emit = defineEmits<{
  (e: 'open', runId: string): void
  (e: 'stop', runId: string): void
  (e: 'rerun', run: SubagentRunView): void
}>()

const TERMINAL = new Set(['completed', 'failed', 'lost', 'cancelled', 'partial'])
const STATUS_COLOR: Record<string, string> = {
  queued: 'default',
  running: 'processing',
  reasoning: 'processing',
  responding: 'processing',
  tool: 'processing',
  retrying: 'warning',
  completed: 'success',
  failed: 'error',
}
</script>

<template>
  <div class="sa-board">
    <section
      v-for="col in columns"
      :key="col.id"
      class="sa-board-col"
    >
      <h4 class="sa-board-title">
        {{ $t(col.label) }}<span class="sa-board-count">{{ col.runs.length }}</span>
      </h4>
      <div
        v-for="run in col.runs"
        :key="run.run_id"
        class="sa-board-card"
        role="button"
        tabindex="0"
        :title="run.summary || run.run_id"
        @click="emit('open', run.run_id)"
        @keydown.enter.prevent="emit('open', run.run_id)"
      >
        <div class="sa-board-card-head">
          <span class="sa-board-name">{{ run.profile || run.run_id.slice(-6) }}</span>
          <Tag :color="STATUS_COLOR[run.status] || 'default'">
            {{ run.status }}
          </Tag>
        </div>
        <div class="sa-board-meta">
          {{ $t('workflow.steps') }} {{ run.usage?.steps || 0 }}
          · ↑{{ run.usage?.input_tokens || 0 }}/↓{{ run.usage?.output_tokens || 0 }}
        </div>
        <div class="sa-board-actions">
          <button
            v-if="!TERMINAL.has(String(run.status))"
            type="button"
            class="sa-board-btn"
            :disabled="cancelling.has(run.run_id)"
            @click.stop="emit('stop', run.run_id)"
          >{{ $t('common.stop') }}</button>
          <button
            v-else-if="run.status === 'failed' || run.status === 'lost'"
            type="button"
            class="sa-board-btn"
            :disabled="rerunning.has(run.run_id)"
            @click.stop="emit('rerun', run)"
          >{{ $t('common.retry') }}</button>
        </div>
      </div>
      <div
        v-if="!col.runs.length"
        class="sa-board-empty"
      >—</div>
    </section>
  </div>
</template>

<style scoped>
.sa-board { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.sa-board-col {
  min-width: 0; padding: 6px; border-radius: var(--radius-md);
  background: var(--surface-2, rgba(127, 127, 127, 0.06));
}
.sa-board-title {
  margin: 0 0 6px; font-size: var(--fs-xs); color: var(--text-tertiary);
  display: flex; align-items: center; justify-content: space-between;
}
.sa-board-count { font-family: var(--font-mono); }
.sa-board-card {
  padding: 6px 8px; margin-bottom: 6px; border-radius: var(--radius-sm);
  background: var(--bg-card); border: var(--hairline-width) solid var(--border-subtle);
  cursor: pointer;
}
.sa-board-card:focus-visible { outline: var(--focus-ring-width) solid var(--focus-ring-color); outline-offset: 1px; }
.sa-board-card-head { display: flex; align-items: center; justify-content: space-between; gap: 4px; }
.sa-board-name {
  font-size: var(--fs-xs); color: var(--text-primary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.sa-board-meta { margin-top: 2px; font-size: var(--fs-xs); color: var(--text-tertiary); }
.sa-board-actions { display: flex; justify-content: flex-end; margin-top: 4px; }
.sa-board-btn {
  border: none; background: transparent; color: var(--primary);
  font-size: var(--fs-xs); cursor: pointer; padding: 0 2px;
}
.sa-board-btn:disabled { opacity: .5; cursor: default; }
.sa-board-empty { font-size: var(--fs-xs); color: var(--text-tertiary); text-align: center; padding: 4px; }
</style>
