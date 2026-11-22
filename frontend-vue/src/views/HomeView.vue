<script setup lang="ts">
/**
 * 首页 = 个人 Agent 的「驾驶舱」。
 *
 * 定位：这不是一页产品介绍，而是每天打开就想"接着上次干"的地方。因此顺序固定为：
 *   1. **一句话开工**（主输入框，占视觉中心）；
 *   2. **继续上次**（最近会话一键续上，保留上下文）；
 *   3. **最近会话 / 能力入口 / 近 7 天用量**（三块真数据，不展示任何未接线的营销文案）。
 *
 * 数据来源（都是既有端点，不新增后端）：
 *   - `GET /v1/conversations?per_page=6` → 最近会话（后端已按 pinned DESC, updated_at DESC 排序）；
 *   - `GET /v1/billing/usage?groupBy=day&range=7d` → 用量概览（金额单位是分）；
 *   - 快捷命令复用 `executeQuickCommand`：建会话 → /v1/quick-execute → 跳 /chat?task=。
 *
 * 失败降级：两块数据各自独立加载、独立报错 —— 会话拉不到不该让用量卡片也变成错误态。
 */
import { computed, markRaw, onMounted, ref } from 'vue'
import type { Component } from 'vue'
import { useRouter } from 'vue-router'
import { Button, Drawer, Input } from 'ant-design-vue'
import {
  MessageOutlined, UserOutlined, HistoryOutlined, BlockOutlined,
  BookOutlined, ThunderboltOutlined, ArrowRightOutlined, SafetyCertificateOutlined,
  PlusOutlined,
} from '@ant-design/icons-vue'
import { api } from '../api'
import { executeQuickCommand } from '../components/WorkstationNav.vue'
import EmptyState from '../components/common/EmptyState.vue'
import LanguageSwitcher from '../components/common/LanguageSwitcher.vue'
import ThemeSwitcher from '../components/ThemeSwitcher.vue'
import SelfCheckPanel from '../components/health/SelfCheckPanel.vue'
import { useAuthStore } from '../stores/auth'
import { formatSmart } from '../utils/datetime'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const router = useRouter()
const auth = useAuthStore()

/** 最近会话只取一屏（更多交给「查看全部」） */
const RECENT_LIMIT = 6

const selfCheckOpen = ref(false)

// ── 快捷入口：六类能力 ──
interface Entry {
  path: string
  label: string
  desc: string
  icon: Component
}

const entries = computed<Entry[]>(() => [
  { path: '/chat', label: t('chat.chat'), desc: t('chat.ai_chat_assistant'), icon: markRaw(MessageOutlined) },
  { path: '/agents', label: 'Agent', desc: t('agent.multi_agent_collaboration'), icon: markRaw(UserOutlined) },
  { path: '/memory', label: t('memory.memory_2'), desc: t('memory.record_user_preferences_and_long_term_context_auto_attached_in_future_conversations'), icon: markRaw(HistoryOutlined) },
  { path: '/skills', label: t('agent.skill'), desc: t('agent.tool_mcp'), icon: markRaw(BlockOutlined) },
  { path: '/knowledge', label: t('knowledge.knowledge_base'), desc: t('knowledge.rag_retrieval_augmentation'), icon: markRaw(BookOutlined) },
  { path: '/plugins', label: t('common.plugin'), desc: t('common.extensions'), icon: markRaw(ThunderboltOutlined) },
])

/** 开场提示词：把"不知道能干什么"变成一次点击。 */
const samplePrompts = computed(() => [
  t('common.home_sample_plan'),
  t('common.home_sample_code'),
  t('common.home_sample_research'),
])

// ── 一句话开工 ──
const quickInput = ref('')
const quickRunning = ref(false)

function fillPrompt(text: string) {
  quickInput.value = text
}

async function runQuickCommand() {
  const command = quickInput.value.trim()
  if (!command || quickRunning.value) return
  quickRunning.value = true
  try {
    await executeQuickCommand(command)
    quickInput.value = ''
  } catch {
    // 失败提示由 api 拦截器统一给出；保留输入内容让用户重试
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

/** 「继续上次」= 列表第一条（后端已按置顶 + 最近更新排序）。 */
const continueSession = computed(() => sessions.value[0] ?? null)

function openSession(id?: string) {
  if (!id) return
  router.push({ path: '/chat', query: { session: id } })
}

/** 时段问候：让首页像"你的工具"而不是一块看板。 */
const greeting = computed(() => {
  const h = new Date().getHours()
  if (h < 6) return t('common.home_greeting_night')
  if (h < 12) return t('common.home_greeting_morning')
  if (h < 18) return t('common.home_greeting_afternoon')
  return t('common.home_greeting_evening')
})

// ── 用量概览 ──
interface DailyRow {
  day: string
  tx_count: number
  credits_spent: number
}

interface UsageData {
  daily?: DailyRow[]
  total_spent?: number
  period_days?: number
}

const usage = ref<UsageData | null>(null)
const usageLoading = ref(false)
const usageError = ref('')

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
    <div class="home-shell">
      <!-- 顶部：问候 + 系统自检入口 -->
      <header class="home-head">
        <div class="home-greeting">
          <span class="greeting-line">{{ greeting }}</span>
          <h1 class="greeting-title">{{ $t('common.home_title') }}</h1>
          <p class="greeting-sub">{{ $t('common.home_subtitle') }}</p>
        </div>
        <div class="home-head-actions">
          <!-- 语言 / 主题：从窗口顶栏移到这里（顶栏不再常驻一排开关）。 -->
          <LanguageSwitcher />
          <ThemeSwitcher />
          <button
            v-if="auth.isAdmin"
            type="button"
            class="selfcheck-btn"
            data-testid="home-selfcheck-entry"
            @click="selfCheckOpen = true"
          >
            <SafetyCertificateOutlined />
            <span>{{ $t('health.entry') }}</span>
          </button>
        </div>
      </header>

      <!-- 主操作：一句话开工 -->
      <section class="composer-card">
        <div class="composer">
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
        <div class="composer-samples">
          <button
            v-for="(p, i) in samplePrompts"
            :key="i"
            type="button"
            class="sample-chip"
            @click="fillPrompt(p)"
          >
            {{ p }}
          </button>
        </div>
      </section>

      <!-- 继续上次：个人 Agent 最重要的一个动作 -->
      <button
        v-if="continueSession"
        type="button"
        class="continue-card"
        @click="openSession(continueSession.id)"
      >
        <span class="continue-main">
          <span class="continue-kicker">{{ $t('common.home_continue_kicker') }}</span>
          <span class="continue-title">{{ sessionName(continueSession) }}</span>
          <span class="continue-time">{{ formatSmart(continueSession.updated_at) }}</span>
        </span>
        <span class="continue-btn">
          {{ $t('common.home_continue_action') }}
          <ArrowRightOutlined />
        </span>
      </button>

      <div class="home-grid">
        <!-- 最近会话 -->
        <section class="panel recent-panel">
          <div class="panel-head">
            <h2 class="panel-title">{{ $t('common.recent_sessions') }}</h2>
            <button type="button" class="panel-link" @click="router.push('/chat')">
              {{ $t('common.view_all') }}
            </button>
          </div>
          <div v-if="sessionsLoading" class="panel-hint">{{ $t('common.loading') }}</div>
          <EmptyState
            v-else-if="!sessions.length"
            :description="sessionsError || $t('common.no_sessions_yet')"
            size="list"
          />
          <template v-else>
            <ul class="session-list">
              <li v-for="s in sessions" :key="s.id">
                <button type="button" class="session-row" @click="openSession(s.id)">
                  <span class="session-title">{{ sessionName(s) }}</span>
                  <span class="session-time">{{ formatSmart(s.updated_at) }}</span>
                </button>
              </li>
            </ul>
          </template>
        </section>

        <aside class="home-side">
          <!-- 能力入口 -->
          <section class="panel">
            <div class="panel-head">
              <h2 class="panel-title">{{ $t('common.home_capabilities') }}</h2>
              <button
                type="button"
                class="panel-link"
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
              </button>
            </div>
          </section>

          <!-- 用量（近 7 天） -->
          <section class="panel">
            <div class="panel-head">
              <h2 class="panel-title">{{ $t('common.usage_overview') }}</h2>
              <span class="panel-range">{{ $t('common.last_7_days') }}</span>
            </div>
            <div v-if="usageLoading" class="panel-hint">{{ $t('common.loading') }}</div>
            <EmptyState
              v-else-if="!usage"
              :description="usageError || $t('common.no_usage_data_yet')"
              size="list"
            />
            <div v-else class="usage-stats">
              <div class="usage-stat">
                <span class="usage-value">{{ usageCalls }}</span>
                <span class="usage-label">{{ $t('billing.usage_col_events') }}</span>
              </div>
              <div class="usage-stat">
                <span class="usage-value">{{ usage.total_spent ?? 0 }}</span>
                <span class="usage-label">{{ $t('billing.usage_col_cost') }}</span>
              </div>
            </div>
          </section>
        </aside>
      </div>

      <footer class="home-footer">
        <span class="home-footer-brand">
          <span class="home-footer-logo">C</span>chiron
        </span>
        <span class="home-footer-note">
          {{ $t('common.self_hosted_open_source_your_data_stays_on_your_machine') }}
        </span>
        <button type="button" class="footer-new" @click="router.push('/chat')">
          <PlusOutlined />
          {{ $t('common.home_new_chat') }}
        </button>
      </footer>
    </div>

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
.home {
  min-height: 100%;
  background:
    radial-gradient(900px 420px at 20% -10%, color-mix(in srgb, var(--primary) 14%, transparent), transparent 70%),
    radial-gradient(700px 380px at 90% 0%, color-mix(in srgb, var(--node-indigo, #6366f1) 10%, transparent), transparent 70%);
}
.home-shell {
  max-width: 1080px;
  margin: 0 auto;
  padding: 48px 28px 56px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.home-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.home-head-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.home-greeting { display: flex; flex-direction: column; gap: 6px; }
.greeting-line { font-size: 13px; color: var(--text-secondary); letter-spacing: 0.02em; }
.greeting-title { font-size: 28px; line-height: 1.2; margin: 0; font-weight: var(--fw-semibold); }
.greeting-sub { margin: 0; color: var(--text-secondary); font-size: 13px; }
.selfcheck-btn {
  display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid var(--border-card, var(--border));
  background: var(--bg-elevated, var(--bg-card));
  color: var(--text-secondary);
  border-radius: 999px;
  padding: 6px 12px;
  font-size: 12px;
  cursor: pointer;
  transition: color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.selfcheck-btn:hover { color: var(--primary); border-color: var(--primary); }

/* 主输入：视觉中心 */
.composer-card {
  background: var(--bg-elevated, var(--bg-card));
  border: 1px solid var(--border-card, var(--border));
  border-radius: 18px;
  padding: 16px;
  box-shadow: var(--shadow-popover);
}
.composer { display: flex; gap: 10px; align-items: center; }
.quick-input { flex: 1; }
.composer-samples { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.sample-chip {
  border: 1px dashed var(--border-card, var(--border));
  background: transparent;
  color: var(--text-secondary);
  border-radius: 999px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sample-chip:hover { color: var(--primary); border-color: var(--primary); }

/* 继续上次 */
.continue-card {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  width: 100%; text-align: start; font: inherit; color: inherit;
  border: 1px solid var(--border-card, var(--border));
  background: linear-gradient(120deg, color-mix(in srgb, var(--primary) 10%, transparent), transparent 60%), var(--bg-elevated, var(--bg-card));
  border-radius: 14px;
  padding: 14px 18px;
  cursor: pointer;
  transition: border-color var(--dur-fast) ease, transform var(--dur-fast) ease;
}
.continue-card:hover { border-color: var(--primary); transform: translateY(-1px); }
.continue-main { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.continue-kicker { font-size: 11px; color: var(--primary); letter-spacing: 0.08em; text-transform: uppercase; }
.continue-title { font-size: 15px; font-weight: var(--fw-semibold); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.continue-time { font-size: 12px; color: var(--text-tertiary); }
.continue-btn { color: var(--primary); display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }

.home-grid { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: 18px; }
.home-side { display: flex; flex-direction: column; gap: 18px; }
.panel {
  background: var(--bg-elevated, var(--bg-card));
  border: 1px solid var(--border-card, var(--border));
  border-radius: 14px;
  padding: 16px;
}
.panel-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.panel-title { margin: 0; font-size: 14px; font-weight: var(--fw-semibold); }
.panel-link { border: none; background: none; color: var(--primary); font-size: 12px; cursor: pointer; padding: 0; }
.panel-range { font-size: 12px; color: var(--text-tertiary); }
.panel-hint { font-size: 13px; color: var(--text-secondary); padding: 12px 0; }

.session-list { list-style: none; margin: 0; padding: 0; }
.session-list li + li { border-top: 1px solid var(--border-card, var(--border)); }
.session-row {
  width: 100%;
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  border: none; background: none; text-align: start;
  padding: 10px 4px; cursor: pointer; border-radius: 8px;
}
.session-row:hover { background: color-mix(in srgb, var(--primary) 6%, transparent); }
.session-title { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-time { font-size: 12px; color: var(--text-tertiary); white-space: nowrap; }

.entry-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.entry-card {
  display: flex; flex-direction: column; align-items: flex-start; gap: 6px;
  border: 1px solid var(--border-card, var(--border));
  background: transparent; border-radius: 10px;
  padding: 10px; cursor: pointer; text-align: start;
}
.entry-card:hover { border-color: var(--primary); color: var(--primary); }
.entry-icon { font-size: 16px; color: var(--primary); }
.entry-name { font-size: 12px; font-weight: var(--fw-medium); }

.usage-stats { display: flex; gap: 32px; padding: 6px 0 2px; }
.usage-stat { display: flex; flex-direction: column; gap: 2px; }
.usage-value { font-size: 26px; font-weight: var(--fw-semibold); font-variant-numeric: tabular-nums; }
.usage-label { font-size: 12px; color: var(--text-secondary); }

.home-footer {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding-top: 8px; color: var(--text-tertiary); font-size: 12px;
}
.home-footer-brand { display: inline-flex; align-items: center; gap: 6px; color: var(--text-secondary); }
.home-footer-logo {
  width: 18px; height: 18px; border-radius: 6px; display: inline-flex; align-items: center; justify-content: center;
  background: var(--primary); color: var(--text-inverse); font-size: 11px; font-weight: 700;
}
.home-footer-note { flex: 1; min-width: 200px; }
.footer-new {
  display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid var(--border-card, var(--border)); background: transparent;
  color: var(--text-secondary); border-radius: 999px; padding: 6px 12px; font-size: 12px; cursor: pointer;
}
.footer-new:hover { color: var(--primary); border-color: var(--primary); }

@media (max-width: 900px) {
  .home-shell { padding: 32px 18px 44px; }
  .home-grid { grid-template-columns: minmax(0, 1fr); }
  .entry-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .greeting-title { font-size: 24px; }
  .composer { flex-direction: column; align-items: stretch; }
}
</style>
