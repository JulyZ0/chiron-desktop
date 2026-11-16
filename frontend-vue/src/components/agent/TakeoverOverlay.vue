<script setup lang="ts">
/**
 * Super-agent 接管段的全站遮罩。
 *
 * ## 为什么是遮罩而不是禁用按钮
 *
 * 接管期间 Agent 正在同一会话上跑真实回合。用户可以点的地方每多一个，就多一个
 * 与自动回合争抢同一会话的入口 —— 输入框、发送、重试、继续生成，逐个置灰既容易
 * 漏，也挡不住快捷键和路由跳转。一个 `position: fixed` 的全屏遮罩把整棵 DOM 都
 * 挡在外面，是唯一不需要穷举的做法。
 *
 * ## 但必须留一个出口
 *
 * 遮罩里**只**允许一件事：打断。这是用户要求的硬条件 —— 锁住但能撤销，
 * 否则 Agent 跑飞时人只能杀进程。
 *
 * ## 打断为什么是两拍
 *
 * - `POST /v1/agent/interrupt` 停掉正在跑的 run（真正停止生成）
 * - `POST /v1/superagent/interrupt` 取消接管段（撤锁、写冷却、once 模式关开关）
 *
 * 只发前者：段还在、锁还挂着，遮罩不会消失。
 * 只发后者：锁撤了但 run 仍在写文件。
 * 两个都要发，且并发发 —— 每多一次串行往返，run 就多改一个文件。
 *
 * ## `known=false` 不等于「没接管」
 *
 * 引擎的 `/segment/{id}` 用 `known` 区分"Redis 查过了，没有段"与"Redis 都连不上，
 * 我不知道"。后者若当成"没接管"，遮罩会在引擎抖动的瞬间消失 —— 恰好在最不该
 * 放开的时候放开。所以拿不到答案时**保持上一次的状态**。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import {
  getTakeoverSegment,
  interruptAgentRun,
  interruptTakeover,
  listTakeovers,
} from '../../api'

const { t } = useI18n()

/** 轮询间隔：接管是分钟级的，5 秒足够及时，又不至于打满网关 */
const POLL_MS = 5000
/** 已运行时间的刷新间隔 */
const TICK_MS = 1000

interface ActiveSegment {
  sessionId: string
  takeoverId: string
  startedAt: number
  /** 该会话今日已消耗，0 表示未知 */
  tokensUsed: number
  /** 日预算，0 = 不限 */
  budget: number
}

const segment = ref<ActiveSegment | null>(null)
const stopping = ref(false)
const nowMs = ref(Date.now())

let pollTimer: ReturnType<typeof setInterval> | null = null
let tickTimer: ReturnType<typeof setInterval> | null = null
/** 防止轮询请求叠加 */
let polling = false

const visible = computed(() => segment.value !== null)

const elapsedText = computed(() => {
  if (!segment.value) return ''
  const total = Math.max(0, Math.floor((nowMs.value - segment.value.startedAt) / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`
})

/** 预算剩余；未设预算返回 `null`，交由模板显示"不限" */
const budgetLeft = computed(() => {
  if (!segment.value) return null
  if (!segment.value.budget) return null
  return Math.max(0, segment.value.budget - segment.value.tokensUsed)
})

async function refresh() {
  if (polling) return
  polling = true
  try {
    const items = await listTakeovers()
    const enabled = items.filter((it) => it.enabled)
    if (!enabled.length) {
      segment.value = null
      return
    }

    const snaps = await Promise.all(
      enabled.map((it) =>
        getTakeoverSegment(it.session_id).catch(() => null),
      ),
    )

    // 先看有没有确定的「在接管」。`known=false` 的会话跳过而不是判定为无。
    for (let i = 0; i < enabled.length; i += 1) {
      const snap = snaps[i]
      if (!snap || snap.known === false) continue
      if (!snap.active) continue
      const started = snap.started_at ? Date.parse(String(snap.started_at)) : Date.now()
      segment.value = {
        sessionId: snap.session_id || enabled[i].session_id,
        takeoverId: snap.takeover_id || enabled[i].id,
        startedAt: Number.isNaN(started) ? Date.now() : started,
        tokensUsed: enabled[i].tokens_used_today || 0,
        budget: enabled[i].daily_token_budget || 0,
      }
      return
    }

    // 全部查询都拿不到答案（引擎/Redis 不可用）⇒ 维持现状，不擅自撤锁
    const anyKnown = snaps.some((s) => s !== null && s.known !== false)
    if (!anyKnown && segment.value) return

    segment.value = null
  } catch {
    // 拉取失败同样维持现状：宁可多锁一会儿，也不要在引擎抖动时放开
  } finally {
    polling = false
  }
}

async function stopTakeover() {
  if (!segment.value || stopping.value) return
  stopping.value = true
  const { sessionId, takeoverId } = segment.value
  try {
    // 并发发，不串行：run 每多跑一秒都在写盘
    const [, takeResult] = await Promise.all([
      interruptAgentRun(sessionId),
      interruptTakeover(sessionId, takeoverId).catch(() => null),
    ])
    segment.value = null
    if (takeResult?.disabled) {
      message.success(t('agent.superagent_interrupt_disabled'))
    } else {
      message.success(t('agent.superagent_stopped'))
    }
  } finally {
    stopping.value = false
    // 立刻再查一次：段可能没撤干净（例如引擎侧失败），不能让遮罩假消失
    void refresh()
  }
}

onMounted(() => {
  void refresh()
  pollTimer = setInterval(() => void refresh(), POLL_MS)
  tickTimer = setInterval(() => {
    nowMs.value = Date.now()
  }, TICK_MS)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
  if (tickTimer) clearInterval(tickTimer)
  pollTimer = null
  tickTimer = null
})
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="tk-overlay"
      role="dialog"
      aria-modal="true"
      :aria-label="$t('agent.superagent_overlay_title')"
    >
      <div class="tk-overlay-card">
        <div class="tk-overlay-pulse" aria-hidden="true">
          <span class="tk-overlay-dot" />
        </div>
        <h2 class="tk-overlay-title">
          {{ $t('agent.superagent_overlay_title') }}
        </h2>
        <p class="tk-overlay-sub">
          {{ $t('agent.superagent_overlay_input_locked') }}
        </p>

        <dl class="tk-overlay-stats">
          <div class="tk-overlay-stat">
            <dt>{{ $t('agent.superagent_overlay_elapsed') }}</dt>
            <dd class="tk-mono">
              {{ elapsedText }}
            </dd>
          </div>
          <div class="tk-overlay-stat">
            <dt>{{ $t('agent.superagent_overlay_spent') }}</dt>
            <dd class="tk-mono">
              {{ segment?.tokensUsed ?? 0 }} tokens
            </dd>
          </div>
          <div class="tk-overlay-stat">
            <dt>{{ $t('agent.superagent_overlay_budget') }}</dt>
            <dd class="tk-mono">
              {{ budgetLeft === null ? $t('agent.superagent_overlay_unlimited') : `${budgetLeft} tokens` }}
            </dd>
          </div>
        </dl>

        <!-- 唯一可点的东西。不给二次确认：每多一次往返，run 就多改一个文件。 -->
        <button
          type="button"
          class="tk-overlay-stop"
          :disabled="stopping"
          @click="stopTakeover"
        >
          {{ stopping ? $t('agent.superagent_overlay_stopping') : $t('agent.superagent_stop') }}
        </button>
        <p class="tk-overlay-hint">
          {{ $t('agent.superagent_stop_text') }}
        </p>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* 遮罩本身吃掉所有指针事件 —— 这是「全站锁」的实现处，不是各页面各自禁用 */
.tk-overlay {
  position: fixed;
  inset: 0;
  z-index: var(--z-takeover);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--takeover-scrim);
  backdrop-filter: blur(6px);
  cursor: not-allowed;
  /* 遮罩出现时不该有入场动画的延迟感：接管是既成事实，不是提示 */
}

.tk-overlay-card {
  width: 100%;
  max-width: 420px;
  padding: 28px 24px 20px;
  border-radius: var(--radius-lg, 12px);
  background: var(--bg-card, #fff);
  box-shadow: var(--takeover-card-shadow);
  text-align: center;
  cursor: default;
}

.tk-overlay-pulse { display: flex; justify-content: center; margin-bottom: 12px; }
.tk-overlay-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--primary, #1677ff);
  box-shadow: 0 0 0 0 var(--primary, #1677ff);
  animation: tk-pulse var(--dur-pulse) ease-out infinite;
}
@keyframes tk-pulse {
  0% { box-shadow: 0 0 0 0 var(--takeover-ring); }
  70% { box-shadow: 0 0 0 12px transparent; }
  100% { box-shadow: 0 0 0 0 transparent; }
}

.tk-overlay-title {
  margin: 0 0 6px;
  font-size: 18px;
  font-weight: var(--fw-semibold, 600);
  color: var(--text-primary);
}
.tk-overlay-sub {
  margin: 0 0 18px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.tk-overlay-stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin: 0 0 20px;
  padding: 12px 0;
  border-top: var(--hairline-width) solid var(--border-card, var(--border));
  border-bottom: var(--hairline-width) solid var(--border-card, var(--border));
}
.tk-overlay-stat { display: flex; flex-direction: column; gap: 4px; }
.tk-overlay-stat dt { font-size: 11px; color: var(--text-secondary); }
.tk-overlay-stat dd { margin: 0; font-size: 13px; color: var(--text-primary); }
.tk-mono { font-variant-numeric: tabular-nums; }

.tk-overlay-stop {
  width: 100%;
  padding: 10px 16px;
  border: none;
  border-radius: var(--radius-md, 8px);
  background: var(--danger, #d4380d);
  color: var(--takeover-stop-fg);
  font-size: 14px;
  font-weight: var(--fw-medium, 500);
  cursor: pointer;
  transition: opacity var(--dur-fast);
}
.tk-overlay-stop:hover:not(:disabled) { opacity: 0.88; }
.tk-overlay-stop:disabled { opacity: 0.6; cursor: not-allowed; }
.tk-overlay-stop:focus-visible { outline: 2px solid var(--takeover-stop-fg); outline-offset: 2px; }

.tk-overlay-hint {
  margin: 10px 0 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-secondary);
}

@media (prefers-reduced-motion: reduce) {
  .tk-overlay-dot { animation: none; }
}
</style>
