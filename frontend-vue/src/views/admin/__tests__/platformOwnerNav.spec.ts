/**
 * H83：平台 Owner 专属后台入口必须按 capability 收敛（PRD 9.10）。
 *
 * 缺陷形态：路由与侧边栏都不区分「平台 owner」与「租户 admin」，但后端那几页用的是
 * `RequirePlatformOwner`（`internal/api/routes_admin.go` 的 platformReadMW/platformWriteMW）。
 * 于是租户 admin 在菜单里**看得见**「运行时监控 / 数据存储 / 自诊断 / 依赖管理」，
 * 点进去却只有一片 403 —— 用户看到的是"功能坏了"，而不是"这不是给你的"。
 *
 * 修法是把三处口径对齐：
 *   1. 路由 meta `requiresPlatformOwner`（页面意图）
 *   2. 守卫按 `auth.isPlatformOwner` 判定（导航决策）
 *   3. 侧边栏按同一标记过滤（呈现）
 *
 * 这组用例钉住"三处同时存在且同口径"，因为只改一处都会退化成"看得见点不动"或
 * "看不见但能直接输 URL 进"。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const ROOT = resolve(__dirname, '../../../..')
const routerSrc = readFileSync(resolve(ROOT, 'src/router/index.ts'), 'utf8')
const guardSrc = readFileSync(resolve(ROOT, 'src/router/guard.ts'), 'utf8')
const layoutSrc = readFileSync(resolve(ROOT, 'src/views/admin/Layout.vue'), 'utf8')

/** 后端标注为平台级的页面（与 routes_admin.go 的 platform*MW 对应）。 */
const PLATFORM_PAGES = ['monitor', 'datastores', 'diagnostics', 'deps']

describe('H83 路由标记', () => {
  it('平台级页面都带 requiresPlatformOwner', () => {
    for (const page of PLATFORM_PAGES) {
      const routeStart = routerSrc.indexOf(`path: '${page}'`)
      expect(routeStart, `找不到路由 ${page}`).toBeGreaterThan(-1)
      const rest = routerSrc.slice(routeStart + 1)
      const nextRoute = rest.indexOf("path: '")
      const block = nextRoute > 0 ? rest.slice(0, nextRoute) : rest
      expect(block, `${page} 缺少 requiresPlatformOwner`).toContain('requiresPlatformOwner: true')
    }
  })

  it('普通 admin 页面不带该标记（避免过度收敛）', () => {
    for (const page of ['users', 'personas', 'providers', 'shares', 'audit', 'settings', 'market']) {
      const routeStart = routerSrc.indexOf(`path: '${page}'`)
      if (routeStart === -1) continue
      // 窗口必须止于**本路由**：按字符数切会跨到下一条 route，把别人的标记算进来。
      const rest = routerSrc.slice(routeStart + 1)
      const nextRoute = rest.indexOf("path: '")
      const block = nextRoute > 0 ? rest.slice(0, nextRoute) : rest
      expect(block, `${page} 不该是平台专属`).not.toContain('requiresPlatformOwner')
    }
  })
})

describe('H83 守卫', () => {
  it('守卫读取 requiresPlatformOwner 并用 isPlatformOwner 判定', () => {
    expect(guardSrc).toContain('to.meta.requiresPlatformOwner')
    expect(guardSrc).toContain('auth.isPlatformOwner')
  })

  it('判定发生在 requiresAdmin 判定之后（先弹非管理员，再谈平台专属）', () => {
    const adminAt = guardSrc.indexOf('if (to.meta.requiresAdmin)')
    const ownerAt = guardSrc.indexOf('to.meta.requiresPlatformOwner')
    expect(adminAt).toBeGreaterThan(-1)
    expect(ownerAt).toBeGreaterThan(adminAt)
  })

  it('非 owner 一律回到首页（不留在 403 页面）', () => {
    const block = guardSrc.slice(guardSrc.indexOf('to.meta.requiresPlatformOwner'))
    expect(block.slice(0, 600)).toContain("next('/')")
  })
})

describe('H83 侧边栏', () => {
  it('平台级菜单项标记为 platformOwnerOnly', () => {
    for (const path of ['/admin/monitor', '/admin/datastores', '/admin/diagnostics', '/admin/deps']) {
      const idx = layoutSrc.indexOf(`key: '${path}'`)
      expect(idx, `找不到菜单项 ${path}`).toBeGreaterThan(-1)
      expect(layoutSrc.slice(idx, idx + 120)).toContain('platformOwnerOnly: true')
    }
  })

  it('渲染用的是过滤后的分组，而不是原始分组', () => {
    expect(layoutSrc).toContain('visibleMenuGroups')
    // 模板里不该再直接遍历未过滤的 menuGroups
    const templateLoops = layoutSrc.match(/v-for="g in menuGroups"/g) ?? []
    expect(templateLoops).toHaveLength(0)
  })
})

// ── 行为层：过滤函数本身 ────────────────────────────────────────────────────
describe('H83 菜单过滤行为', () => {
  // 与 Layout.vue 内联的过滤逻辑同构（保持这里可独立验证"非 owner 看不到"）。
  const filterGroups = (groups: Array<{ key: string, children: Array<{ key: string, platformOwnerOnly?: boolean }> }>, isOwner: boolean) =>
    groups
      .map(group => ({
        ...group,
        children: group.children.filter(item => !item.platformOwnerOnly || isOwner),
      }))
      .filter(group => group.children.length > 0)

  const groups = [
    {
      key: 'g-monitor',
      children: [{ key: '/admin/monitor', platformOwnerOnly: true }],
    },
    {
      key: 'g-access',
      children: [{ key: '/admin/users' }, { key: '/admin/audit' }],
    },
  ]

  it('平台 owner 看到全部', () => {
    const visible = filterGroups(groups, true)
    expect(visible.flatMap(g => g.children.map(c => c.key))).toEqual([
      '/admin/monitor',
      '/admin/users',
      '/admin/audit',
    ])
  })

  it('租户 admin 看不到平台专属项，但仍能看到租户级菜单', () => {
    const visible = filterGroups(groups, false)
    expect(visible.flatMap(g => g.children.map(c => c.key))).toEqual(['/admin/users', '/admin/audit'])
  })

  it('整组都被过滤掉时不留下空分组', () => {
    const visible = filterGroups(groups, false)
    expect(visible.map(g => g.key)).not.toContain('g-monitor')
  })
})
