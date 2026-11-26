/**
 * 流式"水墨浮现"的纯计算（从 `MessageItem.vue` 抽出，便于单测）。
 *
 * ## 只给**本批新增**字符建 span
 *
 * 修前是"整条正文逐字拆 span"：2000 字封顶、每次 delta 都要重建整条字符数组，
 * 已显示的字还会因为 `--i` 变化而参与动画（观感上是整段重播）。现在把正文切成
 * `prefix`（已提交的纯文本）+ `tail`（本批新增字符），只有 tail 建 span：
 *
 *  - DOM 规模只与本批增量有关，与消息总长无关；因此**取消 2000 字总量上限**；
 *  - 已提交部分是一个文本节点，不会重播动画；
 *  - 复制/查找/读屏拿到的仍是完整正文（prefix 是真实文本，不是占位）。
 *
 * ## 一个上限仍然要留
 *
 * 单次 delta 本身可能是几万字（模型一次吐完 / 断流重放）。此时逐字 span 反而制造
 * 大量节点与动画，因此给 tail 一个上限，超过就退回整段纯文本（不做动画）。
 */

/** 本批内错开档位上限：一次返回一大段时，最后一个字最多等 12 档。 */
export const INK_MAX_STAGGER = 12

/** 单批最多逐字拆多少字符；超过退回纯文本（避免一次 5 万字建 5 万 span）。 */
export const INK_MAX_TAIL_CHARS = 2000

export interface RevealSlice {
  /** 已提交正文（纯文本节点）。 */
  prefix: string
  /** 本批新增字符；超过 `INK_MAX_TAIL_CHARS` 时为空，调用方退回纯文本。 */
  tail: string[]
  /** 本批字符数是否超过逐字上限。 */
  overflow: boolean
}

/**
 * 把正文切成"已提交前缀 + 本批新增字符"。
 *
 * `baseChars` 是**字符数**（不是 UTF-16 code unit）—— emoji / 扩展区汉字必须按码点算，
 * 否则 `slice` 会把代理对劈开，tail 里出现半个字符。
 */
export function revealSlice(text: string, baseChars: number): RevealSlice {
  const chars = Array.from(text || '')
  const base = Math.min(Math.max(Math.trunc(baseChars) || 0, 0), chars.length)
  const tailChars = chars.slice(base)
  const overflow = tailChars.length > INK_MAX_TAIL_CHARS
  return {
    prefix: chars.slice(0, base).join(''),
    tail: overflow ? [] : tailChars,
    overflow,
  }
}

/** 本批第 i 个字符的错开档位：0..INK_MAX_STAGGER。 */
export function inkIndexFor(i: number): number {
  return Math.min(Math.max(i, 0), INK_MAX_STAGGER)
}

/**
 * 根据上一次/这次的正文长度算出新的前缀长度。
 *
 * 只在"纯追加"时把上一次的长度当已提交前缀；正文被替换/缩短（重试、编辑）时归零，
 * 让新正文重新浮现 —— 否则会把旧前缀留在屏幕上，与新正文拼成一句不存在的话。
 */
export function nextBaseChars(before: string, now: string): number {
  const prev = Array.from(before || '').length
  const next = Array.from(now || '').length
  return next >= prev ? prev : 0
}
