/**
 * 布局断点与宽度常量的**唯一**事实源（G18）。
 *
 * 为什么是纯函数：断点原来散在四处字面量里 —— `usePanelCollapse.ts` 的
 * `(max-width: 1024px)`、`useWorkbench.ts` 的 `(min-width: 1280px)`、`ChatSidePanel.vue`
 * 的 `matchMedia(1024)` 与样式里的 768/576、`HomeScene3D.vue` 的 `innerWidth <= 768`。
 * 这些判定都要 `window` 才能跑，于是"1024 到底含不含边界"这件事**没有一处能单测**，
 * 只能靠读代码确认两处写法恰好一样；一旦一处是 `<=` 另一处是 `<`，界面就会在边界
 * 那 1px 上抖（侧栏一会儿收起一会儿展开）。
 *
 * 这里只做"给定宽度 → 布局判定"，不碰 `window`、不碰 Vue 响应式：
 * 调用方拿到宽度后自己 `matchMedia` / `resize` 订阅，判定逻辑在这里一次定死，可单测。
 *
 * ## 边界约定（改之前先读）
 *
 * - **上界含边界**：`<=576` 手机、`<=768` 紧凑、`<=1024` 抽屉/收起侧栏；
 * - **下界也含边界**：`>=1280` 工作台默认展开。
 *
 * 这与既有 `matchMedia('(max-width: 1024px)')`、`matchMedia('(min-width: 1280px)')`
 * 逐字一致 —— 迁移常量**不改变**任何既有行为，只是把判据收敛到一处。
 *
 * ## TODO（G18 未覆盖到的调用点）
 *
 * 下列文件仍在使用同样的字面量，但它们的归属在别的批次（`ChatView.vue` / `chat/**`
 * 正在被其他 agent 改），本轮不动。等它们空出来时把字面量换成这里的常量：
 *
 * - `src/views/ChatView.vue`：`narrowQuery: '(max-width: 1024px)'` 与样式里的 576/768/1024；
 * - `src/components/chat/ChatSidePanel.vue`：`matchMedia('(max-width: 1024px)')` 与 420px 宽度；
 * - `src/components/chat/useSplitWidth.ts`：分屏最小宽度 260 与"最多 60% 容器宽"；
 * - `src/components/home/HomeScene3D.vue`：`innerWidth <= 768`；
 * - 其余 `.vue` 里的 `@media (max-width: 576px/768px)`：CSS 无法引用 TS 常量，若要统一需
 *   先落成 CSS 自定义属性（`--bp-sm` / `--bp-md`），那是另一件事。
 */

/** 断点（px）。语义化名字而不是 `sm/md/lg`：名字说明的是"这一侧会发生什么"。 */
export const LAYOUT_BREAKPOINTS = {
  /** 手机：单列、抽屉铺满。`<=576` */
  mobile: 576,
  /** 紧凑（平板竖屏及以下）：收窄间距、隐藏次要栏。`<=768` */
  compact: 768,
  /** 侧面板让位给正文：`<=1024` 时改成抽屉并默认收起 */
  sidebarDrawer: 1024,
  /** 工作台（第三栏）默认展开：`>=1280`。1152～1279 之间强开三栏会把正文压到没法读 */
  workbenchWide: 1280,
} as const

/** 面板宽度常量（px / 比例）。 */
export const PANEL_WIDTHS = {
  /** 可拖拽分屏栏的最小宽度：再窄就只剩滚动条，拖不到也读不了 */
  splitPaneMin: 260,
  /** 分屏栏最多占容器宽度的比例：超过就把正文挤成零宽 */
  splitPaneMaxRatio: 0.6,
  /** 会话侧面板在宽屏下的最大宽度（窄屏时铺满） */
  sidePanelMax: 420,
} as const

/** 归一化宽度：`innerWidth` 可能是 `undefined` / `NaN` / 负数（jsdom、SSR、老 webview）。 */
function normalizeWidth(width: number): number {
  return Number.isFinite(width) && width > 0 ? width : 0
}

/** 布局判定结果。字段名贴调用点的语言，值全部由宽度算出。 */
export interface LayoutResolution {
  /** 归一化后的宽度（非法输入按 0） */
  width: number
  /** `<=576`：手机形态 */
  phone: boolean
  /** `<=768`：紧凑形态（间距/栏位收窄） */
  compact: boolean
  /** `<=1024`：侧面板以抽屉出现（覆盖正文） */
  drawer: boolean
  /** `<=1024`：侧栏自动收起，把宽度让给正文 */
  collapseSidebar: boolean
  /** `>=1280`：工作台默认展开 */
  workbenchDefaultOpen: boolean
}

/**
 * 给定视口宽度，算出全部布局判定。
 *
 * 纯函数：同样的输入永远同样的输出，不读 `window`、不缓存、不订阅。
 */
export function resolveLayout(width: number): LayoutResolution {
  const w = normalizeWidth(width)
  const drawer = w <= LAYOUT_BREAKPOINTS.sidebarDrawer
  return {
    width: w,
    phone: w <= LAYOUT_BREAKPOINTS.mobile,
    compact: w <= LAYOUT_BREAKPOINTS.compact,
    drawer,
    // 与 `drawer` 同一条判据、两个名字：调用点说的分别是"形态"与"是否让位"。
    // 两者必须永远相等（有单测盯住），否则会出现"收了却还是抽屉"的半截状态。
    collapseSidebar: drawer,
    workbenchDefaultOpen: w >= LAYOUT_BREAKPOINTS.workbenchWide,
  }
}

/** 窄屏（侧面板改抽屉 / 收起）的 media query —— 供 `matchMedia` 调用点使用。 */
export function narrowSidebarQuery(): string {
  return `(max-width: ${LAYOUT_BREAKPOINTS.sidebarDrawer}px)`
}

/** 宽屏（工作台默认展开）的 media query。 */
export function workbenchWideQuery(): string {
  return `(min-width: ${LAYOUT_BREAKPOINTS.workbenchWide}px)`
}

/**
 * 分屏栏在给定容器宽度下的最大宽度。
 *
 * 保留最小可读宽度：容器越窄，`60%` 越没有意义 —— 300px 的窗口里算出来 180px 的栏，
 * 拖过去等于把正文关掉。
 */
export function splitPaneMaxWidth(containerWidth: number): number {
  const w = normalizeWidth(containerWidth)
  return Math.max(PANEL_WIDTHS.splitPaneMin, Math.round(w * PANEL_WIDTHS.splitPaneMaxRatio))
}

/** 侧面板在给定容器宽度下的实际宽度：`min(420, 容器宽)`（窄屏铺满）。 */
export function sidePanelWidth(containerWidth: number): number {
  return Math.min(PANEL_WIDTHS.sidePanelMax, normalizeWidth(containerWidth))
}
