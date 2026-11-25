/**
 * H73：确认框必须绑定**服务端签发**的计划摘要（PRD 9.2）。
 *
 * 前端早已把目标 workspace/generation/摘要钉在确认框上，但服务端此前不校验 ——
 * 于是"确认的是 A、删的是 B"这类漂移在服务端完全不可见。现在网关 dry-run 会签发
 * `plan_digest`，执行时重算比对（不一致 409）。
 *
 * 前端因此必须**优先使用服务端摘要**：
 *   - 服务端有 ⇒ 用它（这是网关唯一认得的凭据）；
 *   - 服务端没有（老网关）⇒ 回退本地摘要，但执行请求会被新网关以"缺摘要"拒绝，
 *     属于预期的版本错配，不能静默放过。
 */
import { describe, it, expect } from 'vitest'
import {
  computeGcPlanDigest,
  hasServerSignedDigest,
  resolveGcPlanDigest,
} from '../checkpointGcPlan'
import type { CheckpointGcResult } from '../../api/checkpoints'

function plan(over: Partial<CheckpointGcResult> = {}): CheckpointGcResult {
  return {
    scanned: 3,
    referenced: 5,
    deleted: 2,
    freed_bytes: 1024,
    dry_run: true,
    expired: 1,
    orphan: 0,
    kept_young: 2,
    store_files: 4,
    store_bytes: 4096,
    refs: 9,
    commits: 7,
    tree_paths: 11,
    dangling_refs: ['d1'],
    planned: ['sha-1', 'sha-2'],
    retention_days: 30,
    stats: { sample_count: 0, avg_file_count: 0, avg_byte_size: 0, window_days: 30 },
    ...over,
  } as CheckpointGcResult
}

describe('resolveGcPlanDigest (H73)', () => {
  it('服务端签发摘要时优先用它', () => {
    const p = plan({ plan_digest: 'server-signed-abc' })
    expect(resolveGcPlanDigest(p, 'ws-1')).toBe('server-signed-abc')
    expect(hasServerSignedDigest(p)).toBe(true)
  })

  it('服务端未签发时回退本地摘要（并明确标记为未签名）', () => {
    const p = plan()
    const local = computeGcPlanDigest(p, 'ws-1')
    expect(resolveGcPlanDigest(p, 'ws-1')).toBe(local)
    expect(hasServerSignedDigest(p)).toBe(false)
  })

  it('服务端摘要为空串/空白时按未签发处理（不能把空白当凭据）', () => {
    for (const value of ['', '   ']) {
      const p = plan({ plan_digest: value })
      expect(hasServerSignedDigest(p)).toBe(false)
      expect(resolveGcPlanDigest(p, 'ws-1')).toBe(computeGcPlanDigest(p, 'ws-1'))
    }
  })

  it('本地摘要随 workspace 与计划内容变化（防止"切项目不被发现"）', () => {
    const a = computeGcPlanDigest(plan(), 'ws-a')
    const b = computeGcPlanDigest(plan(), 'ws-b')
    const c = computeGcPlanDigest(plan({ deleted: 9 }), 'ws-a')
    expect(a).not.toBe(b)
    expect(a).not.toBe(c)
  })
})
