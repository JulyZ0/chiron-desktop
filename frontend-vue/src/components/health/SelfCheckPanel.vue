<script setup lang="ts">
/**
 * 系统自检面板（P1-9）：把"启动后第一件该确认的事"收成一张可折叠的清单。
 *
 * 三个入口复用**同一个组件**，数据来自**同一个** `useSelfCheck` 单例：
 *   1. 首页右上角「系统自检」按钮 → 抽屉（`variant="drawer"`，默认展开）；
 *   2. 设置面板顶部（常驻，折叠）；
 *   3. 欢迎引导第一步之后（常驻，折叠）。
 *
 * 两条不能破的边界：
 *   - **非 admin 整体隐藏**：`/v1/admin/diagnostics/summary` 对普通用户是 403。
 *     隐藏而不是渲染"自检失败"，否则用户会以为系统坏了（见 useSelfCheck 的 available）。
 *   - **失败不阻塞**：任何一项读不到都退化成 `unknown` 徽标 + 一行说明，
 *     面板本身永远渲染得出来，也不弹全局 toast。
 */
import { computed, ref, useId, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Button, Tag } from 'ant-design-vue'
import { DownOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import { useSelfCheck, type SelfCheckItem, type SelfCheckStatus } from '../../composables/useSelfCheck'

const { t } = useI18n()
const router = useRouter()

const props = withDefaults(
  defineProps<{
    /** inline = 页面内嵌；drawer = 抽屉里（无边框、默认展开） */
    variant?: 'inline' | 'drawer'
    /** 初始是否折叠。抽屉里默认展开（用户点开就是为了看） */
    defaultCollapsed?: boolean
  }>(),
  { variant: 'inline', defaultCollapsed: true },
)

const { available, items, loading, error, fetchedAt, overall, issueCount, ensureLoaded, refresh } =
  useSelfCheck()

const collapsed = ref(props.defaultCollapsed)

// 角色异步到达：available 为真（含"挂载时已是 admin"）才第一次拉取。
// 缓存命中时 ensureLoaded 直接返回，三个入口不会各自打一组请求。
watch(
  available,
  allowed => {
    if (allowed) void ensureLoaded()
  },
  { immediate: true },
)

/**
 * `aria-controls` 需要一个稳定且**唯一**的 id。面板可能被多处挂载（首页抽屉 +
 * 设置页各一份），`useId()` 保证同一页面里两份不会撞同一个 id。
 */
const bodyId = `self-check-body-${useId()}`

const statusText = computed(() => t(`health.status_${overall.value}`))
const updatedText = computed(() => {
  if (!fetchedAt.value) return ''
  const time = new Date(fetchedAt.value).toLocaleTimeString()
  return t('health.updated', { time })
})

function statusColor(status: SelfCheckStatus): string {
  switch (status) {
    case 'ok':
      return 'success'
    case 'warn':
      return 'warning'
    case 'error':
      return 'error'
    default:
      return 'default'
  }
}

/**
 * 每一行的说明。
 *
 * 五项清单直接用后端给的观测事实（那是"事实"，不是建议）；模型 / 价格两项的
 * 文案要跟随界面语言，所以在这里按 `extra` 的数值组句 —— 不把中文字符串
 * 冻进 composable 的状态里（切语言后不会变）。
 */
function detailText(item: SelfCheckItem): string {
  if (item.key === 'models') {
    if (item.status === 'unknown') return t('health.detail_models_unknown')
    const count = item.extra?.count ?? 0
    return count > 0
      ? t('health.detail_models_count', { count })
      : t('health.detail_models_none')
  }
  if (item.key === 'pricing') {
    const total = item.extra?.total ?? 0
    const priced = item.extra?.priced ?? 0
    const unpriced = item.extra?.unpriced ?? Math.max(0, total - priced)
    if (total <= 0) return t('health.detail_pricing_empty', { days: item.extra?.days ?? 0 })
    if (item.status === 'ok') return t('health.detail_pricing_all', { total, priced })
    return t('health.detail_pricing_partial', { total, unpriced })
  }
  return item.detail
}

/**
 * 有落点就出「去修复」—— 包括 ok 行。
 *
 * 与 `admin/DiagnosticsView` 的取舍**不同**：那一页有几十条体检项，ok 行再挂入口
 * 会变成噪音；这里只有七行，且用户点「去修复」也可能只是想**去改配置**
 * （模型正常但想加一个）。没有落点的项（tables 由迁移统一处理、checkpoints 是
 * 引擎内部保护）一律不出按钮，避免点了发现无处可去。
 */
function showFix(item: SelfCheckItem): boolean {
  return !!item.fixPath
}

function goFix(path: string | null): void {
  if (!path) return
  void router.push(path)
}

const hasItems = computed(() => items.value.length > 0)
</script>

<template>
  <section
    v-if="available"
    class="self-check"
    :class="[`self-check--${variant}`, { 'self-check--collapsed': collapsed }]"
    data-testid="self-check-panel"
  >
    <div class="self-check-head">
      <button
        type="button"
        class="self-check-toggle"
        :aria-expanded="!collapsed"
        :aria-controls="bodyId"
        :aria-label="collapsed ? t('health.expand') : t('health.collapse')"
        data-testid="self-check-toggle"
        @click="collapsed = !collapsed"
      >
        <DownOutlined
          class="self-check-chevron"
          :class="{ 'is-open': !collapsed }"
        />
        <span class="self-check-title">{{ t('health.title') }}</span>
        <Tag
          :color="statusColor(overall)"
          class="self-check-overall"
          data-testid="self-check-overall"
        >
          {{ statusText }}
        </Tag>
        <span
          v-if="issueCount > 0 && collapsed"
          class="self-check-issue-count"
          data-testid="self-check-issue-count"
        >
          {{ issueCount }}
        </span>
      </button>

      <div class="self-check-actions">
        <span
          v-if="updatedText"
          class="self-check-updated"
        >{{ updatedText }}</span>
        <Button
          size="small"
          :loading="loading"
          data-testid="self-check-refresh"
          @click="refresh()"
        >
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ t('health.refresh') }}
        </Button>
      </div>
    </div>

    <div
      v-show="!collapsed"
      :id="bodyId"
      class="self-check-body"
    >
      <!-- 拉取失败单独一行，但**不清空**清单：unknown 行本身已经说明"这一项看不见" -->
      <p
        v-if="error"
        class="self-check-error"
        data-testid="self-check-error"
      >
        {{ t('health.error', { msg: error }) }}
      </p>

      <ul
        v-if="hasItems"
        class="self-check-list"
      >
        <li
          v-for="item in items"
          :key="item.key"
          class="self-check-item"
          :class="`is-${item.status}`"
          :data-testid="`self-check-item-${item.key}`"
        >
          <Tag
            :color="statusColor(item.status)"
            class="self-check-badge"
            :data-testid="`self-check-status-${item.key}`"
          >
            {{ t(`health.status_${item.status}`) }}
          </Tag>
          <span class="self-check-label">{{ t(`health.item_${item.key}`) }}</span>
          <span
            v-if="detailText(item)"
            class="self-check-detail"
          >{{ detailText(item) }}</span>
          <button
            v-if="showFix(item)"
            type="button"
            class="self-check-fix"
            :class="{ 'is-muted': item.status === 'ok' }"
            :data-testid="`self-check-fix-${item.key}`"
            @click="goFix(item.fixPath)"
          >
            {{ t('health.fix') }}
          </button>
        </li>
      </ul>

      <p
        v-else
        class="self-check-empty"
        data-testid="self-check-empty"
      >
        {{ loading ? t('health.loading') : t('health.empty') }}
      </p>

      <p class="self-check-hint">
        {{ t('health.hint') }}
      </p>
    </div>
  </section>
</template>

<style scoped>
.self-check {
  border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  padding: var(--space-2) var(--space-3);
}
/* 抽屉里已有容器与内边距：去掉外框，只保留内容 */
.self-check--drawer {
  border: none;
  border-radius: 0;
  background: transparent;
  padding: 0;
}
.self-check-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.self-check-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-1);
  border: none;
  background: transparent;
  color: var(--text-primary);
  font-size: var(--fs-sm);
  font-family: inherit;
  cursor: pointer;
  border-radius: var(--radius-md);
  transition: background var(--dur-fast) var(--ease-out);
}
.self-check-toggle:hover {
  background: var(--bg-subtle, transparent);
}
.self-check-toggle:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
}
.self-check-title {
  font-weight: var(--fw-medium);
}
.self-check-chevron {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  transition: transform var(--dur-fast) var(--ease-out);
}
.self-check-chevron.is-open {
  transform: rotate(180deg);
}
.self-check-overall {
  margin-inline-start: var(--space-1);
}
.self-check-issue-count {
  min-width: 18px;
  padding: 0 5px;
  border-radius: var(--radius-full);
  background: var(--warning-bg, var(--bg-subtle, transparent));
  color: var(--text-primary);
  font-size: var(--fs-xs);
  text-align: center;
}
.self-check-actions {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  margin-inline-start: auto;
}
.self-check-updated {
  font-size: var(--fs-xs);
  color: var(--text-muted, var(--text-secondary));
  font-variant-numeric: tabular-nums;
}
.self-check-body {
  margin-top: var(--space-2);
}
.self-check-error {
  margin: 0 0 var(--space-2);
  font-size: var(--fs-xs);
  color: var(--error);
  word-break: break-word;
}
.self-check-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
/* 行内四列：徽标 / 名称 / 说明 / 修复。说明列 flex:1 吃掉剩余宽度，
   窄屏时整行换行（见下面的 media 查询），不压成一条竖排。 */
.self-check-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) 0;
  border-block-start: var(--hairline-width) solid var(--border-subtle, var(--border));
}
.self-check-item:first-child {
  border-block-start: none;
}
.self-check-badge {
  flex: 0 0 auto;
  margin: 0;
}
.self-check-label {
  flex: 0 0 auto;
  font-size: var(--fs-sm);
}
.self-check-detail {
  flex: 1 1 auto;
  min-width: 0;
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  word-break: break-word;
}
.self-check-fix {
  flex: 0 0 auto;
  padding: 0 var(--space-2);
  border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--primary);
  font-size: var(--fs-xs);
  font-family: inherit;
  cursor: pointer;
  transition: border-color var(--dur-fast) var(--ease-out);
}
.self-check-fix:hover {
  border-color: var(--primary);
}
/* ok 行的入口退成次要色：它在那里是"去改配置"，不是在喊"这里坏了" */
.self-check-fix.is-muted {
  color: var(--text-secondary);
}
.self-check-fix:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
}
.self-check-empty,
.self-check-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-muted, var(--text-secondary));
}
.self-check-hint {
  margin-top: var(--space-2);
}
@media (max-width: 560px) {
  .self-check-item {
    flex-wrap: wrap;
  }
  .self-check-detail {
    flex-basis: 100%;
  }
}
</style>
