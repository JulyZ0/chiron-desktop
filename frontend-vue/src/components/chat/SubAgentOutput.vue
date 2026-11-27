<script setup lang="ts">
/**
 * 子 Agent「输出」区：层级面包屑 + run 并列 tab + 结构化过程。
 *
 * 与主对话流共用 `ToolCallCard` / `ToolResultBlock` / `AskCard` —— 摘要、展开、
 * 失败配色只有一份实现，两个界面不会漂移。
 */
import { Spin } from 'ant-design-vue'
import type { SubagentRunView } from '../../api/subagent'
import SubAgentApprovalCard from './SubAgentApprovalCard.vue'
import AskCard from './AskCard.vue'
import ToolCallCard from './ToolCallCard.vue'
import ToolResultBlock from './ToolResultBlock.vue'
import type { SubagentOutputRow } from './subagentOutput'

export interface RunCrumbView {
  runId: string
  label: string
  known: boolean
}

const props = defineProps<{
  sessionId: string
  selectedRun: SubagentRunView | null
  selectedRunId: string
  outputTabs: SubagentRunView[]
  rows: SubagentOutputRow[]
  hiddenRows: number
  droppedNote: string
  breadcrumbs: RunCrumbView[]
  historyLoading: boolean
  canSteer: boolean
  steering: boolean
  pendingByRun: Record<string, number>
  approvalSubmitting: Set<string>
  approvalDecisions: Record<string, string>
  approvalErrors: Record<string, string>
  askAnswers: Record<string, string>
  askErrors: Record<string, string>
}>()

const steerText = defineModel<string>('steerText', { default: '' })

const emit = defineEmits<{
  (e: 'select-run', runId: string): void
  (e: 'back'): void
  (e: 'crumb', crumb: RunCrumbView): void
  (e: 'steer'): void
  (e: 'approval', payload: { toolCallId: string; approved: boolean; args?: string }): void
  (e: 'answer', payload: { toolCallId: string; value: string }): void
}>()

const TERMINAL = new Set(['completed', 'failed', 'lost', 'cancelled', 'partial'])
</script>

<template>
  <div class="output">
    <nav
      v-if="breadcrumbs.length"
      class="run-breadcrumb"
      data-testid="run-breadcrumb"
      :aria-label="$t('agent.subagent_breadcrumb_aria')"
    >
      <button
        type="button"
        class="crumb crumb-root"
        @click="emit('back')"
      >
        {{ $t('agent.subagent_breadcrumb_root') }}
      </button>
      <template
        v-for="(crumb, index) in breadcrumbs"
        :key="crumb.runId"
      >
        <span
          class="crumb-sep"
          aria-hidden="true"
        >›</span>
        <span
          v-if="index === breadcrumbs.length - 1"
          class="crumb crumb-current"
          aria-current="page"
        >{{ crumb.label }}</span>
        <button
          v-else
          type="button"
          class="crumb"
          :disabled="!crumb.known"
          :title="crumb.label"
          @click="emit('crumb', crumb)"
        >
          {{ crumb.label }}
        </button>
      </template>
    </nav>

    <div
      v-if="outputTabs.length > 1"
      class="output-tabs"
      role="tablist"
      :aria-label="$t('agent.sub_agent_view')"
    >
      <button
        v-for="run in outputTabs"
        :key="run.run_id"
        type="button"
        role="tab"
        class="output-tab"
        :class="{ active: run.run_id === selectedRunId }"
        :aria-selected="run.run_id === selectedRunId"
        :title="`${run.profile || run.run_id} · ${run.status}`"
        @click="emit('select-run', run.run_id)"
      >
        <span
          class="output-tab-dot"
          :data-state="TERMINAL.has(run.status) ? 'done' : 'live'"
          aria-hidden
        />
        <span class="output-tab-name">{{ run.profile || run.run_id.slice(0, 8) }}</span>
        <span
          v-if="pendingByRun[run.run_id]"
          class="output-tab-pending"
          :title="$t('agent.sub_agent_is_waiting_for_your_confirmation_or_answer')"
        >⚠{{ pendingByRun[run.run_id] > 1 ? pendingByRun[run.run_id] : '' }}</span>
      </button>
    </div>

    <template v-if="selectedRun">
      <div class="output-head">
        <span>{{ selectedRun.profile || selectedRun.run_id }}</span>
        <span class="output-meta">
          {{ $t('workflow.steps') }} {{ selectedRun.usage?.steps || 0 }}
        </span>
      </div>
      <div class="output-summary">
        {{ selectedRun.summary || $t('common.running_no_summary_yet') }}
      </div>
      <div
        v-if="canSteer"
        class="steer-box"
      >
        <input
          v-model="steerText"
          class="steer-input"
          type="text"
          maxlength="2000"
          :placeholder="$t('agent.subagent_steer_placeholder')"
          :disabled="steering"
          @keydown.enter.prevent="emit('steer')"
        >
        <button
          type="button"
          class="steer-send"
          :disabled="steering || !steerText.trim()"
          @click="emit('steer')"
        >
          {{ steering ? $t('agent.subagent_steer_sending') : $t('agent.subagent_steer_send') }}
        </button>
      </div>
      <Spin :spinning="historyLoading">
        <div
          v-if="hiddenRows"
          class="output-hidden"
        >
          {{ $t('common.n_more_items_above', { n: hiddenRows }) }}
        </div>
        <div class="stream">
          <template
            v-for="row in rows"
            :key="row.key"
          >
            <div
              v-if="row.kind === 'reasoning'"
              class="line reasoning"
            >
              <span class="line-tag">{{ $t('chat.reasoning') }}</span>{{ row.content }}
            </div>
            <div
              v-else-if="row.kind === 'text'"
              class="line text"
            >
              {{ row.content }}
            </div>
            <div
              v-else-if="row.kind === 'notice'"
              class="line notice"
            >
              {{ row.content }}
            </div>
            <div
              v-else-if="row.kind === 'tool'"
              class="line tool"
            >
              <ToolCallCard
                v-if="row.call"
                :item="row.call"
              />
              <ToolResultBlock
                v-if="row.result"
                :item="row.result"
                :session-id="sessionId"
              />
            </div>
            <div
              v-else-if="row.kind === 'approval'"
              class="line approval"
            >
              <SubAgentApprovalCard
                :tool-name="row.event.tool_name || ''"
                :args="row.event.tool_arguments || ''"
                :content="row.event.content || ''"
                :submitting="approvalSubmitting.has(row.event.tool_call_id || '')"
                :decision="approvalDecisions[row.event.tool_call_id || ''] || ''"
                :error="approvalErrors[row.event.tool_call_id || ''] || ''"
                @decide="(approved: boolean) => emit('approval', { toolCallId: row.event.tool_call_id || '', approved })"
                @edit="(args: string) => emit('approval', { toolCallId: row.event.tool_call_id || '', approved: true, args })"
              />
            </div>
            <div
              v-else-if="row.kind === 'ask'"
              class="line ask"
            >
              <AskCard
                :question="row.event.question || row.event.content || ''"
                :options="row.event.options || []"
                :allow-free-text="row.event.allow_free_text !== false"
                :expired="!!askErrors[row.event.tool_call_id || '']"
                @answer="(value: string) => emit('answer', { toolCallId: row.event.tool_call_id || '', value })"
              />
              <div
                v-if="askAnswers[row.event.tool_call_id || '']"
                class="ask-done"
              >
                {{ $t('chat.replied') }}：{{ askAnswers[row.event.tool_call_id || ''] }}
              </div>
              <div
                v-if="askErrors[row.event.tool_call_id || '']"
                class="approval-error"
              >
                {{ askErrors[row.event.tool_call_id || ''] }}
              </div>
            </div>
      
            <div
              v-else-if="row.kind === 'status'"
              class="line status"
            >
              {{ row.event.status }}<span v-if="row.event.truncated"> {{ $t('common.preview_truncated') }}</span>
            </div>
            <div
              v-else-if="row.kind === 'done' && droppedNote"
              class="line dropped"
            >
              {{ $t('chat.subagent_events_dropped', { n: droppedNote }) }}
            </div>
            <div
              v-else-if="row.kind === 'done'"
              class="line done"
            >
              {{ $t('common.end') }}：{{ row.event.status }}
              <span v-if="row.event.usage"> · ↑{{ row.event.usage.input_tokens || 0 }}/↓{{ row.event.usage.output_tokens || 0 }}</span>
            </div>
          </template>
          <div
            v-if="!rows.length"
            class="stream-empty"
          >
            {{ $t('errors.no_output_yet_running_or_process_expired') }}
          </div>
        </div>
      </Spin>
    </template>
    <div
      v-else
      class="sa-empty"
    >
      {{ $t('agent.select_a_sub_agent_in_run_first') }}
    </div>
  </div>
</template>

<style scoped>
.output { border-top: var(--hairline-width) solid var(--border-color, rgba(127, 127, 127, 0.24)); padding-top: 10px; }
.run-breadcrumb {
  display: flex; align-items: center; gap: 4px; flex-wrap: wrap;
  margin-bottom: 8px; font-size: var(--fs-xs); color: var(--text-secondary, #595959);
}
.run-breadcrumb .crumb {
  padding: 2px 4px; border: none; border-radius: var(--radius-sm);
  background: transparent; color: inherit; font-size: inherit; cursor: pointer;
}
.run-breadcrumb .crumb:hover:not(:disabled) { color: var(--primary-color, #1677ff); background: var(--bg-hover, rgba(0, 0, 0, 0.03)); }
.run-breadcrumb .crumb:disabled { cursor: default; opacity: 0.7; }
.run-breadcrumb .crumb-current { color: var(--text-primary); font-weight: var(--fw-medium); cursor: default; }
.run-breadcrumb .crumb-root { font-weight: var(--fw-medium); }
.run-breadcrumb .crumb-sep { color: var(--text-tertiary, #8c8c8c); flex: none; }
.output-head { display: flex; justify-content: space-between; font-weight: var(--fw-semibold); }
.output-meta { font-size: var(--fs-xs); color: var(--text-tertiary, #8c8c8c); }
.output-tabs {
  display: flex; gap: 4px; overflow-x: auto; padding: 6px 0;
}
.output-tab {
  display: flex; align-items: center; gap: 4px; flex: none;
  padding: 3px 8px; border: var(--hairline-width) solid var(--border-color, rgba(127, 127, 127, 0.24));
  border-radius: var(--radius-sm); background: transparent; color: var(--text-secondary, #595959);
  font-size: var(--fs-xs); cursor: pointer;
}
.output-tab:hover { background: var(--bg-hover); color: var(--text-primary); }
.output-tab.active { background: var(--primary-bg); border-color: var(--primary); color: var(--primary); }
.output-tab-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.output-tab-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text-tertiary, #8c8c8c); flex: none; }
.output-tab-dot[data-state='live'] { background: var(--primary); animation: sa-pulse var(--dur-pulse) ease-in-out infinite; }
@media (prefers-reduced-motion: reduce) { .output-tab-dot[data-state='live'] { animation: none; } }
.output-tab-pending { flex: none; color: var(--warning, #d97706); font-weight: var(--fw-semibold); }
.output-hidden { color: var(--text-tertiary, #8c8c8c); font-size: var(--fs-xs); padding: 4px 0; }
.output-summary {
  white-space: pre-wrap; color: var(--text-secondary, #595959); font-size: var(--fs-xs);
  margin: 4px 0 8px;
}
.stream { max-height: 320px; overflow: auto; display: flex; flex-direction: column; gap: 4px; }
.line { white-space: pre-wrap; line-height: 1.5; }
.line.reasoning { color: var(--text-tertiary, #8c8c8c); font-style: italic; }
.line.notice { color: var(--warning, var(--warning)); }
.line.status, .line.done { color: var(--text-secondary, #595959); font-size: var(--fs-xs); }
.line-tag {
  display: inline-block; margin-inline-end: 6px; padding: 0 4px;
  border-radius: var(--radius-sm); background: var(--bg-subtle); color: var(--text-tertiary);
  font-size: var(--fs-xs); font-style: normal;
}
.line.approval { width: 100%; }
.line.ask { width: 100%; }
.ask-done { margin-top: 6px; font-size: var(--fs-xs); color: var(--success, #16a34a); }
.approval-error { margin-top: 4px; font-size: var(--fs-xs); color: var(--danger, #dc2626); }
.stream-empty { color: var(--text-tertiary, #8c8c8c); font-size: var(--fs-xs); }
.sa-empty { padding: 10px 2px; color: var(--text-tertiary, #8c8c8c); font-size: var(--fs-xs); }
.steer-box { display: flex; gap: 6px; margin-bottom: 8px; }
.steer-input {
  flex: 1; min-width: 0; padding: 4px 8px;
  border: var(--hairline-width) solid var(--border-color, rgba(127, 127, 127, 0.24));
  border-radius: var(--radius-sm); background: var(--bg-card); color: var(--text-primary);
  font-size: var(--fs-xs);
}
.steer-input:focus-visible { outline: var(--focus-ring-width) solid var(--focus-ring-color); outline-offset: 1px; }
.steer-send {
  flex: none; padding: 4px 10px; border: none; border-radius: var(--radius-sm);
  background: var(--primary); color: var(--on-solid); font-size: var(--fs-xs); cursor: pointer;
}
.steer-send:disabled { opacity: .5; cursor: default; }
</style>
