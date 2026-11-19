<script setup lang="ts">
/**
 * 子 Agent **对照栏**（分屏）—— 取代此前的右下角浮卡。
 *
 * ## 为什么是"栏"而不是"浮卡"
 *
 * 子 Agent 的产出天然是**要与主对话并排比对**的东西：它改了什么、做了什么决定、
 * 与主 Agent 的结论差在哪。浮卡（限高到输入区顶部、看完即关）表达不了这种并置关系，
 * 还会盖住正文。这里把它做成和「参考另一个会话」同一类东西：布局里的一栏，
 * 可拖宽度、可一直开着、滚自己的。
 *
 * ## 与 SessionPreviewPane 的区别
 *
 * - 参考栏比的是**另一个会话**（`MessageList` 只读回放）；
 * - 本栏看的是**当前会话的子 Agent 运行**（实时事件 + 事件端点回放）。
 * 两者都是对照，所以共用同一套分栏手感（`useSplitWidth`），但宽度键各自独立 ——
 * 同时打开时用户可以分别调宽窄。
 */
import { computed } from 'vue'
import type { SubagentEvent } from '../../api/subagent'
import SubAgentPanel from './SubAgentPanel.vue'
import { SUBAGENT_DOCK_WIDTH_KEY } from './useSubAgentDock'
import { useSplitWidth } from './useSplitWidth'

const props = withDefaults(defineProps<{
  /** 当前会话 id；为空时面板提示"无会话" */
  sessionId?: string
  /** 父组件累积的实时 `subagent.*` 事件 */
  liveEvents?: SubagentEvent[]
  /** 手柄贴哪条边 —— 由父级布局决定（`is-swapped` 会把对照栏换到右侧） */
  handleSide?: 'left' | 'right'
  /** 外部请求定位的 run（点对话流里的子 Agent 工具卡时带上） */
  focusRunId?: string
  /** 请求序号：连点同一个 run 也要能重新定位 */
  focusSeq?: number
}>(), { sessionId: '', liveEvents: () => [], handleSide: 'right', focusRunId: '', focusSeq: 0 })

const emit = defineEmits<{ (e: 'close'): void }>()

const {
  paneRef,
  resizing,
  paneStyle,
  onResizeStart,
  onResizeMove,
  onResizeEnd,
  onResizeKey,
  resetWidth,
} = useSplitWidth({
  storageKey: SUBAGENT_DOCK_WIDTH_KEY,
  handleSide: () => props.handleSide,
})

const runningCount = computed(() => {
  const active = new Set<string>()
  for (const e of props.liveEvents) {
    if (!e?.run_id) continue
    if (e.type === 'subagent.done') active.delete(e.run_id)
    else active.add(e.run_id)
  }
  return active.size
})
</script>

<template>
  <aside
    ref="paneRef"
    class="subagent-pane"
    :class="{ resizing }"
    :style="paneStyle"
  >
    <!-- 分栏手柄：与参考栏同一条手感（拖动改宽、双击重置、←/→ 微调） -->
    <div
      class="sp-resize"
      :class="`handle-${handleSide}`"
      role="separator"
      aria-orientation="vertical"
      :aria-label="$t('auth.drag_to_resize_double_click_to_reset')"
      tabindex="0"
      @pointerdown="onResizeStart"
      @pointermove="onResizeMove"
      @pointerup="onResizeEnd"
      @pointercancel="onResizeEnd"
      @dblclick="resetWidth"
      @keydown="onResizeKey"
    />
    <header class="sp-head">
      <span class="sp-title">{{ $t('agent.sub_agent_dock') }}</span>
      <span
        v-if="runningCount"
        class="sp-running"
        :title="$t('agent.sub_agent_is_running')"
      >{{ runningCount }}</span>
      <button
        type="button"
        class="sp-close"
        :title="$t('common.close')"
        @click="emit('close')"
      >
        ✕
      </button>
    </header>
    <div class="sp-body">
      <SubAgentPanel
        :session-id="sessionId"
        :live-events="liveEvents"
        :focus-run-id="focusRunId"
        :focus-seq="focusSeq"
        hide-header
      />
    </div>
  </aside>
</template>

<style scoped>
/* 结构与 .preview-pane 对齐：自己滚动、不把主会话的滚动位置带跑 */
.subagent-pane {
  position: relative;
  display: flex; flex-direction: column; min-width: 0; height: 100%;
  border-inline-start: var(--hairline-width) solid var(--border-subtle);
  overflow: hidden;
}
.sp-head {
  flex: none; display: flex; align-items: center; gap: 8px;
  padding: 6px 12px; border-bottom: var(--hairline-width) solid var(--border-subtle);
}
.sp-title {
  flex: 1; min-width: 0;
  font-size: var(--fs-sm); font-weight: var(--fw-semibold);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
/* 运行中计数：栏收起时看不到角标，栏内头部这一颗就是"还在跑几个" */
.sp-running {
  flex: none; min-width: 16px; padding: 0 5px; border-radius: var(--radius-full);
  background: var(--primary-bg); color: var(--primary);
  font-size: var(--fs-xs); font-variant-numeric: tabular-nums; text-align: center;
}
.sp-close {
  flex: none; width: 22px; height: 22px; padding: 0;
  display: inline-flex; align-items: center; justify-content: center;
  border: none; border-radius: var(--radius-sm); background: transparent;
  color: var(--text-tertiary); cursor: pointer;
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.sp-close:hover { background: var(--bg-hover); color: var(--text-primary); }
.sp-body { flex: 1; min-height: 0; overflow-y: auto; }

/* 分栏手柄：与 SessionPreviewPane 同一套（平时不可见，悬停/拖动/聚焦才显形）。
   z-index 用 token —— scripts/check-z-index-tokens.mjs 会拦裸数字。 */
.sp-resize {
  position: absolute; top: 0; bottom: 0; width: 6px;
  z-index: var(--z-sticky);
  cursor: col-resize;
  background: transparent;
  transition: background-color var(--dur-fast) var(--ease-out);
  touch-action: none;
}
.sp-resize.handle-right { right: -3px; }
.sp-resize.handle-left { left: -3px; }
.sp-resize:hover,
.subagent-pane.resizing .sp-resize { background: var(--primary-bg); }
.sp-resize:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
.subagent-pane.resizing { user-select: none; }
</style>
