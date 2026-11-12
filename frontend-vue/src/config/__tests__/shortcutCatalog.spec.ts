import { describe, it, expect } from 'vitest'
import { SHORTCUT_CATALOG } from '../shortcutCatalog'

/**
 * `shortcutCatalog` 的守卫测试。
 *
 * 钉的是**写目录时最容易静默踩空**的两件事：
 *
 *  1. id 撞了 —— 后一个会覆盖前一个的持久化键名（`storageBase` 按 id 存），
 *     用户改其中一个，另一个跟着变。
 *  2. 非 Esc 的默认组合撞了 —— 两处都注册在 window 上，`preventDefault` 拦不住
 *     对方，按一次会同时触发两个动作。这不是假想：命令面板曾硬编码 Ctrl/⌘+K，
 *     而「打开会话列表」的默认键也是 `mod+k`，按一次同时弹出两个浮层。
 *
 * Esc 是**刻意允许重复**的：它承担「逐层关闭」（停生成 > 关浮层 > 关侧栏），
 * 重复由声明顺序表达优先级 —— 那是 shortcutCatalog.ts 的既定契约，别把它当 bug。
 */
describe('shortcutCatalog', () => {
  it('id 不重复', () => {
    const ids = SHORTCUT_CATALOG.map(s => s.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('非 Esc 的默认组合不重复', () => {
    const byKeys = new Map<string, string[]>()
    for (const spec of SHORTCUT_CATALOG) {
      const keys = spec.defaultKeys.toLowerCase()
      if (keys === 'escape') continue
      byKeys.set(keys, [...(byKeys.get(keys) ?? []), spec.id])
    }
    const conflicts = [...byKeys].filter(([, ids]) => ids.length > 1)
    // 失败时直接给出「哪几个 id 抢了同一个键」，而不是干巴巴的 false
    expect(conflicts).toEqual([])
  })

  it('每条都可从目录取到默认键与文案键', () => {
    for (const spec of SHORTCUT_CATALOG) {
      expect(spec.defaultKeys, `${spec.id} 缺 defaultKeys`).toBeTruthy()
      expect(spec.labelKey, `${spec.id} 缺 labelKey`).toBeTruthy()
    }
  })
})
