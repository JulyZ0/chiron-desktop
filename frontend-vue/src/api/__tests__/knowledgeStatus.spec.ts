import { describe, it, expect } from 'vitest'
import { isKbBuilding, isKbTerminal, KB_BUILDING_STATUSES, KB_TERMINAL_STATUSES } from '../knowledge'

/**
 * 索引状态判据 —— 锁住**"构建完成/正在构建"这两个判断的取值来源**。
 *
 * 回归背景（真实故障，两处同源）：`KnowledgeDetailView.vue` 曾用
 * `status === 'active'` 判断"构建完成"、用 `status !== 'building'` 决定是否显示
 * 「构建索引」按钮。而 worker（`pageindex-service/app/index_worker.py`）只写
 * `indexing` / `ready` / `error` —— `active` 与 `building` **从未被任何写入方写过**。
 *
 * 后果不是崩溃，而是更糟的静默：
 * - 索引真跑完、库早已 `ready`，界面还在一轮轮轮询，十轮后默默放弃（用户以为没成功）；
 * - "构建中"的按钮分支永远不生效 —— 用户能在构建过程中反复点「构建索引」，
 *   每次都往队列再投一个任务，**重复烧 LLM 费用**。
 *
 * 所以这里不只测函数，也把取值集合本身钉住：哪天引擎侧 `status.py` 增删取值，
 * 这条断言会先红，而不是让界面再次"看起来好了"。
 */
describe('知识库索引状态判据（镜像 python-engine/app/knowledge/status.py）', () => {
  it('取值集合与引擎侧逐项一致', () => {
    // 引擎侧 status.py：BUILDING_STATUSES = {indexing}
    //                    TERMINAL_STATUSES = {ready, error}
    expect([...KB_BUILDING_STATUSES]).toEqual(['indexing'])
    expect([...KB_TERMINAL_STATUSES]).toEqual(['ready', 'error'])
  })

  it('把 worker 真实写入的 indexing 认作"正在构建"', () => {
    expect(isKbBuilding('indexing')).toBe(true)
  })

  it('不再把从未存在的 building / active 当作合法状态', () => {
    // 这两个正是旧实现里写死的字面量；它们必须是"不是构建中"
    expect(isKbBuilding('building')).toBe(false)
    expect(isKbBuilding('active')).toBe(false)
  })

  it('ready 与 error 都算终态（error 继续轮询只会让用户干等）', () => {
    expect(isKbTerminal('ready')).toBe(true)
    expect(isKbTerminal('error')).toBe(true)
  })

  it('indexing / disabled 不是终态', () => {
    expect(isKbTerminal('indexing')).toBe(false)
    expect(isKbTerminal('disabled')).toBe(false)
  })

  it('空值与非状态字符串一律按"未构建、未终态"处理', () => {
    for (const v of [undefined, null, '', 'weird-status']) {
      expect(isKbBuilding(v)).toBe(false)
      expect(isKbTerminal(v)).toBe(false)
    }
  })
})
