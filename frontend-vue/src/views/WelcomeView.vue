<script setup lang="ts">
/**
 * 首次运行引导（P1-10）：新装机按「配置模型 → 连通性 → 可选能力 → 第一条消息」走完，
 * 再进主界面。
 *
 * 三条设计约束（都来自"这份页面会被第一次打开软件的人看到"）：
 *  1. **每一步都能跳过**。装好就用是硬需求，任何一个组件今天不可用都不该把用户锁在引导里；
 *  2. **状态只从真实接口来**。模型看 `/v1/models`、连通性看 `/v1/system/components`、
 *     能力看 `/v1/kb` 与 `/v1/system/isolation` —— 不读上一次的界面缓存，
 *     否则用户会在"看起来已就绪"的引导里反复撞墙；
 *  3. **进度按 userId 隔离**，存在 localStorage（见 `api/welcomeStatus.ts`）。
 *     引导是"本机 + 本账号"的一次性体验，不需要后端新表。
 *
 * 与自动跳转的分工：跳不跳由路由守卫判定（有登录、无模型、未 done），本页只负责
 * "到了之后怎么走"。守卫判定的缓存见 `shouldAutoEnterWelcome` 的 30s TTL 注释。
 */
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Button, Spin } from 'ant-design-vue'
import {
  CheckCircleFilled,
  CloseCircleFilled,
  MinusCircleOutlined,
  ReloadOutlined,
} from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import type { LlmModel } from '../api'
import { useAuthStore } from '../stores/auth'
// 第一步之后常驻的自检面板（P1-9）：与首页抽屉 / 设置顶部共用同一个组件与单例数据源
import SelfCheckPanel from '../components/health/SelfCheckPanel.vue'
import {
  WELCOME_STEPS,
  invalidateWelcomeModelCache,
  loadWelcomeProgress,
  markWelcomeStep,
  probeCapabilities,
  probeConnectivity,
  probeModels,
  resetWelcomeProgress,
  setWelcomeStep,
  type CapabilitiesProbe,
  type ConnectivityProbe,
  type WelcomeProgress,
  type WelcomeStepId,
  type WelcomeStepStatus,
} from '../api/welcomeStatus'

const { t } = useI18n()
const router = useRouter()
const auth = useAuthStore()

// 进度按 userId 隔离：身份异步到达（本地会话 / profile 拉取）时重新读一次，
// 否则会把上一位用户的进度当成自己的。
const userId = computed(() => auth.user?.id ?? '')
const progress = ref<WelcomeProgress>(loadWelcomeProgress(userId.value))
watch(userId, id => {
  progress.value = loadWelcomeProgress(id)
})

const currentStep = computed<WelcomeStepId>(() => progress.value.step)
const currentIndex = computed(() => Math.max(0, WELCOME_STEPS.indexOf(currentStep.value)))
const finished = computed(() => progress.value.done)

/**
 * 步骤 id（camelCase，同时是存储键）→ 文案 key 的映射。
 *
 * 不直接拼 `welcome.step_${step}`：`firstMessage` 的文案 key 是 `step_first_message`，
 * 拼字符串会得到一个永远命不中的键（界面上显示原始 key）。
 */
const STEP_LABEL_KEYS: Record<WelcomeStepId, string> = {
  models: 'welcome.step_models',
  connectivity: 'welcome.step_connectivity',
  capabilities: 'welcome.step_capabilities',
  firstMessage: 'welcome.step_first_message',
}

function stepLabel(step: WelcomeStepId): string {
  return t(STEP_LABEL_KEYS[step])
}

/** 三组探测各自的状态机：idle = 还没查，loading = 查询中，ready = 有结果（成功或失败） */
type ProbeState = 'idle' | 'loading' | 'ready'

const modelsState = ref<ProbeState>('idle')
const models = ref<LlmModel[]>([])
const modelsFailed = ref(false)

const connectivityState = ref<ProbeState>('idle')
const connectivity = ref<ConnectivityProbe | null>(null)

const capabilitiesState = ref<ProbeState>('idle')
const capabilities = ref<CapabilitiesProbe | null>(null)

async function loadModels(): Promise<void> {
  modelsState.value = 'loading'
  const probe = await probeModels()
  models.value = probe.models
  modelsFailed.value = !probe.ok
  modelsState.value = 'ready'
}

async function loadConnectivity(): Promise<void> {
  connectivityState.value = 'loading'
  connectivity.value = await probeConnectivity()
  connectivityState.value = 'ready'
}

async function loadCapabilities(): Promise<void> {
  capabilitiesState.value = 'loading'
  capabilities.value = await probeCapabilities()
  capabilitiesState.value = 'ready'
}

/** 只在进入某一步时按需探测：先让第 1 步尽快出结果，不把四个请求堆在启动路径上 */
function ensureStepData(step: WelcomeStepId): void {
  if (step === 'models' && modelsState.value === 'idle') void loadModels()
  if (step === 'connectivity' && connectivityState.value === 'idle') void loadConnectivity()
  if (step === 'capabilities' && capabilitiesState.value === 'idle') void loadCapabilities()
}

watch(currentStep, step => ensureStepData(step), { immediate: true })

const modelsReady = computed(
  () => modelsState.value === 'ready' && !modelsFailed.value && models.value.length > 0,
)
const connectivityReady = computed(
  () => connectivityState.value === 'ready' && connectivity.value?.allUp === true,
)

/**
 * "完成"按钮是否可用。
 *
 * 只有前两步有硬性条件（模型必须存在、必需组件必须健康）：第 3 步是可选能力，
 * 第 4 步只是跳转。条件不满足时用户仍然可以跳过，不会被卡住。
 */
const canAdvance = computed(() => {
  if (currentStep.value === 'models') return modelsReady.value
  if (currentStep.value === 'connectivity') return connectivityReady.value
  return true
})

const modelsSummary = computed(() => {
  if (modelsState.value !== 'ready') return t('welcome.models_loading')
  if (modelsFailed.value) return t('welcome.models_error')
  if (models.value.length > 0) return t('welcome.models_count', { count: models.value.length })
  return t('welcome.models_empty')
})

const knowledgeSummary = computed(() => {
  if (capabilitiesState.value !== 'ready') return t('welcome.kb_loading')
  const knowledge = capabilities.value?.knowledge
  if (!knowledge || !knowledge.ok) return t('welcome.kb_error')
  if (knowledge.ready > 0) {
    return t('welcome.kb_ready', { ready: knowledge.ready, total: knowledge.total })
  }
  if (knowledge.indexing > 0) return t('welcome.kb_indexing')
  return t('welcome.kb_none')
})

const isolationLabel = computed(() => {
  const isolation = capabilities.value?.isolation
  if (!isolation?.ok || !isolation.effective) return t('welcome.isolation_unknown')
  return t(`settings.isolation_level_${isolation.effective}`)
})

const isolationBelowFloor = computed(() => capabilities.value?.isolation.belowFloor === true)

function stepRecorded(step: WelcomeStepId): boolean {
  return !!progress.value.steps[step]
}

function stepReachable(step: WelcomeStepId): boolean {
  const index = WELCOME_STEPS.indexOf(step)
  return index >= 0 && index <= currentIndex.value
}

/**
 * 记录当前步的结局并前进。
 *
 * 最后一步（第一条消息）不再停留在本页：完成 → `/chat`，跳过 → `/`。
 * 两种结局都写 done，因此此后不会再被守卫自动拉回引导。
 */
function finishCurrentStep(status: WelcomeStepStatus): void {
  const step = currentStep.value
  progress.value = markWelcomeStep(userId.value, step, status)
  if (step === 'firstMessage') {
    void router.push(status === 'completed' ? '/chat' : '/')
    return
  }
  ensureStepData(progress.value.step)
}

function goToStep(step: WelcomeStepId): void {
  if (!stepReachable(step)) return
  progress.value = setWelcomeStep(userId.value, step)
}

function prevStep(): void {
  if (currentIndex.value <= 0) return
  goToStep(WELCOME_STEPS[currentIndex.value - 1])
}

/** 跳过全部：把四步都记成 skipped 并回首页 —— 跳过也是"走完"，不能反复弹 */
function skipAll(): void {
  for (const step of WELCOME_STEPS) {
    progress.value = markWelcomeStep(userId.value, step, 'skipped')
  }
  void router.push('/')
}

function restart(): void {
  progress.value = resetWelcomeProgress(userId.value)
  modelsState.value = 'idle'
  connectivityState.value = 'idle'
  capabilitiesState.value = 'idle'
  invalidateWelcomeModelCache()
  ensureStepData(progress.value.step)
}

function openModels(): void {
  // 用户在 /models 存完模型回到首页时，守卫不该还拿着"没有模型"的旧缓存把人弹回引导
  invalidateWelcomeModelCache()
  void router.push('/models')
}
</script>

<template>
  <div
    class="welcome-page"
    data-testid="welcome-page"
  >
    <header class="welcome-head">
      <button
        v-if="!finished"
        type="button"
        class="welcome-skip-all"
        data-testid="welcome-skip-all"
        @click="skipAll"
      >
        {{ t('welcome.skip_all') }}
      </button>
      <span class="welcome-kicker">{{ t('welcome.kicker') }}</span>
      <h1 class="welcome-title">
        {{ t('welcome.title') }}
      </h1>
      <p class="welcome-sub">
        {{ t('welcome.subtitle') }}
      </p>
    </header>

    <template v-if="finished">
      <section
        class="welcome-card welcome-done"
        data-testid="welcome-done"
      >
        <CheckCircleFilled class="welcome-done-icon" />
        <h2>{{ t('welcome.done_title') }}</h2>
        <p class="welcome-desc">
          {{ t('welcome.done_desc') }}
        </p>
        <div class="welcome-actions welcome-actions--center">
          <Button
            type="primary"
            data-testid="welcome-done-chat"
            @click="router.push('/chat')"
          >
            {{ t('welcome.go_chat') }}
          </Button>
          <Button
            data-testid="welcome-restart"
            @click="restart"
          >
            {{ t('welcome.restart') }}
          </Button>
          <Button
            type="text"
            data-testid="welcome-done-home"
            @click="router.push('/')"
          >
            {{ t('welcome.back_home') }}
          </Button>
        </div>
      </section>
    </template>

    <template v-else>
      <!-- 步骤导航：已完成/已到达的步骤可回看；尚未到达的置灰（键盘仍可聚焦，故用 disabled） -->
      <nav
        class="welcome-steps-wrap"
        :aria-label="t('welcome.steps_label')"
      >
        <ol class="welcome-steps">
          <li
            v-for="(step, index) in WELCOME_STEPS"
            :key="step"
          >
            <button
              type="button"
              class="step-btn"
              :class="{ active: currentStep === step }"
              :disabled="!stepReachable(step)"
              :aria-current="currentStep === step ? 'step' : undefined"
              :data-testid="`welcome-step-${step}`"
              @click="goToStep(step)"
            >
              <span class="step-num">{{ index + 1 }}</span>
              <span class="step-name">{{ stepLabel(step) }}</span>
              <CheckCircleFilled
                v-if="stepRecorded(step)"
                class="step-done-icon"
              />
            </button>
          </li>
        </ol>
      </nav>

      <section
        class="welcome-card"
        aria-live="polite"
      >
        <!-- ① 模型 -->
        <div
          v-if="currentStep === 'models'"
          data-testid="welcome-panel-models"
        >
          <h2>{{ t('welcome.models_title') }}</h2>
          <p class="welcome-desc">
            {{ t('welcome.models_desc') }}
          </p>
          <div
            class="welcome-status"
            :class="modelsReady ? 'ok' : (modelsState === 'ready' ? 'warn' : '')"
            role="status"
          >
            <Spin
              v-if="modelsState !== 'ready'"
              size="small"
            />
            <CheckCircleFilled
              v-else-if="modelsReady"
              class="status-icon"
            />
            <MinusCircleOutlined
              v-else
              class="status-icon"
            />
            <span>{{ modelsSummary }}</span>
          </div>
          <p
            v-if="!auth.isAdmin"
            class="welcome-hint"
          >
            {{ t('welcome.models_admin_hint') }}
          </p>
          <div class="welcome-actions">
            <Button
              type="primary"
              data-testid="welcome-primary"
              :disabled="!canAdvance"
              @click="finishCurrentStep('completed')"
            >
              {{ t('welcome.next') }}
            </Button>
            <Button
              v-if="auth.isAdmin"
              data-testid="welcome-configure"
              @click="openModels"
            >
              {{ t('welcome.go_configure') }}
            </Button>
            <Button
              data-testid="welcome-recheck"
              @click="loadModels"
            >
              <ReloadOutlined />
              {{ t('welcome.recheck') }}
            </Button>
            <Button
              type="text"
              data-testid="welcome-skip"
              @click="finishCurrentStep('skipped')"
            >
              {{ t('welcome.skip_step') }}
            </Button>
          </div>
        </div>

        <!-- ② 连通性 -->
        <div
          v-else-if="currentStep === 'connectivity'"
          data-testid="welcome-panel-connectivity"
        >
          <h2>{{ t('welcome.conn_title') }}</h2>
          <p class="welcome-desc">
            {{ t('welcome.conn_desc') }}
          </p>
          <div
            class="welcome-status"
            :class="connectivityReady ? 'ok' : (connectivityState === 'ready' ? 'warn' : '')"
            role="status"
          >
            <Spin
              v-if="connectivityState !== 'ready'"
              size="small"
            />
            <CheckCircleFilled
              v-else-if="connectivityReady"
              class="status-icon"
            />
            <MinusCircleOutlined
              v-else
              class="status-icon"
            />
            <span>
              {{ connectivityState !== 'ready'
                ? t('welcome.conn_loading')
                : (connectivityReady
                  ? t('welcome.conn_ok')
                  : (connectivity?.ok ? t('welcome.conn_degraded') : t('welcome.conn_error'))) }}
            </span>
          </div>
          <ul
            v-if="connectivityState === 'ready'"
            class="component-list"
          >
            <li
              v-for="row in connectivity?.rows ?? []"
              :key="row.key"
              class="component-row"
            >
              <CheckCircleFilled
                v-if="row.up === true"
                class="icon-ok"
              />
              <CloseCircleFilled
                v-else-if="row.up === false"
                class="icon-down"
              />
              <MinusCircleOutlined
                v-else
                class="icon-unknown"
              />
              <span class="component-name">{{ t(`welcome.component_${row.key}`) }}</span>
              <span class="component-state">
                {{ row.up === true
                  ? t('welcome.component_up')
                  : (row.up === false ? t('welcome.component_down') : t('welcome.component_unknown')) }}
              </span>
            </li>
          </ul>
          <div class="welcome-actions">
            <Button
              type="primary"
              data-testid="welcome-primary"
              :disabled="!canAdvance"
              @click="finishCurrentStep('completed')"
            >
              {{ t('welcome.next') }}
            </Button>
            <Button
              v-if="auth.isAdmin"
              data-testid="welcome-diagnostics"
              @click="router.push('/admin/diagnostics')"
            >
              {{ t('welcome.conn_diagnostics') }}
            </Button>
            <Button
              data-testid="welcome-recheck"
              @click="loadConnectivity"
            >
              <ReloadOutlined />
              {{ t('welcome.recheck') }}
            </Button>
            <Button
              type="text"
              data-testid="welcome-skip"
              @click="finishCurrentStep('skipped')"
            >
              {{ t('welcome.skip_step') }}
            </Button>
          </div>
        </div>

        <!-- ③ 可选能力 -->
        <div
          v-else-if="currentStep === 'capabilities'"
          data-testid="welcome-panel-capabilities"
        >
          <h2>{{ t('welcome.cap_title') }}</h2>
          <p class="welcome-desc">
            {{ t('welcome.cap_desc') }}
          </p>
          <div
            class="cap-row"
            role="status"
          >
            <span class="cap-name">{{ t('welcome.kb_label') }}</span>
            <span class="cap-value">{{ knowledgeSummary }}</span>
          </div>
          <div class="cap-row">
            <span class="cap-name">{{ t('welcome.isolation_label') }}</span>
            <span class="cap-value">{{ isolationLabel }}</span>
          </div>
          <p class="welcome-hint">
            {{ t('welcome.isolation_note') }}
          </p>
          <p
            v-if="isolationBelowFloor"
            class="welcome-hint welcome-hint--warn"
          >
            {{ t('welcome.isolation_below_floor') }}
          </p>
          <div class="welcome-actions">
            <Button
              type="primary"
              data-testid="welcome-primary"
              @click="finishCurrentStep('completed')"
            >
              {{ t('welcome.next') }}
            </Button>
            <Button
              data-testid="welcome-open-knowledge"
              @click="router.push('/knowledge')"
            >
              {{ t('welcome.kb_open') }}
            </Button>
            <Button
              type="text"
              data-testid="welcome-skip"
              @click="finishCurrentStep('skipped')"
            >
              {{ t('welcome.skip_step') }}
            </Button>
          </div>
        </div>

        <!-- ④ 第一条消息 -->
        <div
          v-else
          data-testid="welcome-panel-firstMessage"
        >
          <h2>{{ t('welcome.fm_title') }}</h2>
          <p class="welcome-desc">
            {{ t('welcome.fm_desc') }}
          </p>
          <div class="welcome-actions">
            <Button
              type="primary"
              data-testid="welcome-primary"
              @click="finishCurrentStep('completed')"
            >
              {{ t('welcome.fm_start') }}
            </Button>
            <Button
              type="text"
              data-testid="welcome-skip"
              @click="finishCurrentStep('skipped')"
            >
              {{ t('welcome.fm_skip') }}
            </Button>
          </div>
        </div>
      </section>

      <!-- 系统自检（P1-9）：走完第一步后常驻显示（折叠）。
           只在引导进行中出现 —— 完成态那一屏保持"只看结果"，且已完成的用户
           再进来时不该有任何额外探测（见 WelcomeView.spec 的"不探测"用例）。 -->
      <SelfCheckPanel
        v-if="currentIndex >= 1"
        class="welcome-selfcheck"
        data-testid="welcome-selfcheck"
      />

      <footer class="welcome-foot">
        <Button
          v-if="currentIndex > 0"
          data-testid="welcome-back"
          @click="prevStep"
        >
          {{ t('welcome.back') }}
        </Button>
        <span class="welcome-progress">
          {{ t('welcome.progress', { current: currentIndex + 1, total: WELCOME_STEPS.length }) }}
        </span>
      </footer>
    </template>
  </div>
</template>

<style scoped>
/* 单栏 + 限宽：引导页在任何窗口宽度下都只有一条阅读线，窄屏不折叠成卡片墙 */
.welcome-page {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
  max-width: 880px;
  margin: 0 auto;
  padding: var(--space-8) var(--space-6) var(--space-16);
}

.welcome-head {
  position: relative;
}
.welcome-kicker {
  font-size: var(--fs-xs);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--primary);
}
.welcome-title {
  margin: var(--space-2) 0 var(--space-2);
  font-size: var(--fs-3xl);
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
}
.welcome-sub {
  max-width: 62ch;
  margin: 0;
  color: var(--text-secondary);
  font-size: var(--fs-base);
  line-height: 1.6;
}
.welcome-skip-all {
  position: absolute;
  inset-inline-end: 0;
  top: 0;
  padding: 2px 6px;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-tertiary);
  font-size: var(--fs-sm);
  cursor: pointer;
  transition: color var(--dur-fast) ease;
}
.welcome-skip-all:hover {
  color: var(--text-primary);
}
.welcome-skip-all:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 1px;
}

/* ── 步骤导航 ── */
.welcome-steps {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}
.step-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: 6px 12px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-full);
  background: var(--bg-surface);
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  cursor: pointer;
  transition:
    background var(--dur-fast) ease,
    border-color var(--dur-fast) ease,
    color var(--dur-fast) ease;
}
.step-btn:hover:not(:disabled) {
  border-color: var(--primary);
  color: var(--text-primary);
}
.step-btn:focus-visible {
  outline: 2px solid var(--primary);
  outline-offset: 1px;
}
.step-btn:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
.step-btn.active {
  border-color: var(--primary);
  background: var(--primary-bg);
  color: var(--text-primary);
}
.step-num {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: var(--radius-full);
  background: var(--bg-surface-hover);
  font-size: var(--fs-xs);
}
.step-btn.active .step-num {
  background: var(--primary);
  color: var(--bg-surface);
}
.step-done-icon {
  color: var(--success);
  font-size: var(--fs-sm);
}

/* ── 卡片 ── */
.welcome-card {
  padding: var(--space-6);
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--comp-card-radius);
  background: var(--bg-surface);
  box-shadow: var(--shadow-sm);
}
.welcome-card h2 {
  margin: 0 0 var(--space-2);
  font-size: var(--fs-xl);
  color: var(--text-primary);
}
.welcome-desc {
  margin: 0 0 var(--space-4);
  color: var(--text-secondary);
  font-size: var(--fs-base);
  line-height: 1.6;
}

.welcome-status {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-lg);
  background: var(--bg-surface-hover);
  color: var(--text-secondary);
  font-size: var(--fs-sm);
  line-height: 1.5;
}
.welcome-status.ok {
  background: var(--success-bg);
  color: var(--success);
}
.welcome-status.warn {
  background: var(--warning-bg);
  color: var(--warning);
}
.status-icon {
  flex: none;
}

/* ── 组件清单 ── */
.component-list {
  display: grid;
  gap: var(--space-1);
  margin: var(--space-3) 0 0;
  padding: 0;
  list-style: none;
}
.component-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
}
.component-name {
  color: var(--text-primary);
}
.component-state {
  color: var(--text-tertiary);
}
.icon-ok {
  color: var(--success);
}
.icon-down {
  color: var(--error);
}
.icon-unknown {
  color: var(--text-tertiary);
}

/* ── 能力行 ── */
.cap-row {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-2);
  padding: var(--space-3) 0;
  border-bottom: var(--hairline-width) solid var(--border-subtle);
  font-size: var(--fs-sm);
}
.cap-name {
  min-width: 120px;
  color: var(--text-primary);
}
.cap-value {
  color: var(--text-secondary);
  line-height: 1.5;
}

.welcome-hint {
  margin: var(--space-3) 0 0;
  color: var(--text-tertiary);
  font-size: var(--fs-sm);
  line-height: 1.6;
}
.welcome-hint--warn {
  color: var(--warning);
}

/* ── 操作区 ── */
.welcome-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-5);
}
.welcome-actions--center {
  justify-content: center;
}

.welcome-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
}
.welcome-progress {
  color: var(--text-tertiary);
  font-size: var(--fs-xs);
}

/* ── 完成态 ── */
.welcome-done {
  text-align: center;
}
.welcome-done-icon {
  color: var(--success);
  font-size: var(--fs-4xl);
}
.welcome-done h2 {
  margin: var(--space-3) 0 var(--space-2);
}

/* 窄屏：单栏、跳过按钮不再绝对定位（否则会压住标题） */
@media (max-width: 640px) {
  .welcome-page {
    padding: var(--space-5) var(--space-4) var(--space-12);
  }
  .welcome-skip-all {
    position: static;
    align-self: flex-start;
    margin-bottom: var(--space-1);
  }
  .welcome-head {
    display: flex;
    flex-direction: column;
  }
  .welcome-steps {
    flex-direction: column;
  }
  .step-btn {
    width: 100%;
    justify-content: flex-start;
  }
  .welcome-card {
    padding: var(--space-4);
  }
  .cap-name {
    min-width: 0;
  }
}
</style>
