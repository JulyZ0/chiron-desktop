# PRD：团队服务模式（多用户 + 后台统一管理 + 账密登录）

- 状态：草案（待评审）
- 目标版本：v0.x（服务模式首个可用版本）
- 关联代码：`internal/api/routes_auth.go`、`internal/api/auth.go`、`internal/api/admin.go`、`internal/api/tenant.go`、`internal/auth/*`、`config/config.go`
- 前置文档：`docs/db-migration-entry.md`

---

## 1. 背景与问题

当前产品以 **桌面本地模式** 为主：Electron 壳拉起引导器（embedded PG + Redis）、Go 网关、Python 引擎与 pageindex 服务，`LOCAL_BOOTSTRAP=1` 时本机回环可免口令铸造 owner 会话（`internal/api/auth_local_session.go`），注册路由靠 `DISABLE_REGISTRATION=1` 关闭。

要支持 **团队服务模式**：

> 部署在一台服务器/内网主机上，多人通过浏览器使用；不提供注册页面；账号由后台统一创建与管理；用户用 **邮箱/用户名 + 密码** 登录。

现有代码已经具备相当多的地基（JWT、RBAC、tenant_id、审计、限流、用户列表/改/删、口令复杂度校验），但缺的是 **「没有注册入口时，账号怎么产生、怎么交付、怎么停用」** 这条闭环，以及服务化部署所需的安全与运维能力。

---

## 2. 目标与非目标

### 2.1 目标（P0）

1. **无注册页**：团队模式下 `/v1/auth/register` 不可用，前端不暴露注册入口。
2. **后台建号**：owner/admin 在管理台创建用户，设置角色、初始密码（或一次性邀请码）。
3. **账密登录**：登录页支持「邮箱或用户名 + 密码」；昵称用于欢迎语与个人信息展示。
4. **首登改密**：管理员创建的账号首次登录必须修改密码，未改密前只允许访问改密页。
5. **统一管理**：管理员可重置密码、禁用/启用账号、调整角色、删除账号；所有动作可审计。
6. **会话可控**：禁用账号或重置密码后，该用户的已有 token 在可接受时间内失效；支持「强制下线」。
7. **安全基线**：登录限流、失败锁定、密码策略、Cookie 安全属性、HTTPS 部署说明。
8. **桌面能力隔离**：团队模式下隐藏/禁用只对单机有意义的入口（UAC 提权助手、本机隔离档位、本地免口令会话）。
9. **Web 直连**：普通成员**无需安装客户端**，浏览器打开部署方域名即可登录并完成全部日常操作（交付形态见 §2.2，部署见 §9.1）。

### 2.2 交付形态：Web 优先，域名直连（无客户端）

普通成员 **不下载、不安装任何客户端**，直接用浏览器访问部署方提供的域名（如
`https://chiron.example.com`）完成登录与全部日常操作。桌面版继续作为单机形态保留，
两者共用同一套 Go 网关与前端产物。

这意味着团队模式的验收对象是 **浏览器**，而不是 Electron：

- 前端由 Go 网关同源托管（已有能力，见 `internal/api/web_static.go` 的 `CHIRON_WEB_ROOT`）；
- 身份走 `httpOnly` Cookie + JWT，同源请求自动携带；
- 域名、TLS、反代由部署方提供（见 §9.1）；
- 桌面专属能力（UAC 提权助手、本机隔离档位、内嵌 PG）在 Web 形态下隐藏并由后端拒绝。

### 2.3 非目标（本期不做）

- 自助注册、邮箱验证、找回密码邮件（可留接口，不首发）。
- 计费/订阅、组织层级（多租户 SaaS）、跨租户共享。
- SSO/OIDC 的团队化管理界面（现有 OIDC 能力保留，不作为 P0 入口）。
- 每用户独立引擎进程/容器（资源隔离靠配额与并发限制，见 §9）。
- 移动端原生 App / 离线 PWA 工作流（浏览器可用即可；现有 Service Worker 仅做缓存，不承诺离线）。

---

## 3. 现状盘点（可作为实现的锚点）

| 能力 | 现状 | 位置 |
|---|---|---|
| 账密登录 | 已有，bcrypt 校验，登录成功签发 JWT + httpOnly cookie | `internal/api/auth.go:120-200` |
| 注册 | 已有，但可用 `DISABLE_REGISTRATION` 关闭 | `internal/api/auth.go:204`、`config/config.go:277` |
| 角色/权限 | owner / admin / user 三档，权限表集中定义 | `internal/auth/jwt.go:157-190` |
| 多租户 | JWT 带 `tenant_id`，中间件注入请求上下文，RLS 辅助 | `internal/api/tenant.go`、迁移 0001 |
| 用户管理 | 列表/详情/改角色/删除 | `internal/api/admin.go:148-354` |
| 审计 | 登录成功/失败、管理动作落 audit_logs | `internal/api/auth.go`、`db.AuditLog` |
| 限流 | 登录等公开路由挂 `rlMW` | `internal/api/routes_auth.go` |
| 本地免口令 | 回环可免口令铸 owner（桌面模式） | `internal/api/auth_local_session.go` |
| 口令复杂度 | 8-128 位，大小写+数字+特殊字符 | `internal/auth/crypto.go:28` |

### 3.1 Web 直连的现状与阻塞（必须优先解决）

已经具备的部分：

- 网关可在自身端口托管 SPA（`internal/api/web_static.go`），`/v1` 等 API 前缀不会被
  SPA 兜底吞掉；静态资源与客户端路由都已处理。
- 对外基址 `PUBLIC_BASE_URL`、`COOKIE_SECURE` 推导、CORS 白名单、分享链接绝对地址
  均已存在（`config/config.go`、`internal/api/cors_runtime.go`、`internal/api/share_link.go`）。
- 对话主链路使用 SSE（EventSource），不依赖 WebSocket；浏览器同源可直接用。

**关键阻塞：前端没有登录页。**

- `frontend-vue/src/router/index.ts` 明确写着「登录 / 注册 / 忘记密码 / 重置密码：
  **已随云端模式一并删除**」，路由表里没有 `/login`。
- `frontend-vue/src/router/guard.ts` 对未认证用户 `next('/')`，而不是去登录页；
  `src/api/index.ts` 的 401 拦截器同样回首页。
- 桌面端靠 `ensureLocalSession()` 调 `POST /v1/auth/session` 免口令建会话
  （`stores/auth.ts`），而团队模式必须关闭这条路由。

结论：**不改前端认证入口，浏览器访问只能看到空数据与 401，无法登录。**
这是「无需客户端、域名直连」的唯一硬阻塞，必须作为 M3 的第一项交付。

**其余关键缺口**

1. **没有「管理员创建用户」API 与页面**：`Register` 是公开自助注册，不能由后台代建；`admin.go` 只有改/删。
2. **没有改密接口**：注释里提到 `POST /v1/auth/password`，但路由未注册。
3. **没有禁用/锁定状态**：`users` 表无 `status`/`must_change_password`/`locked_until` 等列。
4. **删除即物理删除**：`DELETE /v1/admin/users/{id}` 直接删行，历史会话/审计的归属会断。
5. **登录失败无锁定**：仅审计与限流，没有账号级失败计数。
6. **JWT 只有黑名单 jti**：无法「按用户一键下线」。
7. **团队模式与桌面模式没有显式开关**：仅靠若干 env 组合，容易漏配出「能注册 / 免口令」的口子。
8. **管理台权限口径不统一**：`/v1/admin/diagnostics/summary` 要求 platform owner，租户 admin 会 403（团队模式下需明确谁看什么）。

---

## 4. 角色与权限（团队单租户）

首期采用 **单租户（默认租户）**，角色沿用现有三档：

| 角色 | 典型人 | 权限 |
|---|---|---|
| owner | 部署负责人 / IT | 全部管理权限；创建/删除 admin；查看平台级诊断；配置模型与密钥；强制下线任意用户 |
| admin | 团队管理员 | 创建/管理普通用户；查看用量与审计；不能改 owner、不能删除自己 |
| user | 普通成员 | 对话、知识库、个人设置；看不到管理台 |

权限矩阵（与 `auth.RolePermissions` 对齐，新增项标注 *）：

| 操作 | owner | admin | user |
|---|---|---|---|
| 登录 / 改自己的密码 | ✓ | ✓ | ✓ |
| 创建用户 / 重置其密码 | ✓ | ✓ | ✗ |
| 禁用/启用用户 | ✓ | ✓ | ✗ |
| 修改角色 | ✓ | 仅 user | ✗ |
| 删除用户 | 仅非 owner | 仅 user | ✗ |
| 强制下线（单用户/全员）* | ✓ | ✓（非 owner） | ✗ |
| 查看审计 / 用量 | ✓ | ✓ | 仅自己 |
| 模型/密钥/平台诊断 | ✓ | 只读（按需） | ✗ |

---

## 5. 核心用户旅程

### 5.1 首次部署

1. 服务模式是后台「服务」页的高级开关（保存即生效），也可用 `CHIRON_DEPLOY_MODE=team` 在部署层强制打开。
2. **owner 就是运行程序的第一个 Windows 用户**：桌面模式首次运行已创建本地 owner，不存在"首次部署另建 owner"的流程；本机回环仍保留免口令入口。
3. owner 用浏览器打开 `https://<域名>`（或用邮箱/用户名 + 密码登录）→ 进入管理台 → 创建成员。

### 5.2 创建成员并交付账号

1. 管理员在「用户管理」点「新建用户」，填邮箱、姓名、角色。
2. 系统生成一次性初始密码（或管理员自定义，需满足策略），返回 **只显示一次** 的凭据卡片，可复制。
3. 新用户 `must_change_password=true`。
4. 可选 P1：生成一次性邀请链接（token，24h 有效），用户打开后自行设置密码，避免明文传递。

### 5.3 成员首次登录

1. 浏览器打开 `https://<域名>`，落到登录页（无注册入口，无需安装任何客户端）。
2. 凭初始密码登录成功 → 强制跳转「设置新密码」，旧密码校验通过后才发放正式会话/解除限制。
3. 改密成功 → 进入工作台；审计记录 `password_changed(first_login)`。刷新或新标签页保持登录（Cookie）。

### 5.4 日常管理与离职

- 重置密码：管理员生成临时密码，用户下次登录强制改密；该用户所有旧 token 立即失效。
- 禁用：`status=disabled`，登录被拒；现有 token 在下一次校验时被拒（见 §7 会话吊销）。
- 删除：软删除（`status=deleted`，保留审计与消息归属），或物理删除前要求先禁用并转移资源（推荐软删除）。
- 强制下线：按用户/全员吊销。

---

## 6. 功能需求与验收标准

### P0-1 部署模式开关

- 新增 `CHIRON_DEPLOY_MODE`：`desktop`（默认）| `team`。
- `team` 强制：
  - `DISABLE_REGISTRATION=1`（不可被其它 env 覆盖回开）；
  - 不注册 `POST /v1/auth/session`（本地免口令）；
  - 前端隐藏注册页、本地引导、UAC 助手、隔离档位面板；
  - `/healthz` 暴露当前模式（只读，无敏感信息）。
- 验收：team 模式下 `POST /v1/auth/register` 返回 403；`POST /v1/auth/session` 404；登录页无注册链接。

### P0-2 管理员创建用户

- `POST /v1/admin/users`（owner/admin，`PermUsersManage` 或新 `PermUserAdmin`）
  - body：`email, username?, nickname?, role, initial_password?`
  - 校验邮箱唯一（按租户）、角色合法、密码策略；不传密码则生成 16 位随机强密码。
  - 返回：用户信息 + `initial_password`（仅此一次）。
- 验收：重复邮箱 409；角色越权 403；审计 `user_created`；新用户 `must_change_password=true`。

### P0-3 自助改密（含首登强制）

- `POST /v1/auth/password`
  - 已登录：校验旧密码 + 新密码策略；成功后吊销该用户其它会话。
  - 首登：允许「旧密码=初始密码」修改；成功后清除 `must_change_password`。
- 未完成首登改密的会话：除 `/v1/auth/password`、`/v1/auth/logout`、`/v1/auth/profile` 外一律 403（中间件判定）。
- 验收：初始密码登录后访问 `/v1/conversations` 返回 403 且带 `must_change_password` 标识；改密后可正常访问。

### P0-4 管理员重置密码 / 禁用 / 启用

- `POST /v1/admin/users/{id}/password/reset` → 返回一次性临时密码，置 `must_change_password=true`，吊销旧会话。
- `POST /v1/admin/users/{id}/disable` / `enable`。
- 禁止：禁用/删除自己；admin 操作 owner；删除最后一个 owner。
- 验收：禁用后登录返回 401（统一错误文案，不泄露账号是否存在）；旧 token 失效；审计记录操作者与目标。

### P0-5 登录安全

- 失败计数（Redis，按 `tenant+email`）：5 次失败锁定 15 分钟；成功清零。
- 登录接口限流（已有 `rlMW`，补充按 IP + 账号维度）。
- 错误文案统一为「邮箱或密码错误」，不区分账号不存在/密码错/被禁用。
- 审计保留 `login_failed`，但日志不记录密码与完整 token。
- 验收：连续失败触发锁定并返回 429/423；锁定期间即使密码正确也拒绝；到期自动解锁。

### P0-6 会话吊销

- JWT 增加 `pwd_at`（密码最后修改时间）或 `sess_v`（会话版本），校验时与用户表比对；改密/重置/禁用时递增即可让全部旧 token 失效。
- 保留现有 `jti` 黑名单用于单 token 登出。
- 管理员「强制下线」：写 Redis 纪元 `jwt:epoch:<userID>`（现有 `internal/api/session_revoke.go` 已实现），中间件比对 `epoch > token.iat`。
- 验收：改密后旧 token 立即 401；强制下线后 60s 内全部失效（允许缓存上限）。

### P0-7 管理台界面

- 用户列表：邮箱、姓名、角色、状态、最近登录、创建时间。
- 操作：新建、重置密码、禁用/启用、改角色、删除、强制下线。
- 空状态与错误态：无权限/加载失败给明确说明，不展示假数据。
- 验收：admin 看不到 owner 的敏感操作；所有写操作有二次确认。

### P0-8 团队部署安全基线

- `COOKIE_SECURE=1`（HTTPS 部署时强制）；文档要求反代终止 TLS。
- 必须配置强 `APP_SECRET`（≥32 字符）与独立 `JWT_SECRET`；弱密钥启动失败（已有校验）。
- CORS 白名单化；禁止 `*`。
- 提供 `docker-compose.team.yml`：gateway + engine + pageindex + PG + Redis + 反代示例。
- 验收：按文档部署后，未登录访问业务 API 全部 401；注册/本地引导不可达。

---

## 7. 技术方案

### 7.1 数据模型（迁移 0008）

`users` 增列（保持向后兼容，默认值可回填）：

| 列 | 类型 | 说明 |
|---|---|---|
| `status` | varchar(16) not null default 'active' | active / disabled / deleted |
| `must_change_password` | boolean not null default false | 首登或重置后强制改密 |
| `username` | varchar(64) | 可选登录标识；按租户大小写不敏感唯一 |
| `nickname` | varchar(128) | 展示名（欢迎语/个人信息页），为空回落到 name/username/email |
| `password_changed_at` | timestamptz | 改密/重置的事实时间 |
| `last_login_at` | timestamptz | 管理台展示 |

> 会话撤销**不建 `session_epoch` 列**：现有 Redis 纪元（`jwt:epoch:<userID>`）已经是唯一真相源，再落一列会形成两个口径。

`audit_logs` 已有，新增动作枚举：`user_created`、`user_disabled`、`user_enabled`、`password_reset`、`password_changed`、`forced_logout`。

> 登录失败计数建议放 Redis（`login:fail:<tenant>:<email>`，TTL 15m），避免每次失败写库；`locked_until` 仅作为兜底快照。

### 7.2 认证流程

1. `POST /v1/auth/login`：按邮箱或用户名查用户（大小写不敏感）→ 校验状态（disabled/deleted 拒绝）→ 失败锁定 → bcrypt → 签发 JWT（含 `must_change_password`）。
2. `AuthMiddleware`/`RequirePermission`：
   - 校验 Redis 会话纪元（`epoch > token.iat` ⇒ 401）；
   - `must_change_password=true` 时只放行改密/登出/profile。
3. `POST /v1/auth/password`：校验旧密码 → 策略 → 更新 hash 与 `password_changed_at`、`must_change_password=false` → 写 Redis 纪元吊销全部旧 token → 重签当前会话。
4. 管理动作（重置密码/禁用/删除）同样写 Redis 纪元。

> 兼容：桌面模式不启用 `must_change_password`；Redis 纪元无记录时按"从未撤销"处理，不影响本地免口令会话。

### 7.3 API 清单

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/v1/auth/login` | 公开 | 已有，补状态/锁定 |
| POST | `/v1/auth/logout` | 登录 | 已有 |
| GET | `/v1/auth/profile` | 登录 | 已有 |
| POST | `/v1/auth/password` | 登录 | **新增**：自助改密 |
| POST | `/v1/admin/users` | owner/admin | **新增**：创建用户 |
| POST | `/v1/admin/users/{id}/password/reset` | owner/admin | **新增** |
| POST | `/v1/admin/users/{id}/disable` | owner/admin | **新增** |
| POST | `/v1/admin/users/{id}/enable` | owner/admin | **新增** |
| POST | `/v1/admin/users/{id}/force-logout` | owner/admin | **新增** |
| GET/PUT/DELETE | `/v1/admin/users/{id}` | 已有 | 删除改为软删除（默认） |

### 7.4 配置项

```dotenv
CHIRON_DEPLOY_MODE=team          # desktop | team（部署层强制）
CHIRON_SERVICE_MODE=1            # 运行层开关，等价于后台「服务」页的开关
DISABLE_REGISTRATION=1           # team/服务模式强制为 1
LOCAL_BOOTSTRAP=1                # 保留：owner 是本机首个 Windows 用户
COOKIE_SECURE=1                  # HTTPS
PASSWORD_MIN_LENGTH=12           # 可选，默认沿用 8
LOGIN_MAX_FAILURES=5
LOGIN_LOCK_MINUTES=15
```

---

## 8. 前端改造

### 8.1 恢复 Web 登录入口（M3 第一项，阻塞其余全部页面）

当前路由表已删除登录页，必须补回，且不能只补一个表单：

1. **`/login` 公开路由**：邮箱 + 密码；无注册/找回入口（除非后续启用邮件能力）；
   SSO 按钮按 `/v1/auth/sso/providers` 动态显示。
2. **登录成功**：写入 `httpOnly` Cookie（同源），前端仅保留 `user` 与内存态；
   不把 token 写入 localStorage（Web 端现有 `loadDesktopToken` 恒空，正是为了避免这一点）。
3. **路由守卫**：
   - 未认证 + 非 `meta.public` → `next('/login?redirect=<目标>')`；
   - 桌面本地模式保持现有 `isAuthenticated` 恒真与免口令行为，不改；
   - `must_change_password` → 除 `/change-password`、`/profile`、登出外一律拦截。
4. **401 拦截器**：Web 端清登录态并跳 `/login`（带 `redirect`），不再回首页；
   同源 Cookie 过期时只跳一次，避免多请求风暴。
5. **登出**：调用 `POST /v1/auth/logout` 吊销 jti，清 Cookie 与内存态，回 `/login`。
6. **首屏**：`main.ts` 在挂载前尝试 `GET /v1/auth/session`（Cookie → 用户信息）；
   未登录直接渲染登录页，不发起业务请求、不弹一串 401 toast。
7. **分享页**（`/s/:token`）保持匿名可访问，不因未登录被守卫弹走。

### 8.2 其余页面

1. **强制改密页**：`must_change_password` 时全局路由守卫拦截，仅允许改密/登出。
2. **管理台 → 用户管理**：新建（凭据只展示一次，可复制）、重置密码、禁用/启用、改角色、强制下线、删除。
3. **账户设置**：修改密码、查看最近登录。
4. **模式感知**：`team` 模式下隐藏「本机隔离」「提权助手」「数据栈诊断」等桌面专属页；这些页面在后端也应返回不可用，而非仅靠前端隐藏。
5. **角色感知**：admin 不显示 owner 专属操作；普通用户不显示管理入口（现有 `requiresAdmin` 继续使用，补 `requiresOwner`）。
6. **浏览器形态适配**：
   - 文件选择用原生 `<input type=file>` / 拖拽，不依赖 Electron 对话框；
   - 剪贴板、通知走浏览器 API，失败时静默降级（现有 `utils/clipboard.ts` 已有回退）；
   - 不依赖 `window.chironDesktop`；`stores/device.ts` 在无 preload 时判定为 cloud，
     团队模式下需再区分 `team` 以隐藏桌面入口；
   - 至少覆盖 Chrome/Edge 最近 2 个大版本；Safari/Firefox 作为 P1 验证。

---

## 9. 服务化运行与资源

- **拓扑**：反代（TLS）→ Go 网关 → Python 引擎 → pageindex；PG/Redis 外置或容器化。
- **会话与引擎**：引擎按 `session_id` 路由，团队模式下需确认多用户并发时的：
  - 每用户/全局并发上限（`AgentMaxConcurrency` 已有，按租户/用户拆分需新增配额）；
  - 引擎内 `_ACTIVE_RUNTIMES` 仅存单实例状态，多副本部署需保持 session 亲和（已有 `WithSession` 一致性哈希）。
- **文件与知识库**：`STORAGE_ROOT` 按租户/用户分目录（现有 `tenant_id` 列已覆盖元数据，物理路径需校验）；对象存储建议外置 S3/MinIO（P1）。
- **隔离**：团队模式下 `restricted`/AppContainer 等 **本机进程隔离** 对服务器无意义且需要 Windows；Linux 部署应关闭隔离面板，改用容器/沙箱服务（`sandbox-service/`）。

### 9.1 域名直连部署（无客户端）

> 操作手册（Windows 主机 + Caddy/Nginx、固定端口、防火墙、验收）：
> [`docs/team-domain-deployment.md`](./team-domain-deployment.md)。

推荐拓扑：

```
浏览器 ──HTTPS──> 反向代理（Caddy/Nginx，TLS 终止）
                      │  同域转发
                      ▼
                  Go 网关 :8080（同时托管 SPA + /v1 API + SSE）
                      ├── Python 引擎 :8000
                      └── pageindex-service :8100
                  PG / Redis（内网，不暴露）
```

**域名与路径**

- 推荐 **独立子域名**（`chiron.example.com`）并部署在根路径。
- 不推荐子路径（`example.com/chiron/`）：SPA 目前挂载在网关根路径（`mountSPA` 注册
  `GET /`），子路径需要同时改静态资源 `BASE_URL`、网关挂载前缀与反代重写，风险面大；
  若必须支持，单独立项。
- DNS A/AAAA 指向反代；证书用 ACME 自动签发或企业证书。

**网关 / 反代配置要点**

| 项 | 要求 |
|---|---|
| `BIND_HOST` | `0.0.0.0`（容器内），宿主只暴露反代端口 |
| `PUBLIC_BASE_URL` | `https://chiron.example.com`（分享链接与 Cookie Secure 推导） |
| `CHIRON_WEB_ROOT` | 含 `index.html` 的前端产物目录（容器内 `/app/web`） |
| `COOKIE_SECURE` | `1`（缺省也会按 `PUBLIC_BASE_URL` 的 https 推导，仍建议显式） |
| `CORS_ORIGINS` | 同源部署可留空；前后端分域时填精确 origin，禁止 `*` |
| `TRUSTED_PROXY_CIDRS` | 反代所在网段，用于正确解析客户端 IP 与审计 |
| SSE | 关闭代理缓冲（Nginx `proxy_buffering off`），读超时 ≥ 10 分钟 |
| 上传 | `client_max_body_size` ≥ 64MB（知识库上传上限），媒体文件另计 |
| WebSocket | 仅 `/ws/rpa`（浏览器插件）需要 Upgrade 透传；对话主链路是 SSE |
| 缓存 | `index.html` 必须 `no-cache`；带 hash 的 `assets/*` 可长缓存 |
| 安全头 | HSTS、`X-Content-Type-Options`、`Referrer-Policy`；CSP 按前端产物实测放开 |

**桌面 vs Web 能力对照**

| 能力 | Web（团队） | 桌面（单机） |
|---|---|---|
| 对话 / 知识库 / 模型 / 管理台 | ✓ | ✓ |
| 账密登录 / 首登改密 | ✓ | 免口令（OS 身份） |
| 本机隔离档位 / UAC 提权助手 | ✗（隐藏且后端拒绝） | ✓ |
| 内嵌 PG/Redis、依赖管理（uv） | ✗ | ✓ |
| 文件上传 / 下载 | ✓（浏览器） | ✓（原生对话框） |
| 工作区文件路径 | 服务器端路径 | 本机路径 |
| 分享链接 | ✓（域名可达） | ✓（需配置 `CHIRON_WEB_ROOT`） |

---

## 10. 里程碑

| 阶段 | 内容 | 交付物 | 验收门禁 |
|---|---|---|---|
| M1（1 周） | 模式开关 + 建号/改密/禁用 API + 迁移 0008 | 后端接口与单测；`CHIRON_DEPLOY_MODE` | 注册/本地引导在 team 下不可达；建号→首登改密闭环 |
| M2（1 周） | 登录安全 + 会话吊销 | 失败锁定、Redis 会话纪元、强制下线 | 改密/禁用后旧 token 失效；锁定与解锁用例 |
| M3（1-2 周） | **恢复 Web 登录入口** + 强制改密 + 用户管理 | `/login` 页面、认证守卫与 401 跳转、登出；管理台页面；i18n | 全新浏览器打开域名即可登录；未登录跳登录并回跳；三角色端到端验收 |
| M4（1 周） | 域名直连部署与运维 | `docker-compose.team.yml`、反代示例（Caddy/Nginx）、TLS/SSE/上传配置、备份/升级说明 | 按文档从零部署，通过 §11 全部验收 |

> 每个阶段结束跑：`make check`（Go）、`pytest`（引擎）、`npm run build && npm test`（前端）。

---

## 11. 验收清单（发布前逐条打勾）

- [ ] **全新浏览器（无客户端）打开 `https://<域名>` 可直接登录使用**
- [ ] 刷新 / 新标签页保持登录；登出后回登录页且旧 Cookie 失效
- [ ] 未登录访问业务页跳 `/login`，登录后回到原目标页
- [ ] 分享链接 `https://<域名>/s/<token>` 匿名可打开
- [ ] 反代下 SSE 流式输出不卡顿（关缓冲）
- [ ] team 模式：无注册页、无免口令会话、无桌面隔离入口
- [ ] owner（本机首个 Windows 用户）可建号；admin 可建普通成员
- [ ] 初始密码只展示一次；首登强制改密生效
- [ ] 改密后旧 token 立即失效；管理台「强制下线」生效
- [ ] 禁用账号不能登录；启用后恢复
- [ ] 5 次失败锁定 15 分钟；错误文案不泄露账号状态
- [ ] 删除为软删除；最后一个 owner 不可删
- [ ] 所有管理动作有审计（操作者、目标、时间、IP）
- [ ] HTTPS 下 Cookie 为 Secure + HttpOnly + SameSite
- [ ] 多用户并发 10 会话压测无明显串话/串数据
- [ ] 桌面模式回归：本地免口令、注册开关、隔离面板不受影响

---

## 12. 风险与待决策（推荐默认值）

| 风险/问题 | 影响 | 推荐决策 |
|---|---|---|
| 初始密码交付方式 | 明文密码经聊天工具外传 | 首期「一次性展示 + 强制改密」；P1 加邀请链接/邮件 |
| 共享账号 | 无法审计到人 | 禁止共享；一人一号；管理台可见最近登录 |
| 多副本会话吊销延迟 | 最长一个校验周期 | 校验用户表/Redis 版本，目标 <60s |
| 引擎单进程承载多用户 | 资源争抢、串话风险 | 首期限并发 + 每租户配额；P1 按用户路由/多引擎 |
| 软删除与唯一索引 | 邮箱复用冲突 | 软删除时改写邮箱为 `deleted+<id>@...`，保留原值到审计 |
| 管理台权限口径 | 租户 admin 403 的平台级项 | 明确：平台诊断仅 owner；租户用量/审计 admin 可看 |
| 密码找回 | 无邮件服务时无法自助 | P0 由管理员重置；P1 配 SMTP 后加找回 |
| **前端无登录页** | Web 用户无法建立会话，域名直连完全不可用 | M3 第一项：恢复 `/login`、守卫与 401 跳转（见 §3.1、§8.1） |
| 反代缓冲 SSE | 流式回答变成"一次性出现"或超时 | 示例配置默认 `proxy_buffering off` + 长读超时，并列入部署自检 |
| 子路径部署 | 静态资源/路由 404 | P0 只支持根域名；子路径单独立项 |
| 浏览器兼容差异 | 上传/剪贴板/通知行为不同 | P0 以 Chromium 内核为准；Safari/Firefox 列入 P1 回归 |
| 上传大小限制 | 知识库大文件被反代拒绝 | 反代与网关两侧上限对齐（≥64MB），给出明确 413 文案 |

**待评审决策**

1. 账号标识：已确认 **邮箱 + 用户名双入口**，并增加昵称用于展示（已按此实现）。
2. 删除默认软删还是硬删？（推荐：软删，30 天后由保留期任务清理。）
3. 团队模式是否保留 `/v1/auth/register` 路由但返回 403，还是完全不注册？（推荐：不注册，减少攻击面。）
4. 密码策略是否可配置？（推荐：P0 用现有复杂度，P1 增加 `PASSWORD_MIN_LENGTH` 等。）

---

## 13. 上线检查（运维）

1. 生成并保存 `APP_SECRET`（≥32 字符），配置 `JWT_SECRET`、`INTERNAL_TOKEN`。
2. 执行 `alembic upgrade head`（含 0008）。
4. 配置 DNS 指向反代；反代开启 TLS 与 HSTS；`PUBLIC_BASE_URL=https://<域名>`；
   `COOKIE_SECURE=1`；仅暴露反代端口，网关/引擎/PG/Redis 不直接对外。
5. 反代关闭 SSE 缓冲、放宽读超时；上传上限 ≥64MB；`/ws/rpa` 允许 Upgrade。
6. 配置备份：PG 每日全量 + WAL；Redis 可重建但会丢会话黑名单，接受短时失效。
7. 冒烟：全新浏览器打开域名 → 登录 → 首登改密 → 刷新保持登录 → 登出 →
   未登录跳转 → 分享链接匿名打开 → SSE 流式输出正常。


---

## 14. 实现进度（本轮已落地）

- 迁移 `0008_team_accounts`：`username` / `nickname` / `status` / `must_change_password` /
  `password_changed_at` / `last_login_at` 与用户名唯一索引。
- 登录：邮箱或用户名（大小写不敏感）+ 失败锁定（Redis，5 次锁 15 分钟）+ 禁用账号统一报错。
- 后台账号管理：建号（邮箱/用户名/昵称/角色/初始密码）、重置密码、禁用/启用、
  强制下线、编辑、软删除；初始/临时密码只返回一次。
- 自助改密 `POST /v1/auth/password`；未改密会话由 AuthMiddleware 拦截为
  `403 password_change_required`。
- 服务模式：后台「服务」页新增开关（保存即生效，关闭公开注册）；`CHIRON_DEPLOY_MODE=team`
  与 `CHIRON_SERVICE_MODE=1` 亦可作为部署级强制。
- Web：恢复 `/login` 与 `/change-password`；守卫未登录跳登录并回跳；401 跳登录；
  首屏用 httpOnly cookie 恢复会话；后台新增「用户管理」页。
- owner：沿用本机首个 Windows 用户（本地回环免口令保留），不再提供环境变量引导。

**仍需下一轮**

- 浏览器端到端手工验收（真实域名/反代/TLS）；部署步骤见
  [`docs/team-domain-deployment.md`](./team-domain-deployment.md)，API 冒烟用
  `python scripts/team_smoke.py`；
- 管理台的分页与审计导出；
- 邮件/邀请链接交付初始密码（P1）。
