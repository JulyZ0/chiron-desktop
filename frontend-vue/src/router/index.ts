import { createRouter, createWebHashHistory, createWebHistory } from 'vue-router'
import { authGuard } from './guard'
import { setupRouteProgress } from './progress'
import { setupSchemaMismatchWatch } from './schemaMismatch'

import { t } from '../i18n'
const routes = [
  {
    path: '/',
    name: 'Home',
    component: () => import('../views/HomeView.vue'),
  },
  // 登录页：团队服务模式（浏览器直连域名）的入口。
  //
  // 桌面本地模式不用它 —— 身份由网关按 OS 账户免口令下发；但同一套前端产物
  // 也被网关托管给浏览器，必须保留一条可达的登录路由。`meta.public` 让守卫
  // 不预先拉取身份，页面自己用 cookie 恢复会话并决定是否回跳。
  {
    path: '/login',
    name: 'Login',
    component: () => import('../views/LoginView.vue'),
    meta: { public: true },
  },
  {
    // 首登/管理员重置后的强制改密页。requiresAuth：没有会话就没有旧密码可验。
    path: '/change-password',
    name: 'ChangePassword',
    component: () => import('../views/ChangePasswordView.vue'),
    meta: { requiresAuth: true },
  },
  {
    // 首次运行引导（P1-10）：配置模型 → 连通性 → 可选能力 → 第一条消息。
    // 与守卫的 `WELCOME_ENTRY_PATHS` 配合 —— 只有"/"与"/chat"会**自动**把人送来，
    // 本页自身以及 /models、/knowledge 等目标页不会被反复拉回（否则一点「去配置」
    // 就会被弹回引导，形成死循环）。requiresAuth：引导要按 userId 记进度，匿名没有身份。
    path: '/welcome',
    name: 'Welcome',
    component: () => import('../views/WelcomeView.vue'),
    meta: { requiresAuth: true },
  },
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
  // 分享落地页（匿名可访问）。两条路径指向同一个页面：
  // - `/s/:token` 是**对外发出去**的短链形态（见 utils/shareLink.ts）；
  // - `/share/:id` 是早期链接的兼容别名 —— 链接发出去就收不回来，只能加别名，
  //   不能改旧路径，否则已经散落在聊天记录里的链接全部变 404。
  // 两者都必须标 `meta.public`：守卫据此跳过 `fetchProfile`，匿名访客没有身份可拉，
  // 多打一次 `/v1/auth/profile` 只会在控制台留下一条 401。
  // 无 requiresAuth：权限判定在后端（`GET /v1/share/{id}` 只放行未撤销、未过期的分享）。
  {
    path: '/s/:token',
    name: 'ShareShort',
    component: () => import('../views/ShareView.vue'),
    meta: { public: true },
  },
  {
    path: '/share/:id',
    name: 'Share',
    component: () => import('../views/ShareView.vue'),
    meta: { public: true },
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
    // 价格一览表：只读参考价（多源交叉验证后写入 model_prices），**不参与计费**。
    // 单独一页而不是塞进计费页：它是"各模型单价清单"，与"我用掉了多少"是两个问题。
    path: '/billing/prices',
    name: 'BillingPrices',
    component: () => import('../views/PricesView.vue'),
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
    // 资源市场：按**源**分 tab 浏览 agents / skills / knowledge / mcp 并安装到本机。
    // 不属于五大工作台 —— 它是「去哪找资源」的入口，不是一种工作形态，
    // 所以不进 WORKSTATION_ROUTES（那五个是工作台定义，受 shared/workstations.json 约束）。
    path: '/market',
    name: 'Market',
    component: () => import('../views/MarketView.vue'),
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
    // 通知中心：账号菜单（components/accountMenu.ts）的 `notifications` 项一直
    // 返回这个路径，但路由表里此前**没有**它 —— 点进去必然落到 404 兜底页。
    // 后端是 `GET /v1/notifications`（internal/api/notifications.go），产生方只有
    // 两个真实事件：子 Agent 失败、分享被管理员撤销。
    path: '/notifications',
    name: 'Notifications',
    component: () => import('../views/NotificationsView.vue'),
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
        redirect: { path: '/admin/monitor', query: { tab: 'overview' } },
      },
      {
        // 仪表盘已并入「运行时监控」的总览 Tab；保留旧路径重定向，书签不会 404。
        path: 'dashboard',
        redirect: { path: '/admin/monitor', query: { tab: 'overview' } },
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
        // 用户管理：团队模式的后台建号入口（成员、角色、禁用、重置密码）。
        path: 'users',
        name: 'AdminUsers',
        component: () => import('../views/admin/UsersView.vue'),
        meta: { title: t('admin.userAdmin.page_title') },
      },
      {
        // 人格管理：原用户设置里的「行为塑形」搬到后台，支持多人格与对话内选择。
        path: 'personas',
        name: 'AdminPersonas',
        component: () => import('../views/admin/PersonasView.vue'),
        meta: { title: t('admin.persona_title') },
      },
      {
        // 引擎功能开关：只对桌面端有意义（改的是本机引擎进程环境）。
        path: 'engine-env',
        name: 'AdminEngineEnv',
        component: () => import('../views/admin/EngineTogglesView.vue'),
        meta: { title: t('settings.engine_env_title') },
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
        path: 'shares',
        name: 'AdminShares',
        component: () => import('../views/admin/SharesView.vue'),
        meta: { title: t('admin.share_management') },
      },
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
      // 「服务」页：这台机器对谁可见（监听地址 / 对外域名）。
      // 与「系统设置」相邻 —— 它同样是决定部署形态的配置，而不是某个业务功能。
      {
        path: 'service',
        name: 'AdminService',
        component: () => import('../views/admin/ServiceView.vue'),
        meta: { title: t('admin.service.page_title') },
      },
      // 「知识库」页：索引服务用本地模式还是外部 pageindex 服务。
      // 紧挨「服务」：两者都是"这台机器依赖谁"的部署级选择，而不是业务配置。
      {
        path: 'knowledge',
        name: 'AdminKnowledgeConfig',
        component: () => import('../views/admin/KnowledgeConfigView.vue'),
        meta: { title: t('admin.knowledge.page_title') },
      },
      // 资源市场「外部源」：初装默认关闭，开启后本机才开始访问第三方 URL。
      // 与「系统设置」相邻 —— 它同样是影响部署行为的系统级配置。
      {
        path: 'market',
        name: 'AdminMarketSources',
        component: () => import('../views/admin/MarketSourcesPanel.vue'),
        meta: { title: t('admin.market.page_title') },
      },
      // Python 依赖管理：pip list / PyPI 查询 / 安装（后台任务 + 轮询）/ 卸载。
      // 懒加载，与其它 admin 页一致；后端要求平台 owner（见 internal/api/deps_admin.go）。
      {
        path: 'deps',
        name: 'AdminDeps',
        component: () => import('../views/admin/DepsView.vue'),
        meta: { title: t('admin.deps_title') },
      },
      // ── 旧入口重定向（书签 / 文档 / 告警链接兼容）──
      { path: 'redis', redirect: { path: '/admin/datastores', query: { tab: 'redis' } } },
      { path: 'database', redirect: { path: '/admin/datastores', query: { tab: 'database' } } },
    ],
  },
  // schema 漂移诊断页。网关在「代码期望的迁移 head ≠ 数据库 alembic_version」时只
  // 提供 /healthz，业务路由全 503（见 cmd/chiron/schema_mismatch.go）；跳转由
  // setupSchemaMismatchWatch 非阻塞地触发。`public` 是必须的：此刻没有可用的鉴权
  // 接口，守卫若等 initToken 建立会话，用户会卡在转圈上而看不到这条诊断。
  {
    path: '/schema-mismatch',
    name: 'SchemaMismatch',
    component: () => import('../views/SchemaMismatchView.vue'),
    meta: { public: true },
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

// schema 漂移跳转（非阻塞）：启动探一次 + 每次导航后节流探测，只在
// status=schema_mismatch 时把人送到 /schema-mismatch（见 src/router/schemaMismatch.ts）
setupSchemaMismatchWatch(router)

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
