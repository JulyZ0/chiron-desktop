import type { RouteLocationNormalized, NavigationGuardNext } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { currentDeviceMode, isDevAuthBypassEnabled } from '../stores/device'
import { requestManager } from '../utils/requestOptimizer'
import { WELCOME_ENTRY_PATHS, shouldAutoEnterWelcome } from '../api/welcomeStatus'

/**
 * 全局路由守卫：
 * - requiresAuth：未登录（无 user）→ 重定向 /login
 * - requiresAdmin：非 admin/owner 角色 → 重定向（登录态回首页，未登录回 /login）
 * - cloudOnly：只有云端存在的页面（登录/注册/计费）→ 本地模式下回首页
 *
 * S 安全：token 已迁至 httpOnly cookie，JS 不可读。admin 权限**不得**信任
 * localStorage.role（可被用户篡改为 owner）。每次进入 admin 页都向后端拉权威身份，
 * 防止伪 admin 借守卫放行敏感管理接口。
 *
 * 双模式（docs/desktop-local/contracts.md §1）：本地模式的服务端就是同一个 Go 网关，
 * 只是运行位置在本机，身份由 `POST /v1/auth/session` 免口令下发（真实 user + 真实
 * JWT，见 internal/api/auth_local_session.go）。因此**判定逻辑与云端共用同一条路径**，
 * 差别只有两点：
 *   1. 本地没有登录页，`isAuthenticated` 在本地模式恒真（stores/auth.ts），所以
 *      requiresAuth 失败不会把人弹到 /login 再弹回来；
 *   2. `fetchProfile` 在本地模式打的是会话端点而非 /v1/auth/profile（core 链路没有
 *      那条端点），角色仍然来自后端，不是本地存储。
 * 云专属路由由 `meta.cloudOnly` 统一拦截 —— 路由本身不删，只是本地模式下不可达
 *（书签/深链也不会落到 501 页面）。管理后台**不再标 cloudOnly**：本地模式的账户
 * 就该进得去，否则模型密钥、审计、运行时监控在单机上全都无从配置。
 */
export async function authGuard(
  to: RouteLocationNormalized,
  _from: RouteLocationNormalized,
  next: NavigationGuardNext,
) {
  // 路由切换时取消进行中的请求，避免接口瀑布和内存泄漏
  if (to.path !== _from.path) {
    requestManager.cancelAll()
  }

  const auth = useAuthStore()

  // 匿名页面（`meta.public`，如分享落地页 `/s/:token`）**不等待任何鉴权初始化**。
  //
  // 这里的 `await` 是"永远转圈"的直接来源之一：`initToken` 在桌面端要等主进程解密
  // token（safeStorage/IPC）、本地模式会顺带发起 `POST /v1/auth/session`、开发绕过模式
  // 还会真登录一次 —— 这些都与一个匿名访客无关，却卡在**组件挂载之前**：`loading`
  // 还没来得及被任何分支清掉，页面就已经停在骨架/启动转圈上。请求失败尚有 catch，
  // 这个 await 没有兜底 —— 它不 settle，页面就永远不结束。
  //
  // 分享数据本身不需要身份（后端 `GET /v1/share/{id}` 只放行未撤销、未过期的分享），
  // 所以这里既不放宽权限，也不引入登录依赖：只是不再为一个公开页面去建立会话。
  if (to.meta?.public !== true) {
    // 桌面端 token 解密是异步的；main.ts 通常已等待，这里兜底保证后续请求带上 Bearer。
    // 本地模式下这一步建立的是本地会话（见 stores/auth.ts 的 initToken / ensureLocalSession）。
    await auth.initToken?.()
  }

  const localMode = currentDeviceMode() === 'local'

  // 云专属页面（登录/注册/忘记密码/计费）在本地模式不存在对应后端，
  // 放进去只会得到 501 页面。统一回首页，而不是让用户对着空白页怀疑装坏了。
  if (localMode && to.meta?.cloudOnly) {
    next('/')
    return
  }

  // admin 访问（或还没有 user）时向后端拉取权威身份。
  // fetchProfile 内部失败会登出；此处不抛错，继续按现有本地态降级判定。
  // meta.public 的页面不需要登录态，跳过拉取（如分享页）。
  //
  // H68：守卫的每个结论都属于**某一次认证**，而 `initToken` / `fetchProfile` 都是异步的。
  // 期间用户登出并以另一个账号登录时，用 A 的 role 给 B 放行（或把 B 当游客弹回登录页）
  // 都是错的。所以抓一份代次，任何身份写回之后都比对一次；不一致就按当前账号重取一次。
  let fence = auth.generation
  const refreshIdentity = async () => {
    try {
      await auth.fetchProfile()
    } catch {
      /* fetchProfile 已内部登出 */
    }
    if (auth.generation === fence) return
    // 拉取期间换过账号：刚拿到的判定依据作废，用当前账号再取一次（只补一次，
    // 反复抖动时交给下面的"仍在切换中"分支处理，绝不用旧账号的身份做导航决定）。
    fence = auth.generation
    try {
      await auth.fetchProfile()
    } catch {
      /* 同上 */
    }
  }
  if (to.meta.requiresAdmin || (!auth.user && !to.meta.public)) {
    await refreshIdentity()
  }

  // 登录页：已登录则直接回到目标页（或首页），避免登录后点浏览器后退又停在表单上。
  if (to.path === '/login') {
    if (auth.isAuthenticated) {
      const redirect = typeof to.query.redirect === 'string' ? to.query.redirect : '/'
      next(redirect)
      return
    }
    next()
    return
  }

  // 首登/重置后强制改密：除改密页与个人信息页外一律拦回改密页。
  // **公开页例外**：分享落地页是匿名访客看的，与登录用户的改密状态无关；
  // 把它也拦走会让"已登录但待改密"的访客打开分享链接时看到改密页。
  if (
    !to.meta.public &&
    auth.mustChangePassword &&
    to.path !== '/change-password' &&
    to.path !== '/profile'
  ) {
    next('/change-password')
    return
  }

  // H68：写回（导航决定）之前最后比对一次代次。
  // 到这一步已经按更便宜的顺序做过判定，期间的登录/登出切换会让那些依据整体作废：
  // 此时用**当前**账号再确认一次身份；如果仍在切换中，就退回一个与身份无关的落地页，
  // 而不是拿旧账号的结论去导航（那会把新账号弹去登录页，或把 A 的 role 当成 B 的）。
  if (auth.generation !== fence) {
    fence = auth.generation
    await refreshIdentity()
    if (auth.generation !== fence) {
      if (to.meta.public) next()
      else next('/')
      return
    }
  }

  if (to.meta.requiresAuth && !auth.isAuthenticated) {
    // 桌面本地模式没有登录页：身份由 OS 账户免口令下发，保持原行为回首页。
    // 团队 Web 模式则送到登录页，并带上回跳地址。
    if (localMode) {
      next('/')
      return
    }
    next({ path: '/login', query: { redirect: to.fullPath } })
    return
  }

  if (to.meta.requiresAdmin) {
    const role = auth.user?.role
    // 开发环境绕过：无登录态时 role 恒为 undefined，admin 页会一律被弹回首页。
    // 该函数在生产构建里恒为 false（编译期 DCE），不构成线上后门。
    // API Key 会话：后端已剔除全部管理权限（auth.APIKeyPermissions），
    // 界面不能再按角色放行 —— 否则用户点进去只会连吃 403。
    if (auth.isApiKeySession || (role !== 'admin' && role !== 'owner' && !isDevAuthBypassEnabled())) {
      next('/')
      return
    }

    // H83：`requiresPlatformOwner` 页面的**第二道**判定。
    //
    // 租户 admin 与平台 owner 在后台里是两种角色，后端那几页用的是
    // `RequirePlatformOwner`（见 internal/api/routes_admin.go）。此前路由层不区分，
    // 租户 admin 点进「运行时监控 / 数据存储 / 自诊断 / 依赖管理」只会得到一片 403 ——
    // 用户看到的是"功能坏了"，而实际是"你没这个权限却被告知有这个入口"。
    // 这里按**同一口径**（role === 'owner'，与 stores/auth 的 isPlatformOwner 一致）收敛，
    // 并保持开发绕过与上面一致。
    if (to.meta.requiresPlatformOwner && !isDevAuthBypassEnabled()) {
      if (auth.isApiKeySession || !auth.isPlatformOwner) {
        next('/')
        return
      }
    }
  }

  // 首次运行引导（P1-10）：有登录、当前没有任何可用模型、且本账号还没走完引导时，
  // 把**两个主入口**（"/" 与 "/chat"）的首次进入重定向到 /welcome。
  //
  // API Key 会话不走引导：它会把人送去 /models 配模型，而那条路由要求交互式登录 ——
  // 结果是在 /welcome 与 /models/首页之间来回弹。
  //
  // 三个"不打扰"的边界（判定细节见 api/welcomeStatus.ts:shouldAutoEnterWelcome）：
  //   · 已有模型 → 不跳：能对话就说明不需要这一步；
  //   · 已完成/已跳到底 → 不跳：进度按 userId 存 localStorage，不再反复弹；
  //   · 探测失败 → 不跳：网关抖动时把人从首页拽走，比漏一次引导伤害更大。
  //
  // 只拦 entry paths 而非全部 requiresAuth 路由：/models、/knowledge、/admin/* 是
  // 用户在引导里被"去配置"送去的目标页，拦它们会形成"点去配置→弹回引导"的死循环。
  // 位置放在 requiresAdmin 之后：非管理员访问 admin 页应先被弹回首页，而不是被引导截走。
  if (auth.user && !auth.isApiKeySession && WELCOME_ENTRY_PATHS.includes(to.path)) {
    try {
      if (await shouldAutoEnterWelcome(auth.user.id)) {
        next('/welcome')
        return
      }
    } catch {
      /* 判定失败：按原有流程放行 */
    }
  }

  next()
}
