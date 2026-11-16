/**
 * transcript 滚动单写者 —— 本项目唯一允许写消息列表 scrollTop 的地方。
 *
 * 借鉴 Reasonix `transcriptViewportWriter` 的契约，落到浏览器 Vue SPA：
 * 1. 单写者：分页 prepend、流式追加、折叠、跳转都只提交"意图"，由本模块写滚动位置。
 * 2. 输入租约：用户滚动期间持有租约，此期间程序写入被拒绝；否则"自动跟随底部"
 *    会抢走用户正在阅读的位置（本项目历史上"上滚后内容跳动"的根因之一）。
 * 3. writer provenance：程序写入登记 pending，直到匹配的原生 scroll 事件被消费；
 *    若落点不符则判定为用户滚动并立即进入租约 —— 这是区分"我写的"与"用户滚的"的唯一依据。
 * 4. 确定性：时间可注入，便于用事件序列做回归测试。
 */

export interface ViewportHost {
  scrollTop: number
  scrollHeight: number
  clientHeight: number
}

export interface TranscriptViewportOptions {
  /** 用户输入后租约时长（ms）。期间程序写入被拒绝。 */
  leaseMs?: number
  /** 贴底判定阈值（px）。 */
  bottomThreshold?: number
  /** 可注入时钟（测试用）。 */
  now?: () => number
}

export interface TranscriptViewport {
  /** 用户输入开始（wheel / touchstart / keydown / 拖动滚动条）→ 立即取得租约。 */
  noteUserInput(): void
  /** 原生 scroll 事件入口（滚动容器必须转发到这里）。 */
  handleScroll(el: ViewportHost): void
  /** 请求跟随底部；被输入租约拒绝或已贴底时返回 false（后者不产生冗余写入）。 */
  follow(el: ViewportHost): boolean
  /** 请求恢复到指定偏移（分页 prepend 后保持视口锚点）。 */
  restore(el: ViewportHost, offset: number): void
  /** 滚动到顶部（触顶加载更早消息时）。 */
  toTop(el: ViewportHost): void
  /** 结构性几何变化（折叠/展开）后保持锚点：写入的落点可能被浏览器钳制（总高变小），
   * 因此按**实际落点**登记 provenance —— 否则下一个原生 scroll 事件会被误判成用户
   * 滚动，折叠后 1.2s 内自动跟随会被无谓阻断。 */
  afterStructuralChange(el: ViewportHost, offset: number): void
  /** 当前是否处于用户输入租约中。 */
  isUserOwned(): boolean
  /** 是否已贴底（供"回到底部"按钮/未读徽标）。 */
  isAtBottom(el: ViewportHost): boolean
  /** 主动释放租约（重新连接、切会话等场景）。 */
  release(): void
}

export function createTranscriptViewport(
  opts: TranscriptViewportOptions = {},
): TranscriptViewport {
  const leaseMs = opts.leaseMs ?? 1200
  const bottomThreshold = opts.bottomThreshold ?? 4
  const now = opts.now ?? (() => Date.now())

  /** 已提交但尚未被原生 scroll 事件确认的程序写入值。 */
  let pendingOffset: number | null = null
  let leaseUntil = 0

  function isUserOwned(): boolean {
    return now() < leaseUntil
  }

  function isAtBottom(el: ViewportHost): boolean {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= bottomThreshold
  }

  /**
   * 真实可达的底部偏移。必须用 scrollHeight - clientHeight 而非 scrollHeight：
   * 浏览器会把 scrollTop 钳制到该值，若登记 scrollHeight，pending 与原生落点永远不符，
   * 每次程序跟随都会被误判成用户滚动（参照项目："native geometry is authoritative"）。
   */
  function bottomOffset(el: ViewportHost): number {
    return Math.max(0, el.scrollHeight - el.clientHeight)
  }

  /** 写入并登记 pending；返回是否真正发生了物理写入。 */
  function write(el: ViewportHost, offset: number): boolean {
    if (Math.abs(el.scrollTop - offset) <= 1) {
      // 目标位置已到位：按契约不得重复赋值，也不登记 pending
      pendingOffset = null
      return false
    }
    el.scrollTop = offset
    pendingOffset = offset
    return true
  }

  return {
    noteUserInput() {
      leaseUntil = now() + leaseMs
    },

    handleScroll(el: ViewportHost) {
      if (pendingOffset !== null) {
        const expected = pendingOffset
        pendingOffset = null
        // 落点与程序写入一致 → 本次滚动由程序引起，不夺取租约
        if (Math.abs(el.scrollTop - expected) <= 1) return
      }
      // 无法归因于程序写入 → 用户滚动，刷新租约
      leaseUntil = now() + leaseMs
    },

    follow(el: ViewportHost): boolean {
      if (isUserOwned()) return false
      return write(el, bottomOffset(el))
    },

    restore(el: ViewportHost, offset: number) {
      // 分页/切会话的锚点恢复优先于租约：它不是"抢用户位置"，而是保持用户当前视口
      write(el, offset)
    },

    toTop(el: ViewportHost) {
      write(el, 0)
    },

    afterStructuralChange(el: ViewportHost, offset: number) {
      // 登记钳制后的真实落点：折叠使 scrollHeight 变小时浏览器会改写 scrollTop
      if (write(el, offset)) pendingOffset = el.scrollTop
    },

    isUserOwned,
    isAtBottom,

    release() {
      leaseUntil = 0
      pendingOffset = null
    },
  }
}
