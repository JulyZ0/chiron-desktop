/**
 * 会话分享（从 `ChatView.vue` 抽出的簇，L3-4 拆簇）。
 *
 * 边界：分享弹窗的状态 + 选消息 → 生成链接 → 撤销 / 复制。会话列表读取、当前会话与
 * 切换动作通过**依赖注入**（`sessions` / `activeSessionId` / `switchSession` / `items`），
 * 本 composable 不持有会话状态；账号态在内部经 `useAuthStore()` 取得（`isGuest` 只服务于
 * 分享弹窗里的游客提示）。
 */
import { copyTextOrThrow } from '../utils/clipboard'
import { computed, ref } from 'vue'
import type { Ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { message } from 'ant-design-vue'

import { createShare, getActiveShare, revokeShare } from '../api'
import type { ShareInfo } from '../api'
import { useAuthStore } from '../stores/auth'
import type { ChatItem, ChatSession } from '../components/chat/chat-types'
import { serverErrorMessage } from '../utils/apiError'
import { resolveShareUrl, safeShareUrl, ShareLinkUnavailableError } from '../utils/shareLink'

export interface ChatShareDeps {
  /** 当前会话的消息列表（分享候选的来源） */
  items: Ref<ChatItem[]>
  /** 会话列表（按 id 找到分享目标） */
  sessions: Ref<ChatSession[]>
  /** 当前会话 id（与分享目标不同则先切换） */
  activeSessionId: Ref<string>
  /** 切换到指定会话 */
  switchSession: (id: string) => Promise<void>
}

export function useChatShare(deps: ChatShareDeps) {
  const { t } = useI18n()
  const authStore = useAuthStore()
  const { items, sessions, activeSessionId, switchSession } = deps

  const shareOpen = ref(false)
  const shareTarget = ref<ChatSession | null>(null)
  const shareInfo = ref<(ShareInfo & { url?: string }) | null>(null)
  const shareLoading = ref(false)
  const shareRevoking = ref(false)
  const shareError = ref('')
  const shareMessageIds = ref<string[]>([])

  const isGuest = computed(() => !authStore.user)

  // 分享候选：会话中所有文本消息（用户可勾选；工具调用/思考块不分享）
  const shareCandidates = computed(() => items.value
    .filter((it): it is Extract<ChatItem, { kind: 'text' }> =>
      it.kind === 'text' && (it.role === 'user' || it.role === 'assistant') && !!it.id)
    .map(it => ({
      id: it.id as string,
      role: it.role,
      preview: (it.content || '').replace(/\s+/g, ' ').trim().slice(0, 56),
    })))

  async function openShare(id: string) {
    const s = sessions.value.find(x => x.id === id)
    if (!s) return
    if (id !== activeSessionId.value) {
      await switchSession(id)
    }
    shareTarget.value = s
    shareInfo.value = null
    shareError.value = ''
    shareMessageIds.value = shareCandidates.value.map(c => c.id)
    if (!isGuest.value) {
      try { shareInfo.value = await getActiveShare(s.id) } catch { /* 无活跃分享 */ }
    }
    shareOpen.value = true
  }

  function toggleShareMessage(id: string) {
    const i = shareMessageIds.value.indexOf(id)
    if (i >= 0) shareMessageIds.value.splice(i, 1)
    else shareMessageIds.value.push(id)
  }

  async function generateShare() {
    if (!shareTarget.value) return
    if (shareMessageIds.value.length === 0) { message.warning(t('chat.please_select_at_least_one_message_to_share')); return }
    shareLoading.value = true
    shareError.value = ''
    try {
      shareInfo.value = await createShare(shareTarget.value.id, shareMessageIds.value)
    } catch (e) {
      shareError.value = serverErrorMessage(e, t('errors.failed_to_generate_share_link'))
    } finally {
      shareLoading.value = false
    }
  }

  async function revokeCurrentShare() {
    if (!shareTarget.value || !shareInfo.value) return
    shareRevoking.value = true
    try {
      await revokeShare(shareTarget.value.id)
      shareInfo.value = null
      message.success(t('errors.share_cancelled_link_is_invalid'))
    } catch {
      message.error(t('errors.failed_to_cancel_sharing'))
    } finally {
      shareRevoking.value = false
    }
  }

  function shareUrl(): string {
    // 链接以后端返回的 url 为准（桌面端是 file://，origin 拼出来的是坏链）。
    // 拿不到可达链接时返回空串（模板里直接绑定，不能抛），提示由
    // `shareLinkUnavailable` 驱动、复制按钮给出可读错误。
    return safeShareUrl(shareInfo.value?.url, window.location.origin, shareInfo.value?.share_id || '')
  }

  /** 有分享但链接不可达（后端没给 url 且页面不在 http(s) 上）—— 用来显示配置提示 */
  const shareLinkUnavailable = computed(() => Boolean(shareInfo.value) && !shareUrl())

  async function copyShareLink() {
    try {
      const url = resolveShareUrl(
        shareInfo.value?.url,
        window.location.origin,
        shareInfo.value?.share_id || '',
      )
      await copyTextOrThrow(url)
      message.success(t('common.link_copied'))
    } catch (e) {
      if (e instanceof ShareLinkUnavailableError) {
        // 可读提示 + 指向真正的修复点：链接主机是部署配置，不是前端能猜的。
        message.error(t('errors.share_link_unavailable_please_configure_public_base_url'))
        return
      }
      message.error(t('errors.copy_failed'))
    }
  }

  return {
    shareOpen, shareTarget, shareInfo, shareLoading, shareRevoking, shareError,
    shareMessageIds, isGuest, shareCandidates, shareLinkUnavailable,
    openShare, toggleShareMessage, generateShare, revokeCurrentShare, shareUrl, copyShareLink,
  }
}
