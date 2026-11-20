<script setup lang="ts">
/**
 * 本机隔离档位 —— 设置页「执行隔离」标签页。
 *
 * ## 这个面板要回答的三个问题
 *
 * 1. **现在是什么档位？** 显示 `requested`（配置请求）与 `effective`（实际生效）
 *    两个值。两者不同就是发生了降级 —— 必须显式展示，否则就是静默降级。
 * 2. **能改成什么？** 只列出 `selectable` 里的档位（本机真能做到的）。
 *    低于安全底线的 `job` 可直接选；`none` 要先二次确认 —— 它等于完全没有边界。
 * 3. **怎么撤销？** 两个层次：撤销设置（回到配置文件）+ 撤销磁盘产物
 *    （AppContainer profile、目录完整性标签）。前者免费，后者必要 ——
 *    不撤销会让工作区目录长期是 Low 标签，影响后续未隔离使用。
 *
 * ## 为什么档位说明写在 UI 里而不是只放文档
 *
 * 用户选档位时看到的应该是"这个档位能挡住什么、挡不住什么"，而不是一个抽象名字。
 * 尤其是 `low_il` —— 它是 Windows 上的实际默认值，但它挡不住同用户读取，
 * 必须说清楚，否则用户会以为自己在跑 Docker 级隔离。
 */
import { computed, onMounted, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { SafetyOutlined, ReloadOutlined, WarningOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import {
  clearIsolationLevel,
  getIsolationHelper,
  getIsolationStatus,
  revokeIsolationArtifacts,
  setIsolationLevel,
  startIsolationHelper,
  stopIsolationHelper,
  type IsolationHelperStatus,
  type IsolationLevel,
  type IsolationStatus,
} from '../../api'
import { useAuthStore } from '../../stores/auth'
import { serverErrorDetail, serverErrorMessage } from '../../utils/apiError'

const { t } = useI18n()
const authStore = useAuthStore()

const loading = ref(false)
const saving = ref(false)
const revoking = ref(false)
const status = ref<IsolationStatus | null>(null)
/** 加载失败的原文；有值时展示重试入口而不是空白面板 */
const loadError = ref('')
/** 待切换的档位（提交前先记下来，避免提交中状态抖回旧值） */
const pendingLevel = ref<IsolationLevel | null>(null)
/**
 * 提权助手状态。
 *
 * `null` 有明确含义：**还没问到**（或问失败）。与 `{available: false}` 不同 ——
 * 后者是"问了，答案是不可用"。混用会让某一帧闪出"助手不可用"的假象，
 * 而这正好是本面板最容易被误读的一条信息。
 */
const helperStatus = ref<IsolationHelperStatus | null>(null)
/** 启停助手进行中（UAC 弹窗期间可能停留数秒，必须给按钮加 loading） */
const helperBusy = ref(false)
/**
 * 启用助手失败时**留在面板上**的原因（`message.error` 几秒后就消失了，
 * 而用户往往是在 UAC 弹窗之后才回来看这一屏）。
 *
 * 为什么不复用 `helperStatus.detail`：那个字段是"助手现在为什么不可用"的探测结论，
 * 属于**状态**；这里是"上一次启用动作为什么失败"的**事件**。两者会同时成立且内容不同
 * （例如 UAC 被拒绝：状态仍是"未启动"，事件是"你取消了授权"）。
 */
const helperError = ref('')
/** 与 `helperError` 配套的可执行指引（后端下发；空串表示不需要额外动作） */
const helperHint = ref('')
/** 面板上那条错误的**标题**：启用与停用失败的措辞不同，所以标题随动作而变 */
const helperErrorTitle = ref('')

/** 档位由弱到强，与后端 `_ORDER` 严格一致 */
const ALL_LEVELS: IsolationLevel[] = ['none', 'job', 'low_il', 'restricted', 'appcontainer']

const currentEffective = computed(() => status.value?.effective ?? null)

/**
 * 档位是否可选：以后端 `selectable` 为准。
 *
 * 不在前端自己算底线 —— 底线是安全策略，只能有**一处**事实来源。
 * 前端重算一遍，两边规则一旦漂移就会出现"UI 可选、后端拒绝"或反之。
 */
function isSelectable(level: IsolationLevel): boolean {
  return status.value?.selectable?.includes(level) ?? false
}

/**
 * 为什么这个档位不可选 —— 让鼠标悬停时能解释原因，而不是单纯置灰。
 *
 * 现在 `none`/`job` 都出现在 `selectable` 里（可以被显式选择），所以这里只剩
 * **本机缺机制** 这一种：例如 `restricted` 需要引擎以管理员身份运行才能拿到
 * `SeIncreaseQuotaPrivilege`（实测 err=5）。把这种情况下说成"低于底线"会让
 * 用户去翻配置，而真正缺的是权限。
 *
 * 判据来自后端的 `unavailable_reasons`，前端不解析文本分类 ——
 * 那种做法在切换语言或改措辞时必然失效。
 */
function disabledReason(level: IsolationLevel): string {
  if (!status.value) return ''
  if (isSelectable(level)) return ''
  const reason = (status.value.unavailable_reasons?.[level] || '').trim()
  if (reason) return t('settings.isolation_unavailable_hint', { reason })
  return t('settings.isolation_unavailable_unknown')
}

/**
 * 选择该档位是否必须先二次确认（由后端 `acknowledge_required` 决定）。
 *
 * 只有 `none`（完全没有边界）在此列。`job` 仍有 Job Object 边界，不值得为它
 * 加一道门槛 —— 否则"切一下看看"的成本高到没人会用这个面板。
 */
function needsAcknowledge(level: IsolationLevel): boolean {
  return status.value?.acknowledge_required?.includes(level) ?? false
}

function levelLabel(level: IsolationLevel): string {
  return t(`settings.isolation_level_${level}`)
}

function levelDescription(level: IsolationLevel): string {
  return t(`settings.isolation_level_${level}_desc`)
}

/**
 * 鉴权未就绪时的自愈：先补建本机会话，再重试一次。
 *
 * 为什么必须在面板这一层兜：`initToken()` 里本地分支是 `void ensureLocalSession()`
 * —— **刻意不 await**，为了让首帧不被本地后端拖住。设置页若在这之前就挂载，
 * `requestToken()` 返回空串，网关在 `ValidateToken` 之前就判 `tokenStr == ""`，
 * 于是回一句 `missing authorization`（英文原文，且与"权限不足"毫无关系）。
 * 这里补一次会话再重试，正常路径下用户根本看不到这个错误。
 */
async function load() {
  loading.value = true
  loadError.value = ''
  try {
    status.value = await getIsolationStatus()
    await loadHelper()
  } catch (err) {
    const status401 = (err as { response?: { status?: number } })?.response?.status === 401
    if (status401) {
      const ready = await authStore.ensureLocalSession(true)
      if (ready) {
        try {
          status.value = await getIsolationStatus()
          await loadHelper()
          return
        } catch (retryErr) {
          loadError.value = serverErrorMessage(retryErr, t('settings.isolation_auth_pending'))
          return
        }
      }
      loadError.value = t('settings.isolation_auth_pending')
      return
    }
    loadError.value = serverErrorMessage(err, t('settings.isolation_load_failed'))
  } finally {
    loading.value = false
  }
}

/**
 * 拉取提权助手状态。
 *
 * **失败不许影响面板**：helper 只是 `restricted` 档的前置条件，它探测失败时面板
 * 其余部分（当前档位、其它档位、撤销入口）全都有效。把整块面板打成"加载失败"，
 * 等于让一个附加信息的故障掩盖掉主信息。
 *
 * 所以这里单独 try/catch，失败时把 `helperStatus` 留在 `null`（未知），
 * 让界面说"查不到"而不是谎称"不可用"。
 */
async function loadHelper() {
  try {
    helperStatus.value = await getIsolationHelper()
  } catch {
    helperStatus.value = null
  }
}

/**
 * 让用户确认"我要在没有隔离的情况下执行"。
 *
 * 返回 `true` 才继续提交。文案直接把后果说清楚（工具将以主进程同等权限直跑），
 * 而不是问一句含糊的"确定吗" —— 这个决定会写审计流水，用户应当知道自己在点什么。
 */
function acknowledgeBelowFloor(level: IsolationLevel): Promise<boolean> {
  return new Promise((resolve) => {
    Modal.confirm({
      title: t('settings.isolation_confirm_title'),
      content: t('settings.isolation_confirm_body', { level: levelLabel(level) }),
      okText: t('settings.isolation_confirm_ok'),
      okType: 'danger',
      cancelText: t('common.cancel'),
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    })
  })
}

async function applyLevel(level: IsolationLevel) {
  if (!isSelectable(level) || saving.value) return
  // 选的就是当前生效档位：不必往返一次
  if (level === status.value?.runtime_override) return

  // 低于安全底线且**完全没有边界**的档位（目前只有 `none`）要先明确点头。
  //
  // 为什么不在这里也拦 `job`：它仍有 Job Object 边界，加门槛会让"切一下看看"
  // 变得很贵，而用户付出的确认成本换不来对应的安全性。是否要确认由后端
  // `acknowledge_required` 决定 —— 前端不自己判断档位强弱，避免两边规则漂移。
  if (needsAcknowledge(level) && !(await acknowledgeBelowFloor(level))) return

  saving.value = true
  pendingLevel.value = level
  try {
    const next = await setIsolationLevel(
      level,
      t('settings.isolation_change_reason'),
      needsAcknowledge(level),
    )
    status.value = next
    message.success(t('settings.isolation_changed', { level: levelLabel(next.effective) }))
    // 请求值被抬到底线时，明确告诉用户"你要的没生效、实际是这个"
    if (next.requested !== next.effective) {
      message.warning(
        t('settings.isolation_raised_to_floor', {
          requested: levelLabel(next.requested),
          effective: levelLabel(next.effective),
        }),
      )
    }
    if (next.running_processes_unaffected) {
      message.info(t('settings.isolation_running_unaffected'))
    }
  } catch (err) {
    message.error(serverErrorMessage(err, t('settings.isolation_change_failed')))
  } finally {
    saving.value = false
    pendingLevel.value = null
  }
}

async function resetToConfig() {
  if (saving.value) return
  saving.value = true
  try {
    const next = await clearIsolationLevel()
    status.value = next
    message.success(
      next.cleared
        ? t('settings.isolation_reset_done', { level: levelLabel(next.effective) })
        : t('settings.isolation_reset_noop'),
    )
  } catch (err) {
    message.error(serverErrorMessage(err, t('settings.isolation_reset_failed')))
  } finally {
    saving.value = false
  }
}

async function revokeArtifacts() {
  if (revoking.value) return
  revoking.value = true
  try {
    const result = await revokeIsolationArtifacts()
    // 如实回报：profile 删没删掉、标签还原没有 —— 不能只说一句"已撤销"
    const okLabel = result.integrity_labels?.every((item) => item.ok)
    message.success(
      okLabel
        ? t('settings.isolation_revoke_done')
        : t('settings.isolation_revoke_partial', { detail: result.appcontainer_profile }),
    )
    await load()
  } catch (err) {
    message.error(serverErrorMessage(err, t('settings.isolation_revoke_failed')))
  } finally {
    revoking.value = false
  }
}

/**
 * 「`restricted` 为什么是灰的」—— 这个档位不可选，是不是就缺这个助手？
 *
 * 判据是**两件事同时成立**：档位不在 `selectable` 里，且助手确实不可用。
 * 只看助手状态不够 —— 引擎自己已提权时 `restricted` 走本地路径、根本不需要助手，
 * 此时助手显示"未运行"是完全正常的，不该催用户去点 UAC。
 */
const restrictedNeedsHelper = computed(() => {
  if (helperStatus.value?.engine_elevated) return false
  if (isSelectable('restricted')) return false
  return helperStatus.value !== null && !helperStatus.value.available
})

/** 助手是否已在运行（可用即视为运行中：探测成功就是它活着） */
const helperRunning = computed(() => helperStatus.value?.available === true)

/**
 * 是否展示助手这一节。
 *
 * 平台不支持时整节隐藏：展示一个永远点不动的按钮，等于把"你的系统不行"
 * 伪装成一个可以操作的问题。
 */
const showHelperSection = computed(() => helperStatus.value?.supported !== false)

async function enableHelper() {
  if (helperBusy.value) return
  // 弹 UAC 之前先说清楚"你将要同意什么"。这一步不能省 —— 用户点这一下之后
  // 出现的是系统级弹窗，那时候再解释已经晚了。
  const confirmed = await new Promise<boolean>((resolve) => {
    Modal.confirm({
      title: t('settings.isolation_helper_title'),
      content: t('settings.isolation_helper_confirm'),
      okText: t('settings.isolation_helper_enable'),
      cancelText: t('common.cancel'),
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    })
  })
  if (!confirmed) return

  helperBusy.value = true
  try {
    const result = await startIsolationHelper()
    status.value = result
    helperStatus.value = result.helper
    if (result.ok) {
      helperError.value = ''
      helperHint.value = ''
      helperErrorTitle.value = ''
      message.success(t('settings.isolation_helper_enabled'))
    } else if (result.cancelled) {
      // 用户主动拒绝 UAC：正常选择，用 info 而不是 error。
      helperError.value = ''
      helperHint.value = ''
      helperErrorTitle.value = ''
      message.info(t('settings.isolation_helper_cancelled'))
    } else {
      // `reason` 是后端逐条给出的人类可读原因（缺端口文件 / 未提权 / 进程被拦…）。
      // 只有它为空时才退回通用文案 —— 反过来（总是用通用文案）会让每一台机器上的
      // 每一种失败都长成同一句"本机无法启用此档位"，用户和我们都无从下手。
      helperError.value = result.reason || t('settings.isolation_unavailable_unknown')
      helperHint.value = result.hint || ''
      helperErrorTitle.value = t('settings.isolation_helper_enable_failed', {
        reason: helperError.value,
      })
      message.error(helperErrorTitle.value)
    }
  } catch (err) {
    // 走到这里说明请求本身没成功（网关 404/500、超时、网络断开）。
    // 必须把**后端原文**带出来：这正是"网关缺路由"那次事故里唯一能指向病因的线索，
    // 而兜底文案把它盖成了一句像是"本机能力不足"的空话。
    const detail = serverErrorDetail(err, '')
    helperError.value = detail || t('settings.isolation_unavailable_unknown')
    helperHint.value = ''
    helperErrorTitle.value = t('settings.isolation_helper_enable_failed', {
      reason: helperError.value,
    })
    message.error(helperErrorTitle.value)
  } finally {
    helperBusy.value = false
  }
}

async function disableHelper() {
  if (helperBusy.value) return
  helperBusy.value = true
  try {
    const result = await stopIsolationHelper()
    status.value = result
    helperStatus.value = result.helper
    if (result.ok) {
      helperError.value = ''
      helperHint.value = ''
      helperErrorTitle.value = ''
      message.success(t('settings.isolation_helper_disabled'))
    } else {
      helperError.value = result.reason || t('settings.isolation_unavailable_unknown')
      helperHint.value = ''
      helperErrorTitle.value = t('settings.isolation_helper_disable_failed', {
        reason: helperError.value,
      })
      message.error(helperErrorTitle.value)
    }
  } catch (err) {
    const detail = serverErrorDetail(err, '')
    helperError.value = detail || t('settings.isolation_unavailable_unknown')
    helperHint.value = ''
    helperErrorTitle.value = t('settings.isolation_helper_disable_failed', {
      reason: helperError.value,
    })
    message.error(helperErrorTitle.value)
  } finally {
    helperBusy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="iso-block">
    <div class="iso-head">
      <SafetyOutlined class="iso-icon" />
      <div>
        <div class="iso-title">
          {{ $t('settings.isolation_title') }}
        </div>
        <p class="iso-intro">
          {{ $t('settings.isolation_intro') }}
        </p>
      </div>
    </div>

    <!-- 加载失败要给出可执行的重试，不是空白 -->
    <a-alert
      v-if="loadError"
      type="warning"
      show-icon
      class="iso-alert"
      :message="loadError"
    >
      <template #action>
        <a-button
          size="small"
          :loading="loading"
          @click="load"
        >
          {{ $t('common.retry') }}
        </a-button>
      </template>
    </a-alert>

    <a-spin v-else-if="loading && !status" />

    <template v-else-if="status">
      <!-- 当前状态：请求值 vs 实际生效值 -->
      <div class="iso-current">
        <div class="iso-current-row">
          <span class="iso-current-label">{{ $t('settings.isolation_current') }}</span>
          <a-tag :color="status.below_floor ? 'red' : 'green'">
            {{ levelLabel(status.effective) }}
          </a-tag>
          <span
            v-if="status.runtime_override"
            class="iso-temp-tag"
          >
            {{ $t('settings.isolation_temp_applied') }}
          </span>
        </div>
        <!-- 降级必须显式说明，这是整个面板最重要的一条信息 -->
        <a-alert
          v-if="status.requested !== status.effective"
          :type="status.below_floor ? 'error' : 'warning'"
          show-icon
          class="iso-alert"
          :message="$t('settings.isolation_degraded_title')"
          :description="
            [
              $t('settings.isolation_degraded_body', {
                requested: levelLabel(status.requested),
                effective: levelLabel(status.effective),
              }),
              ...(status.reasons || []),
            ].join(' ')
          "
        />
        <a-alert
          v-if="status.below_floor"
          :type="status.user_selected ? 'warning' : 'error'"
          show-icon
          class="iso-alert"
          :message="
            status.user_selected
              ? $t('settings.isolation_below_floor_selected_title')
              : $t('settings.isolation_below_floor_title')
          "
          :description="
            status.user_selected
              ? $t('settings.isolation_below_floor_selected_body', { floor: levelLabel(status.floor) })
              : $t('settings.isolation_below_floor_body')
          "
        />
        <div class="iso-floor-hint">
          <WarningOutlined />
          <span>{{ $t('settings.isolation_floor_note', { floor: levelLabel(status.floor) }) }}</span>
        </div>
      </div>

      <!-- 档位选择 -->
      <div class="iso-picker">
        <div class="iso-picker-title">
          {{ $t('settings.isolation_pick') }}
        </div>
        <div class="iso-levels">
          <button
            v-for="level in ALL_LEVELS"
            :key="level"
            type="button"
            class="iso-level"
            :class="{
              'iso-level--active': level === currentEffective,
              'iso-level--pending': level === pendingLevel,
              'iso-level--disabled': !isSelectable(level),
            }"
            :disabled="!isSelectable(level) || saving"
            :title="disabledReason(level)"
            @click="applyLevel(level)"
          >
            <div class="iso-level-head">
              <span class="iso-level-name">{{ levelLabel(level) }}</span>
              <a-tag
                v-if="level === status.floor"
                color="blue"
                class="iso-level-badge"
              >
                {{ $t('settings.isolation_floor_badge') }}
              </a-tag>
              <a-tag
                v-if="level === currentEffective"
                color="green"
                class="iso-level-badge"
              >
                {{ $t('settings.isolation_active_badge') }}
              </a-tag>
            </div>
            <p class="iso-level-desc">
              {{ levelDescription(level) }}
            </p>
          </button>
        </div>
      </div>

      <!--
        提权助手：`restricted` 档的前置条件。
        只在"缺它会导致该档不可选"或"它已经在跑"时才显眼；引擎自己已提权时
        这一档根本不需要助手，此时不该催用户去点一次 UAC。
      -->
      <div
        v-if="showHelperSection"
        class="iso-helper"
      >
        <div class="iso-helper-head">
          <div class="iso-helper-title">
            {{ $t('settings.isolation_helper_title') }}
          </div>
          <a-tag :color="helperRunning ? 'green' : 'default'">
            {{
              helperRunning
                ? $t('settings.isolation_helper_running')
                : $t('settings.isolation_helper_stopped')
            }}
          </a-tag>
        </div>
        <p class="iso-helper-body">
          {{ $t('settings.isolation_helper_body') }}
        </p>
        <!-- 助手不可用且正是它挡住了 restricted：把因果讲明，别让用户对着灰按钮猜 -->
        <a-alert
          v-if="restrictedNeedsHelper"
          type="warning"
          show-icon
          class="iso-alert"
          :message="$t('settings.isolation_helper_blocked')"
          :description="helperStatus?.detail || ''"
        />
        <!--
          上一次启用/停用动作的失败原因：**留在面板上**而不是只弹一条 message。
          UAC 弹窗会把焦点抢走，用户回来时那条 toast 往往已经消失了，只剩一个
          "助手未运行"的状态 —— 于是他能做的只有再点一次同样的按钮。
        -->
        <a-alert
          v-if="helperError"
          type="error"
          show-icon
          class="iso-alert"
          :message="helperErrorTitle"
          :description="helperHint"
        />
        <div
          v-if="helperStatus?.engine_elevated"
          class="iso-helper-note"
        >
          {{ $t('settings.isolation_helper_engine_elevated') }}
        </div>
        <div class="iso-actions">
          <a-button
            v-if="!helperRunning"
            type="primary"
            :loading="helperBusy"
            :disabled="helperBusy"
            @click="enableHelper"
          >
            {{ $t('settings.isolation_helper_enable') }}
          </a-button>
          <a-button
            v-else
            :loading="helperBusy"
            :disabled="helperBusy"
            @click="disableHelper"
          >
            {{ $t('settings.isolation_helper_disable') }}
          </a-button>
        </div>
      </div>

      <!-- 撤销：设置层 -->
      <div class="iso-actions">
        <a-button
          :disabled="!status.runtime_override || saving"
          :loading="saving"
          @click="resetToConfig"
        >
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ $t('settings.isolation_reset') }}
        </a-button>
        <span class="iso-action-hint">{{ $t('settings.isolation_reset_hint') }}</span>
      </div>

      <!-- 撤销：磁盘产物层 —— 这是"随时能撤销"里容易被忽略的一半 -->
      <div class="iso-revoke">
        <div class="iso-revoke-title">
          {{ $t('settings.isolation_revoke_title') }}
        </div>
        <p class="iso-revoke-body">
          {{ $t('settings.isolation_revoke_body') }}
        </p>
        <a-popconfirm
          :title="$t('settings.isolation_revoke_confirm')"
          :ok-text="$t('common.confirm')"
          :cancel-text="$t('common.cancel')"
          @confirm="revokeArtifacts"
        >
          <a-button
            danger
            size="small"
            :loading="revoking"
          >
            {{ $t('settings.isolation_revoke_button') }}
          </a-button>
        </a-popconfirm>
      </div>
    </template>
  </div>
</template>

<style scoped>
.iso-block { padding: 4px 0 20px; }
.iso-head { display: flex; gap: 10px; margin-bottom: 16px; }
.iso-icon { font-size: 20px; color: var(--primary); margin-top: 2px; }
.iso-title { font-weight: var(--fw-semibold); color: var(--text-primary); }
.iso-intro { margin: 4px 0 0; color: var(--text-secondary); font-size: 13px; line-height: 1.6; }
.iso-alert { margin-bottom: 12px; }

.iso-current { margin-bottom: 20px; }
.iso-current-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.iso-current-label { color: var(--text-secondary); font-size: 13px; }
.iso-temp-tag { color: var(--text-secondary); font-size: 12px; }
.iso-floor-hint {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.6;
}

.iso-picker { margin-bottom: 20px; }
.iso-picker-title { font-weight: var(--fw-semibold); color: var(--text-primary); margin-bottom: 10px; }
.iso-levels { display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); }

/* 用原生 button 而不是 a-radio：每个档位要带两行说明，radio 的排版撑不开 */
.iso-level {
  display: block;
  width: 100%;
  text-align: start;
  padding: 10px 12px;
  border: var(--hairline-width) solid var(--border-card, var(--border));
  border-radius: var(--radius-md, 8px);
  background: var(--bg-card, transparent);
  color: var(--text-primary);
  cursor: pointer;
  transition: border-color var(--dur-fast), background-color var(--dur-fast);
}
.iso-level:hover:not(.iso-level--disabled) { border-color: var(--primary); }
.iso-level--active { border-color: var(--primary); background: var(--primary-bg, rgba(0, 0, 0, 0.02)); }
.iso-level--pending { opacity: 0.6; }
.iso-level--disabled { cursor: not-allowed; opacity: 0.5; }
.iso-level:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }

.iso-level-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.iso-level-name { font-weight: var(--fw-medium, 500); }
.iso-level-badge { margin-inline-start: 0; }
.iso-level-desc { margin: 4px 0 0; color: var(--text-secondary); font-size: 12px; line-height: 1.5; }

.iso-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }
.iso-action-hint { color: var(--text-secondary); font-size: 12px; }

.iso-helper {
  padding: 14px 0 16px;
  margin-bottom: 4px;
  border-top: var(--hairline-width) solid var(--border-card, var(--border));
}
.iso-helper-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }
.iso-helper-title { font-weight: var(--fw-semibold); color: var(--text-primary); }
.iso-helper-body { margin: 0 0 10px; color: var(--text-secondary); font-size: 12px; line-height: 1.6; }
.iso-helper-note { margin-bottom: 10px; color: var(--text-secondary); font-size: 12px; line-height: 1.6; }

.iso-revoke {
  padding-top: 16px;
  border-top: var(--hairline-width) solid var(--border-card, var(--border));
}
.iso-revoke-title { font-weight: var(--fw-semibold); color: var(--text-primary); margin-bottom: 6px; }
.iso-revoke-body { margin: 0 0 10px; color: var(--text-secondary); font-size: 12px; line-height: 1.6; }

@media (prefers-reduced-motion: reduce) {
  .iso-level { transition: none; }
}
</style>
