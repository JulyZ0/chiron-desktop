<!-- Unified CallChain Timeline — 聊天界面底部折叠面板 -->
<template>
  <div
    v-if="isVisible"
    class="callchain-timeline"
  >
    <CollapseTransition>
      <div
        class="timeline-header"
        role="button"
        tabindex="0"
        :aria-expanded="isExpanded"
        @click="toggle"
        @keydown.enter.prevent="toggle"
        @keydown.space.prevent="toggle"
      >
        <span class="header-title">
          <svg
            width="16"
            height="16"
            viewBox="0 0 16 16"
            fill="none"
          >
            <path
              d="M8 1v14M1 8h14"
              stroke="currentColor"
              stroke-width="2"
            />
          </svg>
          {{ $t('agent.tool_call_chain_n_tools_ms_ms_total', { n: spanCount, ms: totalDurationMs }) }}
        </span>
        <!-- 差距C：时间线 / 执行图切换（同一份 trace 的两种视图） -->
        <button
          class="view-toggle"
          :title="$t('chat.trace_view_toggle_tooltip')"
          @click.stop="viewMode = viewMode === 'graph' ? 'timeline' : 'graph'"
        >
          {{ viewMode === 'graph' ? $t('chat.trace_view_timeline') : $t('chat.trace_view_graph') }}
        </button>
        <span class="header-icon">{{ isExpanded ? '▼' : '▶' }}</span>
      </div>

      <div
        v-if="isExpanded"
        ref="bodyRef"
        class="timeline-body"
        data-testid="trace-body"
        :data-follow="stickToTail ? 'true' : 'false'"
        @scroll.passive="onScroll"
      >
        <!-- 差距C：执行图（父子连通）。时间线回答"有哪些步骤"，执行图回答"从哪来"。 -->
        <div
          v-if="viewMode === 'graph'"
          class="graph-body"
        >
          <div
            v-for="(row, idx) in graphRows"
            :key="idx"
            class="graph-node"
            :style="{ paddingInlineStart: (row.depth * 18 + 8) + 'px' }"
          >
            <span class="graph-kind">{{ row.node.kind || 'span' }}</span>
            <span class="node-name">{{ getSpanDisplayName(row.node) }}</span>
            <span class="node-duration">{{ durationLabel(row.node) }}</span>
          </div>
          <div
            v-if="!graphRows.length"
            class="graph-empty"
          >
            {{ $t('chat.trace_graph_empty') }}
          </div>
        </div>
        <!-- 时间线渲染（G10 轮次折叠 + G12 尾部窗口） -->
        <template v-else>
          <!-- G10：轮数超上限时的折叠提示。修前：整份 trace 一次渲染，没有上限也没有提示。 -->
          <div
            v-if="hiddenTurnCount > 0"
            class="cap-notice"
            data-testid="trace-cap-notice"
          >
            <span class="cap-text">{{ $t('chat.trace_capped_recent_turns', { n: DEFAULT_VISIBLE_TURNS, total: turnGroups.length }) }}</span>
            <button
              type="button"
              class="cap-action"
              data-testid="trace-show-all"
              @click="showAllTurns = true"
            >
              {{ $t('chat.trace_show_all_turns', { total: turnGroups.length }) }}
            </button>
          </div>
          <div
            v-else-if="showAllTurns && turnGroups.length > DEFAULT_VISIBLE_TURNS"
            class="cap-notice"
          >
            <button
              type="button"
              class="cap-action"
              data-testid="trace-collapse-recent"
              @click="showAllTurns = false"
            >
              {{ $t('chat.trace_collapse_to_recent', { n: DEFAULT_VISIBLE_TURNS }) }}
            </button>
          </div>

          <!-- G12：向前翻页入口。修前：没有分页，历史越长渲染越多。 -->
          <button
            v-if="hasEarlierRows"
            type="button"
            class="load-earlier"
            data-testid="trace-load-earlier"
            @click="loadEarlier"
          >
            {{ $t('chat.trace_load_earlier', { n: earlierRowPageSize }) }}
          </button>

          <div
            v-for="(span, idx) in windowedRows"
            :key="rowKey(span, idx)"
            class="timeline-node"
            data-testid="trace-row"
            :data-span-name="span.span_name"
          >
            <div
              class="node-line"
              :class="getNodeClass(span)"
            >
              <span class="node-icon">{{ getNodeIcon(span) }}</span>
              <div class="node-content">
                <div class="node-header">
                  <span class="node-name">{{ getSpanDisplayName(span) }}</span>
                  <!-- 进行中的 span 只标"已开始"：修前照抄后端 0 值渲染成 "0ms"，那是虚构耗时 -->
                  <span
                    class="node-duration"
                    data-testid="trace-duration"
                  >{{ durationLabel(span) }}</span>
                </div>

                <!-- 展开详情 -->
                <div
                  v-if="span.showDetails"
                  class="node-details"
                >
                  <pre class="metadata-json">{{ formatMetadata(span.metadata) }}</pre>
                </div>
              </div>

              <button
                class="details-toggle"
                @click.stop="span.showDetails = !span.showDetails"
              >
                {{ span.showDetails ? $t('common.collapse') : $t('common.details') }}
              </button>
            </div>
          </div>

          <div
            v-if="!windowedRows.length"
            class="graph-empty"
            data-testid="trace-empty"
          >
            {{ $t('chat.trace_timeline_empty') }}
          </div>

          <!-- 完成事件 -->
          <div
            v-if="windowedRows.length"
            class="timeline-node complete"
          >
            <CheckCircleOutlined style="color: var(--success)" />
            <span>{{ $t('chat.inference_complete_total_ms_ms', { ms: totalDurationMs }) }}</span>
          </div>

          <!-- 跟随尾部哨兵：流式新增行时滚到这里（不直接写 scrollTop，滚动写入归 transcriptViewport） -->
          <div
            ref="tailRef"
            class="tail-sentinel"
            aria-hidden="true"
          />
        </template>
      </div>
    </CollapseTransition>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import { CheckCircleOutlined } from '@ant-design/icons-vue'
import CollapseTransition from '@/components/CollapseTransition.vue'
import { api } from '../api'
import { useI18n } from 'vue-i18n'
import {
  DEFAULT_PAGE_SIZE,
  DEFAULT_VISIBLE_TURNS,
  capToRecentTurns,
  groupSpansByTurn,
  isInProgressSpan,
  sumTopLevelDurationMs,
  tailWindow,
} from './chat/traceWindow'

const { t } = useI18n()

interface TraceSpan {
  trace_id: string
  span_name: string
  /** 进行中的 span 没有时长（后端可能给 null，也可能整字段缺席） */
  duration_ms?: number | null
  timestamp: string
  tenant_id: number
  metadata?: Record<string, unknown>
  showDetails?: boolean
  // 差距C（执行图）：稳定的节点 id / 父指针 / 类别。
  span_id?: string
  parent_span_id?: string
  kind?: string
  // G10/G12：轮次归属与状态（老数据可能没有）
  turn_id?: string
  turn_index?: number
  status?: string
}

/** 执行图节点（含子节点，递归）。 */
interface TraceGraphNode extends TraceSpan {
  children?: TraceGraphNode[]
}

const props = defineProps<{
  /** 兼容旧用法：直接传入 spans */
  visible?: boolean
  spans?: TraceSpan[]
  /** 新用法：传 traceId 由组件自行拉取 GET /v1/traces/{trace_id} */
  traceId?: string
  tenantId?: string | number
}>()

const emit = defineEmits<{
  (e: 'select', span: TraceSpan): void
}>()

const fetchedSpans = ref<TraceSpan[]>([])
const fetching = ref(false)

// traceId 模式：拉取该 trace 的全部 span
watch(() => props.traceId, async (id) => {
  if (!id) {
    fetchedSpans.value = []
    return
  }
  fetching.value = true
  try {
    const response = await api.get(`/v1/traces/${encodeURIComponent(id)}`)
    const payload = response.data?.data ?? response.data
    fetchedSpans.value = (payload?.spans || []).map((s: TraceSpan) => ({ ...s, showDetails: false }))
  } catch {
    fetchedSpans.value = []
  } finally {
    fetching.value = false
  }
}, { immediate: true })

// 展示用的 spans：traceId 模式用拉取结果，否则用传入的 spans
const displaySpans = computed<TraceSpan[]>(() =>
  props.traceId ? fetchedSpans.value : (props.spans || [])
)

const isVisible = computed(() =>
  props.traceId ? displaySpans.value.length > 0 : !!props.visible
)

const isExpanded = ref(false)

function toggle() {
  isExpanded.value = !isExpanded.value
}

// ── 差距C：执行图视图 ──
//
// 时间线回答"这次有哪些步骤"（扁平），执行图回答"某一步是从哪来的"（父子连通）。
// 两者是同一次 trace 的两种视图，因此放在同一组件里切换，而不是两个入口。
const viewMode = ref<'timeline' | 'graph'>('timeline')
const graphRoots = ref<TraceGraphNode[]>([])

watch([() => props.traceId, viewMode], async ([id, mode]) => {
  if (!id || mode !== 'graph') return
  try {
    const response = await api.get(`/v1/traces/${encodeURIComponent(id)}/graph`)
    const payload = response.data?.data ?? response.data
    graphRoots.value = payload?.roots || []
  } catch {
    graphRoots.value = []
  }
}, { immediate: true })

interface GraphRow { node: TraceGraphNode; depth: number }

/** 把树压成"带缩进的行" —— 避免为 v-for 引入递归组件（渲染更稳、更易测）。 */
function flatten(nodes: TraceGraphNode[], depth = 0, out: GraphRow[] = []): GraphRow[] {
  for (const n of nodes) {
    out.push({ node: n, depth })
    if (n.children?.length) flatten(n.children, depth + 1, out)
  }
  return out
}

const graphRows = computed<GraphRow[]>(() => flatten(graphRoots.value))

const spanCount = computed(() => displaySpans.value.filter(s => s.span_name.startsWith('tool:')).length)

/**
 * 总耗时：只累加顶层 span。
 * 修前：`reduce((sum, s) => sum + s.duration_ms, 0)` —— 父 span 已含子 span，父子重复计入。
 */
const totalDurationMs = computed(() => sumTopLevelDurationMs(displaySpans.value))

// ── G10：按轮折叠 ──
//
// 修前：整份 trace 一次性 `v-for` 渲染，40 轮任务要渲染上千行。
// 现在默认只展开最近 DEFAULT_VISIBLE_TURNS 轮，并给出"只看最近 N 轮"的提示 + 展开全部。
const showAllTurns = ref(false)

const turnGroups = computed(() => groupSpansByTurn(displaySpans.value))

const cappedTurns = computed(() =>
  showAllTurns.value
    ? { groups: turnGroups.value, hiddenTurns: 0 }
    : capToRecentTurns(turnGroups.value, DEFAULT_VISIBLE_TURNS)
)

const hiddenTurnCount = computed(() => cappedTurns.value.hiddenTurns)

// ── G12：尾部窗口 + 向前翻页 ──
//
// 单轮也可能有上百次工具调用：窗口只挂载尾部 N 页，翻页时向前追加一页。
// 挂载行数因此与"加载了几页"成正比，不随历史线性增长。
const pages = ref(1)

const scopedSpans = computed<TraceSpan[]>(() => cappedTurns.value.groups.flatMap(g => g.spans))

const tailResult = computed(() => tailWindow(scopedSpans.value, DEFAULT_PAGE_SIZE, pages.value))

const windowedRows = computed<TraceSpan[]>(() => tailResult.value.rows)

const hasEarlierRows = computed(() => tailResult.value.hasMore)

const earlierRowPageSize = computed(() => Math.min(DEFAULT_PAGE_SIZE, tailResult.value.hiddenCount))

function loadEarlier() {
  pages.value += 1
}

// 换 trace（或 spans 换成另一份）时回到"最近 N 轮 + 第一页"，避免沿用上一条 trace 的翻页深度
watch(() => props.traceId, () => {
  pages.value = 1
  showAllTurns.value = false
  stickToTail.value = true
})

// ── G12：流式跟随尾部（用户上滑即停止） ──
const bodyRef = ref<HTMLElement | null>(null)
const tailRef = ref<HTMLElement | null>(null)
const stickToTail = ref(true)
/** 距底多少像素内仍算"在尾部"：给亚像素与滚动惯性留余量。 */
const FOLLOW_THRESHOLD_PX = 24

function onScroll() {
  const el = bodyRef.value
  if (!el) return
  const distance = el.scrollHeight - el.clientHeight - el.scrollTop
  // 修前：没有"跟随"概念，流式新增行只能用户自己往下滚
  stickToTail.value = distance <= FOLLOW_THRESHOLD_PX
}

/**
 * 跟随尾部：滚到哨兵处，而不是直接写 `scrollTop`。
 * 滚动位置的单一写者是 `transcriptViewport`（见 scripts/check-transcript-scroll.mjs），
 * 这里的目标容器是轨迹窗格自己的滚动框，用 scrollIntoView 表达"停在末尾"这个意图。
 */
async function followTail() {
  if (!stickToTail.value) return
  await nextTick()
  const el = tailRef.value
  if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'end' })
}

watch([() => windowedRows.value.length, isExpanded], () => { void followTail() })
watch(stickToTail, () => { void followTail() })

function rowKey(span: TraceSpan, idx: number): string {
  return span.span_id || `${span.span_name}#${tailResult.value.start + idx}`
}

function durationLabel(span: TraceSpan | TraceGraphNode): string {
  if (isInProgressSpan(span)) return t('chat.trace_row_in_progress')
  const ms = Number(span.duration_ms ?? 0)
  return `${Number.isFinite(ms) ? Math.round(ms) : 0}ms`
}

function getNodeClass(span: TraceSpan): string {
  if (span.span_name === 'llm_call') return 'type-llm'
  if (span.span_name.startsWith('tool:')) return 'type-tool'
  if (span.span_name.includes('workflow')) return 'type-workflow'
  return 'type-default'
}

function getNodeIcon(span: TraceSpan): string {
  if (span.span_name === 'llm_call') return '🧠'
  if (span.span_name.startsWith('tool:')) {
    const toolName = span.span_name.replace('tool:', '')
    const iconMap: Record<string, string> = {
      read_file: '📄',
      write_file: '✏️',
      shell_exec: '💻',
      grep_files: '🔍',
      workflow_run: '⚙️',
    }
    return iconMap[toolName] || '🔧'
  }
  return '📊'
}

function getSpanDisplayName(span: TraceSpan): string {
  if (span.span_name === 'llm_call') {
    const model = span.metadata?.model || 'unknown'
    const inputTokens = span.metadata?.input_tokens || 0
    return t('agent.llm_call_model_tokens_tokens', { model, tokens: inputTokens })
  }
  if (span.span_name.startsWith('tool:')) {
    const toolName = span.span_name.replace('tool:', '')
    return capitalize(toolName.replace(/_/g, ' '))
  }
  return span.span_name
}

function formatMetadata(metadata?: Record<string, unknown>): string {
  if (!metadata) return '{}'
  return JSON.stringify(metadata, null, 2)
}

function capitalize(str: string): string {
  return str.charAt(0).toUpperCase() + str.slice(1)
}
</script>

<style scoped>
/* 差距C：时间线 / 执行图 切换与执行图渲染 */
.view-toggle {
  margin-inline-start: 8px;
  padding: 1px 8px;
  font-size: var(--fs-xs);
  border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-md);
  background: transparent;
  cursor: pointer;
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.view-toggle:hover { color: var(--primary); }
.graph-body {
  padding: 6px 0;
}
.graph-node {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 8px;
  font-size: var(--fs-sm);
}
.graph-kind {
  font-size: var(--fs-xs);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0 4px;
}
.graph-empty {
  padding: 8px;
  font-size: var(--fs-sm);
}
/* G10/G12：折叠提示与向前翻页入口 */
.cap-notice {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
  padding: 6px 10px;
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  background: var(--bg-secondary);
  border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-md);
}
.cap-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cap-action,
.load-earlier {
  flex-shrink: 0;
  padding: 2px 8px;
  font-size: var(--fs-sm);
  color: var(--primary);
  background: none;
  border: var(--hairline-width) solid var(--primary);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background-color var(--dur-fast) ease, color var(--dur-fast) ease;
}
.cap-action:hover,
.load-earlier:hover {
  background: var(--primary);
  color: var(--on-solid);
}
.load-earlier {
  display: block;
  width: 100%;
  margin-bottom: 8px;
  padding: 4px 8px;
}
.tail-sentinel {
  height: 1px;
}
.callchain-timeline {
  margin-top: 12px;
  border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  background: var(--bg-card);
}

.timeline-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 16px;
  background: var(--bg-secondary);
  cursor: pointer;
  user-select: none;
  transition: background var(--dur-fast) ease;
}

.timeline-header:hover {
  background: var(--bg-hover);
}

.header-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-sm);
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
}

.header-icon {
  font-size: var(--fs-sm);
  color: var(--text-tertiary);
}

.timeline-body {
  padding: 12px;
  max-height: 400px;
  overflow-y: auto;
}

.timeline-node {
  display: flex;
  align-items: center;
  margin-bottom: 8px;
}

.complete {
  color: var(--success);
  font-weight: var(--fw-medium);
}

.node-line {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  background: var(--bg-card);
  border: var(--hairline-width) solid var(--border);
  border-inline-start: 3px solid var(--border);
  border-radius: var(--radius-lg);
  transition: background var(--dur-fast) ease, border-color var(--dur-fast) ease;
}

.node-line:hover {
  background: var(--bg-hover);
}

.node-line.type-llm {
  border-inline-start-color: var(--node-violet);
}

.node-line.type-tool {
  border-inline-start-color: var(--info);
}

.node-line.type-workflow {
  border-inline-start-color: var(--node-pink);
}

.node-icon {
  font-size: var(--fs-xl);
}

.node-content {
  flex: 1;
  min-width: 0;
}

.node-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}

.node-name {
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--text-primary);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.node-duration {
  font-size: var(--fs-sm);
  color: var(--text-tertiary);
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
}

.node-details {
  margin-top: 8px;
}

.metadata-json {
  background: var(--bg-secondary);
  padding: 8px 12px;
  border-radius: var(--radius-md);
  font-family: var(--font-mono);
  font-size: var(--fs-sm);
  line-height: 1.5;
  overflow-x: auto;
  max-height: 200px;
  overflow-y: auto;
  color: var(--text-secondary);
  /* 这里的内容是工具入参（模型 ID / 无空格路径 / JSON），用 anywhere —— break-all
     会把正常英文单词也拦腰截断（见 style.css 的 .wrap-anywhere 注） */
  overflow-wrap: anywhere;
  word-break: normal;
}

.details-toggle {
  padding: 4px 10px;
  font-size: var(--fs-sm);
  color: var(--primary);
  background: none;
  border: var(--hairline-width) solid var(--primary);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background var(--dur-fast) ease, color var(--dur-fast) ease;
  flex-shrink: 0;
}

.details-toggle:hover {
  background: var(--primary);
  color: var(--on-solid);
}

.details-toggle:focus-visible,
.timeline-header:focus-visible,
.cap-action:focus-visible,
.load-earlier:focus-visible {
  outline: var(--focus-ring-width) solid var(--focus-ring-color);
  outline-offset: var(--focus-ring-offset);
}

/* 移动端：行内元素压缩，详情按钮触控目标放大 */
@media (max-width: 768px) {
  .timeline-body { padding: 10px; }
  .node-line { gap: 8px; padding: 8px 10px; }
  .details-toggle { min-height: 32px; }
  .cap-action, .load-earlier { min-height: 32px; }
}

@media (prefers-reduced-motion: reduce) {
  .timeline-header,
  .node-line,
  .details-toggle,
  .cap-action,
  .load-earlier {
    transition: none;
  }
}
</style>
