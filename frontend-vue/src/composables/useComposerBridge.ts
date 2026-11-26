/**
 * 输入区与 ChatView 之间的**装配胶水**（从 `ChatView.vue` 整块抽出，行为逐字不变）。
 *
 * 这里装的不是输入框本身（那是 `components/chat/ChatInput.vue`），而是"外部世界怎么把
 * 内容送进输入框、失败消息怎么回到输入框"这一层：ChatView 只持有这些函数的引用，模板
 * 绑定名一个字都没改。
 *
 * 四段：
 *
 * 1. **`ref` + 文本投递**（`chatInputRef` / `onQuoteText` / `applyChatPrefill`）——
 *    消息区划词「引用」、Agent/工作流「在对话中继续」都往 `insertText` 走**同一条**通道。
 *    两处各写一份插入逻辑就会漂移（换行、光标位置、chip 识别各一份）。
 * 2. **附件通道**（`onWorkbenchReference`）—— 工作台「加入对话」用的是输入框**已有的**
 *    项目文件引用通道（`attachProjectFile` → `insertFileRefToken`，只插 token 不读内容）。
 *    在 ChatView 里另读一次文件再 push 附件，就会有两套 `data:` URL 构造逻辑。
 * 3. **`@` 提及 → 激活芯片**（`onMentionAdd`）—— chips 只是 UI 呈现，`runtime.context`
 *    才是提交时真正生效的东西；只改 chips 不写 runtime，刷新后引用就从界面消失。
 * 4. **失败暂存队列**（`outbox*`）—— 发送失败的消息落盘，输入框顶部横幅据此提供
 *    「立即重发 / 全部丢弃 / 取回编辑」。取回编辑就是"草稿恢复"：从队列删一条、内容
 *    连同附件交回 Composer（留两条会变成两份相同草稿）。
 *
 * ## 依赖为什么是 getter / 回调
 *
 * 这几段都要读"当前会话""当前账号""当前激活芯片"，而它们在 ChatView 里声明得更晚、
 * 或本身就是 ChatView 私有类型。传值会在构造时把某一次快照固化下来（切会话后仍写旧
 * 会话的队列），所以一律惰性求值。
 */
import { computed, ref, watch, nextTick, type Ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { notify } from '../utils/notify'
import {
  addToOutbox,
  bumpRetryCount,
  clearOutbox,
  readOutbox,
  removeFromOutbox,
  type OutboxEntry,
} from '../components/chat/failedOutbox'
import { buildWorkbenchContext, MAX_CONTEXT_KBS, type ContextChip } from '../components/chat/contextChips'
import { buildPrefillText, setChatPrefill, takeChatPrefill } from '../components/chat/chatPrefill'
import type { ChatAttachment } from '../components/chat/chat-types'

/**
 * 输入框实例只需要这几个方法 —— 只声明用到的部分，避免把 `ChatInput.vue` 的
 * 整个 `InstanceType` 绑进来（那是组件内部形状，不属于本模块的契约）。
 */
export interface ComposerHandle {
  insertText: (text: string) => void
  setDraft: (text: string) => void
  setCommandDraft: (cmd: string) => void
  restoreDraft: (text: string, attachments?: ChatAttachment[]) => void
  attachProjectFile: (path: string, name: string) => void | Promise<void>
}

export interface UseComposerBridgeOptions {
  /** 当前会话 id（暂存队列按会话隔离，空串 = 无会话） */
  sessionId: () => string
  /** 当前账号 id（隐私键的账号命名空间） */
  userId: () => string | undefined
  /** 已激活的上下文芯片（`?kb=` 与 `@` 提及共用的那一份） */
  contextChips: () => ContextChip[]
  /** 覆写芯片列表（提及去重/上限判定都在本模块内做完，调用方只负责落地） */
  setContextChips: (chips: ContextChip[]) => void
  /** 把上下文写进会话运行时（单一事实源） */
  persistRuntime: (patch: Record<string, unknown>) => void
  /** 真正发送一条消息：立即重发复用**正常发送路径**，不另写一套提交 */
  sendMessage: (
    text: string,
    attachments?: ChatAttachment[],
    opts?: { clientMsgId?: string },
  ) => Promise<boolean>
}

export interface ComposerBridge {
  /** 模板 `ref="chatInputRef"` 绑的就是它 */
  chatInputRef: Ref<ComposerHandle | null>
  /** 消息区划词「引用到输入框」 */
  onQuoteText: (text: string) => void
  /** 工作台「加入对话」 */
  onWorkbenchReference: (payload: { path: string; name: string }) => void
  /** `@` 提及选中资源 → 激活芯片（同步到 runtime.context） */
  onMentionAdd: (p: { type: string; id: string; name: string }) => void
  /** 「在对话中继续」的落地端 */
  applyChatPrefill: () => Promise<void>
  /** 失败暂存队列（本会话） */
  outbox: Ref<OutboxEntry[]>
  /** 横幅预览用的降维视图 */
  outboxPreview: Ref<Array<{ id: string; preview: string; attachmentCount: number }>>
  /** 重新读盘（切会话、发送失败、增删队列后都要走） */
  refreshOutbox: () => void
  /** 把一条暂存消息取回输入框重新编辑 */
  retrieveOutbox: (id: string) => void
  /** 重发整条队列 */
  retryOutbox: () => Promise<void>
  /** 丢弃本会话全部暂存消息 */
  discardOutbox: () => void
  /** 发送失败时入队（`sendMessage` 的 catch 里调） */
  queueFailedSend: (
    sessionId: string,
    text: string,
    attachments?: ChatAttachment[],
    clientMsgId?: string,
  ) => void
}

export function useComposerBridge(options: UseComposerBridgeOptions): ComposerBridge {
  const { t } = useI18n()

  // ── 1. 输入框实例与文本投递 ────────────────────────────────────────────────
  const chatInputRef = ref<ComposerHandle | null>(null)

  function onQuoteText(text: string) {
    chatInputRef.value?.insertText?.(text)
  }

  /**
   * 「在对话中继续」的落地端：Agent 会话 / 工作流结果经 sessionStorage 投递到这里，
   * 插入输入框（不自动发送）—— 用户可以先修改再发。
   */
  async function applyChatPrefill(): Promise<void> {
    const prefill = takeChatPrefill()
    if (!prefill) return
    const text = buildPrefillText(prefill)
    await nextTick()
    if (chatInputRef.value?.insertText) {
      chatInputRef.value.insertText(text)
    } else {
      // 输入框还没挂载（首帧）：把投递放回去，别让用户的那一次点击被静默吞掉
      setChatPrefill(prefill)
    }
  }

  // ── 2. 附件通道 ────────────────────────────────────────────────────────────
  /**
   * 工作台里点「加入对话」：走的是输入框**同一条附件通道**。
   *
   * 不在 ChatView 里另读一次文件再 push 附件，是因为那样就有两处构造 `data:` URL
   * 的逻辑（编码方式、id 去重、二进制处理各一份），而它们一旦漂移，模型收到的
   * 内容就取决于用户是"@ 选的"还是"面板点的"。
   */
  function onWorkbenchReference(payload: { path: string; name: string }) {
    void chatInputRef.value?.attachProjectFile?.(payload.path, payload.name)
  }

  // ── 3. `@` 提及 → 激活芯片 ─────────────────────────────────────────────────
  const MENTION_CHIP_LABEL = computed<Record<string, string>>(() => ({
    kb: t('knowledge.knowledge_base'), agent: 'Agent', skill: t('agent.skill'), plugin: t('common.plugin'),
  }))

  function onMentionAdd(p: { type: string; id: string; name: string }) {
    const chips = options.contextChips()
    if (chips.some(c => c.type === p.type && c.value === p.id)) return
    // 知识库多选有上限（≤5）：每个库都是一次分钟级推理式检索，选到第 6 个时
    // 该**当场拦住并说明**，而不是默默截断 —— 用户会以为第 6 个也生效了。
    if (p.type === 'kb' && chips.filter(c => c.type === 'kb').length >= MAX_CONTEXT_KBS) {
      notify.warning(t('knowledge.max_knowledge_bases_n', { n: MAX_CONTEXT_KBS }))
      return
    }
    const next: ContextChip[] = [
      ...chips,
      {
        type: p.type as ContextChip['type'],
        label: `${MENTION_CHIP_LABEL.value[p.type] || p.type} ${p.name}`,
        value: p.id,
      },
    ]
    options.setContextChips(next)
    options.persistRuntime({ context: buildWorkbenchContext(next) })
  }

  // ── 4. 失败暂存队列 ────────────────────────────────────────────────────────
  const outbox = ref<OutboxEntry[]>([])

  function refreshOutbox() {
    const sid = options.sessionId()
    outbox.value = sid ? readOutbox(sid, options.userId()) : []
  }
  // 按会话隔离：切会话就重新读 —— 否则会把 A 会话的失败消息挂到 B 会话的重发按钮上。
  watch(options.sessionId, refreshOutbox, { immediate: true })

  /** 横幅预览：输入区只关心"哪条、长什么样、几个附件"，不该接触存储格式。
   *  摘要按**单行**收敛：换行会把条目撑高，列表里三行放不下。 */
  const outboxPreview = computed(() => outbox.value.map(e => ({
    id: e.id,
    preview: e.content.replace(/\s+/g, ' ').trim().slice(0, 200),
    attachmentCount: e.attachments?.length ?? 0,
  })))

  /** 把一条暂存消息取回输入框重新编辑：从队列里删掉它，内容（含附件）交回 Composer。
   *  队列随之少一条 —— 留在队列里再取回一次会变成两条相同的草稿。 */
  function retrieveOutbox(id: string) {
    const sid = options.sessionId()
    if (!sid) return
    const entry = outbox.value.find(e => e.id === id)
    if (!entry) return
    removeFromOutbox(sid, options.userId(), id)
    refreshOutbox()
    chatInputRef.value?.restoreDraft?.(entry.content, entry.attachments)
  }

  /** 重发暂存队列：逐条走正常发送路径，成功一条就从本地删一条；
   *  失败的只累加 retryCount（它本来就在队列里，不该再入队一次）。 */
  async function retryOutbox() {
    const sid = options.sessionId()
    if (!sid) return
    const uid = options.userId()
    for (const entry of readOutbox(sid, uid)) {
      // 复用首次提交的 clientMsgId：同一条消息重试不该被服务端当成新消息执行第二次。
      const ok = await options.sendMessage(entry.content, entry.attachments, {
        clientMsgId: entry.clientMsgId,
      })
      if (ok) removeFromOutbox(sid, uid, entry.id)
      else bumpRetryCount(sid, uid, entry.id)
    }
    refreshOutbox()
  }

  /** 丢弃本会话全部暂存消息（用户明确放弃这几次发送）。 */
  function discardOutbox() {
    const sid = options.sessionId()
    if (!sid) return
    clearOutbox(sid, options.userId())
    refreshOutbox()
  }

  /** 发送失败入队：条目直接成为最新视图（顺带省掉一次读盘） */
  function queueFailedSend(
    sessionId: string,
    text: string,
    attachments?: ChatAttachment[],
    clientMsgId?: string,
  ) {
    outbox.value = addToOutbox(sessionId, options.userId(), { content: text, attachments, clientMsgId })
  }

  return {
    chatInputRef,
    onQuoteText,
    onWorkbenchReference,
    onMentionAdd,
    applyChatPrefill,
    outbox,
    outboxPreview,
    refreshOutbox,
    retrieveOutbox,
    retryOutbox,
    discardOutbox,
    queueFailedSend,
  }
}
