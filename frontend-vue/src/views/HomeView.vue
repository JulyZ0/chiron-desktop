<script setup lang="ts">
/**
 * 首页 = 工作台仪表盘（原来是一张营销落地页：特性卡片 + 假窗口截图 + docker compose
 * 终端块）。落地页对**已经装好并每天在用**的人来说全是噪音 —— 他要的是"接着上次干"
 * 和"这个月烧了多少"。所以首页改为三块真数据：快捷入口、最近会话、用量概览。
 *
 * 数据来源（都是既有端点，不新增后端）：
 *   - `GET /v1/conversations?per_page=N` → 最近会话（后端已按 pinned DESC, updated_at DESC
 *     排序，前端只截取前 N 条，不自己重排）；
 *   - `GET /v1/billing/usage?groupBy=day&range=7d` → 用量概览（`usage_events` 聚合，
 *     金额单位是**分**，前端按既有的 "credits" 标签展示，与计费页口径一致）；
 *   - 快捷命令复用 `executeQuickCommand`：建 uni 会话 → /v1/quick-execute → 跳
 *     /chat?task=，与停靠坞里那条路径**同一份实现**，不复制一份。
 *
 * 失败降级：两块数据各自独立加载、独立报错 —— 会话拉不到不该让用量卡片也变成错误态
 * （反过来也一样）。首页是入口页，任何一块坏了都只该影响它自己。
 */
import { computed, markRaw, onMounted, ref } from 'vue'
import type { Component } from 'vue'
import { useRouter } from 'vue-router'
import { Button, Drawer, Input } from 'ant-design-vue'
import {
  MessageOutlined, UserOutlined, HistoryOutlined, BlockOutlined,
  BookOutlined, ThunderboltOutlined, ArrowRightOutlined, SafetyCertificateOutlined,
} from '@ant-design/icons-vue'
import { api } from '../api'
import { executeQuickCommand } from '../components/WorkstationNav.vue'
import HomeScene3D from '../components/home/HomeScene3D.vue'
import EmptyState from '../components/common/EmptyState.vue'
// 系统自检（P1-9）：入口在右上角，内容是抽屉里的同一个面板组件（单例数据源）
import SelfCheckPanel from '../components/health/SelfCheckPanel.vue'
import { useAuthStore } from '../stores/auth'
import { formatSmart } from '../utils/datetime'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const router = useRouter()
const auth = useAuthStore()

/** 最近会话只取一屏（更多交给「查看全部」） */
const RECENT_LIMIT = 6

/**
 * 系统自检抽屉（P1-9）。面板**只在打开后才挂载**（`v-if`）：抽屉关着的时候
 * 不该有一次"用户没在等"的探测，首页测试也不会因此多出三组请求。
 */
const selfCheckOpen = ref(false)

// ── 快捷入口 ──
// 图标必须 markRaw：图标是静态组件，被 Vue 响应式代理会破坏渲染。
interface Entry {
  path: string
  label: string
  desc: string
  icon: Component
}

// computed 而不是模块级常量：文案要跟随界面语言切换（模块级 `t()` 只在首次 import 时求值，
// 切语言后标签会停在旧语言上 —— 停靠坞用的 WORKSTATION_LABELS 就是那个行为，这里不跟）。
const entries = computed<Entry[]>(() => [
  { path: '/chat', label: t('chat.chat'), desc: t('chat.ai_chat_assistant'), icon: markRaw(MessageOutlined) },
  { path: '/agents', label: 'Agent', desc: t('agent.multi_agent_collaboration'), icon: markRaw(UserOutlined) },
  { path: '/memory', label: t('memory.memory_2'), desc: t('memory.record_user_preferences_and_long_term_context_auto_attached_in_future_conversations'), icon: markRaw(HistoryOutlined) },
  { path: '/skills', label: t('agent.skill'), desc: t('agent.tool_mcp'), icon: markRaw(BlockOutlined) },
  { path: '/knowledge', label: t('knowledge.knowledge_base'), desc: t('knowledge.rag_retrieval_augmentation'), icon: markRaw(BookOutlined) },
  { path: '/plugins', label: t('common.plugin'), desc: t('common.extensions'), icon: markRaw(ThunderboltOutlined) },
])

// ── 快速命令：一句话直接开工 ──
const quickInput = ref('')
const quickRunning = ref(false)

async function runQuickCommand() {
  const command = quickInput.value.trim()
  if (!command || quickRunning.value) return
  quickRunning.value = true
  try {
    await executeQuickCommand(command)
    quickInput.value = ''
  } catch {
    // 失败提示由 api 拦截器统一给出；这里保留输入内容让用户重试
  } finally {
    quickRunning.value = false
  }
}

// ── 最近会话 ──
interface SessionRow {
  id?: string
  title?: string
  alias?: string
  pinned?: boolean
  updated_at?: string
}

const sessions = ref<SessionRow[]>([])
const sessionsLoading = ref(false)
const sessionsError = ref('')

async function loadSessions() {
  if (!auth.isAuthenticated) return
  sessionsLoading.value = true
  sessionsError.value = ''
  try {
    const { data } = await api.get('/v1/conversations', { params: { per_page: RECENT_LIMIT } })
    const payload = data?.data
    sessions.value = (Array.isArray(payload) ? payload : []).slice(0, RECENT_LIMIT)
  } catch {
    sessions.value = []
    sessionsError.value = t('errors.failed_to_load')
  } finally {
    sessionsLoading.value = false
  }
}

/** 展示名：别名优先（用户改过的名字比自动标题更可读），没有就退回标题 */
function sessionName(s: SessionRow): string {
  return (s.alias || '').trim() || (s.title || '').trim() || t('common.no_title_yet')
}

function openSession(id?: string) {
  if (!id) return
  router.push({ path: '/chat', query: { session: id } })
}

// ── 用量概览 ──
interface DailyRow {
  day: string
  tx_count: number
  credits_spent: number
}

interface UsageData {
  daily?: DailyRow[]
  /** 区间累计花费（单位：分，沿用计费页的 "credits" 展示口径） */
  total_spent?: number
  period_days?: number
}

const usage = ref<UsageData | null>(null)
const usageLoading = ref(false)
const usageError = ref('')

/** 区间内 LLM 调用次数：`daily[].tx_count` 是每天的事件数，合计即总量 */
const usageCalls = computed(() =>
  (usage.value?.daily ?? []).reduce((sum, d) => sum + (Number(d.tx_count) || 0), 0),
)

async function loadUsage() {
  if (!auth.isAuthenticated) return
  usageLoading.value = true
  usageError.value = ''
  try {
    const { data } = await api.get('/v1/billing/usage', { params: { groupBy: 'day', range: '7d' } })
    usage.value = (data?.data as UsageData | undefined) ?? null
  } catch {
    // 库未就绪时后端回 503 —— 那是"暂时读不到"，与"这周没用过"不同，所以走错误态
    // 而不是 0，否则用户会以为统计坏了。
    usage.value = null
    usageError.value = t('errors.failed_to_load')
  } finally {
    usageLoading.value = false
  }
}

onMounted(() => {
  void loadSessions()
  void loadUsage()
})
</script>

<template>
  <div class="home">
    <!-- Hero：3D 粒子背景 + 一句话快速命令（首页的主操作就是"直接开工"） -->
    <section class="hero">
      <div
        class="hero-glow"
        aria-hidden
      />
      <div
        class="hero-grid-bg"
        aria-hidden
      />
      <HomeScene3D />
      <!-- 系统自检入口：右上角常驻。非 admin 看不见（面板读的是 admin 端点，
           给他一个点开必然 403 的按钮只是噪音）。 -->
      <button
        v-if="auth.isAdmin"
        type="button"
        class="hero-selfcheck"
        data-testid="home-selfcheck-entry"
        @click="selfCheckOpen = true"
      >
        <SafetyCertificateOutlined />
        <span>{{ $t('health.entry') }}</span>
      </button>
      <div class="hero-content">
        <span class="hero-badge">
          <span class="hero-badge-dot" />{{ $t('agent.chiron_self_hosted_ai_agent_console') }}
        </span>
        <h1 class="hero-title">
          {{ $t('agent.let_the_agent') }}
          <span class="hero-title-accent">{{ $t('common.continuous_work') }}</span>
        </h1>
        <p class="hero-sub">
          {{ $t('common.traceable_visible_process_your_local_intelligent_workspace') }}
        </p>
        <div class="hero-quick">
          <Input
            v-model:value="quickInput"
            size="large"
            class="quick-input"
            :placeholder="$t('common.quick_command_placeholder')"
            :disabled="quickRunning"
            @press-enter="runQuickCommand"
          />
          <Button
            type="primary"
            size="large"
            class="quick-run"
            :loading="quickRunning"
            @click="runQuickCommand"
          >
            {{ $t('common.execute') }}
            <ArrowRightOutlined />
          </Button>
        </div>
      </div>
    </section>

    <section class="dashboard">
      <!-- 快捷入口：六大工作台 -->
      <div class="dash-block entries">
        <div class="dash-head">
          <h2 class="dash-title">
            {{ $t('common.quick_start') }}
          </h2>
          <!-- 首次运行引导（P1-10）的常驻入口：引导只在"无模型 + 未走完"时自动跳，
               已配置好的人执行"重新运行"时仍需要一个手动入口。 -->
          <button
            type="button"
            class="dash-link"
            data-testid="home-welcome-entry"
            @click="router.push('/welcome')"
          >
            {{ $t('welcome.restart') }}
          </button>
        </div>
        <div class="entry-grid">
          <button
            v-for="e in entries"
            :key="e.path"
            type="button"
            class="entry-card"
            @click="router.push(e.path)"
          >
            <span class="entry-icon"><component :is="e.icon" /></span>
            <span class="entry-name">{{ e.label }}</span>
            <span class="entry-desc">{{ e.desc }}</span>
          </button>
        </div>
      </div>

      <div class="dash-cols">
        <!-- 最近会话 -->
        <div class="dash-block">
          <div class="dash-head">
            <h2 class="dash-title">
              {{ $t('common.recent_sessions') }}
            </h2>
            <button
              type="button"
              class="dash-link"
              @click="router.push('/chat')"
            >
              {{ $t('common.view_all') }}
            </button>
          </div>
          <div
            v-if="sessionsLoading"
            class="dash-hint"
          >
            {{ $t('common.loading') }}
          </div>
          <EmptyState
            v-else-if="!sessions.length"
            :description="sessionsError || $t('common.no_sessions_yet')"
            size="list"
          />
          <ul
            v-else
            class="session-list"
          >
            <li
              v-for="s in sessions"
              :key="s.id"
            >
              <button
                type="button"
                class="session-row"
                @click="openSession(s.id)"
              >
                <span class="session-title">{{ sessionName(s) }}</span>
                <span class="session-time">{{ formatSmart(s.updated_at) }}</span>
              </button>
            </li>
          </ul>
        </div>

        <!-- 用量概览（近 7 天） -->
        <div class="dash-block">
          <div class="dash-head">
            <h2 class="dash-title">
              {{ $t('common.usage_overview') }}
            </h2>
            <span class="dash-range">{{ $t('common.last_7_days') }}</span>
          </div>
          <div
            v-if="usageLoading"
            class="dash-hint"
          >
            {{ $t('common.loading') }}
          </div>
          <EmptyState
            v-else-if="!usage"
            :description="usageError || $t('common.no_usage_data_yet')"
            size="list"
          />
          <div
            v-else
            class="usage-stats"
          >
            <div class="usage-stat">
              <span class="usage-value">{{ usageCalls }}</span>
              <span class="usage-label">{{ $t('billing.usage_col_events') }}</span>
            </div>
            <div class="usage-stat">
              <span class="usage-value">{{ usage.total_spent ?? 0 }}</span>
              <span class="usage-label">{{ $t('billing.usage_col_cost') }}</span>
            </div>
          </div>
        </div>
      </div>
    </section>

    <footer class="home-footer">
      <span class="home-footer-brand">
        <span class="home-footer-logo">MC</span>chiron
      </span>
      <span class="home-footer-note">{{ $t('common.self_hosted_open_source_your_data_stays_on_your_machine') }}</span>
    </footer>

    <!-- 系统自检抽屉：内容与设置页 / 欢迎页是**同一个** SelfCheckPanel →
         同一个 useSelfCheck 单例，三处看到同一份读数与同一个时间戳。 -->
    <Drawer
      :open="selfCheckOpen"
      :title="$t('health.title')"
      placement="right"
      :width="440"
      data-testid="home-selfcheck-drawer"
      @close="selfCheckOpen = false"
    >
      <SelfCheckPanel
        v-if="selfCheckOpen"
        variant="drawer"
        :default-collapsed="false"
      />
    </Drawer>
  </div>
</template>

<style scoped>
.home { min-height: 100%; background: var(--bg-page); color: var(--text-primary); overflow-x: hidden; }

.hero {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  /* 仪表盘页首屏要让出空间给数据 —— 原来 72vh 的 Hero 会把三块内容全挤到折叠线以下 */
  min-height: min(380px, 52vh);
  padding: 56px 24px 44px;
  text-align: center;
  overflow: hidden;
}
.hero-glow {
  position: absolute;
  inset: -30% -20% auto -20%;
  height: 85%;
  background:
    radial-gradient(ellipse 45% 55% at 50% 0%, var(--primary-bg), transparent 70%),
    radial-gradient(ellipse 30% 40% at 78% 12%, var(--accent-bg), transparent 70%),
    radial-gradient(ellipse 30% 40% at 22% 10%, var(--info-bg), transparent 70%);
  pointer-events: none;
  filter: blur(20px);
}
.hero-grid-bg {
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(var(--border-subtle) 1px, transparent 1px),
    linear-gradient(90deg, var(--border-subtle) 1px, transparent 1px);
  background-size: 44px 44px;
  mask-image: radial-gradient(ellipse 60% 55% at 50% 0%, black 20%, transparent 75%);
  -webkit-mask-image: radial-gradient(ellipse 60% 55% at 50% 0%, black 20%, transparent 75%);
  pointer-events: none;
  opacity: 0.5;
}
.hero-content { position: relative; z-index: var(--z-local); width: 100%; max-width: 720px; }
.hero-badge {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 5px 14px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-full);
  background: var(--bg-surface);
  font-size: 12px;
  font-weight: var(--fw-medium);
  color: var(--text-secondary);
  box-shadow: var(--shadow-sm);
}
.hero-badge-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--primary);
}
/* 系统自检入口：贴 Hero 右上角。inset-inline-end 在 RTL 下自动镜像到左侧。 */
.hero-selfcheck {
  position: absolute;
  inset-block-start: var(--space-4);
  inset-inline-end: var(--space-6);
  z-index: var(--z-sticky);
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-1) var(--space-3);
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-full);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-family: inherit;
  font-size: var(--fs-xs);
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease-out), border-color var(--dur-fast) var(--ease-out);
}
.hero-selfcheck:hover {
  color: var(--text-primary);
  border-color: var(--primary);
}
.hero-selfcheck:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 2px;
}
/* 窄屏：Hero 内容会顶到角落，入口缩小内边距免得压住标题 */
@media (max-width: 560px) {
  .hero-selfcheck {
    inset-block-start: var(--space-2);
    inset-inline-end: var(--space-2);
  }
}
.hero-title {
  margin: 20px 0 12px;
  font-size: clamp(28px, 4.4vw, 44px);
  line-height: 1.18;
  font-weight: var(--fw-bold);
  letter-spacing: -0.02em;
  color: var(--text-primary);
}
.hero-title-accent {
  background: linear-gradient(100deg, var(--primary), var(--accent));
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  -webkit-text-fill-color: transparent;
}
.hero-sub {
  margin: 0 auto;
  font-size: 14px;
  line-height: 24px;
  color: var(--text-secondary);
  max-width: 560px;
}

/* 快速命令：输入框与按钮同高、同一行；窄屏自动换行 */
.hero-quick {
  display: flex;
  gap: 10px;
  align-items: stretch;
  margin: 26px auto 0;
  max-width: 620px;
}
.quick-input { flex: 1; min-width: 0; }
.quick-run {
  flex: none;
  height: auto;
  border-radius: var(--radius-xl);
  padding: 0 22px;
  font-weight: var(--fw-semibold);
  box-shadow: var(--shadow-md), 0 4px 14px var(--primary-bg);
}

/* ── 仪表盘 ── */
.dashboard {
  max-width: 1080px;
  margin: 0 auto;
  padding: 8px 24px 48px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.dash-block {
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--comp-card-radius);
  background: var(--bg-surface);
  box-shadow: var(--shadow-sm);
  padding: 18px 20px 20px;
}
.dash-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}
.dash-title {
  font-size: 15px;
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
}
.dash-link {
  flex: none;
  border: 0;
  background: transparent;
  padding: 2px 4px;
  border-radius: var(--radius-md);
  font-size: 12px;
  color: var(--primary);
  cursor: pointer;
  transition: background var(--dur-fast) ease;
}
.dash-link:hover { background: var(--primary-bg); }
.dash-range { flex: none; font-size: var(--fs-xs); color: var(--text-tertiary); }
.dash-hint { padding: 24px 0; text-align: center; font-size: 13px; color: var(--text-tertiary); }

/* 快捷入口网格 */
.entry-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 10px;
}
.entry-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  text-align: start;
  padding: 14px 14px 16px;
  border: var(--hairline-width) solid var(--border-subtle);
  border-radius: var(--sig-radius-card);
  background: var(--bg-surface-hover);
  color: inherit;
  cursor: pointer;
  transition: transform var(--dur-fast) var(--ease-out),
              border-color var(--dur-fast) var(--ease-out),
              box-shadow var(--dur-fast) var(--ease-out);
}
.entry-card:hover {
  transform: translateY(-2px);
  border-color: var(--primary);
  box-shadow: var(--shadow-md);
}
.entry-card:focus-visible { outline: var(--focus-ring-width) solid var(--focus-ring-color); outline-offset: var(--focus-ring-offset); }
.entry-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  margin-bottom: 6px;
  border-radius: var(--radius-md);
  background: var(--primary-bg);
  color: var(--primary);
  font-size: 16px;
}
.entry-name { font-size: 13px; font-weight: var(--fw-semibold); color: var(--text-primary); }
.entry-desc {
  font-size: var(--fs-xs);
  line-height: 17px;
  color: var(--text-tertiary);
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

/* 最近会话 + 用量：并排，窄屏堆叠 */
.dash-cols {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
  gap: 16px;
}
.session-list { display: flex; flex-direction: column; gap: 2px; }
.session-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
  padding: 9px 10px;
  border: 0;
  border-radius: var(--radius-lg);
  background: transparent;
  color: inherit;
  text-align: start;
  cursor: pointer;
  transition: background var(--dur-fast) ease;
}
.session-row:hover { background: var(--bg-surface-hover); }
.session-row:focus-visible { outline: var(--focus-ring-width) solid var(--focus-ring-color); outline-offset: var(--focus-ring-offset); }
.session-title {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.session-time { flex: none; font-size: var(--fs-xs); color: var(--text-tertiary); font-variant-numeric: tabular-nums; }

/* 用量数字 */
.usage-stats {
  display: flex;
  align-items: stretch;
  gap: 12px;
  padding: 6px 0 4px;
}
.usage-stat {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 14px;
  border-radius: var(--radius-lg);
  background: var(--bg-surface-hover);
  border: var(--hairline-width) solid var(--border-subtle);
}
.usage-value {
  font-size: 24px;
  font-weight: var(--fw-bold);
  line-height: 1.1;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
  word-break: break-all;
}
.usage-label { font-size: var(--fs-xs); color: var(--text-tertiary); }

/* ── 页脚 ── */
.home-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 18px 28px;
  border-top: var(--hairline-width) solid var(--border-subtle);
  font-size: 12px;
  color: var(--text-tertiary);
  background: var(--bg-surface);
}
.home-footer-brand { display: flex; align-items: center; gap: 8px; font-weight: var(--fw-semibold); color: var(--text-secondary); }
.home-footer-logo {
  width: 20px; height: 20px; border-radius: var(--radius-md);
  background: linear-gradient(135deg, var(--primary), var(--accent));
  color: var(--on-solid); font-size: var(--fs-xs); font-weight: var(--fw-bold);
  display: inline-flex; align-items: center; justify-content: center;
}

@media (max-width: 900px) {
  .dash-cols { grid-template-columns: 1fr; }
}
@media (max-width: 640px) {
  .hero { min-height: 320px; padding: 44px 18px 34px; }
  .hero-quick { flex-direction: column; }
  .quick-run { width: 100%; height: 40px; }
  .dashboard { padding-inline: 16px; }
  .entry-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .usage-stats { flex-direction: column; }
  .home-footer { flex-direction: column; text-align: center; }
}
@media (hover: none) {
  .entry-card:hover { transform: none; }
}
@media (prefers-reduced-motion: reduce) {
  .entry-card { transition: none; }
}
</style>
