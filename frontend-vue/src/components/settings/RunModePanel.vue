<script setup lang="ts">
/**
 * 运行模式 / 本机数据 —— 设置页「运行模式」标签页的内容。
 *
 * 云端模式已废除，应用只有本地一种形态（契约 docs/desktop-local/contracts.md §1），
 * 所以这里**不再有「切换模式」**。取而代之的是唯一一个破坏性动作：**清空本机数据**。
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

const { t } = useI18n()
const device = useDeviceStore()

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
})
</script>

<template>
  <div class="run-mode-panel">
    <!-- ── 当前运行形态：只有本地，没有可切的东西 ── -->
    <div class="mode-block">
      <div class="mode-block-title">
        {{ $t('settings.run_mode') }}
      </div>
      <div class="mode-row">
        <span class="mode-label">{{ $t('settings.run_mode_current', { mode: $t('settings.run_mode_local') }) }}</span>
        <a-tag color="green">
          {{ $t('settings.run_mode_local') }}
        </a-tag>
      </div>
      <p class="mode-hint mode-hint-block">
        {{ $t('settings.run_mode_local_only_hint') }}
      </p>
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
</style>
