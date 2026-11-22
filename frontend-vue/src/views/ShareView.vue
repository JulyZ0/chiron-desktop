<script setup lang="ts">
import { copyTextOrThrow } from '../utils/clipboard'
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import MarkdownIt from 'markdown-it'
import type { Token } from 'markdown-it'
import DOMPurify from 'dompurify'
import { getPublicShare } from '../api'
import type { PublicShare } from '../api'
import { splitThinking, stripUserInputTag } from '../components/chat/chat-types'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import EmptyState from '../components/common/EmptyState.vue'
import { useI18n } from 'vue-i18n'

import { errorStatus, errorCode } from '../utils/apiError'
const { t } = useI18n()

const route = useRoute()

const share = ref<PublicShare | null>(null)
const loading = ref(true)
const error = ref('')
/** 失败是否值得重试：网络/超时/5xx 值得，404/410 不值得（重试一百次也还是失效）。 */
const canRetry = ref(false)
/**
 * 需要访问密码（后端 401 + `share_password_required`）。
 *
 * 单独一个状态而不是塞进 `error`：这不是失败终态，而是一个**等待输入**的中间态 ——
 * 用错误页展示它，访客会以为链接坏了，然后关掉页面。
 */
const passwordRequired = ref(false)
/** 已提交过的密码（重试时复用，避免访客再输一遍）。 */
const password = ref('')
const passwordError = ref('')
const passwordSubmitting = ref(false)

/**
 * 访客侧的显示选项：只影响本页渲染，不改分享数据、也不写服务端。
 *
 * 为什么放在前端：分享链接是匿名只读页面，创建者无法预先知道访客的屏幕与偏好；
 * 让访客自己调主题/版心/字号，比在创建时锁死一套排版更实用。选择存 localStorage，
 * 同一浏览器下次打开保持。
 */
type ShareTheme = 'auto' | 'light' | 'dark'
type ShareWidth = 'narrow' | 'wide'
type ShareFont = 'small' | 'medium' | 'large'

const DISPLAY_STORAGE_KEY = 'chiron.share.display'
const display = ref<{ theme: ShareTheme; width: ShareWidth; font: ShareFont }>({
  theme: 'auto',
  width: 'narrow',
  font: 'medium',
})

try {
  const raw = localStorage.getItem(DISPLAY_STORAGE_KEY)
  if (raw) {
    const saved = JSON.parse(raw) as Partial<typeof display.value>
    if (saved.theme === 'auto' || saved.theme === 'light' || saved.theme === 'dark') display.value.theme = saved.theme
    if (saved.width === 'narrow' || saved.width === 'wide') display.value.width = saved.width
    if (saved.font === 'small' || saved.font === 'medium' || saved.font === 'large') display.value.font = saved.font
  }
} catch {
  /* 隐私模式 / 损坏的 JSON：用默认值，不影响阅读 */
}

watch(display, value => {
  try {
    localStorage.setItem(DISPLAY_STORAGE_KEY, JSON.stringify(value))
  } catch {
    /* 存不了只是下次要重选，不值得打断阅读 */
  }
}, { deep: true })

const themeChoices = [
  { value: 'auto', label: 'common.share_display_auto' },
  { value: 'light', label: 'common.share_display_light' },
  { value: 'dark', label: 'common.share_display_dark' },
] as const
const widthChoices = [
  { value: 'narrow', label: 'common.share_display_narrow' },
  { value: 'wide', label: 'common.share_display_wide' },
] as const
const fontChoices = [
  { value: 'small', label: 'common.share_display_small' },
  { value: 'medium', label: 'common.share_display_medium' },
  { value: 'large', label: 'common.share_display_large' },
] as const

/**
 * 请求硬超时（与 `getPublicShare` 内部的 axios timeout 同一量级）。
 *
 * 为什么视图层还要一道：axios 的 timeout 依赖适配器回包/取消**真的**被触发；
 * 代理把连接挂住、适配器被 mock、或宿主（file:// 外壳）吞掉错误时它可能永远不 settle。
 * 匿名落地页没有任何别的内容可看，"一直转圈"就是最差的终态 —— 这里用竞速保证
 * 10s 内必定落到 成功 / 失效 / 可重试 三种终态之一。
 */
const SHARE_DEADLINE_MS = 10_000

/** 本次加载的代号：重试或卸载后回来的旧响应直接丢弃，不再改写页面状态。 */
let loadSeq = 0

// 轻量 markdown 渲染（公开页不引入 KaTeX/mermaid，代码块带复制按钮）
const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
md.renderer.rules.fence = (tokens: Token[], idx: number) => {
  const token = tokens[idx]
  const lang = md.utils.escapeHtml((token.info || '').trim().toLowerCase() || 'code')
  const code = md.utils.escapeHtml(token.content)
  const encoded = encodeURIComponent(token.content)
  return `<div class="code-block"><div class="code-block-head"><span class="code-lang">${lang}</span><button type="button" class="code-copy" data-code="${encoded}">${t('common.copy')}</button></div><pre><code>${code}</code></pre></div>`
}

function renderMarkdown(src: string): string {
  try {
    return DOMPurify.sanitize(md.render(src), {
      // button 必须放行：代码块的「复制」是真按钮才能键盘可达（<span> 无 tabindex 不可聚焦）；
      // type 同理需在 ALLOWED_ATTR 里，否则被 DOMPurify 剥掉。
      ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'code', 'pre', 'ul', 'ol', 'li', 'a', 'blockquote', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'span', 'div', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'hr', 'del', 'sup', 'sub', 'button'],
      ALLOWED_ATTR: ['href', 'target', 'rel', 'class', 'data-code', 'type'],
      ALLOW_DATA_ATTR: true,
      FORBID_TAGS: ['style', 'script', 'iframe', 'object', 'embed', 'form', 'input', 'textarea', 'select', 'marquee', 'link', 'meta'],
      FORBID_ATTR: ['style', 'onerror', 'onload', 'onclick', 'onmouseover', 'onfocus', 'onblur', 'onchange', 'onsubmit', 'onreset', 'onkeydown', 'onkeyup', 'onkeypress'],
    })
  } catch {
    return md.utils.escapeHtml(src)
  }
}

/**
 * 渲染用的消息：把落库原文拆成「思考」与「正文」两段。
 *
 * `<user_input>` 包装由后端剥离（internal/api/share.go 的 stripUserInputTag），
 * 这里再剥一次只是兜底（老数据、缓存等）。
 *
 * `[thinking]` 块**保留**，拆到 reasoning 里由模板折叠展示 —— 公开页不该裸露标签，
 * 但也不该把"这次回答想了什么"整个丢掉。顺序与 ChatView 一致。
 */
// 分享页是**只读降级**渲染：只出正文（+ 折叠的思考），不出任何运行时徽标。
// 知识库检索事件（`knowledge_retrieval`）是实时过程事件、不落库，所以这里天然没有；
// 即便将来把过程事件一并存下来，也不该在公开页展示 —— "检索了 3 个库 / 某库 403"
// 泄露的是库的存在性与权限边界，而看分享页的人没有库权限。
const messages = computed(() =>
  (share.value?.messages ?? []).map(m => {
    const { reasoning, body } = splitThinking(
      stripUserInputTag(String(m.content ?? '')),
      { loose: true },
    )
    return { ...m, reasoning, body }
  }),
)

function handleClick(e: MouseEvent) {
  const btn = (e.target as HTMLElement).closest('.code-copy') as HTMLElement | null
  if (!btn) return
  const code = decodeURIComponent(btn.dataset.code || '')
  if (!code) return
  copyTextOrThrow(code).then(() => {
    btn.textContent = t('common.copiedExcl')
    setTimeout(() => { btn.textContent = t('common.copy') }, 2000)
  }).catch(() => { /* clipboard unavailable */ })
}

/**
 * 给任意 promise 加硬超时（超时 reject，不让页面无限等待）。
 *
 * 不接 AbortSignal：请求由 `getPublicShare` 自己的 axios timeout 负责取消，
 * 这里只管"页面状态必须收口"这一件事，避免两个取消源互相干扰。
 */
function withDeadline<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error('share: deadline exceeded'))
    }, ms)
    promise.then(
      (value) => { clearTimeout(timer); resolve(value) },
      (err) => { clearTimeout(timer); reject(err) },
    )
  })
}

/**
 * 加载分享内容 —— 本页**唯一**的状态机入口，保证必然收口到三种终态：
 *   成功（渲染会话内容）/ 明确失效（404 不存在或已过期、410 已被撤销或过期）/ 可重试失败。
 *
 * 之前的写法把清除 loading 只放在 `finally` 里，看似必然执行，实际漏了「等不到」这一态：
 * 请求被全局拦截器挂住（401 跳转、限流退避）、或迟迟不 settle 时，`finally` 永远不会到达。
 */
async function loadShare() {
  const seq = ++loadSeq
  // 短链 `/s/:token` 与兼容别名 `/share/:id` 共用本页，参数名不同，两处都读。
  const id = String(route.params.token || route.params.id || '')

  loading.value = true
  error.value = ''
  canRetry.value = false
  passwordError.value = ''
  share.value = null

  try {
    if (!id) throw new Error('share: missing token')
    // 有密码才带第二个参数：无密码的首次加载保持原来的调用形态（也避免发一个空头）。
    const request = password.value ? getPublicShare(id, password.value) : getPublicShare(id)
    const data = await withDeadline(request, SHARE_DEADLINE_MS)
    // 空 body / 非 JSON（网关兜底吐了 HTML）也按失败处理：静默渲染空白同样是"没终态"。
    if (!data || !Array.isArray(data.messages)) throw new Error('share: malformed payload')
    if (seq === loadSeq) {
      share.value = data
      passwordRequired.value = false
    }
  } catch (e) {
    if (seq !== loadSeq) return
    const status = errorStatus(e)
    const code = errorCode(e)
    if (code === 'share_password_required') {
      // 需要密码（含密码错误）：停在"等待输入"态，不把访客赶到错误页。
      passwordRequired.value = true
      if (password.value) passwordError.value = t('errors.share_password_wrong')
    } else if (code === 'share_revoked') {
      // 被创建者主动撤销：与"过了有效期"是两件事，文案必须分开
      error.value = t('common.this_share_was_deleted_by_its_creator')
    } else if (code === 'share_view_limit') {
      // 达到创建者设定的访问次数上限：与"过期"分开说，否则访客以为链接太老
      error.value = t('errors.share_view_limit_reached')
    } else if (status === 410 || status === 404) {
      // 410 share_expired 与 404 都可以用"不存在或已失效"覆盖（不误导成"被删除"）
      error.value = t('errors.share_does_not_exist_or_has_expired')
    } else {
      error.value = t('errors.load_failed_please_retry_later')
      canRetry.value = true
    }
  } finally {
    // 只有"最新一次加载"才有权收口，避免旧请求把新一轮的 loading 提前关掉
    if (seq === loadSeq) loading.value = false
  }
}

/** 提交访问密码：立刻进入加载态，失败（密码错）时回到表单并提示。 */
async function submitPassword() {
  if (passwordSubmitting.value) return
  const value = password.value.trim()
  if (!value) {
    passwordError.value = t('errors.share_password_empty')
    return
  }
  // 用 trim 后的值提交，避免手机输入法带出的尾随空格被当成密码的一部分
  password.value = value
  passwordSubmitting.value = true
  try {
    await loadShare()
  } finally {
    passwordSubmitting.value = false
  }
}

onMounted(loadShare)

function formatDate(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return t('common.year_month_day_hh_mm', {
    year: d.getFullYear(), month: d.getMonth() + 1, day: d.getDate(),
    hh: String(d.getHours()).padStart(2, '0'), mm: String(d.getMinutes()).padStart(2, '0'),
  })
}
</script>

<template>
  <div
    class="share-page"
    :data-theme="display.theme"
    :data-width="display.width"
    :data-font="display.font"
  >
    <header class="share-header">
      <div class="share-brand">
        <span class="share-brand-mark">MC</span>
        <span>{{ $t('chat.chiron_conversation_share') }}</span>
      </div>
      <div class="share-display">
        <div
          class="share-display-group"
          role="group"
          :aria-label="$t('common.share_display_theme')"
        >
          <span class="share-display-label">{{ $t('common.share_display_theme') }}</span>
          <button
            v-for="opt in themeChoices"
            :key="opt.value"
            type="button"
            :class="{ active: display.theme === opt.value }"
            :aria-pressed="display.theme === opt.value"
            @click="display.theme = opt.value"
          >
            {{ $t(opt.label) }}
          </button>
        </div>
        <div
          class="share-display-group"
          role="group"
          :aria-label="$t('common.share_display_width')"
        >
          <span class="share-display-label">{{ $t('common.share_display_width') }}</span>
          <button
            v-for="opt in widthChoices"
            :key="opt.value"
            type="button"
            :class="{ active: display.width === opt.value }"
            :aria-pressed="display.width === opt.value"
            @click="display.width = opt.value"
          >
            {{ $t(opt.label) }}
          </button>
        </div>
        <div
          class="share-display-group"
          role="group"
          :aria-label="$t('common.share_display_font')"
        >
          <span class="share-display-label">{{ $t('common.share_display_font') }}</span>
          <button
            v-for="opt in fontChoices"
            :key="opt.value"
            type="button"
            :class="{ active: display.font === opt.value }"
            :aria-pressed="display.font === opt.value"
            @click="display.font = opt.value"
          >
            {{ $t(opt.label) }}
          </button>
        </div>
      </div>
    </header>

    <main class="share-main">
      <PageSkeleton
        v-if="loading"
        variant="detail"
        :rows="4"
      />
      <!-- 需要访问密码：表单态而不是错误态（链接是好的，只差一个口令） -->
      <form
        v-else-if="passwordRequired"
        class="share-password"
        @submit.prevent="submitPassword"
      >
        <h2 class="share-password-title">
          {{ $t('errors.share_password_required_title') }}
        </h2>
        <p class="share-password-hint">
          {{ $t('errors.share_password_required_hint') }}
        </p>
        <label
          class="share-password-field"
          for="share-password-input"
        >
          <input
            id="share-password-input"
            v-model="password"
            class="share-password-input"
            type="password"
            autocomplete="off"
            :placeholder="$t('errors.share_password_placeholder')"
            :aria-invalid="passwordError ? 'true' : 'false'"
          >
        </label>
        <p
          v-if="passwordError"
          class="share-password-error"
          role="alert"
        >
          {{ passwordError }}
        </p>
        <button
          type="submit"
          class="share-password-submit"
          :disabled="passwordSubmitting"
        >
          {{ $t('errors.share_password_submit') }}
        </button>
      </form>
      <div
        v-else-if="error"
        class="share-error"
      >
        <EmptyState
          size="page"
          :description="error"
        />
        <!-- 只有"重试有意义"的失败才给按钮：失效链接重试一百次也还是失效 -->
        <button
          v-if="canRetry"
          type="button"
          class="share-retry"
          @click="loadShare"
        >
          {{ $t('common.retry') }}
        </button>
      </div>
      <template v-else-if="share">
        <div class="share-head">
          <h1 class="share-title">
            {{ share.title || $t('chat.new_conversation') }}
          </h1>
          <div class="share-meta">
            {{ $t('chat.date_n_messages', { date: formatDate(share.created_at), n: share.messages.length }) }}
          </div>
        </div>

        <div
          class="share-thread"
          data-click-delegate
          @click="handleClick"
        >
          <div
            v-for="(m, i) in messages"
            :key="i"
            class="share-msg"
            :class="m.role"
          >
            <div class="share-msg-bubble">
              <!-- 思考过程折叠展示：不裸露 [thinking] 标签，也不丢弃内容 -->
              <details
                v-if="m.reasoning"
                class="share-msg-reasoning"
              >
                <summary>{{ $t('chat.reasoning_process') }}</summary>
                <div class="share-msg-reasoning-body">
                  {{ m.reasoning }}
                </div>
              </details>
              <div
                v-if="m.role === 'assistant'"
                class="share-msg-text"
                v-html="renderMarkdown(m.body)"
              />
              <div
                v-else
                class="share-msg-text"
              >
                {{ m.body }}
              </div>
            </div>
          </div>
        </div>
      </template>
    </main>

    <footer class="share-footer">
      {{ $t('common.chiron_generated_this_page_is_viewable_by_anyone_with_the_link') }}
    </footer>
  </div>
</template>

<style scoped>
.share-page { min-height: var(--app-height); display: flex; flex-direction: column; background: var(--bg-page); color: var(--text-primary); }
/* 访客侧显示选项：覆盖应用主题变量，只作用于本页 */
.share-page[data-theme="light"] {
  color-scheme: light;
  --bg-page: #ffffff; --bg-card: #ffffff; --bg-secondary: #f6f8fa; --bg-hover: #eef1f4; --bg-code: #f6f8fa;
  --text-primary: #1f2328; --text-secondary: #424a53; --text-tertiary: #6e7781; --text-muted: #6e7781;
  --border: #d0d7de; --bubble-user: #0969da; --bubble-user-text: #ffffff;
}
.share-page[data-theme="dark"] {
  color-scheme: dark;
  --bg-page: #0d1117; --bg-card: #161b22; --bg-secondary: #21262d; --bg-hover: #21262d; --bg-code: #0d1117;
  --text-primary: #e6edf3; --text-secondary: #c9d1d9; --text-tertiary: #8b949e; --text-muted: #8b949e;
  --border: #30363d; --bubble-user: #1f6feb; --bubble-user-text: #ffffff;
}
.share-page[data-width="wide"] .share-main { max-width: 1080px; }
.share-page[data-font="small"] .share-msg-text { font-size: 15px; line-height: 25px; }
.share-page[data-font="small"] .share-msg.user .share-msg-bubble { font-size: 14px; line-height: 22px; }
.share-page[data-font="large"] .share-msg-text { font-size: 18px; line-height: 31px; }
.share-page[data-font="large"] .share-msg.user .share-msg-bubble { font-size: 17px; line-height: 26px; }
.share-header { height: 52px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 0 24px; border-bottom: var(--hairline-width) solid var(--border); background: var(--bg-card); }
.share-display { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.share-display-group { display: inline-flex; align-items: center; gap: 2px; padding: 2px; border: var(--hairline-width) solid var(--border); border-radius: var(--radius-md); background: var(--bg-page); }
.share-display-label { padding: 0 4px; font-size: 12px; color: var(--text-tertiary); }
.share-display button {
  min-height: 28px; padding: 4px 8px; cursor: pointer;
  font-family: inherit; font-size: 12px; color: var(--text-secondary);
  background: transparent; border: none; border-radius: calc(var(--radius-md) - 2px);
}
.share-display button:hover { background: var(--bg-hover); }
.share-display button.active { background: var(--primary); color: var(--on-solid); }
.share-display button:focus-visible { outline: 2px solid var(--primary); outline-offset: 1px; }
.share-brand { display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: var(--fw-semibold); color: var(--text-primary); }
.share-brand-mark { width: 22px; height: 22px; border-radius: var(--radius-md); background: linear-gradient(135deg, var(--primary), var(--primary-dark)); color: var(--on-solid); font-weight: var(--fw-bold); font-size: var(--fs-xs); display: inline-flex; align-items: center; justify-content: center; }
.share-main { flex: 1; width: 100%; max-width: 748px; margin: 0 auto; padding: 32px 24px 60px; min-width: 0; }
.share-head { margin-bottom: 24px; }
.share-title { font-size: 22px; line-height: 30px; font-weight: var(--fw-bold); margin: 0 0 6px; word-break: break-word; }
.share-meta { font-size: 12px; color: var(--text-tertiary); }
.share-thread { display: flex; flex-direction: column; gap: 18px; }
.share-error { display: flex; flex-direction: column; align-items: center; gap: 16px; }
/* 密码表单：与内容同宽上限，居中；输入框 16px 防止 iOS 聚焦时放大页面 */
.share-password { max-width: 360px; margin: 48px auto 0; display: flex; flex-direction: column; gap: 10px; }
.share-password-title { margin: 0; font-size: 18px; line-height: 26px; font-weight: var(--fw-semibold); color: var(--text-primary); }
.share-password-hint { margin: 0; font-size: 13px; line-height: 20px; color: var(--text-secondary); }
.share-password-field { display: block; }
.share-password-input {
  width: 100%; box-sizing: border-box; min-height: 40px; padding: 8px 12px;
  font-family: inherit; font-size: 16px; color: var(--text-primary);
  background: var(--bg-card); border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-md);
}
.share-password-input:focus-visible { outline: 2px solid var(--primary); outline-offset: 1px; border-color: var(--primary); }
.share-password-error { margin: 0; font-size: 13px; color: var(--danger, #d4380d); }
.share-password-submit {
  min-height: 40px; padding: 8px 18px; cursor: pointer;
  font-family: inherit; font-size: 14px; font-weight: var(--fw-semibold); color: var(--on-solid);
  background: var(--primary); border: none; border-radius: var(--radius-md);
}
.share-password-submit:disabled { opacity: 0.6; cursor: default; }
.share-password-submit:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.share-retry {
  min-height: 36px; padding: 8px 18px; cursor: pointer;
  font-family: inherit; font-size: 14px; color: var(--text-primary);
  background: var(--bg-card); border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-md);
  transition: background-color var(--dur-normal), border-color var(--dur-normal);
}
.share-retry:hover { background: var(--bg-hover); border-color: var(--primary); }
.share-retry:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.share-msg { display: flex; }
.share-msg.user { justify-content: flex-end; }
.share-msg-bubble { min-width: 0; max-width: 100%; }
.share-msg.user .share-msg-bubble {
  max-width: min(525px, 100%);
  padding: 10px 16px;
  background: var(--bubble-user); color: var(--bubble-user-text);
  border-radius: 22px; border-bottom-right-radius: 6px;
  font-size: 15px; line-height: 24px; white-space: pre-wrap; word-break: break-word;
}
.share-msg-text { font-size: 16px; line-height: 28px; color: var(--text-primary); word-break: break-word; }
.share-msg-text :deep(p) { margin: 16px 0; }
.share-msg-text :deep(p:first-child) { margin-top: 0; }
.share-msg-text :deep(h1) { font-size: 24px; line-height: 34px; font-weight: var(--fw-bold); margin: 32px 0 16px; }
.share-msg-text :deep(h2) { font-size: 22px; line-height: 32px; font-weight: var(--fw-bold); margin: 32px 0 16px; }
.share-msg-text :deep(h3) { font-size: 20px; line-height: 30px; font-weight: var(--fw-bold); margin: 32px 0 16px; }
.share-msg-text :deep(ul), .share-msg-text :deep(ol) { padding-inline-start: 24px; margin: 16px 0; }
.share-msg-text :deep(li) { margin: 4px 0; }
.share-msg-text :deep(blockquote) { margin: 16px 0; padding: 6px 12px; border-inline-start: 3px solid var(--primary); background: var(--bg-hover); border-radius: var(--radius-md); }
.share-msg-text :deep(a) { color: var(--primary); text-decoration: none; }
.share-msg-text :deep(a:hover) { text-decoration: underline; }
.share-msg-text :deep(code) { font-family: var(--font-mono); font-size: 0.9em; background: var(--bg-secondary); padding: 2px 6px; border-radius: var(--radius-md); }
.share-msg-text :deep(.code-block) { margin: 16px 0; background: var(--bg-code); border-radius: var(--radius-xl); overflow: hidden; max-width: 100%; }
.share-msg-text :deep(.code-block-head) { display: flex; justify-content: space-between; align-items: center; padding: 8px 14px; background: var(--bg-secondary); }
.share-msg-text :deep(.code-lang) { font-family: var(--font-mono); font-size: 12px; color: var(--text-primary); }
.share-msg-text :deep(.code-copy) { background: none; border: none; color: var(--text-tertiary); cursor: pointer; font-family: inherit; font-size: 12px; padding: 4px 10px; min-height: 32px; border-radius: var(--radius-md); transition: color var(--dur-normal), background-color var(--dur-normal); }
.share-msg-text :deep(.code-copy:hover) { color: var(--primary); background: var(--bg-hover); }
/* 代码块：横向滚动而非折行，长行不撑破页面 */
.share-msg-text :deep(pre) { margin: 0 !important; padding: 16px; overflow-x: auto; white-space: pre; word-break: normal; -webkit-overflow-scrolling: touch; }
.share-msg-text :deep(pre code) { background: none; padding: 0; font-size: 0.9em; color: var(--text-code); }
.share-msg-text :deep(table) { border-collapse: collapse; margin: 16px 0; width: 100%; }
.share-msg-text :deep(th), .share-msg-text :deep(td) { border: var(--hairline-width) solid var(--border); padding: 8px 12px; text-align: start; }
.share-msg-text :deep(th) { background: var(--bg-secondary); font-weight: var(--fw-semibold); }
.share-footer { padding: 18px 24px; text-align: center; font-size: 12px; color: var(--text-muted); border-top: var(--hairline-width) solid var(--border); }

/* 移动端：小屏收紧 padding 和字号 */
@media (max-width: 640px) {
  .share-header { height: auto; min-height: 48px; padding: 8px 16px; flex-wrap: wrap; }
  .share-display { justify-content: flex-start; }
  .share-display-label { display: none; }
  .share-main { padding: 20px 16px 40px; }
  .share-title { font-size: 18px; line-height: 26px; }
  .share-msg.user .share-msg-bubble { font-size: 14px; line-height: 22px; padding: 8px 12px; }
  .share-msg-text { font-size: 15px; line-height: 26px; }
  .share-msg-text :deep(h1) { font-size: 20px; line-height: 28px; margin: 24px 0 12px; }
  .share-msg-text :deep(h2) { font-size: 18px; line-height: 26px; margin: 24px 0 12px; }
  .share-msg-text :deep(h3) { font-size: 16px; line-height: 24px; margin: 24px 0 12px; }
  .share-msg-text :deep(pre) { padding: 12px; font-size: 12px; }
  /* 宽表格窄屏横向滚动，避免撑破页面 */
  .share-msg-text :deep(table) { display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .share-footer { padding: 14px 16px; font-size: var(--fs-xs); }
}

/* 窄屏（≤576px）：复制按钮触控目标 ≥ 40px */
@media (max-width: 576px) {
  .share-msg-text :deep(.code-copy) { min-height: 40px; padding: 8px 14px; font-size: 13px; }
}

@media (prefers-reduced-motion: reduce) {
  .share-page { animation: none; transition: none; }
}
</style>
