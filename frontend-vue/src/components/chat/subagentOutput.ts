/**
 * 子 Agent 的「输出」投影：把 `subagent.*` 事件流变成**可渲染的行**。
 *
 * ## 为什么要有这一层
 *
 * 引擎现在发的工具事件与主 Agent **同形**（`subagent.tool_call` /
 * `subagent.tool_result`，字段 `tool_call_id` / `tool_name` / `tool_arguments` /
 * `content` / `error`）。但事件流本身是**扁平**的：一次调用一条、一次结果一条，
 * 靠 `tool_call_id` 才对得上。模板里直接 v-for 事件，只能渲染成"调用了 X"和
 * "结果 Y"两行互不相干的东西 —— 读不出"这条结果是那次调用的回音"。
 *
 * 因此在这里配对成「调用卡 + 结果块」一行（与主对话流的 `ToolCallItem` +
 * `ToolResultItem` 完全同构），模板就能复用 `ToolCallCard` / `ToolResultBlock`。
 *
 * 纯函数、无 Vue 依赖：配对规则容易被后续改动碰坏（比如有人给结果也发 tool_name），
 * 放在能被单测直接钉住的地方比藏在 computed 里安全。
 */
import type { SubagentEvent } from '../../api/subagent'
import type { ToolCallItem, ToolResultItem, ToolStatus } from './chat-types'

/** 一行输出。`key` 稳定（用于 v-for），来源是事件 id 或位置兜底。 */
export type SubagentOutputRow =
  | { kind: 'reasoning'; key: string; content: string }
  | { kind: 'text'; key: string; content: string }
  | { kind: 'notice'; key: string; content: string }
  /**
   * 工具调用（可能带结果）。
   *
   * `call` 可缺失：**结果先于调用到达**时（Redis Stream 裁剪、或回放只留了后半截），
   * 仍要渲染结果块 —— 丢掉它等于把"它拿到了什么"整段吞掉，比缺个工具名糟得多。
   */
  | { kind: 'tool'; key: string; call?: ToolCallItem; result?: ToolResultItem }
  | { kind: 'approval'; key: string; event: SubagentEvent }
  | { kind: 'ask'; key: string; event: SubagentEvent }
  | { kind: 'status'; key: string; event: SubagentEvent }
  | { kind: 'done'; key: string; event: SubagentEvent }

/**
 * 工具结果正文里**指向某个子 Agent 运行**的 run_id（没有就返回空串）。
 *
 * ## 为什么从正文里认，而不是看工具名
 *
 * `ToolResultItem` 不带工具名（见 chat-types.ts）：`tool_call` / `tool_result`
 * 在投影层就是靠 `tool_call_id` 配对的，名字只留在调用卡上，结果块那头没有。
 * 把工具名补进结果块要改投影层与后端历史形状，而这里真正需要的只是**一个 id** ——
 * 引擎的工具返回体本来就把 `result_ref` / `run_id` 放在顶层（见
 * python-engine/app/agent/subagent_runner.py 的 `to_tool_payload` 与
 * app/tools/subagent.py 的后台/同步两个返回体）。
 *
 * ## 只在"恰好一个 run"时给入口
 *
 * `subagent_list` 之类的返回体里可能带**一列** run（`runs: [...]` 或 `steps` 里
 * 各自带 run_id）。那种情况下"点开看哪一个"没有正确答案，点了也只会把用户带到
 * 一个随机的 run 上 —— 所以只认顶层单值的 `result_ref` / `run_id`，其余一律不给入口。
 */
export function subagentRunIdOf(content: string): string {
  if (!content) return ''
  const text = content.trimStart()
  if (text[0] !== '{') return ''
  let obj: unknown = null
  try { obj = JSON.parse(text) } catch {
    // 解析失败最常见的原因不是"形状不对"，而是**预览被截断**：列表接口只带前
    // 4096 字符（internal/session 的 outputPreviewBytes），而子 Agent 的 `output`
    // 经常比这长。此时仍要给入口 —— 用户看的就是这条历史，入口不该因为
    // "摘要长了点"而消失。
    //
    // 兜底判据刻意保守：`result_ref` 在整段文本里**只能出现一次**，且取得到的值
    // 非空。`output` 若是一段 JSON dump（哪怕被转义成 `\"result_ref\"`），它里面
    // 就嵌着别的 run 的 id —— 出现第二处就说明"点开看哪个"又变成猜，宁可不给入口。
    if ((text.match(/result_ref/g) || []).length !== 1) return ''
    const m = /"result_ref"\s*:\s*"([^"\\]{1,64})"/.exec(text)
    return m ? m[1].trim() : ''
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return ''
  const rec = obj as Record<string, unknown>
  // `result_ref` 在前：两种返回体都带它，而后台那次连 `run_id` 都同时给了同一个值。
  for (const field of ['result_ref', 'run_id']) {
    const v = rec[field]
    if (typeof v === 'string' && v.trim()) return v.trim()
  }
  return ''
}

/** 已有结果时的调用状态：失败标红，成功收成 done（与主对话流同一套取值）。 */
function statusOf(result: ToolResultItem | undefined): ToolStatus {
  if (!result) return 'running'
  return result.isError ? 'error' : 'done'
}

/**
 * 事件流 → 行序列。
 *
 * 顺序即到达顺序：调用卡出现的位置就是它被发起的那一刻，结果块紧跟在它下面
 * （不是追加到末尾）—— 否则多个工具交错时，"结果"与"调用"会被别的输出隔开。
 */
export function projectSubagentOutput(events: readonly SubagentEvent[]): SubagentOutputRow[] {
  const rows: SubagentOutputRow[] = []
  /** tool_call_id → 该调用对应的行下标 */
  const callRow = new Map<string, number>()

  for (let i = 0; i < events.length; i++) {
    const e = events[i]
    if (!e) continue
    const key = e.id || `${e.type}:${i}`

    switch (e.type) {
      case 'subagent.reasoning':
        if (e.content) rows.push({ kind: 'reasoning', key, content: e.content })
        break
      case 'subagent.text':
        if (e.content) rows.push({ kind: 'text', key, content: e.content })
        break
      case 'subagent.notice':
        if (e.content) rows.push({ kind: 'notice', key, content: e.content })
        break

      case 'subagent.tool_call': {
        const id = e.tool_call_id || ''
        // 同一个 tool_call_id 重复到达（重连补发）时只留第一条：
        // 再插一张一模一样的卡，读起来像"它调了两次同一个工具"。
        if (id && callRow.has(id)) break
        const call: ToolCallItem = {
          kind: 'tool_call',
          id: id || key,
          name: e.tool_name || '',
          arguments: e.tool_arguments || '',
          status: 'running',
        }
        if (id) callRow.set(id, rows.length)
        rows.push({ kind: 'tool', key, call })
        break
      }

      case 'subagent.tool_result': {
        const id = e.tool_call_id || ''
        const result: ToolResultItem = {
          kind: 'tool_result',
          id: key,
          toolCallId: id,
          content: e.content || '',
          // 只有 `error` 字段存在才算失败：有些工具的**成功**返回体里本身就带
          // `error` 键（比如批量操作的部分失败汇总），按内容猜会把成功标红。
          isError: !!e.error,
        }
        const at = id ? callRow.get(id) : undefined
        const target = at === undefined ? undefined : rows[at]
        if (target && target.kind === 'tool') {
          if (target.result) break // 同一次调用只挂一条结果
          target.result = result
          if (target.call) target.call.status = statusOf(result)
        } else {
          // 没有对应调用：仍然渲染结果（见类型注释），调用侧留空。
          rows.push({ kind: 'tool', key, result })
        }
        break
      }

      case 'subagent.approval':
        rows.push({ kind: 'approval', key, event: e })
        break
      case 'subagent.ask':
        rows.push({ kind: 'ask', key, event: e })
        break
      case 'subagent.status':
        rows.push({ kind: 'status', key, event: e })
        break
      case 'subagent.done':
        rows.push({ kind: 'done', key, event: e })
        break
      default:
        // 未知类型保持沉默：这是**给人读**的过程视图，不是原始事件流
        //（排查原始事件由「事件流」tab 负责，那里不丢任何一条）。
        break
    }
  }
  return rows
}
