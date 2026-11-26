import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { message } from 'ant-design-vue'
// antd 已按需引入（unplugin-vue-components），无需全局 app.use(Antd)
import 'ant-design-vue/dist/reset.css'
import '@fontsource-variable/geist'
import router from './router'
import App from './App.vue'
import './style.css'
import { usePerformanceMonitor } from './composables/usePerformanceMonitor'
import { setupRequestInterceptors } from './utils/requestOptimizer'
import { api } from './api'
import { useAuthStore } from './stores/auth'
import { i18n, applyDocumentLocale } from './i18n'
import { installLocaleSync } from './utils/localeSync'
import { syncDevAuthBypassFromUrl } from './stores/device'
import { notify } from './utils/notify'

// 开发环境登录绕过：URL 参数（`?devAuthBypass=1`）必须先落到 localStorage，
// 否则首帧的模式/登录态判定读到的还是旧值。生产构建里这个函数是空操作。
syncDevAuthBypassFromUrl()

// 首屏语言与书写方向必须在 mount 前落到 <html lang/dir>：否则 RTL 语言会先按 LTR
// 渲染再跳变（可见的重排闪烁）。
applyDocumentLocale()

const app = createApp(App)

// ── 全局错误兜底 ──
//
// ErrorBoundary 只覆盖**子树内的渲染/生命周期**错误；事件处理器里的 throw、
// `void someAsync()` 的 rejection、路由懒加载 chunk 404 都不经过它。缺了这三条，
// 线上的表现是"点了没反应"或导航后白屏，且没有任何上报入口。
let chunkErrorNotified = false
router.onError((err) => {
  const text = String((err as { message?: string })?.message ?? err)
  if (/dynamically imported module|Importing a module script failed|Loading chunk/i.test(text)) {
    // 发布新版本后旧页面引用的 chunk 已不在：提示刷新，而不是让 router-view 空白。
    if (!chunkErrorNotified) {
      chunkErrorNotified = true
      notify.error(i18n.global.t('errors.chunk_load_failed'))
    }
    return
  }
  console.error('[router]', err)
})
app.config.errorHandler = (err, _instance, info) => {
  console.error(`[vue:${info}]`, err)
  notify.error(i18n.global.t('errors.unhandled_error'))
}
window.addEventListener('unhandledrejection', (event) => {
  console.error('[unhandledrejection]', event.reason)
  notify.error(i18n.global.t('errors.unhandled_error'))
})

// 标记应用启动
performance.mark('app-init-start')

// 初始化性能监控
const { initMonitor, mark, measure } = usePerformanceMonitor()
initMonitor()

// 全局暴露性能数据 (仅开发环境，供 DevTools 调试)
if (import.meta.env.DEV) {
  window.__ChironPerf = { mark, measure, getReport: () => usePerformanceMonitor().getReport() }
}

// 初始化请求优化（需在全局 api 实例创建后调用）
setupRequestInterceptors(api)

const pinia = createPinia()

app.use(i18n)
app.use(pinia)
app.use(router)

// 语言偏好回写服务端（T1）：邮件等**服务端渲染**的通知按 users.settings.locale
// 决定语言，只存 localStorage 时服务端永远不知道用户选了什么。
// 必须在 app.use(pinia) 之后安装 —— 探测登录态要读 auth store。
installLocaleSync(() => {
  try {
    return !!useAuthStore(pinia).user
  } catch {
    return false
  }
})

// 统一提示时长：成功/信息类 2.5s（"复制成功"看一眼就够）。
// 错误类需要读完，调用点单独传 { duration: 4 }。
message.config({ duration: 2.5 })

// 桌面端 token 由 safeStorage 异步解密；先拿到再挂载，避免首屏请求缺少 Bearer。
// Web 端 initToken 立即返回，不影响启动。
//
// **但必须有上限**：`initToken` 在桌面端 await 主进程 IPC（safeStorage / 设备 token），
// 本地模式还会发起 `POST /v1/auth/session`。这些一旦不 settle，`app.mount` 就永远不会
// 执行 —— 用户看到的是 index.html 里 `#app:empty::before` 那个**启动转圈**，
// 既没有内容也没有错误提示（"一直转圈，永远不结束"就是这么来的）。
// 超时放行不会丢身份：token 落地时写的是请求层每次请求现读的变量（`setAuthToken`），
// 先到的请求最多缺一次 Bearer，后续请求照常带上。
const BOOT_TOKEN_WAIT_MS = 5000
let mounted = false
function mountApp() {
  // 竞速的两侧都可能 settle，挂载只能发生一次
  if (mounted) return
  mounted = true
  app.mount('#app')

  // 标记首次渲染完成
  performance.mark('vue-first-render')
  performance.mark('app-init-end')

  // Lighthouse 性能评分优化标记
  if (import.meta.env.PROD) {
    performance.measure('app-init-duration', 'app-init-start', 'app-init-end')
  }
}

const auth = useAuthStore(pinia)
const bootWait = new Promise<void>((resolve) => {
  setTimeout(resolve, BOOT_TOKEN_WAIT_MS)
})
void Promise.race([
  auth.initToken().catch((err: unknown) => {
    // 拿不到 token 也要出第一帧；页面各自的 401 分支会给出提示
    console.warn('[boot] token init failed:', err)
  }),
  bootWait,
]).then(mountApp, mountApp)

// PWA：生产环境注册 Service Worker（离线壳；开发环境跳过）
// file://（Electron 桌面壳）不是安全上下文，注册只会产生一条 404 失败请求。
// 用 BASE_URL 拼路径：Web 子路径部署时不能再假设站点位于域名根。
if (import.meta.env.PROD && 'serviceWorker' in navigator && location.protocol !== 'file:') {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register(`${import.meta.env.BASE_URL}sw.js`).catch(() => {})
  })
}
