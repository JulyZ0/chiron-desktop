import { createRouter, createWebHashHistory, createWebHistory } from 'vue-router'
import { authGuard } from './guard'
import { setupRouteProgress } from './progress'

import { t } from '../i18n'
const routes = [
  {
    path: '/',
    name: 'Home',
    component: () => import('../views/HomeView.vue'),
  },
  // 登录 / 注册 / 忘记密码 / 重置密码：**已随云端模式一并删除**。
  // 本地模式没有账号密码这一步 —— 身份由网关按 OS 账户免口令下发
  // （见 internal/api/auth_local_session.go），故不再有这四条路由。
  {
    path: '/chat',
    name: 'Chat',
    component: () => import('../views/ChatView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 模型配置：决定 /chat 模型下拉里能选到什么（写 llm_models 全局表，
    // 与后端 /v1/admin/models 的权限口径一致）。
    // 本地模式**同样需要**：服务商密钥与端点就是由这一页写进本机网关的库的，
    // 「设置 → 本地模型」那个平行入口已删除（见 SettingsPanel 的页签列表）。
    path: '/models',
    name: 'Models',
    component: () => import('../views/ModelsView.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    path: '/share/:id',
    name: 'Share',
    component: () => import('../views/ShareView.vue'),
    // 注意：无 requiresAuth — 共享会话可能公开访问，后端应验证 share_id 的权限范围
  },
  {
    path: '/agents',
    name: 'Agents',
    component: () => import('../views/AgentsView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/skills',
    name: 'Skills',
    component: () => import('../views/SkillsView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 用量与计费：云端模式的套餐/支付已废除，这里只保留**本地用量统计**
    // （token 消耗、模型分布），所以不再标 cloudOnly —— 本地就靠这一页看账。
    path: '/billing',
    name: 'Billing',
    component: () => import('../views/BillingView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/profile',
    name: 'Profile',
    component: () => import('../views/ProfileView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 用户级 API Key 自助（P1-3）：管的是「我自己的账号调网关的凭证」，
    // 与 admin 的 LLM 供应商密钥池无关。本地模式也有真账号（网关为本机铸的 owner
    // 会话），所以这一页本地可达 —— 本地网关就是"跑在本机的同一个服务端"。
    path: '/api-keys',
    name: 'UserApiKeys',
    component: () => import('../views/UserApiKeysView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/media',
    name: 'Media',
    component: () => import('../views/MediaView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/plugins',
    name: 'Plugins',
    component: () => import('../views/PluginsView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/knowledge',
    name: 'Knowledge',
    component: () => import('../views/KnowledgeView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/knowledge/settings',
    name: 'KnowledgeSettings',
    component: () => import('../views/KnowledgeSettingsView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/memory',
    name: 'Memory',
    component: () => import('../views/MemoryView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/knowledge/:id',
    name: 'KnowledgeDetail',
    component: () => import('../views/KnowledgeDetailView.vue'),
    meta: { requiresAuth: true },
  },
  // 管理后台路由：本地模式**也可达**。本地后端就是同一个 Go 网关，只是运行位置在
  // 本机；账户是网关为本机铸的真实 owner，角色判定见 router/guard.ts。
  // 「钱与多租户」类页面（支付/套餐/发票/租户域名）在单机上没有实际对象，
  // 但网关照样应答（空列表），所以不在路由层整棵子树封掉 —— 封掉的代价是
  // 模型服务商密钥、审计、监控这些本地真正有用的页面也一起进不去了。
  {
    path: '/admin',
    component: () => import('../views/admin/Layout.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
    children: [
      {
        path: '',
        redirect: '/admin/dashboard',
      },
      {
        path: 'dashboard',
        name: 'AdminDashboard',
        component: () => import('../views/admin/DashboardView.vue'),
        meta: { title: t('common.dashboard') },
      },
      {
        path: 'api-keys',
        name: 'AdminApiKeys',
        component: () => import('../views/admin/ApiKeysView.vue'),
        meta: { title: t('common.api_key_management') },
      },
      {
        // 运行时监控：原「性能监控 / 队列监控 / 缓存监控」三页合并为一个 Tabs 页面（懒加载）
        path: 'monitor',
        name: 'AdminMonitor',
        component: () => import('../views/admin/MonitorView.vue'),
        meta: { title: t('admin.runtime_monitoring') },
      },
      // 旧入口保留重定向，避免书签 / 文档 / 告警链接失效
      {
        path: 'performance',
        redirect: { path: '/admin/monitor', query: { tab: 'performance' } },
      },
      {
        path: 'queue',
        redirect: { path: '/admin/monitor', query: { tab: 'queue' } },
      },
      {
        path: 'cache',
        redirect: { path: '/admin/monitor', query: { tab: 'cache' } },
      },
      {
        path: 'settings',
        name: 'AdminSettings',
        component: () => import('../views/admin/SettingsView.vue'),
        meta: { title: t('settings.system_settings') },
      },
      // ── 邮件发信（邮箱验证码登录 / 注册邮箱验证 / 密码重置 / 欢迎邮件）──
      // 发信服务器地址与凭据全部在此配置，代码中不含任何厂商默认地址。
      // ── 支付渠道配置（支付宝 / 微信支付 / PayPal）──
      // 凭据加密入库 + 保存后热生效；原「系统设置」里的支付卡片已迁移到此处，
      // 避免同一份配置在两处维护。
      // P1-2：发票审核 —— 与支付渠道同属「钱相关的运营动作」，故相邻。
      // P2-2c：套餐目录 —— 决定用户能买什么、平台收多少，同属商业化运营。
      // ── 数据存储（原「Redis 管理」+「数据库管理」合并）──
      {
        path: 'datastores',
        name: 'AdminDataStores',
        component: () => import('../views/admin/DataStoresView.vue'),
        meta: { title: t('common.data_storage') },
      },
      // D1：自诊断（"配了但不生效"的自答清单）
      {
        path: 'diagnostics',
        name: 'AdminDiagnostics',
        component: () => import('../views/admin/DiagnosticsView.vue'),
        meta: { title: t('admin.diagnostics') },
      },
      // ── 平台 ──
      // D3：用量目录（"花在哪"的构成）—— 与成本中心同域，故相邻。
      {
        path: 'api-docs',
        name: 'AdminApiDocs',
        component: () => import('../views/admin/ApiDocsView.vue'),
        meta: { title: t('knowledge.api_documentation') },
      },
      // ── 旧入口重定向（书签 / 文档 / 告警链接兼容）──
      { path: 'redis', redirect: { path: '/admin/datastores', query: { tab: 'redis' } } },
      { path: 'database', redirect: { path: '/admin/datastores', query: { tab: 'database' } } },
    ],
  },
  // 404 兜底：避免未知地址白屏
  {
    path: '/:pathMatch(.*)*',
    name: 'NotFound',
    component: () => import('../views/NotFoundView.vue'),
  },
]

// Electron 桌面壳用 loadFile() 从 file:// 打开构建产物：history 模式既匹配不了
// file 路径，pushState 也无法在 file URL 上工作，表现为路由落空/白屏。
// 只在 file: 下切 hash 路由，Web 端保持 history（分享链接与 nginx fallback 依赖它）。
const isFileProtocol = typeof window !== 'undefined' && window.location.protocol === 'file:'

const router = createRouter({
  history: isFileProtocol ? createWebHashHistory() : createWebHistory(),
  routes,
  scrollBehavior(_to, _from, savedPosition) {
    if (savedPosition) return savedPosition
    return { top: 0 }
  },
})

// 路由守卫（逻辑在 guard.ts，独立可测）
router.beforeEach(authGuard)

// 路由进度条
setupRouteProgress(router)

// 路由预加载：页面空闲时预加载常用路由
// 使用 RequestIdleCallback 避免阻塞主线程
if (import.meta.env.PROD && 'requestIdleCallback' in window) {
  const ROUTE_CHUNKS = [
    () => import('../views/ChatView.vue'),
    () => import('../views/AgentsView.vue'),
    () => import('../views/ProfileView.vue'),
  ]

  const preloadIfNeeded = (): void => {
    if ('connection' in navigator) {
      const conn = (navigator as Navigator & { connection?: { saveData?: boolean; effectiveType?: string } }).connection
      if (!conn) return
      // 弱网或省流模式不预加载
      if (conn.saveData || /2g|3g/i.test(conn.effectiveType)) return
    }
    ROUTE_CHUNKS.forEach(imp => imp().catch(() => {}))
  }

  requestIdleCallback(preloadIfNeeded, { timeout: 3000 })
}

export default router
