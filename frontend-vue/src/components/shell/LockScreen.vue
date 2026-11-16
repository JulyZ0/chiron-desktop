<script setup lang="ts">
/**
 * 本地模式的锁屏。
 *
 * ## 为什么要有一个"锁屏"，而不是"登出"
 *
 * 本地模式的身份来自 OS 账户（一个 Windows 用户 = 一个应用用户），网关按本机
 * 回环来源**免口令**签发会话。也就是说：能坐到这台机器前的人，就自动是这个账户
 * 的主人。共用工作站上这不够 —— 登录 Windows 后人走开、屏幕没锁，回来的是另一个人。
 *
 * 而"登出"在本地模式没有语义：身份来自 OS，销毁会话既没必要（重建得出来），
 * 又危险（重建失败就把用户锁在门外，正是用户报的"点退出登录整个系统不可用"）。
 * 所以「退出登录」的正确落地是**锁定**：挡住界面，直到用该账户的凭据证明
 * "还是同一个人"。
 *
 * ## 这是加固，不是访问控制
 *
 * token 仍在内存里，锁定挡的是"旁人直接操作这个窗口"。真正的隔离靠 OS 账户本身
 * （数据目录按账户 ACL 隔离，见 data-dir-acl.cjs）。所以它**不持久化**：刷新或重启
 * 即解锁 —— 那时人已经重新经过了 OS 登录，再要他输一次密码只是骚扰。
 *
 * ## 为什么在 window 捕获阶段吞键盘
 *
 * 全屏遮罩能挡住鼠标，挡不住键盘：聊天页在 window 上注册了一串快捷键（停止生成、
 * 打开面板、切换会话…）。只画遮罩的话，旁人一个快捷键就能触达后台逻辑，视觉上却
 * 看不到发生了什么 —— 比不锁更糟（用户会以为状态没变）。
 *
 * 所以除了密码输入框内的按键，其余一律在**捕获阶段**吞掉。用捕获而不是冒泡：
 * 冒泡阶段时业务监听器已经先执行完了。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuthStore } from '../../stores/auth'
import { useDeviceStore } from '../../stores/device'

const { t } = useI18n()
const auth = useAuthStore()
const device = useDeviceStore()

const password = ref('')
/** 第二因素：6 位 TOTP 或恢复码。启用 2FA 后才需要。 */
const secondFactor = ref('')
const submitting = ref(false)
const errorText = ref('')
/**
 * 是否已进入"要验证码"那一步。
 *
 * 主进程返回 `needs-second-factor` 时置真 —— 它意味着**密码已经对了**，只是还差
 * 第二因素。这样界面能把两句提示分开："密码不对"与"验证码不对"是两条不同的行动
 * 指引，混成一句会让人反复重试同一个输入。
 */
const needSecondFactor = ref(false)
const inputRef = ref<HTMLInputElement | null>(null)
const secondInputRef = ref<HTMLInputElement | null>(null)

/** 正在验证的账户：`DOMAIN\username`，让用户知道要输哪一个账户的密码 */
const account = computed(() => device.osAccount || '')
const canSubmit = computed(() => {
  if (submitting.value) return false
  if (password.value.length === 0) return false
  // 进入第二因素那一步后，密码已经验证过了，只等码
  if (needSecondFactor.value && secondFactor.value.trim().length === 0) return false
  return true
})

/**
 * 把主进程的结构化结果翻译成一句能照做的话。
 *
 * 三类必须分开（见 `LockVerifyResult`）：
 *   · `bad-credential` —— 密码错，重试有意义；
 *   · `account-issue` —— 账户过期/禁用/需改密码，重试也没用；
 *   · `verification-unavailable` / `unsupported` —— 环境不具备，用户没错。
 * 混成一句"验证失败"会让用户反复试一个正确的密码。
 *
 * 第二因素同理：`replayed`（刚用过，等 30 秒）与 `mismatch`（码不对）不能合并。
 */
function describeFailure(reason: string | undefined, detail: string | undefined): string {
  switch (reason) {
    case 'bad-credential':
      return t('auth.lock_bad_credential')
    case 'account-issue':
      return t('auth.lock_account_issue', { detail: detail || '' })
    case 'verification-unavailable':
      return t('auth.lock_unavailable', { detail: detail || '' })
    case 'unsupported':
      return t('auth.lock_unsupported')
    case 'needs-second-factor':
      return t('auth.lock_second_factor_required')
    case 'replayed':
      return t('auth.lock_second_factor_replayed')
    case 'bad-second-factor':
    case 'mismatch':
    case 'invalid-token':
      return t('auth.lock_second_factor_bad')
    default:
      return detail || t('auth.lock_bad_credential')
  }
}

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  errorText.value = ''
  try {
    const result = await auth.unlock(password.value, secondFactor.value.trim())
    if (result.ok) {
      // 明文立刻清掉：解锁成功后没有任何理由继续留着它们
      password.value = ''
      secondFactor.value = ''
      needSecondFactor.value = false
      return
    }
    // 进入第二因素那一步：密码已对，保留密码（用户不必重输），清空验证码并聚焦
    if (result.reason === 'needs-second-factor') {
      needSecondFactor.value = true
      secondFactor.value = ''
      await nextTick()
      secondInputRef.value?.focus()
      return
    }
    errorText.value = describeFailure(result.reason, result.message)
    if (needSecondFactor.value) {
      // 码错：只清验证码，密码保留 —— 否则用户要为一个输错的 6 位数重打整串密码
      secondFactor.value = ''
      await nextTick()
      secondInputRef.value?.focus()
      return
    }
    // 密码阶段失败：清空并重新聚焦
    password.value = ''
    await nextTick()
    inputRef.value?.focus()
  } finally {
    submitting.value = false
  }
}

/**
 * 捕获阶段吞掉键盘：除本组件的输入框内按键外，任何快捷键都不该触达业务逻辑。
 *
 * **必须先判断是否真的锁着**：本组件在 `App.vue` 里是常驻挂载的（锁屏要能在
 * 任何页面立刻盖上来，不能等路由切换），`onMounted` 里的监听也因此从应用启动
 * 的第一秒就生效。少了这道判断，未锁定时它会吞掉**全站每一个按键** ——
 * 表现是所有输入框（设置页的验证码、搜索框、聊天输入框…）都打不进字，
 * 而遮罩并没有出现，用户完全看不出是锁屏干的。
 */
function swallowKeys(event: KeyboardEvent) {
  if (!auth.locked) return
  const target = event.target as Node | null
  for (const el of [inputRef.value, secondInputRef.value]) {
    if (el && target && el.contains(target)) return
  }
  event.stopPropagation()
  event.preventDefault()
}

onMounted(() => {
  window.addEventListener('keydown', swallowKeys, true)
  void device.ensureReady()
  void nextTick(() => inputRef.value?.focus())
})

onUnmounted(() => {
  window.removeEventListener('keydown', swallowKeys, true)
})

// 重新锁定时清掉上次的残影（错误文案 / 残留密码）
watch(
  () => auth.locked,
  (locked) => {
    if (!locked) {
      password.value = ''
      errorText.value = ''
      return
    }
    void nextTick(() => inputRef.value?.focus())
  },
)
</script>

<template>
  <Teleport to="body">
    <div
      v-if="auth.locked"
      class="lock-overlay"
      role="dialog"
      aria-modal="true"
      :aria-label="t('auth.lock_title')"
      data-test="lock-overlay"
    >
      <form
        class="lock-card"
        @submit.prevent="submit"
      >
        <h2 class="lock-title">
          {{ t('auth.lock_title') }}
        </h2>
        <p class="lock-hint">
          {{ t('auth.lock_hint') }}
        </p>
        <p
          v-if="account"
          class="lock-account"
          data-test="lock-account"
        >
          {{ t('auth.lock_account', { account }) }}
        </p>

        <label
          class="lock-field"
          for="lock-password"
        >
          <span class="lock-label">{{ t('auth.password') }}</span>
          <input
            id="lock-password"
            ref="inputRef"
            v-model="password"
            class="lock-input"
            type="password"
            autocomplete="current-password"
            :disabled="submitting"
            data-test="lock-input"
          >
        </label>

        <!-- 第二因素：只在该账户启用了 2FA 时出现（主进程回 needs-second-factor）。
             此时密码已经验过，用户只需补一个验证码 —— 所以密码框保留、不清空。 -->
        <template v-if="needSecondFactor">
          <p class="lock-hint lock-hint--second">
            {{ t('auth.lock_second_factor_hint') }}
          </p>
          <label
            class="lock-field"
            for="lock-second-factor"
          >
            <span class="lock-label">{{ t('auth.lock_second_factor_label') }}</span>
            <input
              id="lock-second-factor"
              ref="secondInputRef"
              v-model="secondFactor"
              class="lock-input"
              type="text"
              inputmode="numeric"
              autocomplete="one-time-code"
              spellcheck="false"
              :disabled="submitting"
              data-test="lock-second-factor"
            >
          </label>
        </template>

        <p
          v-if="errorText"
          class="lock-error"
          role="alert"
          data-test="lock-error"
        >
          {{ errorText }}
        </p>

        <button
          type="submit"
          class="lock-submit"
          :disabled="!canSubmit"
          :aria-busy="submitting"
          data-test="lock-submit"
        >
          {{ submitting ? t('auth.lock_unlocking') : t('auth.lock_unlock') }}
        </button>
      </form>
    </div>
  </Teleport>
</template>

<style scoped>
/* 层级：`--z-takeover`（2000）是全站最高的语义层。
   锁屏必须盖住一切 —— 包括 Super-agent 接管遮罩，否则"接管中锁屏"会出现
   两层遮罩叠着、下面那层还能被点到。 */
.lock-overlay {
  position: fixed;
  inset: 0;
  z-index: var(--z-takeover);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--bg-page);
  /* 不许点透：这是"挡"，不是"提示" */
  cursor: not-allowed;
}

.lock-card {
  width: 100%;
  max-width: 380px;
  padding: 28px 24px 24px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  box-shadow: var(--takeover-card-shadow);
  cursor: default;
}

.lock-title {
  margin: 0 0 8px;
  font-size: 18px;
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
}

.lock-hint {
  margin: 0 0 6px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-secondary);
}

/* 第二因素的说明比首屏提示更靠下，且需要与上方密码框拉开距离，
   否则两段文字会挤在一起看不出"这是第二步"。 */
.lock-hint--second {
  margin-block-start: 14px;
  color: var(--text-tertiary);
}

.lock-account {
  margin: 0 0 18px;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-tertiary);
}

.lock-field {
  display: block;
  margin-bottom: 14px;
}

.lock-label {
  display: block;
  margin-bottom: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}

.lock-input {
  width: 100%;
  padding: 8px 10px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--radius-sm);
  background: var(--bg-base);
  color: var(--text-primary);
  font-family: inherit;
  font-size: var(--fs-sm);
}

.lock-input:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: 1px;
}

.lock-input:disabled {
  opacity: 0.6;
}

.lock-error {
  margin: 0 0 12px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--warning);
}

.lock-submit {
  width: 100%;
  padding: 9px 16px;
  border: none;
  border-radius: var(--radius-sm);
  background: var(--primary);
  color: var(--on-solid);
  font-size: var(--fs-sm);
  cursor: pointer;
  transition: opacity var(--dur-fast);
}

.lock-submit:hover:not(:disabled) {
  opacity: 0.9;
}

.lock-submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.lock-submit:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: 2px;
}
</style>
