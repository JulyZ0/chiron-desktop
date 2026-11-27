<script setup lang="ts">
/** 原始 subagent.* 事件流（未按语义分组），排查「到底发生了什么」时用。 */
import type { SubagentEvent } from '../../api/subagent'

defineProps<{ events: SubagentEvent[] }>()
</script>

<template>
  <div class="events">
    <div
      v-for="(e, i) in events"
      :key="(e.id as string) || i"
      class="ev-row"
    >
      <span class="ev-type">{{ e.type }}</span>
      <span class="ev-run">{{ e.run_id }}</span>
      <span class="ev-text">{{ e.content || '' }}</span>
    </div>
    <div
      v-if="!events.length"
      class="sa-empty"
    >
      {{ $t('agent.no_sub_agent_events_received_in_this_session_yet') }}
    </div>
  </div>
</template>

<style scoped>
.events { display: flex; flex-direction: column; gap: 2px; max-height: 320px; overflow: auto; }
.ev-row { display: grid; grid-template-columns: 132px 96px 1fr; gap: 6px; font-size: var(--fs-xs); }
.ev-type { color: var(--primary, #1677ff); font-family: var(--font-mono); }
.ev-run { color: var(--text-tertiary, #8c8c8c); font-family: var(--font-mono); }
.ev-text {
  color: var(--text-secondary, #595959);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sa-empty { padding: 10px 2px; color: var(--text-tertiary, #8c8c8c); font-size: var(--fs-xs); }
</style>
