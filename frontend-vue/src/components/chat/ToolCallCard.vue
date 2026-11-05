<script setup lang="ts">
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { CaretRightOutlined } from '@ant-design/icons-vue'
import type { ToolCallItem } from './chat-types'
import { toolSummary, toolTargetPath } from '../../utils/toolFamily'
import { toolGroupKind } from './transcriptProjection'
import { revealInWorkbench } from '../workbench/useWorkbench'

const props = defineProps<{ item: ToolCallItem; depth?: number }>()
const expanded = ref(false)
const { t } = useI18n()

function prettyArgs(): string {
  try {
    return JSON.stringify(JSON.parse(props.item.arguments || '{}'), null, 2)
  } catch {
    return props.item.arguments || ''
  }
}

/** 家族：**复用投影层**的 `toolGroupKind`（组头的划分口径），保证单条与组头同一套分类 */
const family = computed(() => toolGroupKind(props.item.name))

/** 摘要：家族字段优先（读文件看 path、执行看 command、搜索看 pattern…），未登记工具走通用兜底 */
const summary = computed(() => toolSummary(props.item.name, props.item.arguments))

/**
 * 这次调用动的是哪个文件。
 *
 * 取值与 `summary` 同源（同一张字段表），所以"看到什么路径就能点开什么路径"，
 * 两者不可能对不上。空串 = 这个工具没有可打开的目标（命令、搜索、子 Agent…）。
 */
const targetPath = computed(() => toolTargetPath(props.item.name, props.item.arguments))

const padLeft = computed(() => (props.depth || 0) * 22)

/**
 * 交给工作台打开。
 *
 * 这里**不判断路径合法性**：工具参数可能是相对路径、绝对路径或只是 `src`，
 * 能不能映射到当前项目只有工作台知道（它拿到了项目根）。越权判断放在这一层
 * 只会得到两份互相漂移的判据。
 */
function revealTarget() {
  if (targetPath.value) revealInWorkbench(targetPath.value)
}
</script>

<template>
  <div
    class="tool-row-wrap chat-row-shell"
    :data-state="item.status"
    :data-tool="item.name"
    :data-family="family"
  >
    <!-- 工具树缩进连接线 -->
    <div
      v-if="(depth || 0) > 0"
      class="tree-guide"
      :style="{ left: `${padLeft - 14}px` }"
      aria-hidden
    />

    <div
      class="tool-row"
      :style="{ marginLeft: `${padLeft}px` }"
    >
      <button
        class="tool-main chat-row-head"
        type="button"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >
        <CaretRightOutlined
          class="chat-chevron"
          :class="{ open: expanded }"
        />
        <span class="tool-name">{{ item.name }}</span>
        <span
          class="chat-sep"
          aria-hidden
        />
        <span
          class="chat-state-dot"
          :data-state="item.status"
          aria-hidden
        />
        <span class="tool-summary">{{ summary }}</span>
      </button>

      <!-- 打开该文件（悬停才出现；键盘 Tab 到它也可见）。
           放在行头外层：行头本身是 button，嵌套 button 既无效也会吞掉点击。 -->
      <button
        v-if="targetPath"
        type="button"
        class="tool-open"
        :title="t('chat.workbench_open_in_panel', { path: targetPath })"
        @click.stop="revealTarget"
      >↗</button>

      <Transition name="chat-expand">
        <div
          v-if="expanded"
          class="tool-args"
        >
          <pre>{{ prettyArgs() }}</pre>
        </div>
      </Transition>
    </div>
  </div>
</template>

<!-- 行节奏 / chevron / 状态点 / 展开动画 / reduced-motion 来自全局 .chat-* 原语（style.css）；
     此处保留工具行独有的缩进导线、running sweep 流光与参数块 -->
<style scoped>
.tool-row-wrap { position: relative; }
.tree-guide { position: absolute; top: 0; bottom: 0; width: 1px; background: var(--border); }

.tool-row { position: relative; overflow: hidden; border-radius: var(--sig-radius-code); }
.tool-row:hover { background: var(--bg-hover); }
.tool-name { font-family: var(--font-mono); font-size: 13px; color: var(--text-primary); font-weight: var(--fw-regular); white-space: nowrap; }
/* 家族着色：让一屏工具调用能被"扫"出来，而不是一片同色行。
   只按家族调色相、不引入新的颜色体系；未知变量一律带 fallback，缺 token 时退化为原色。 */
.tool-row-wrap[data-family='modify'] .tool-name { color: var(--warning, #d97706); }
.tool-row-wrap[data-family='explore'] .tool-name { color: var(--primary, #2563eb); }
.tool-row-wrap[data-family='delegate'] .tool-name { color: var(--trajectory-tool-call, var(--primary, #2563eb)); }
.tool-summary { flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; color: var(--text-tertiary); font-size: 12px; }

/* 「在面板中打开」：默认隐身，悬停/聚焦才出现。
   常驻会给每一行都加一个按钮，一屏几十个工具调用就成了按钮墙；隐身但又可 Tab 到，
   鼠标与键盘都不需要额外的记忆成本。 */
.tool-open {
  position: absolute; inset-inline-end: 4px; top: 50%;
  display: flex; align-items: center; justify-content: center;
  width: 22px; height: 22px; padding: 0;
  transform: translateY(-50%);
  border: none; border-radius: var(--radius-sm);
  background: var(--bg-secondary); color: var(--text-tertiary);
  font-size: var(--fs-xs); line-height: 1; cursor: pointer;
  opacity: 0; transition: opacity var(--dur-fast) var(--ease-out);
}
.tool-row:hover .tool-open, .tool-open:focus-visible { opacity: 1; }
.tool-open:hover { color: var(--text-primary); background: var(--bg-surface-hover); }
.tool-open:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }

/* running sweep 流光（deepseek ToolRow sweep）—— 借 CDS `epitaxy-linear-sweep` 两点：
   ① 用 transform 而不是 left 驱动。left 每帧触发**布局重算**，transform 交给合成器；
      工具行在流式期间会持续出现，长会话里这个差别是实打实的。
   ② 时长与曲线对到官方值（严格 1.1s / cubic-bezier(.45, 0, .55, 1)）。 */
.tool-row-wrap[data-state='running'] .tool-row { position: relative; }
.tool-row-wrap[data-state='running'] .tool-row::after {
  content: ''; position: absolute; top: 0; bottom: 0; left: 0; width: 300px;
  background: linear-gradient(90deg, transparent 0%, color-mix(in srgb, var(--bg-page) 60%, transparent) 55%, transparent 100%);
  animation: toolRowSweep var(--dur-sweep) cubic-bezier(0.45, 0, 0.55, 1) infinite; pointer-events: none;
}
/* 行程 = 自身宽度的 4.5 倍（-100% → 350%）：300px 的条纹可覆盖最宽 1350px 的容器 */
@keyframes toolRowSweep { 0% { transform: translateX(-100%); } 100% { transform: translateX(350%); } }
@media (prefers-reduced-motion: reduce) {
  .tool-row-wrap[data-state='running'] .tool-row::after { display: none; }
}

.tool-args { padding: 8px 12px; margin: 2px 8px 6px; background: var(--bg-secondary); border: var(--hairline-width) solid var(--border-card); border-radius: var(--sig-radius-button); }
.tool-args pre { margin: 0; font-family: var(--font-mono); font-size: var(--fs-xs); color: var(--text-secondary); white-space: pre-wrap; overflow-wrap: anywhere; word-break: normal; }
@media (max-width: 576px) { .tool-args { margin: 2px 4px 6px; } }
</style>
