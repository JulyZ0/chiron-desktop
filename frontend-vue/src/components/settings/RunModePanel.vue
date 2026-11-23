<script setup lang="ts">
/**
 * 运行模式 / 本机数据 —— 设置页「运行模式」标签页的内容。
 *
 * 两种形态（契约 docs/desktop-local/contracts.md §1）：
 *   · 本地：内置数据库、网关与引擎随桌面壳在本机运行；
 *   · 远程：桌面壳只运行界面，全部 `/v1` 请求发往服务地址，业务在服务端执行。
 * 切换入口是「打开连接设置」——在本地模式下它同样必须是可用的（见 shared.cjs
 * resolveStartupWindow 的 forceSetup）。
 *
 * ## 清空数据为什么要三重确认
 *
 * 这一步会删掉会话、知识库、记忆、上传文件与整个 PG 数据目录，且不可恢复。
 * 误触的代价是用户全部长期数据，所以：
 *
 *   1. 勾选「我已了解」—— 拦住连点两下
 *   2. 手打确认词 —— 拦住不看文案的机械点击
 *   3. Windows Hello（主进程做）—— 拦住"别人替你点"
 *
 * 前两道在渲染进程（防误触），第三道在主进程（防冒用）。**不能互相替代**：
 * 渲染进程说"确认过了"不算数，一个被注入的页面同样能调那个 IPC，
 * 所以身份核验必须在主进程、在真正动手之前完成。
 *
 * ## 渲染进程不删任何东西
 *
 * 所有清理（停后端 → 清分区存储 → 清 token → 删 userData/local → 重启）
 * 都在主进程的 `wipe-data.cjs` 里，这里只收集用户的确认。
 */
import { computed, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { useDeviceStore } from '../../stores/device'
import type { MountDepName, MountDepsStatus } from '../../stores/device'
import { useAuthStore } from '../../stores/auth'
import { api, API_URL } from '../../api'
import { isComponentUp, type ComponentsHealth } from '../../api/systemHealth'
import { serverErrorMessage } from '../../utils/apiError'

const { t } = useI18n()
const device = useDeviceStore()
const auth = useAuthStore()

/** 确认词：与「切换模式」时代同一个思路 —— 要手打，不是点两下 */
const CONFIRM_WORD = 'DELETE'

const modalOpen = ref(false)
const confirmText = ref('')
const acknowledged = ref(false)
/** 主进程正在执行（含 Hello 弹窗的时间，可能十几秒） */
const busy = ref(false)
/** 本机是否有可用的 Windows Hello；false 时改验账户密码 */
const helloAvailable = ref(true)
/** 账户密码：只在 Hello 不可用时需要，用完立即清空 */
const password = ref('')
/** Hello 不可用时，密码成了必填项 */
const passwordRequired = computed(() => !helloAvailable.value)
/** 验证不可用时主进程带回的真实数据目录；拿到之前用文案里的占位路径 */
const wipeDataDir = ref('')

const confirmOk = computed(
  () =>
    acknowledged.value &&
    confirmText.value.trim() === CONFIRM_WORD &&
    (!passwordRequired.value || password.value.length > 0) &&
    !busy.value,
)

function openWipe() {
  // 每次打开都复位：上一次的输入绝不能残留成"已确认"的默认状态
  confirmText.value = ''
  acknowledged.value = false
  password.value = ''
  modalOpen.value = true
  // 提前探测：不可用时立刻把密码框摆出来，别等两次确认后才说"还需要密码"
  void device.checkHelloAvailability().then(ok => { helloAvailable.value = ok })
}

function closeWipe() {
  if (busy.value) return // 清理进行中不允许关掉，避免用户以为取消了
  modalOpen.value = false
}

/** 打开连接设置页：填服务地址 = 切换/回到远程模式；页面里也能切回本地 */
async function openConnection() {
  const ok = await device.openConnectionSetup()
  if (!ok) message.warning(t('settings.run_mode_setup_unsupported'))
}

/** 远程模式的服务地址（来自主进程注入的设备设置快照） */
const serverAddress = computed(() => String(device.settings?.apiUrl || ''))
const testing = ref(false)

async function testConnection() {
  if (testing.value) return
  const url = serverAddress.value.trim()
  if (!url) {
    message.warning(t('settings.run_mode_no_address'))
    return
  }
  testing.value = true
  try {
    await device.testServerUrl(url)
    message.success(t('settings.run_mode_test_ok'))
  } catch (err) {
    const detail = err instanceof Error && err.message ? err.message : String(err)
    message.error(t('settings.run_mode_test_failed', { error: detail }))
  } finally {
    testing.value = false
  }
}

// ── 当前运行状态：远程模式下到底谁在干活、现在通不通 ──
//
// 用户报"远程服务时不清楚应用的工作状态"：只给一段说明文字不够，必须把
// **真实探测结果**摆出来 —— 连接是否可用、服务端各组件的健康态、本机只运行
// 界面、数据在哪、以谁的身份登录。全部取自当前模式实际使用的 API，不另造数据源。
type StatusKind = 'idle' | 'checking' | 'ok' | 'fail'

const isRemote = computed(() => device.mode === 'cloud')
const statusKind = ref<StatusKind>('idle')
const statusLatencyMs = ref<number | null>(null)
const statusError = ref('')
const statusCheckedAt = ref<Date | null>(null)
const components = ref<ComponentsHealth | null>(null)
const modelCount = ref<number | null>(null)

// ── 团队挂载依赖（rclone/WinFsp，仅服务端主机按需安装）──
const depsStatus = ref<MountDepsStatus | null>(null)
const depsBusy = ref<MountDepName | ''>('')

async function loadMountDeps() {
  depsStatus.value = await device.getMountDepsStatus()
}

async function installMountDep(name: MountDepName) {
  if (depsBusy.value) return
  depsBusy.value = name
  try {
    const res = await device.installMountDep(name)
    if (!res.ok) {
      message.error(res.message || t('settings.mount_deps_failed'))
      return
    }
    if (res.launched) message.info(t('settings.mount_deps_winfsp_launched'))
    else message.success(t('settings.mount_deps_done'))
    await loadMountDeps()
  } finally {
    depsBusy.value = ''
  }
}

/** 当前模式实际使用的服务地址：远程取配置，本地取注入的本机基址 */
const activeEndpoint = computed(() => {
  const remote = serverAddress.value.trim()
  if (isRemote.value) return remote
  return API_URL || remote
})

const identity = computed(
  () => auth.user?.email || auth.user?.nickname || auth.user?.name || '—',
)

const dataDir = computed(() => String(device.settings?.local?.dataDir || ''))

const componentRows = computed(() => {
  const c = components.value
  const state = (key: 'gateway' | 'engine' | 'postgres' | 'redis') => {
    if (!c) return 'unknown' as const
    return isComponentUp(c[key]) ? ('up' as const) : ('down' as const)
  }
  return [
    { key: 'gateway', label: t('settings.run_status_component_gateway'), state: state('gateway') },
    { key: 'engine', label: t('settings.run_status_component_engine'), state: state('engine') },
    { key: 'postgres', label: t('settings.run_status_component_postgres'), state: state('postgres') },
    { key: 'redis', label: t('settings.run_status_component_redis'), state: state('redis') },
  ]
})

/**
 * 执行位置：回答"agent 循环、工具、文件到底在哪跑"。
 *
 * 这不是推测，而是由运行模式决定的硬事实：远程模式不启动本机后端，引擎与
 * 工具都在服务地址那台主机上；桌面端只渲染界面并转发请求。
 */
const execRows = computed(() => [
  {
    label: t('settings.run_status_agent_loop'),
    value: isRemote.value
      ? t('settings.run_status_on_server_engine')
      : t('settings.run_status_on_local_engine'),
  },
  {
    label: t('settings.run_status_tool_calls'),
    value: isRemote.value
      ? t('settings.run_status_server_sandbox')
      : t('settings.run_status_local_sandbox'),
  },
  {
    label: t('settings.run_status_file_access'),
    value: isRemote.value
      ? t('settings.run_status_server_files')
      : t('settings.run_status_local_files'),
  },
  {
    label: t('settings.run_status_desktop_role'),
    value: t('settings.run_status_desktop_ui_only'),
  },
])

const statusTag = computed(() => {
  if (statusKind.value === 'checking') {
    return { color: 'processing', text: t('settings.run_status_checking') }
  }
  if (statusKind.value === 'ok') return { color: 'green', text: t('settings.run_status_connected') }
  if (statusKind.value === 'fail') return { color: 'red', text: t('settings.run_status_failed') }
  return { color: 'default', text: t('settings.run_status_unknown') }
})

const checkedAtLabel = computed(() =>
  statusCheckedAt.value ? statusCheckedAt.value.toLocaleTimeString() : '',
)

/**
 * 探一次当前模式的真实状态。
 *
 * 用 `/v1/system/components` 而不是只 ping `/health`：它同时证明「网络通、
 * 鉴权过、依赖在」，并直接回 PG/Redis/引擎/网关 的逐项健康态。远程模式下
 * 这些值描述的是服务地址那台主机；本地模式下就是本机内置栈。
 */
async function refreshStatus() {
  if (statusKind.value === 'checking') return
  statusKind.value = 'checking'
  statusError.value = ''
  const started = performance.now()
  try {
    const res = await api.get('/v1/system/components', { timeout: 8000 })
    const data = res?.data?.data ?? res?.data
    components.value = data && typeof data === 'object' ? (data as ComponentsHealth) : null
    statusKind.value = 'ok'
    statusLatencyMs.value = Math.round(performance.now() - started)
    void loadModelCount()
  } catch (err) {
    components.value = null
    statusLatencyMs.value = null
    statusKind.value = 'fail'
    const fallback = (err as Error)?.message || String(err)
    statusError.value = serverErrorMessage(err, fallback)
  } finally {
    statusCheckedAt.value = new Date()
  }
}

/** 模型数是"能不能干活"的直接证据；取不到就显示 —，不猜。 */
async function loadModelCount() {
  try {
    const { data } = await api.get('/v1/models', { timeout: 6000 })
    const list = Array.isArray(data?.data?.models) ? data.data.models : []
    modelCount.value = list.length
  } catch {
    modelCount.value = null
  }
}


async function submit() {
  if (!confirmOk.value) return
  busy.value = true
  try {
    const result = await device.wipeLocalData(password.value)
    if (result.ok) {
      modalOpen.value = false
      // 成功时主进程会 relaunch，这条提示多半只是一闪而过；
      // 但反过来（先重启再提示）必然提示不到，所以顺序不能换。
      message.success(t('settings.run_mode_wipe_done'))
      return
    }
    // 失败要能区分原因：身份没过不该报成"清空失败"
    if (result.reason === 'needs-password') {
      // 主进程要求密码：切到密码模式再让用户填，不用重开弹窗
      helloAvailable.value = false
      message.info(t('settings.run_mode_wipe_need_password'))
    } else if (result.reason === 'bad-credential') {
      password.value = ''
      message.error(t('settings.run_mode_wipe_bad_password'))
    } else if (result.reason === 'account-issue') {
      message.error(result.message || t('settings.run_mode_wipe_account_issue'))
    } else if (result.reason === 'verification-unavailable') {
      // 记下主进程给的**真实**路径：硬编码 %APPDATA% 在改了数据目录的机器上会指错地方
      wipeDataDir.value = result.dataDir || ''
      // 环境原因：断网 / 域控不可达。重试同一个密码不会有不同结果，
      // 所以不能只回一句"请稍后重试" —— 要么联网后再来，要么用下面的手动清除。
      message.warning(t('settings.run_mode_wipe_verify_unavailable'))
    } else if (result.reason === 'denied') {
      message.warning(t('settings.run_mode_wipe_hello_failed'))
    } else if (result.reason === 'unsupported') {
      message.warning(t('settings.run_mode_wipe_unsupported'))
    } else {
      message.error(t('settings.run_mode_wipe_failed'))
    }
  } catch {
    message.error(t('settings.run_mode_wipe_failed'))
  } finally {
    // 失败时复位，否则用户没法重试；成功时应用会重启，复位也无副作用
    busy.value = false
  }
}

onMounted(() => {
  void device.ensureReady()
  void refreshStatus()
  if (device.isDesktop) void loadMountDeps()
})
</script>

<template>
  <div class="run-mode-panel">
    <!-- ── 运行形态：本地随包后端，或连接远程服务 ── -->
    <div class="mode-block">
      <div class="mode-block-title">
        {{ $t('settings.run_mode') }}
      </div>
      <div class="mode-row">
        <span class="mode-label">{{ $t('settings.run_mode_current', { mode: device.mode === 'cloud' ? $t('settings.run_mode_cloud') : $t('settings.run_mode_local') }) }}</span>
        <a-tag :color="device.mode === 'cloud' ? 'blue' : 'green'">
          {{ device.mode === 'cloud' ? $t('settings.run_mode_cloud') : $t('settings.run_mode_local') }}
        </a-tag>
        <a-button
          :disabled="!device.isDesktop"
          @click="openConnection"
        >
          {{ $t('settings.run_mode_open_setup') }}
        </a-button>
        <a-button
          v-if="device.mode === 'cloud' && device.isDesktop"
          :loading="testing"
          @click="testConnection"
        >
          {{ $t('settings.run_mode_test_connection') }}
        </a-button>
      </div>
      <div v-if="device.mode === 'cloud'" class="mode-row">
        <span class="mode-hint">{{ $t('settings.run_mode_server_address') }}：</span>
        <code class="mode-address">{{ serverAddress || '—' }}</code>
      </div>
      <p class="mode-hint mode-hint-block">
        {{ device.mode === 'cloud' ? $t('settings.run_mode_cloud_hint') : $t('settings.run_mode_local_hint') }}
      </p>
    </div>

    <!-- ── 运行状态：谁在跑 agent、工具与文件在哪、现在通不通 ── -->
    <div class="mode-block">
      <div class="mode-block-title status-title">
        <span>{{ $t('settings.run_status_title') }}</span>
        <a-button
          size="small"
          :loading="statusKind === 'checking'"
          @click="refreshStatus"
        >
          {{ $t('settings.run_status_refresh') }}
        </a-button>
      </div>

      <div class="mode-row status-row">
        <a-tag :color="statusTag.color">{{ statusTag.text }}</a-tag>
        <span v-if="statusLatencyMs !== null" class="mode-hint">{{ statusLatencyMs }} ms</span>
        <span v-if="checkedAtLabel" class="mode-hint">
          {{ $t('settings.run_status_last_checked', { time: checkedAtLabel }) }}
        </span>
      </div>
      <p v-if="statusKind === 'fail'" class="mode-hint mode-hint-block status-error">
        {{ statusError }}
      </p>

      <div class="status-components">
        <span
          v-for="c in componentRows"
          :key="c.key"
          class="status-component"
          :class="`is-${c.state}`"
        >
          <span class="status-dot" aria-hidden />{{ c.label }}
        </span>
      </div>

      <dl class="status-list">
        <div v-for="row in execRows" :key="row.label" class="status-item">
          <dt>{{ row.label }}</dt>
          <dd>{{ row.value }}</dd>
        </div>
        <div class="status-item">
          <dt>{{ $t('settings.run_status_endpoint') }}</dt>
          <dd class="status-code">{{ activeEndpoint || '—' }}</dd>
        </div>
        <div class="status-item">
          <dt>{{ $t('settings.run_status_data') }}</dt>
          <dd>
            {{ isRemote ? $t('settings.run_status_data_server') : (dataDir || $t('settings.run_status_data_local')) }}
          </dd>
        </div>
        <div class="status-item">
          <dt>{{ $t('settings.run_status_identity') }}</dt>
          <dd>{{ identity }}</dd>
        </div>
        <div class="status-item">
          <dt>{{ $t('settings.run_status_models') }}</dt>
          <dd>
            {{ modelCount === null ? '—' : $t('settings.run_status_models_count', { count: modelCount }) }}
          </dd>
        </div>
      </dl>

      <p class="mode-hint mode-hint-block">
        {{ $t('settings.run_status_exec_note') }}
      </p>
    </div>

    <!-- ── 团队挂载依赖：只装在服务端主机，按需下载并校验 ── -->
    <div v-if="device.isDesktop && depsStatus" class="mode-block">
      <div class="mode-block-title status-title">
        <span>{{ $t('settings.mount_deps_title') }}</span>
        <a-button size="small" @click="loadMountDeps">
          {{ $t('settings.mount_deps_refresh') }}
        </a-button>
      </div>
      <p class="mode-hint mode-hint-block">
        {{ $t('settings.mount_deps_hint') }}
      </p>
      <div v-if="!depsStatus.supported" class="mode-row">
        <span class="mode-hint">{{ $t('settings.mount_deps_unsupported') }}</span>
      </div>
      <template v-else>
        <div class="mode-row deps-row">
          <span class="deps-name">rclone</span>
          <a-tag :color="depsStatus.rclone.installed ? 'green' : 'default'">
            {{ depsStatus.rclone.installed ? $t('settings.mount_deps_installed') : $t('settings.mount_deps_missing') }}
          </a-tag>
          <span class="mode-hint">{{ depsStatus.rclone.installed ? depsStatus.rclone.path : depsStatus.rclone.version }}</span>
          <a-button
            v-if="!depsStatus.rclone.installed"
            size="small"
            :loading="depsBusy === 'rclone'"
            @click="installMountDep('rclone')"
          >
            {{ depsBusy === 'rclone' ? $t('settings.mount_deps_installing') : $t('settings.mount_deps_install') }}
          </a-button>
        </div>
        <div class="mode-row deps-row">
          <span class="deps-name">WinFsp</span>
          <a-tag :color="depsStatus.winfsp.installed ? 'green' : 'default'">
            {{ depsStatus.winfsp.installed ? $t('settings.mount_deps_installed') : $t('settings.mount_deps_missing') }}
          </a-tag>
          <span class="mode-hint">{{ depsStatus.winfsp.installed ? (depsStatus.winfsp.installedVersion || depsStatus.winfsp.version) : depsStatus.winfsp.version }}</span>
          <a-button
            v-if="!depsStatus.winfsp.installed"
            size="small"
            :loading="depsBusy === 'winfsp'"
            @click="installMountDep('winfsp')"
          >
            {{ depsBusy === 'winfsp' ? $t('settings.mount_deps_installing') : $t('settings.mount_deps_winfsp_install') }}
          </a-button>
        </div>
      </template>
    </div>

    <!-- ── 清空本机数据：唯一的破坏性动作 ── -->
    <div class="mode-block mode-danger-block">
      <div class="mode-block-title">
        {{ $t('settings.run_mode_wipe_title') }}
      </div>
      <p class="mode-hint mode-hint-block">
        {{ $t('settings.run_mode_wipe_desc') }}
      </p>
      <div class="mode-row">
        <a-button
          danger
          :disabled="!device.isDesktop || device.wiping"
          :loading="device.wiping"
          @click="openWipe"
        >
          {{ $t('settings.run_mode_wipe_button') }}
        </a-button>
        <span v-if="!device.isDesktop" class="mode-hint">
          {{ $t('settings.run_mode_desktop_only') }}
        </span>
      </div>
    </div>

    <a-modal
      v-model:open="modalOpen"
      :title="$t('settings.run_mode_wipe_modal_title')"
      :ok-text="$t('settings.run_mode_wipe_submit')"
      :cancel-text="$t('common.cancel')"
      :confirm-loading="busy"
      :mask-closable="!busy"
      :closable="!busy"
      :ok-button-props="{ disabled: !confirmOk, danger: true }"
      @ok="submit"
      @cancel="closeWipe"
    >
      <a-alert
        type="error"
        show-icon
        class="mode-wipe-alert"
        :message="$t('settings.run_mode_wipe_warning_title')"
        :description="$t('settings.run_mode_wipe_warning_body')"
      />

      <p class="mode-modal-line">
        {{ $t('settings.run_mode_wipe_will_clear') }}
      </p>
      <ul class="mode-clear-list">
        <li>{{ $t('settings.run_mode_wipe_clear_conversations') }}</li>
        <li>{{ $t('settings.run_mode_wipe_clear_knowledge') }}</li>
        <li>{{ $t('settings.run_mode_wipe_clear_files') }}</li>
        <li>{{ $t('settings.run_mode_wipe_clear_cache') }}</li>
      </ul>

      <!-- 第一道：勾选知情 -->
      <a-checkbox v-model:checked="acknowledged" class="mode-ack">
        {{ $t('settings.run_mode_wipe_ack') }}
      </a-checkbox>

      <!-- 第二道：手打确认词 -->
      <div class="mode-confirm">
        <div class="mode-confirm-label">
          {{ $t('settings.run_mode_wipe_confirm_hint', { word: CONFIRM_WORD }) }}
        </div>
        <a-input
          v-model:value="confirmText"
          :placeholder="$t('settings.run_mode_confirm_placeholder', { word: CONFIRM_WORD })"
          :disabled="busy"
        />
      </div>

      <!-- 第三道：身份验证。Hello 可用 → 执行时弹窗；不可用 → 必须输账户密码 -->
      <div v-if="helloAvailable" class="mode-hello-hint">
        <p class="mode-hint">
          {{ $t('settings.run_mode_wipe_hello_hint') }}
        </p>
      </div>
      <div v-else class="mode-hello-hint">
        <div class="mode-confirm-label">
          {{ $t('settings.run_mode_wipe_password_label') }}
        </div>
        <a-input-password
          v-model:value="password"
          :placeholder="$t('settings.run_mode_wipe_password_placeholder')"
          :disabled="busy"
          autocomplete="current-password"
        />
        <p class="mode-hint mode-hello-hint">
          {{ $t('settings.run_mode_wipe_password_hint') }}
        </p>
        <!-- 断网/隔离环境下密码验证本身可能不可用；这时必须给一条可执行的退路，
             否则用户就被锁在"清不了数据"里 -->
        <p class="mode-hint mode-hello-hint">
          {{ $t('settings.run_mode_wipe_offline_hint') }}
        </p>
        <p v-if="wipeDataDir" class="mode-hint mode-data-dir">
          {{ wipeDataDir }}
        </p>
      </div>
    </a-modal>
  </div>
</template>

<style scoped>
.mode-block { padding-block: 4px 20px; }
.mode-block + .mode-block { border-top: var(--hairline-width) solid var(--border-card, var(--border)); padding-top: 20px; }
.mode-block-title { font-weight: var(--fw-semibold); color: var(--text-primary); margin-bottom: 12px; }
.mode-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }
.mode-label { color: var(--text-primary); }
.mode-hint { color: var(--text-secondary); font-size: 12px; }
.mode-hint-block { margin: 0 0 12px; line-height: 1.6; }
.mode-address { font-family: var(--font-mono); font-size: 12px; color: var(--text-primary); }
.mode-danger-block .mode-block-title { color: var(--color-error, #cf1322); }
.mode-wipe-alert { margin-bottom: 16px; }
.mode-modal-line { margin: 0 0 10px; color: var(--text-primary); }
.mode-clear-list {
  margin: 0 0 12px;
  padding-inline-start: 20px;
  color: var(--text-secondary);
  font-size: 13px;
}
.mode-ack { margin-bottom: 4px; }
.mode-confirm { margin-top: 16px; padding-top: 12px; border-top: var(--hairline-width) solid var(--border-card, var(--border)); }
.mode-confirm-label { margin-bottom: 8px; color: var(--text-secondary); font-size: 13px; }
.mode-hello-hint { margin: 12px 0 0; }
.mode-data-dir {
  font-family: var(--font-mono, monospace);
  overflow-wrap: anywhere;
  color: var(--text-primary);
}

/* ── 运行状态 ── */
.status-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.status-row { margin-bottom: 8px; }
.status-error { color: var(--color-error, #cf1322); }
.status-components {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}
.status-component {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border: var(--hairline-width) solid var(--border-card, var(--border));
  border-radius: 999px;
  font-size: 12px;
  color: var(--text-secondary);
}
.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-tertiary);
}
.status-component.is-up .status-dot { background: var(--color-success, #2f855a); }
.status-component.is-down .status-dot { background: var(--color-error, #cf1322); }
.status-component.is-up { color: var(--text-primary); }
.status-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0 0 12px;
}
.status-item { display: flex; gap: 12px; align-items: baseline; }
.status-item dt {
  flex: none;
  width: 76px;
  color: var(--text-tertiary);
  font-size: 12px;
}
.status-item dd {
  margin: 0;
  min-width: 0;
  color: var(--text-primary);
  font-size: 12px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.status-code { font-family: var(--font-mono, monospace); direction: ltr; }
.deps-row { margin-bottom: 8px; }
.deps-name { width: 64px; font-weight: var(--fw-medium); }
</style>
