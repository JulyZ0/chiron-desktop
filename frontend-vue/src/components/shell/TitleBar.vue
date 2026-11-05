<script setup lang="ts">
/**
 * 自绘标题栏。
 *
 * 背景：主窗口在 Windows/Linux 上是 frameless 的，系统那圈 1px 描边和系统标题栏都
 * 不再存在 —— 好处是界面不再像"套在另一个程序里"，代价是**窗口开始不能拖、不能关**。
 * 所以拖动区和最小化/最大化/关闭必须由页面补上，且只能补在这里。
 *
 * 三条边界：
 *  1. 只在桌面壳里出现。浏览器预览 / 老外壳（没有 `titleBar` 成员）时 mode 取
 *     `native`，组件整个不渲染、也不改任何布局变量 —— 那些环境本来就有系统框。
 *  2. 挂在 `App.vue` 顶层而不是 `AppLayout`：登录页不在布局里，同样需要能拖能关。
 *  3. 高度通过 `--app-titlebar-h` 发布给布局层（`#app` 的 padding、固定定位的
 *     顶栏/停靠坞都读它），所以这里**不能**给根元素写死高度后就让内容自己重叠。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { desktopHost, type TitleBarMode, type WindowState } from '../../stores/device'

const { t } = useI18n()
const titleBar = desktopHost()?.titleBar

/** 缺 `titleBar` 的老外壳等价于 native：那时窗口还有系统框，页面不该再画一条 */
const mode: TitleBarMode = titleBar?.mode ?? 'native'
const visible = mode !== 'native'

/**
 * macOS 用 `inset`：红绿灯是系统画的，页面只需让出那块高度做拖动带；
 * Windows/Linux 用 `custom`：三个按钮全部自绘。
 */
const heightPx = mode === 'inset' ? 28 : 36

const maximized = ref(false)
const fullScreen = ref(false)

/** 拖到屏幕边缘（Aero Snap）、Win+方向键、F11 都会改状态，页面收不到点击，只能靠事件跟住 */
function applyState(state: WindowState | null | undefined) {
  if (!state) return
  maximized.value = Boolean(state.maximized)
  fullScreen.value = Boolean(state.fullScreen)
}

function invoke(action: (() => Promise<unknown>) | undefined) {
  // 通道缺失只可能出现在主进程比页面旧的情况，静默即可 —— 窗口还有系统框可用
  action?.()?.catch(() => {})
}

function onMinimize() {
  invoke(titleBar?.minimize)
}

async function onToggleMaximize() {
  try {
    const state = await titleBar?.toggleMaximize()
    applyState(state)
  } catch {
    // 同上：主进程未就绪时不打扰用户
  }
}

function onClose() {
  invoke(titleBar?.close)
}

let unsubscribe: (() => void) | undefined

onMounted(async () => {
  if (!visible) return
  document.documentElement.style.setProperty('--app-titlebar-h', `${heightPx}px`)
  if (!titleBar) return
  try {
    applyState(await titleBar.getState())
  } catch {
    // 首帧拿不到状态不影响可用性，图标先按「未最大化」画
  }
  unsubscribe = titleBar.onStateChange(applyState)
})

onBeforeUnmount(() => {
  unsubscribe?.()
  unsubscribe = undefined
  // 卸载必须收回变量：残留的高度会凭空在顶部留一条空白，且没有标题栏去解释它
  if (visible) document.documentElement.style.removeProperty('--app-titlebar-h')
})

/** 最大化时按钮语义变成「还原」，图标与文案同步换 */
const maximizeLabel = computed(() =>
  maximized.value ? t('common.windowRestore') : t('common.windowMaximize'),
)
</script>

<template>
  <div
    v-if="visible"
    :class="`titlebar titlebar--${mode}`"
    role="presentation"
  >
    <div
      v-if="mode === 'custom'"
      class="titlebar-controls"
    >
      <button
        class="titlebar-btn"
        type="button"
        :aria-label="t('common.windowMinimize')"
        :title="t('common.windowMinimize')"
        @click="onMinimize"
      >
        <svg
          aria-hidden="true"
          viewBox="0 0 10 10"
        >
          <path
            d="M0.5 5.5h9"
            stroke="currentColor"
          />
        </svg>
      </button>
      <button
        class="titlebar-btn"
        type="button"
        :aria-label="maximizeLabel"
        :title="maximizeLabel"
        :disabled="fullScreen"
        @click="onToggleMaximize"
      >
        <!-- 还原态是两个错位方框：与 Windows 原生的观感一致，避免用户误以为是"最大化" -->
        <svg
          v-if="maximized"
          aria-hidden="true"
          viewBox="0 0 10 10"
        >
          <path
            d="M2.5 0.5h7v7"
            stroke="currentColor"
            fill="none"
          />
          <rect
            x="0.5"
            y="2.5"
            width="7"
            height="7"
            stroke="currentColor"
            fill="none"
          />
        </svg>
        <svg
          v-else
          aria-hidden="true"
          viewBox="0 0 10 10"
        >
          <rect
            x="0.5"
            y="0.5"
            width="9"
            height="9"
            stroke="currentColor"
            fill="none"
          />
        </svg>
      </button>
      <button
        class="titlebar-btn titlebar-btn--close"
        type="button"
        :aria-label="t('common.windowClose')"
        :title="t('common.windowClose')"
        @click="onClose"
      >
        <svg
          aria-hidden="true"
          viewBox="0 0 10 10"
        >
          <path
            d="M0.5 0.5l9 9M9.5 0.5l-9 9"
            stroke="currentColor"
          />
        </svg>
      </button>
    </div>
  </div>
</template>

<style scoped>
/**
 * `-webkit-app-region: drag` 让整条成为系统标题栏的拖拽区：按下拖动窗口、拖到屏幕
 * 顶部贴边最大化都由 Windows/macOS 自己处理（比用 JS 逐帧 setPosition 跟手得多）。
 *
 * 代价是 drag 区域**不会派发鼠标事件**，所以任何要点击的东西必须显式 `no-drag`
 * （见 .titlebar-controls）。反过来也不要给 drag 区加 @click —— 收不到的。
 */
.titlebar {
  position: fixed;
  top: 0;
  inset-inline: 0;
  z-index: var(--z-drawer);
  height: 36px;
  display: flex;
  align-items: stretch;
  justify-content: flex-end;
  background: var(--bg-page);
  -webkit-app-region: drag;
  user-select: none;
}

.titlebar--inset {
  height: 28px;
  /* 红绿灯在最左，给它们留出位置，避免本条的背景压住系统画的按钮 */
  background: transparent;
}

.titlebar-controls {
  display: flex;
  align-items: stretch;
  -webkit-app-region: no-drag;
}

.titlebar-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  border: none;
  border-radius: 0;
  background: transparent;
  color: var(--text-secondary);
  cursor: default;
  transition: background-color var(--dur-fast) ease, color var(--dur-fast) ease;
}

.titlebar-btn svg {
  width: 10px;
  height: 10px;
  stroke-width: 1;
  shape-rendering: crispEdges;
}

.titlebar-btn:hover {
  background: var(--bg-surface-hover);
  color: var(--text-primary);
}

.titlebar-btn:active {
  background: var(--bg-hover);
}

.titlebar-btn:focus-visible {
  outline: var(--hairline-width) solid var(--primary);
  outline-offset: calc(var(--hairline-width) * -1);
}

.titlebar-btn:disabled {
  opacity: 0.4;
}

/* 关闭键沿用 Windows 约定：悬停变红。这是唯一一个有"破坏性"语义的按钮，
   让它与另外两个在悬停时区分开，误点代价小一些。 */
.titlebar-btn--close:hover {
  background: var(--error);
  color: var(--on-solid);
}

.titlebar-btn--close:active {
  background: var(--error);
  color: var(--on-solid);
  filter: brightness(0.92);
}
</style>
