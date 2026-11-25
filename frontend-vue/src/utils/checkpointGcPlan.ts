/**
 * 破坏性预览的「不可变计划」绑定（PRD H73 / 9.10）。
 *
 * 背景：`CheckpointTimeline` 的回收流程是「先 dry-run 出计划 → 弹确认 → 再执行」。
 * 旧实现执行时**重新读取当前 UI 状态**（`props.projectId`）——确认框开着的时候用户
 * 切了项目，第二次请求就会打到**没预览过的**项目上。用户看到的是 A 的数字，删的是 B
 * 的缓存：这是那种「界面完全没有异样」的破坏性错误。
 *
 * 这里的做法：dry-run 的结果 + 目标 workspace + 请求代次打包成 `GcPlanBinding`，
 * 执行只认这个绑定：
 *   - `workspaceId` 是**预览时的**目标，执行时不再从 props 取；
 *   - `generation` 是预览时的项目代次，切项目即失效；
 *   - `digest` 是计划内容的摘要，随计划字段变化（客户端复核 + 作为 `plan_digest`
 *     一并下发，供网关做服务端一致性校验）。
 *
 * 摘要只用于「是不是同一份计划」的判等，不是密码学承诺 —— 目的是防界面状态漂移，
 * 不防恶意客户端（那由服务端权限与校验兜底）。
 */

import type { CheckpointGcResult } from '../api/checkpoints'

/** 一次 dry-run 得到的、执行阶段唯一可信的计划。 */
export interface GcPlanBinding {
  /** 预览时的目标 workspace，执行阶段**不再**读当前 props */
  workspaceId: string
  /** 预览时的项目代次；切换项目会让代次自增，确认随即失效 */
  generation: number
  /** 计划内容摘要（含 workspace，保证「换项目」必然换摘要） */
  digest: string
  /** dry-run 返回的完整计划，用于确认框展示与最终复核 */
  result: CheckpointGcResult
}

/** FNV-1a 32 位：同步、无依赖、对短文本足够分散。 */
function fnv1a(text: string): string {
  let hash = 0x811c9dc5
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return hash.toString(16).padStart(8, '0')
}

/**
 * 计划摘要：把「这次要删什么」的字段规范化后取哈希。
 *
 * 故意包含 `workspaceId`：同一份计划内容打到不同 workspace 上，摘要必须不同，
 * 否则「切项目」这种最危险的漂移恰好不会被摘要发现。
 */
/**
 * 取"这次确认框要绑定"的摘要（H73）。
 *
 * 优先用**服务端签发**的 `plan.plan_digest`：只有它才是网关认得的那份计划；
 * 本地 FNV 摘要只在老网关（不返回 digest）时作为**自检**回退 —— 那种情况下执行
 * 请求会被新网关以"缺 plan_digest"拒绝，属于预期的版本错配，而不是静默放过。
 */
export function resolveGcPlanDigest(plan: CheckpointGcResult, workspaceId: string): string {
  const serverDigest = typeof plan.plan_digest === 'string' ? plan.plan_digest.trim() : ''
  return serverDigest || computeGcPlanDigest(plan, workspaceId)
}

/** 这份计划是否带有服务端签发的摘要（决定执行阶段能否通过网关校验）。 */
export function hasServerSignedDigest(plan: CheckpointGcResult): boolean {
  return typeof plan.plan_digest === 'string' && plan.plan_digest.trim().length > 0
}

export function computeGcPlanDigest(plan: CheckpointGcResult, workspaceId: string): string {
  const canonical = JSON.stringify({
    v: 1,
    workspace: workspaceId,
    retention_days: plan.retention_days,
    scanned: plan.scanned,
    referenced: plan.referenced,
    expired: plan.expired,
    tree_paths: plan.tree_paths,
    deleted: plan.deleted,
    freed_bytes: plan.freed_bytes,
    planned: [...(plan.planned ?? [])],
    dangling_refs: [...(plan.dangling_refs ?? [])].sort(),
  })
  return fnv1a(canonical)
}

/** 绑定是否仍然指向「当前 UI 上的那个目标」。 */
export function isGcPlanCurrent(
  plan: Pick<GcPlanBinding, 'workspaceId' | 'generation'>,
  currentWorkspaceId: string,
  currentGeneration: number,
): boolean {
  if (!plan.workspaceId) return false
  if (plan.workspaceId !== currentWorkspaceId) return false
  return plan.generation === currentGeneration
}

/**
 * 执行前的完整复核：目标一致 + 摘要与计划内容自洽。
 *
 * 两道都要过 —— 目标比对拦「切项目」，摘要比对拦「计划对象被替换/改写」。
 */
export function verifyGcPlan(
  plan: GcPlanBinding,
  currentWorkspaceId: string,
  currentGeneration: number,
): { ok: true } | { ok: false; reason: 'target_changed' | 'plan_tampered' } {
  if (!isGcPlanCurrent(plan, currentWorkspaceId, currentGeneration)) {
    return { ok: false, reason: 'target_changed' }
  }
  if (computeGcPlanDigest(plan.result, plan.workspaceId) !== plan.digest) {
    return { ok: false, reason: 'plan_tampered' }
  }
  return { ok: true }
}
