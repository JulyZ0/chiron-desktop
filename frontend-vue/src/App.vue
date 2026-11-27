<script setup lang="ts">
import { computed, onMounted, onUnmounted, watchEffect } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ConfigProvider, theme } from 'ant-design-vue'
import AppLayout from './components/AppLayout.vue'
import TitleBar from './components/shell/TitleBar.vue'
import RouteProgressBar from './components/common/RouteProgressBar.vue'
import ErrorBoundary from './components/common/ErrorBoundary.vue'
import ImageViewer from './components/common/ImageViewer.vue'
import TakeoverOverlay from './components/agent/TakeoverOverlay.vue'
import LockScreen from './components/shell/LockScreen.vue'
import { useThemeStore } from './stores/theme'
import { useTypographyStore } from './stores/typography'
import { languageMeta } from './i18n/languages'
import { desktopHost } from './stores/device'
import { requestQuickAsk, submitQuickAskText } from './utils/quickAsk'

const route = useRoute()
const router = useRouter()
const { locale } = useI18n()
// ant-design-vue 组件库语言（分页器/日期选择器/空态等）必须与界面语言同步，
// 否则会出现"界面英文、组件中文"的混搭。
const antdLocale = computed(() => languageMeta(locale.value).antd)
const themeStore = useThemeStore()
// 实例化即应用持久化的排版偏好（store 内 immediate watch 写入 <html> 变量）
useTypographyStore()
// 匿名分享落地页（`/s/:token`、`/share/:id`）不套应用壳：侧栏、命令面板、
// 接管轮询都假定"有一个登录用户 + 一个当前会话"，匿名访客挂上去只会每 5 秒
// 打一次 401（见 TakeoverOverlay 的挂载条件注释），而且落地页本来就该是干净的单页。
// 因此 `meta.public` 与 Login/Register 一样走裸 `router-view`。
const showLayout = computed(
  // 登录页与强制改密页不套主布局：它们需要在没有可用会话时也能独立渲染。
  //
  // `matched.length === 0`：首帧路由还没解析完（`app.mount` 早于首次导航落定）时，
  // `route.name` / `meta.public` 都是空的。此时若判成"要布局"，匿名访客打开分享链接
  // 会先挂上 AppLayout —— 它的接管心跳（`reportPresence`）没有 `__silent`，401 会触发
  // 全局拦截器 `location.href='/login?redirect=...'`，落地页就这样被"强制登录"了。
  // 未匹配时不挂布局：宁可空一帧，也不要在公开页上建立需要身份的轮询。
  () =>
    route.matched.length > 0 &&
    !['Login', 'Register', 'ChangePassword'].includes(route.name as string) &&
    route.meta.public !== true,
)

const themeConfig = computed(() => ({
  algorithm: themeStore.isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
  token: themeStore.antdTokens,
}))

onMounted(() => {
  themeStore.init()
})

watchEffect(() => {
  const root = document.documentElement
  root.setAttribute('data-theme', themeStore.cssThemeId)
  if (themeStore.isDark) root.classList.add('dark')
  else root.classList.remove('dark')
})

// F10：全局快捷键「快速提问」。
// - 迷你输入框（F10 后半）提交后带文本：先切到聊天页再置位，ChatView 挂载后**直接发送**；
// - 旧信号（降级路径）只要求聚焦输入框。
let unsubQuickAsk: (() => void) | undefined
let unsubQuickAskText: (() => void) | undefined
onMounted(() => {
  const host = desktopHost()
  unsubQuickAsk = host?.onQuickAsk?.(() => {
    requestQuickAsk()
    if (route.path !== '/chat') void router.push('/chat')
  })
  unsubQuickAskText = host?.onQuickAskText?.((text) => {
    if (!text) return
    // 顺序不能反：先导航（ChatView 挂载时消费待发送文本），再置位。
    // 已在聊天页时 push 不会触发重挂载，事件监听会立即消费。
    if (route.path !== '/chat') void router.push('/chat')
    submitQuickAskText(text)
  })
})
onUnmounted(() => {
  unsubQuickAsk?.()
  unsubQuickAsk = undefined
  unsubQuickAskText?.()
  unsubQuickAskText = undefined
})
</script>

<template>
  <ConfigProvider
    :theme="themeConfig"
    :locale="antdLocale"
  >
    <RouteProgressBar />
    <!-- 自绘标题栏：挂在布局之外，登录页/错误页同样需要能拖、能关。
         它在非桌面壳、以及保留系统框的窗口里自己渲染成空，不留高度。 -->
    <TitleBar />
    <ErrorBoundary>
      <AppLayout v-if="showLayout" />
      <router-view v-else />
    </ErrorBoundary>
    <!-- 全局单例：任何位置的图片都能唤起同一个查看器 -->
    <ImageViewer />
    <!-- Super-agent 接管段：全站锁 + 唯一出口（打断）。
         只在已登录布局下挂载 —— 登录页没有会话，挂上去只会每 5 秒打一次 401。 -->
    <TakeoverOverlay v-if="showLayout" />
    <!-- 本地模式锁屏：点「退出登录」后遮住整个界面，直到验证 Windows 账户密码。
         挂在最外层（与 TitleBar / Template 同级）而不是布局内部 —— 锁定期间
         连设置页、命令面板都不该可达。它自己 Teleport 到 body。 -->
    <LockScreen />
  </ConfigProvider>
</template>

<style>
html, body {
  width: 100%;
  height: 100%;
  margin: 0;
  padding: 0;
}

/* 尺寸（含顶部给自绘标题栏让位的 padding-top）全部交给 style.css 的 #app 规则，
   这里只负责表面色。**刻意不写 `padding: 0` 简写** —— 那会抹掉那边的
   padding-top: var(--app-titlebar-h)，而谁赢取决于两份样式表的注入顺序。 */
#app {
  background: var(--bg-page);
  color: var(--text-primary);
  transition: background-color var(--dur-slow) var(--ease-out),
              color var(--dur-slow) var(--ease-out);
}

body {
  font-family: var(--font-sans);
  font-feature-settings: 'cv02', 'cv03', 'cv04', 'cv11';
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  background: var(--bg-page);
  transition: background-color var(--dur-slow) var(--ease-out),
              background-image var(--dur-slow) var(--ease-out);
}

/* 主题切换：全局平滑过渡 - 仅对特定属性生效，避免 * 选择器性能问题 */
:root {
  transition-property: background-color, border-color, box-shadow, color;
  /* 0.3s 正是 CDS 阶梯里的 --dur-sheet（抽屉/大面板一档），改为 token 以免与阶梯脱钩 */
  transition-duration: var(--dur-sheet);
  transition-timing-function: var(--ease-out);
  transition-delay: 0s;
}

/* 减少动画偏好 */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    transition: none !important;
  }
}
</style>
