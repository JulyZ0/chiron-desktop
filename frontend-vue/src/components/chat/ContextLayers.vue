<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

/**
 * 上下文分项明细（点击占用环后展开）。
 *
 * 抽成独立组件而不是塞在 `ContextRing` 里：`Popover` 的内容在弹层内，
 * 挂在环上没法直接断言"界面上真的看得见"，而这正是用户反馈的问题
 * （分项只存在于 `title` 悬停提示）。独立组件可以单独渲染验证。
 */
const props = defineProps<{
  /** 上下文分段（`system`/`tools`/`user`/`assistant`/`tool_results` 的估算 token） */
  breakdown?: Record<string, number> | null
  /** 占用最大的几条（只含类别/角色/大小，**不含消息内容**） */
  largest?: Array<{ class?: string; role?: string; tokens?: number }> | null
  /** `used` 是否为估算值（服务商没回报 usage 时引擎用分项之和兜底） */
  estimated?: boolean | null
}>()

const { t } = useI18n()

/**
 * 五个分项，顺序与引擎 `_context_breakdown` 的桶一致。
 *
 * `labelKey` 全部写成**静态字面量**：i18n 契约禁止在模板里拼接语义片段当 key
 *（`'context_layer_' + k` 那种写法在键集校验里查不出来，漏译也不会报错）。
 */
const LAYER_META = [
  { key: 'system', labelKey: 'common.context_layer_system' },
  { key: 'tools', labelKey: 'common.context_layer_tools' },
  { key: 'user', labelKey: 'common.context_layer_user' },
  { key: 'assistant', labelKey: 'common.context_layer_assistant' },
  { key: 'tool_results', labelKey: 'common.context_layer_tool_results' },
] as const

/**
 * 百分比按**分项之和**算，而不是按模型窗口：用户问的是"这些东西里谁占得多"，
 * 各段相对彼此的份额才是可行动的信息（离压缩线还有多远由占用环本身回答）。
 */
const rows = computed(() => {
  const b = props.breakdown
  if (!b) return []
  const total = LAYER_META.reduce((sum, m) => sum + (Number(b[m.key]) || 0), 0)
  return LAYER_META.map(m => {
    const tokens = Number(b[m.key]) || 0
    return {
      key: m.key,
      label: t(m.labelKey),
      tokens,
      share: total > 0 ? (tokens / total) * 100 : 0,
    }
  })
})

const total = computed(() => rows.value.reduce((sum, r) => sum + r.tokens, 0))

/** 用完整数字（`12,345`）而非 `12.3k`：这一层是要对得上服务商账单的地方。 */
const fmtFull = (n: number) => n.toLocaleString()
/** 最大项沿用状态栏的紧凑单位，与 `title` 里的读数保持一致。 */
const fmtK = (n?: number) => (n ? `${(n / 1000).toFixed(1)}k` : '0')
</script>

<template>
  <div class="ctx-layers">
    <template v-if="rows.length && total > 0">
      <div
        v-for="r in rows"
        :key="r.key"
        class="ctx-layers__row"
      >
        <span class="ctx-layers__label">{{ r.label }}</span>
        <span class="ctx-layers__bar"><i :style="{ inlineSize: `${r.share}%` }" /></span>
        <span class="ctx-layers__num">{{ fmtFull(r.tokens) }}</span>
      </div>
      <div class="ctx-layers__row ctx-layers__row--total">
        <span class="ctx-layers__label">{{ t('common.context_total') }}</span>
        <span class="ctx-layers__num">{{ fmtFull(total) }}</span>
      </div>
    </template>
    <div
      v-else
      class="ctx-layers__empty"
    >
      {{ t('common.context_no_breakdown') }}
    </div>

    <div
      v-if="largest?.length"
      class="ctx-layers__largest"
    >
      <div class="ctx-layers__sub">{{ t('common.context_largest_title') }}</div>
      <div
        v-for="(x, i) in largest"
        :key="i"
        class="ctx-layers__row"
      >
        <span class="ctx-layers__label">{{ x.role || x.class || '?' }}</span>
        <span class="ctx-layers__num">{{ fmtK(x.tokens) }}</span>
      </div>
    </div>

    <div
      v-if="estimated"
      class="ctx-layers__note"
    >
      {{ t('common.context_usage_estimated') }}
    </div>
  </div>
</template>

<style scoped>
/* 标签 / 条形 / 数值三列。条形宽度用逻辑属性 inlineSize，自动跟随 RTL。 */
.ctx-layers { min-inline-size: 220px; font-size: var(--font-size-xs, 12px); }
.ctx-layers__row { display: flex; align-items: center; gap: 8px; line-height: 1.9; }
.ctx-layers__label { flex: 0 0 auto; min-inline-size: 72px; color: var(--text-secondary); }
.ctx-layers__bar { flex: 1 1 auto; block-size: 6px; border-radius: var(--radius-sm); background: var(--border-default); overflow: hidden; }
.ctx-layers__bar i { display: block; block-size: 100%; background: var(--primary); border-radius: inherit; }
.ctx-layers__num { flex: 0 0 auto; font-variant-numeric: tabular-nums; color: var(--text-primary); }
.ctx-layers__row--total { border-block-start: 1px solid var(--border-default); margin-block-start: 4px; padding-block-start: 4px; }
.ctx-layers__row--total .ctx-layers__label,
.ctx-layers__row--total .ctx-layers__num { color: var(--text-primary); font-weight: 600; }
.ctx-layers__sub { color: var(--text-tertiary); margin-block-start: 8px; }
.ctx-layers__largest { border-block-start: 1px solid var(--border-default); margin-block-start: 6px; }
.ctx-layers__empty { color: var(--text-tertiary); }
.ctx-layers__note { color: var(--text-tertiary); margin-block-start: 6px; }
</style>
