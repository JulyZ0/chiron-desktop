import { describe, expect, it } from 'vitest'
import {
  LAYOUT_BREAKPOINTS,
  PANEL_WIDTHS,
  narrowSidebarQuery,
  resolveLayout,
  sidePanelWidth,
  splitPaneMaxWidth,
  workbenchWideQuery,
} from '../layoutBreakpoints'

/**
 * G18：布局解算纯函数化。
 *
 * 修前会怎样：断点散在四处字面量里 —— `usePanelCollapse.ts` 的 `(max-width: 1024px)`、
 * `useWorkbench.ts` 的 `(min-width: 1280px)`、`ChatSidePanel.vue` 的 `matchMedia(1024)` 与
 * 样式里的 768/576、`HomeScene3D.vue` 的 `innerWidth <= 768`。没有一处能单测：判定要
 * `window.matchMedia`，样式要浏览器，于是"1024 到底含不含边界"只能靠读代码猜，
 * 两处写法一旦跑偏（`<=1024` vs `<1024`）界面就在边界那 1px 上闪烁。
 *
 * 边界取舍（本文件即契约）：**上界含边界**——`<=576` 手机、`<=768` 紧凑、`<=1024` 抽屉；
 * **下界也含边界**——`>=1280` 工作台默认展开。也就是 1024 视为"窄"、1280 视为"宽"，
 * 与既有 `matchMedia('(max-width: 1024px)')` / `(min-width: 1280px)` 逐字一致。
 */

/** 六个基准宽度：PRD 指定的断言集（含 1024 / 768 两个边界） */
const WIDTHS = [375, 576, 768, 1024, 1280, 1920] as const

describe('LAYOUT_BREAKPOINTS —— 断点常量只有这一份', () => {
  it('四个断点值与散落字面量逐一对齐（改这里就是改全局）', () => {
    expect(LAYOUT_BREAKPOINTS.mobile).toBe(576)
    expect(LAYOUT_BREAKPOINTS.compact).toBe(768)
    expect(LAYOUT_BREAKPOINTS.sidebarDrawer).toBe(1024)
    expect(LAYOUT_BREAKPOINTS.workbenchWide).toBe(1280)
  })

  it('media query 字符串由常量拼出，不再各处手写', () => {
    expect(narrowSidebarQuery()).toBe('(max-width: 1024px)')
    expect(workbenchWideQuery()).toBe('(min-width: 1280px)')
  })
})

describe('resolveLayout —— 六个基准宽度', () => {
  it('375（手机）：抽屉 + 收起侧栏 + 工作台不默认展开', () => {
    const layout = resolveLayout(375)
    expect(layout.phone).toBe(true)
    expect(layout.compact).toBe(true)
    expect(layout.drawer).toBe(true)
    expect(layout.collapseSidebar).toBe(true)
    expect(layout.workbenchDefaultOpen).toBe(false)
  })

  it('576（手机上限，含边界）：仍是手机布局', () => {
    const layout = resolveLayout(576)
    expect(layout.phone).toBe(true)
    expect(layout.compact).toBe(true)
    expect(layout.workbenchDefaultOpen).toBe(false)
  })

  it('768（平板边界，含边界）：紧凑仍是 true，手机转 false', () => {
    const layout = resolveLayout(768)
    // 修前 HomeScene3D 用 `innerWidth <= 768`、样式用 `@media (max-width: 768px)`，
    // 两者都含边界；这里把"含"写死，避免以后有人改成 `<`。
    expect(layout.compact).toBe(true)
    expect(layout.phone).toBe(false)
    expect(layout.drawer).toBe(true)
  })

  it('1024（抽屉边界，含边界）：侧面板仍按抽屉处理', () => {
    const layout = resolveLayout(1024)
    // 修前 `usePanelCollapse` 与 ChatSidePanel 各自写 `max-width: 1024px`，
    // 边界取舍只能靠"两处恰好一样"来保证 —— 现在只有一处判据。
    expect(layout.drawer).toBe(true)
    expect(layout.collapseSidebar).toBe(true)
    expect(layout.compact).toBe(false)
    expect(layout.workbenchDefaultOpen).toBe(false)
  })

  it('1280（工作台边界，含边界）：工作台默认展开', () => {
    const layout = resolveLayout(1280)
    // 修前 useWorkbench 用 `(min-width: 1280px)` —— 恰好 1280 也要展开，
    // 与 1024 的"抽屉含边界"是同一条'上界含边界'约定的两端。
    expect(layout.workbenchDefaultOpen).toBe(true)
    expect(layout.drawer).toBe(false)
    expect(layout.collapseSidebar).toBe(false)
  })

  it('1920（宽屏）：全部宽松分支', () => {
    const layout = resolveLayout(1920)
    expect(layout).toMatchObject({
      phone: false,
      compact: false,
      drawer: false,
      collapseSidebar: false,
      workbenchDefaultOpen: true,
    })
  })
})

describe('resolveLayout —— 边界外侧与非法输入', () => {
  it('边界外侧各退一格：1025 不抽屉、1279 不展开、769 不紧凑、577 非手机', () => {
    expect(resolveLayout(1025).drawer).toBe(false)
    expect(resolveLayout(1025).collapseSidebar).toBe(false)
    expect(resolveLayout(1279).workbenchDefaultOpen).toBe(false)
    expect(resolveLayout(769).compact).toBe(false)
    expect(resolveLayout(577).phone).toBe(false)
  })

  it('六个基准宽度上，drawer 与 collapseSidebar 永不背离', () => {
    // 两个名字背后是同一条判据（"<=1024 侧面板让位给正文"）。分开命名是为了贴调用点的
    // 语言，但一旦有人只改一边，侧栏就会"收起了却还是抽屉形态"。
    for (const width of WIDTHS) {
      const layout = resolveLayout(width)
      expect(layout.collapseSidebar, `宽度 ${width}`).toBe(layout.drawer)
    }
  })

  it('非法宽度（NaN / 负数 / Infinity）按 0 处理而不是崩', () => {
    // `window.innerWidth` 在 jsdom、SSR、老 webview 里可能是 undefined/0；
    // 解算函数是纯函数，不能因为这些输入抛错。
    for (const bad of [Number.NaN, -1, Number.POSITIVE_INFINITY]) {
      const layout = resolveLayout(bad)
      expect(Number.isFinite(layout.width)).toBe(true)
      expect(layout.width).toBe(0)
      expect(layout.phone).toBe(true)
      expect(layout.drawer).toBe(true)
      expect(layout.workbenchDefaultOpen).toBe(false)
    }
  })

  it('小数宽度按原值判定（不四舍五入到整数断点）', () => {
    expect(resolveLayout(1024.5).drawer).toBe(false)
    expect(resolveLayout(1023.5).drawer).toBe(true)
  })
})

describe('宽度常量（分屏 / 侧面板）', () => {
  it('分屏栏上限是容器宽度的 60%，但不小于最小可读宽度', () => {
    expect(PANEL_WIDTHS.splitPaneMin).toBe(260)
    expect(splitPaneMaxWidth(1920)).toBe(1152)
    expect(splitPaneMaxWidth(1024)).toBe(614)
    // 容器极窄时守住最小宽度：否则拖到 300px 窗口就把分屏压成 0 宽
    expect(splitPaneMaxWidth(300)).toBe(PANEL_WIDTHS.splitPaneMin)
  })

  it('侧面板宽度是 420 与容器宽度的较小者（窄屏铺满）', () => {
    expect(PANEL_WIDTHS.sidePanelMax).toBe(420)
    expect(sidePanelWidth(1920)).toBe(420)
    expect(sidePanelWidth(375)).toBe(375)
  })
})
