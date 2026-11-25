/** 异步写回的代次守卫（PRD 9.2 / H60）。
 *
 * 缺陷形态（散落多处）：组件在 `await` 之后直接 `x.value = result`。只要这次 await
 * 期间用户切了会话 / 换了文件 / 重发了请求，旧响应就会**盖掉新视图** ——
 * 界面显示的是"上一次"的内容，而 `finally` 里的 `loading = false` 还会把新请求的
 * 加载态一起清掉（表现为"点了一下没反应"）。
 *
 * 做法：一个纯同步的计数器。请求前取票、`await` 后验票；票过期就**一个字都不写**。
 * 故意不依赖 Vue 响应式（它只是"第几代"的判等），便于单测与在任意上下文里用。
 */

export interface GenerationGuard {
  /** 作废当前所有在途请求，返回新的代次号（切换视图/会话时调用）。 */
  bump: () => number
  /** 当前代次号：请求发出前取一次。 */
  current: () => number
  /** 这次 await 回来之后，票是否仍然有效。 */
  isCurrent: (ticket: number) => boolean
}

export function createGenerationGuard(): GenerationGuard {
  let generation = 0
  return {
    bump() {
      generation += 1
      return generation
    },
    current() {
      return generation
    },
    isCurrent(ticket: number) {
      return ticket === generation
    },
  }
}

/**
 * 复合键守卫：会话/文件/运行任一变化都让在途请求失效。
 *
 * 为什么单独要它：只比"代次"时，切换 props（`sessionId` 从 A 变 B）不会自动加代，
 * 得靠调用方记得 `bump()`。这里把"键"也纳进来，即使忘了 bump 也不会写错视图 ——
 * 键不同 ⇒ 票必然无效。
 */
export interface KeyedGuard extends GenerationGuard {
  /** 取票：带上当前的键；键变了票就自动失效。 */
  ticket: (key: string) => { generation: number; key: string }
  /** 验票（同时校验键与代次）。 */
  isValid: (t: { generation: number; key: string } | null | undefined) => boolean
}

export function createKeyedGuard(readKey: () => string): KeyedGuard {
  const base = createGenerationGuard()
  return {
    ...base,
    ticket(key: string) {
      return { generation: base.current(), key: String(key ?? '') }
    },
    isValid(t) {
      if (!t) return false
      if (t.key !== String(readKey() ?? '')) return false
      return base.isCurrent(t.generation)
    },
  }
}
