import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ChatHealthPanel from '../ChatHealthPanel.vue'

/**
 * 组件健康/内存面板的契约：
 * - 每个组件一行，`up` 由 available/healthy/ping_ok 统一判定；
 * - **取不到的指标显示「—」，绝不显示 0** —— 0 会被读成"真的用了 0 字节"；
 * - 拉取失败（components 为 null）时不留空壳，显示占位。
 */
const mountWith = (components: unknown) =>
  mount(ChatHealthPanel, { props: { components: components as never } })

describe('ChatHealthPanel（组件健康与内存）', () => {
  it('全部组件健康时逐行展示，并给出各组件体积指标', () => {
    const w = mountWith({
      postgres: { available: true, ping_ok: true },
      redis: { available: true, ping_ok: true },
      engine: { healthy: true },
      gateway: { healthy: true, goroutines: 42, uptime_seconds: 3600 },
      memory: {
        postgres: { database_bytes: 1024 * 1024 * 5, connections: 3 },
        redis: { used_memory_bytes: 2048 },
        engine: { rss_bytes: 1024 * 1024 * 200 },
        gateway: { heap_bytes: 1024 * 1024 * 32, sys_bytes: 1024 * 1024 * 64 },
      },
    })
    const text = w.text()
    expect(text).toContain('postgres')
    expect(text).toContain('5.0 MB')
    expect(text).toContain('connections: 3')
    expect(text).toContain('2.0 KB')
    expect(text).toContain('200 MB')
    expect(text).toContain('goroutines: 42')
    // .down 只应出现在真正不可用的组件上
    expect(w.findAll('.chp-dot.down')).toHaveLength(0)
  })

  it('ping 失败即判为不可用，并展示错误原因', () => {
    const w = mountWith({
      postgres: { available: true, ping_ok: false, ping_error: 'database ping failed' },
      redis: { available: true, ping_ok: true },
      engine: { healthy: true },
      gateway: { healthy: true },
    })
    expect(w.findAll('.chp-dot.down')).toHaveLength(1)
    expect(w.text()).toContain('database ping failed')
  })

  it('缺字段视为不可用 —— 不能因为字段没下发就乐观显示绿点', () => {
    const w = mountWith({ postgres: {}, redis: {}, engine: {}, gateway: {} })
    // gateway 例外：能返回响应即证明网关在跑
    expect(w.findAll('.chp-dot.down')).toHaveLength(3)
  })

  it('取不到内存指标时显示「—」而不是 0', () => {
    const w = mountWith({
      postgres: { available: true, ping_ok: true },
      redis: { available: true, ping_ok: true },
      engine: { healthy: true },
      gateway: { healthy: true },
      // memory 整段缺失
    })
    const text = w.text()
    expect(text).toContain('DB: —')
    expect(text).toContain('used: —')
    expect(text).toContain('RSS: —')
    expect(text).not.toContain('0 B')
  })

  it('数据未取到时显示占位而非空列表', () => {
    const w = mountWith(null)
    expect(w.find('.chp-empty').exists()).toBe(true)
  })
})
