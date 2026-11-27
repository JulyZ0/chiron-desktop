<script setup lang="ts">
/** 子 Agent 用量表（按 run 粒度）。会话语义在「统计」浮层，两者不重复。 */
import { Tag } from 'ant-design-vue'
import type { SubagentRunView } from '../../api/subagent'

defineProps<{ runs: SubagentRunView[] }>()

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
  <div class="usage">
    <div
      v-if="runs.length"
      class="usage-grid"
    >
      <div class="usage-row usage-head">
        <span>{{ $t('common.run') }}</span>
        <span>{{ $t('common.status') }}</span>
        <span class="num">in</span>
        <span class="num">out</span>
        <span class="num">{{ $t('workflow.steps') }}</span>
      </div>
      <div
        v-for="run in runs"
        :key="run.run_id"
        class="usage-row"
      >
        <span class="u-name">{{ run.profile || run.run_id }}</span>
        <Tag :color="STATUS_COLOR[run.status] || 'default'">
          {{ run.status }}
        </Tag>
        <span class="num">{{ run.usage?.input_tokens || 0 }}</span>
        <span class="num">{{ run.usage?.output_tokens || 0 }}</span>
        <span class="num">{{ run.usage?.steps || 0 }}</span>
      </div>
    </div>
    <div
      v-else
      class="sa-empty"
    >
      {{ $t('agent.no_sub_agent_has_run_yet_so_there_is_no_usage_to_compute') }}
    </div>
    <p class="usage-note">
      {{ $t('agent.sub_agents_consume_tokens_independently_cost_multiplies_when_several_run_in_parallel') }}
    </p>
  </div>
</template>

<style scoped>
.usage-grid { display: flex; flex-direction: column; }
.usage-row {
  display: grid;
  grid-template-columns: 1fr auto 56px 56px 48px;
  align-items: center;
  gap: 6px;
  padding: 4px 2px;
  border-bottom: var(--hairline-width) solid var(--border-color, rgba(127, 127, 127, 0.12));
  font-size: var(--fs-xs);
}
.usage-head { color: var(--text-tertiary, #8c8c8c); font-size: var(--fs-xs); }
.usage-row .num { text-align: end; font-variant-numeric: tabular-nums; }
.u-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.usage-note { margin: 10px 0 0; color: var(--text-tertiary, #8c8c8c); font-size: var(--fs-xs); }
.sa-empty { padding: 10px 2px; color: var(--text-tertiary, #8c8c8c); font-size: var(--fs-xs); }
</style>
