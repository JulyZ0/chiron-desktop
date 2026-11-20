<script setup lang="ts">
/**
 * 子 Agent 的**时间线卡片**（每个 run 一张）。
 *
 * 为什么不是表格行：子 Agent 的阅读顺序是「谁 → 在干什么 → 花了多少 → 要不要我处理」，
 * 而表格把前两件事压进固定列宽，摘要只能截断成半句话。卡片把摘要与最近输出各占一行，
 * 状态用左侧时间线圆点表达，失败整张高亮 —— 扫一眼就知道卡在哪。
 *
 * 三条硬约束：
 *  1. **纯展示**：不发请求、不持有计时器。耗时由父组件传入的统一 `now` 计算 ——
 *     每张卡自己 setInterval 会在 20 个 run 时变成 20 条定时器。
 *  2. **键盘可达**：整卡可 Tab → Enter/Space 选中；子按钮各自是 <button>。
 *  3. **不整表重渲染**：父组件按 run_id 复用本组件，事件更新只改 props。
 */
import { computed } from 'vue'
import { Tag } from 'ant-design-vue'
import { DownOutlined, RightOutlined } from '@ant-design/icons-vue'
import type { SubagentRunView } from '../../api/subagent'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()

const props = withDefaults(defineProps<{
  run: SubagentRunView
  depth?: number
  selected?: boolean
  /** 直接子节点数（>0 才显示折叠箭头） */
  childCount?: number
  /** 该 run 的子树是否被折叠 */
  collapsed?: boolean
  /** 最近一段实时输出（未终态时显示） */
  liveTail?: string
  /** 待处理（审批 / 提问）数量 */
  pending?: number
  /** 正在请求取消 */
  cancelling?: boolean
  /** 正在请求重跑（按钮禁用，避免连点派出多个作业） */
  rerunning?: boolean
  /** 统一时钟（毫秒）：只在有活跃 run 时由父组件每秒推进 */
  now?: number
}>(), {
  depth: 1,
  selected: false,
  childCount: 0,
  collapsed: false,
  liveTail: '',
  pending: 0,
  cancelling: false,
  rerunning: false,
  now: 0,
})

const emit = defineEmits<{
  (e: 'select'): void
  (e: 'toggle'): void
  (e: 'stop'): void
  (e: 'pending'): void
  /** 「重试」：父组件先尝试**真实重跑**（POST rerun），失败再回退到放回输入框 */
  (e: 'rerun'): void
}>()

const TERMINAL = new Set(['completed', 'failed', 'cancelled', 'killed', 'stopped', 'lost', 'done', 'partial'])

const terminal = computed(() => TERMINAL.has(String(props.run.status)))
const failed = computed(() => String(props.run.status) === 'failed')
/** 部分完成：有产物但不完整（撞上 wall/token 预算的典型终态）—— 同样值得重跑 */
const partial = computed(() => String(props.run.status) === 'partial')

const STATUS_COLOR: Record<string, string> = {
  queued: 'default',
  running: 'processing',
  reasoning: 'processing',
  responding: 'processing',
  tool: 'processing',
  retrying: 'warning',
  completed: 'success',
  failed: 'error',
  cancelled: 'default',
}

const statusColor = computed(() => STATUS_COLOR[String(props.run.status)] || 'default')
const name = computed(() => props.run.profile || props.run.run_id)
/**
 * 卡片摘要。
 *
 * **失败的 run 必须有可读摘要**：引擎会在失败时把上游原因写进 `summary`
 * （`app/subagent/reporting.py::failure_summary`），但历史行 / 运行期缓存里可能只有
 * `error`（Redis 缓存的 run 不带 summary）。此时回落到 `error`，而不是显示
 * "还没有摘要" —— 用户要的是"为什么 failed"，不是一句"没有摘要"。
 */
const summary = computed(() => {
  const text = (props.run.summary || '').trim()
  if (text) return text
  if (String(props.run.status) === 'failed') return (props.run.error || '').trim()
  return ''
})

/** 已运行时长：只有活跃 run 且拿到 created_at 时才算（终态没有结束时间，宁可不显示） */
const elapsedSec = computed(() => {
  if (terminal.value || !props.now || !props.run.created_at) return 0
  const start = new Date(props.run.created_at).getTime()
  if (Number.isNaN(start)) return 0
  const sec = Math.floor((props.now - start) / 1000)
  return sec > 0 ? sec : 0
})

const usage = computed(() => ({
  input: props.run.usage?.input_tokens || 0,
  output: props.run.usage?.output_tokens || 0,
  steps: props.run.usage?.steps || 0,
}))

/**
 * 是否撞上**预算墙**（卡片高亮 + 「预算超限」标记）。
 *
 * 依据只有后端给的权威结论（`budget_exceeded`）与引擎的终态证据（`partial` +
 * `budget_exceeded:<轴>`）：上限来自部署环境（`SUBAGENT_MAX_*`），前端看不到 ——
 * 拿一个写死的默认值去比 usage 会在高上限部署里误报、低上限部署里漏报。
 */
const budgetExceeded = computed(() => {
  if (props.run.budget_exceeded === true) return true
  if (String(props.run.status) === 'partial') return true
  return String(props.run.error || '').startsWith('budget_exceeded')
})

/** 缩进封顶：深层嵌套只留 3 档视觉差异，再深就靠折叠箭头说明层级 */
const indent = computed(() => 8 + Math.min(props.depth - 1, 3) * 14)

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Enter' && e.key !== ' ') return
  e.preventDefault()
  emit('select')
}
</script>

<template>
  <article
    class="run-card"
    :class="{ selected, running: !terminal, failed, terminal, 'budget-hit': budgetExceeded }"
    :style="{ marginInlineStart: `${indent}px` }"
    :aria-current="selected ? 'true' : undefined"
  >
    <!-- 左侧时间线：圆点颜色 = 状态；活跃时呼吸，失败时红 -->
    <span
      class="rail"
      aria-hidden="true"
    >
      <span class="dot" />
    </span>

    <div class="card-body">
      <header class="card-head">
        <button
          v-if="childCount"
          type="button"
          class="twisty"
          :aria-expanded="!collapsed"
          :aria-label="$t('agent.subagent_card_children_n', { n: childCount })"
          @click.stop="emit('toggle')"
        >
          <DownOutlined v-if="!collapsed" />
          <RightOutlined v-else />
        </button>
        <span
          v-else
          class="twisty placeholder"
          aria-hidden="true"
        />

        <Tag :color="statusColor">
          {{ run.status }}
        </Tag>

        <button
          type="button"
          class="run-name"
          :title="summary || name"
          @click="emit('select')"
          @keydown="onKeydown"
        >
          {{ name }}
        </button>

        <span
          v-if="elapsedSec"
          class="run-elapsed"
        >{{ $t('agent.subagent_card_elapsed', { s: elapsedSec }) }}</span>

        <span
          class="run-budget"
          :class="{ 'budget-hit': budgetExceeded }"
          :title="$t('agent.subagent_card_budget', { input: usage.input, output: usage.output, steps: usage.steps })"
        >
          ↑{{ usage.input }} · ↓{{ usage.output }}<template v-if="usage.steps"> · {{ usage.steps }}</template>
        </span>
        <!-- 预算超限：贴着 budget 数字出现 —— 那个数字为什么这么大，答案就在旁边 -->
        <Tag
          v-if="budgetExceeded"
          color="warning"
          class="budget-tag"
          :title="$t('agent.subagent_budget_exceeded_hint')"
        >
          {{ $t('agent.subagent_budget_exceeded') }}
        </Tag>

        <span class="card-actions">
          <!-- 待处理徽标：不点开这张卡也能看见（审批/提问在等） -->
          <button
            v-if="pending"
            type="button"
            class="run-approval"
            :title="$t('agent.sub_agent_is_waiting_for_your_confirmation_or_answer')"
            @click.stop="emit('pending')"
          >
            ⚠ {{ $t('common.pending') }}{{ pending > 1 ? ` ×${pending}` : '' }}
          </button>
          <button
            type="button"
            class="run-open"
            :aria-label="$t('agent.subagent_card_view')"
            :title="$t('agent.subagent_card_view')"
            @click.stop="emit('select')"
          >
            {{ $t('agent.subagent_card_view') }}
          </button>
          <!-- 失败重试：先**真的重跑**（后端 POST rerun，按原任务再派一次）；
               重跑被拒（409 非终态/无原任务、404 不属于本会话）时，父组件回退到
               "放回输入框"，由用户确认后重新发起 —— 不做假重发。 -->
          <button
            v-if="failed || partial"
            type="button"
            class="run-retry"
            :disabled="rerunning"
            :title="$t('agent.subagent_card_retry_hint')"
            @click.stop="emit('rerun')"
          >
            {{ rerunning ? '…' : $t('agent.subagent_card_retry') }}
          </button>
          <button
            v-if="!terminal"
            type="button"
            class="run-stop"
            :disabled="cancelling"
            :title="$t('agent.stop_this_sub_agent')"
            :aria-label="$t('agent.stop_this_sub_agent')"
            @click.stop="emit('stop')"
          >
            {{ cancelling ? '…' : '■' }}
          </button>
        </span>
      </header>

      <p
        v-if="summary"
        class="card-summary"
        :title="summary"
      >
        {{ summary }}
      </p>
      <p
        v-else
        class="card-summary muted"
      >
        {{ $t('agent.subagent_card_empty_summary') }}
      </p>

      <!-- 最近输出：活跃时是"它现在在做什么"，失败时是失败原因（同一处，换个语气） -->
      <p
        v-if="liveTail"
        class="card-tail"
        :class="{ 'is-error': failed }"
        :title="liveTail"
      >
        <span
          v-if="!terminal"
          class="tail-pulse"
          aria-hidden="true"
        />
        {{ liveTail }}
      </p>

      <footer class="card-foot">
        <span class="foot-depth">{{ $t('agent.subagent_card_depth', { n: depth }) }}</span>
        <span
          v-if="run.redacted_count"
          class="foot-redacted"
          :title="$t('common.sensitive_snippets_desensitized_before_storage')"
        >🔒{{ run.redacted_count }}</span>
        <span
          v-if="run.source"
          class="foot-source"
        >{{ run.source }}</span>
      </footer>
    </div>
  </article>
</template>

<style scoped>
.run-card {
  position: relative;
  display: flex;
  gap: 8px;
  padding: 8px 10px 8px 6px;
  border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-card);
  transition: border-color var(--dur-fast), background var(--dur-fast), box-shadow var(--dur-fast);
}
.run-card:hover { border-color: var(--primary); }
.run-card.selected {
  border-color: var(--primary);
  background: var(--primary-bg);
}
.run-card.failed {
  border-color: var(--error);
  background: var(--error-bg);
}
/* 预算超限：与 failed 分开配色 —— 它不是"出错"，而是"钱用完了"，用户的下一步动作不同 */
.run-card.budget-hit {
  border-color: var(--warning);
  background: var(--warning-bg, var(--bg-card));
}
.run-card:focus-within { box-shadow: 0 0 0 var(--focus-ring-width) var(--focus-ring-glow); }

/* 时间线：一条竖线 + 状态圆点。竖线用伪元素画，不额外占 DOM */
.rail {
  position: relative;
  flex: none;
  width: 12px;
  display: flex;
  justify-content: center;
  padding-top: 6px;
}
.rail::before {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  width: var(--hairline-width);
  background: var(--border);
}
.dot {
  position: relative;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-disabled);
  box-shadow: 0 0 0 3px var(--bg-card);
}
.run-card.running .dot { background: var(--primary); animation: dot-pulse var(--dur-pulse) ease-in-out infinite; }
.run-card.terminal .dot { background: var(--success); }
.run-card.failed .dot { background: var(--error); }

@keyframes dot-pulse {
  50% { opacity: 0.35; }
}

.card-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.card-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.twisty {
  flex: none;
  width: 16px;
  height: 16px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--text-tertiary);
  font-size: 9px;
  cursor: pointer;
  border-radius: var(--radius-sm);
}
.twisty:hover { color: var(--primary); }
.twisty:focus-visible { outline: var(--focus-ring-width) solid var(--focus-ring-color); }
.twisty.placeholder { cursor: default; }

/* 名字本身是按钮：整卡可键盘选中，而点击别的按钮不会误触 */
.run-name {
  min-width: 0;
  max-width: 160px;
  padding: 0;
  border: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 12px;
  font-weight: var(--fw-medium);
  text-align: start;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: pointer;
}
.run-name:hover { color: var(--primary); }
.run-name:focus-visible { outline: var(--focus-ring-width) solid var(--focus-ring-color); outline-offset: 1px; }

.run-elapsed { font-size: 11px; color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
.run-budget { font-size: 11px; color: var(--text-muted); font-variant-numeric: tabular-nums; }
.run-budget.budget-hit { color: var(--warning); font-weight: var(--fw-medium); }
.budget-tag { margin-inline-start: 2px; }

.card-actions { display: flex; align-items: center; gap: 4px; margin-inline-start: auto; }
.card-actions button {
  min-height: 22px;
  padding: 0 6px;
  border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-card);
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
  transition: border-color var(--dur-fast), color var(--dur-fast);
}
.card-actions button:hover { border-color: var(--primary); color: var(--primary); }
.card-actions button:focus-visible { outline: var(--focus-ring-width) solid var(--focus-ring-color); outline-offset: 1px; }
.card-actions button:disabled { opacity: 0.5; cursor: default; }
.run-approval { border-color: var(--warning) !important; color: var(--warning) !important; }
.run-retry { border-color: var(--error) !important; color: var(--error) !important; }
.run-stop { width: 22px; padding: 0 !important; }

.card-summary {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.card-summary.muted { color: var(--text-tertiary); font-style: italic; }

.card-tail {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.card-tail.is-error { color: var(--error); white-space: normal; -webkit-line-clamp: 2; display: -webkit-box; -webkit-box-orient: vertical; }
.tail-pulse {
  flex: none;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--primary);
  animation: dot-pulse var(--dur-pulse) ease-in-out infinite;
}

.card-foot { display: flex; align-items: center; gap: 8px; font-size: 10px; color: var(--text-muted); }

@media (prefers-reduced-motion: reduce) {
  .run-card, .card-actions button { transition: none; }
  .dot, .tail-pulse { animation: none; }
}
</style>
