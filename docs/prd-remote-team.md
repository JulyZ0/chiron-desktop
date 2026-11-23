# PRD：Chiron 远程执行与团队服务模式

- 状态：远程执行 S0–S4、R1/R3/R4 已实现；团队模式核心已落地；R2/R5/R6/R7 与 S5 待推进
- 关联：`internal/api/device_fs.go`、`project_mirror.go`、`sessionmap_projects.go`、`frontend-vue/electron/device-fs.cjs`、`local-stack.cjs`、`python-engine/app/idle_shutdown.py`
- 部署手册：`docs/team-domain-deployment.md`；桌面契约：`docs/desktop-local/contracts.md` §1.1/§1.2

## 1. 三种形态

| 形态 | 执行位置 | 界面 | 数据/身份 |
|---|---|---|---|
| 桌面本地 | 本机：引导器(PG/Redis) + Go 网关 + Python 引擎 | Electron | 本机数据目录；OS 账户免口令会话 |
| 桌面远程 | 服务端：网关 + 引擎池 + PG/Redis；文件经设备桥回桌面 | Electron（只界面） | 服务端；账号密码/API Key |
| 团队 Web | 同远程，浏览器直连域名 | Go 网关托管的 SPA | 服务端；httpOnly Cookie + JWT |

**Agent 循环与工具始终在引擎侧**；桌面端不执行工具。远程模式下 client 项目文件经设备桥（WebSocket）访问。

## 2. 项目身份与路径（解决冲突）

```
ProjectLocator { project_id, provider: server|client, device_id?, root, root_key }
```

- 身份是 `project_id`，不是路径字符串；引擎只依赖身份 + `project_locator`。
- 判重键 `(tenant, user, provider, device_id, root_key_hash)`；`root_key` 由归属方规范化（server 解析 symlink，client 由设备按本机 OS 计算），sha256 入唯一索引。
- 不同用户/设备同名路径允许；同设备大小写、`..`、junction 改写拒绝。`23505` → 409。
- 迁移 `0011_project_locator`；历史行首次判重时惰性回填。

## 3. 文件访问

### 3.1 设备桥

- 桌面主动外连 `/ws/device-fs?token=…&device_id=…`；连接归属写 Redis `device:fs:owner:*`（TTL + 心跳），多网关副本经 `req/resp` 路由。
- 原语：`canonicalize/stat/list/read/walk/hash/write/mkdir/remove/rename/git`。
- 边界：每 op 带 root，设备侧 realpath+词法包含校验；单文件读 8MiB、walk 5000 文件/200MiB、回写 4MiB；写带 `expected_hash`；git 参数由服务端构造（数量 ≤64、单项 ≤2048 字节），输出 8MiB、超时 30s，非零退出码按结果返回而非传输错误。
- client 项目文件浏览 `List/Search/Read/Raw` 已接设备桥，返回与 server 同一套 JSON。
- **client git 完全允许**：`vcs`/`changes`/`vcs/init` 经设备桥执行；读不要求租约，`git init` 等写操作要求项目写租约（R5）。行数统计（insertions/deletions）对 client 项目留空。

### 3.2 引擎访问 client 项目

**当前默认（S2 过渡）**：按回合镜像到服务端临时目录，回合后按差异回写（冲突不覆盖；删除前校验设备哈希仍为基线）。
**R2 方向**：网关 WebDAV facade（已实现）+ rclone/WinFsp 挂载成普通目录，Python 零改动；决策门见 §9.2。

镜像**不包含 `.git`**（walk 跳过点文件与噪音目录）。因此：
- 界面侧 git（状态/改动/init）经设备桥 RPC 直接跑在用户机器上，完整可用；
- **引擎内**的 git 工具在镜像上只能看到工作区、看不到历史与索引；需要 agent 执行 git 时以 R2 挂载为正式方案，切换挂载前该能力明确不可用（不做半真半假的兜底）。

## 4. 多用户执行与配额

- **引擎池**：`CHIRON_ENGINE_POOL_SIZE`（1–4）；引擎自注册 `engine:instance:*`，网关发现；同 session 一致性哈希/run 归属；池副本空闲自停（退出码 75）。心跳携带逐实例 `active_tasks/cpu_percent/memory_mb`，管理端 `GET /v1/admin/engines` 与「运行时 → 性能」引擎池表可见。
- **故障恢复**：primary 监督器重拉、池副本原端口自愈、会话锁心跳/TTL、SSE 重放。
- **配额**：全局 `AGENT_MAX_CONCURRENCY`；每用户 `AGENT_MAX_CONCURRENCY_PER_USER`（默认 2，429）；run token 预算 `AGENT_RUN_MAX_TOKENS`（默认 400k）。
- 空载基线（2026-11-23）：引擎 262MB、网关 40MB、launcher 60MB、pageindex ~123MB、PG ~150MB、Redis 15MB。

## 5. 团队服务模式（Web）

- **交付**：浏览器打开 `https://<域名>`，同源 SPA + `/v1` + SSE；无需客户端。
- **身份**：邮箱或用户名 + 密码；httpOnly Cookie（桌面远程走 Bearer）。无注册页，账号由 owner/admin 创建。
- **账号闭环**：建号返回一次性初始密码 → 首登强制改密（`must_change_password`）→ 重置/禁用/启用/软删除/强制下线；管理动作审计。
- **登录安全**：失败锁定（Redis，5 次/15 分钟）、限流、统一错误文案；改密/禁用写 Redis 会话纪元使旧 token 失效。
- **角色**：owner（部署负责人）/admin（团队成员）/user；平台诊断仅 owner。
- **部署**：TLS 反代（Caddy/Nginx，SSE 关缓冲）、`PUBLIC_BASE_URL`、`COOKIE_SECURE`、CORS 白名单；详见 `docs/team-domain-deployment.md`。
- **桌面能力隔离**：team 模式隐藏/拒绝本机隔离档位、UAC 助手、内嵌数据栈诊断。

## 6. 安全与性能默认值

| 风险层 | 隔离/授权 | 性能策略 | 失败默认 |
|---|---|---|---|
| 读元数据/项目读 | 项目只读租约、根白名单 | 元数据缓存、按需分块、不整仓同步 | 离线明确报错 |
| 项目写 | 写租约 + hash 冲突 + 设备哈希校验 | 批处理、乐观并发、审计异步 | 冲突不覆盖 |
| 代码执行 | 服务端沙箱（Windows 低完整性/Job Object；Linux bwrap/gVisor） | 暖池/会话复用、CPU/内存/时长硬限 | 超时杀进程 |
| 网络/安装 | 默认拒绝 + 域名白名单代理 + 凭证注入 | 连接复用 | 拒绝并给申请路径 |
| MCP/插件 | 独立沙箱、固定版本、默认无网 | 常驻连接 | 单工具失败隔离 |

原则：授权在边界做一次；写/网络 fail-closed；读/状态 fail-fast；不因性能关掉越界检查。

## 7. 运行开关与部署

| 变量 | 默认 | 作用 |
|---|---|---|
| `CHIRON_DEPLOY_MODE` | `desktop` | `team` 强制无注册/无免口令，隐藏桌面能力 |
| `CHIRON_SERVICE_MODE` | 0 | 后台「服务」页开关的部署级等价物 |
| `CHIRON_ENGINE_POOL_SIZE` | 1 | 引擎池 1–4 |
| `CHIRON_ENGINE_IDLE_SECONDS` | 1800 | 池副本空闲自停 |
| `AGENT_MAX_CONCURRENCY` | 20 | 全局同时运行回合 |
| `AGENT_MAX_CONCURRENCY_PER_USER` | 2 | 每用户同时运行回合（0=不限） |
| `AGENT_RUN_MAX_TOKENS` | 400000 | 单 run token 预算 |
| `CHIRON_DEVICE_WRITE_ENABLED` | 1 | 租户级写回总闸；0/false/off 时所有 client 项目只读 |
| `CHIRON_MIRROR_ROOT` | 临时目录 | client 项目镜像根 |

部署：新构建自动 `alembic upgrade head`；源码团队主机先手动执行。team 模式要求 HTTPS、强 `APP_SECRET/JWT_SECRET`、精确 CORS。

**已定部署决策**：团队主机继续**随桌面程序启动**（不做 headless 服务）；团队主机若使用挂载，依赖（rclone/WinFsp）由管理员在应用内按需安装。

## 8. 实现状态

| 项 | 内容 | 提交/状态 |
|---|---|---|
| S0 | ProjectLocator / 迁移 / 引擎上下文 | `258d37b` |
| S1 | 设备隧道、client 项目、`@引用` | `a77e6b4` |
| S2 | 写入/冲突、按回合镜像、检查点回写 | `09d6693`、`74e48b9` |
| S3 | 引擎池、粘滞、空闲缩容 | `7511f1b`、`74e48b9` |
| S4 | 每用户并发、故障恢复 | `7511f1b` |
| R1 | client 文件浏览（list/search/read/raw） | `c792600` |
| R3 | 删除/重命名 + 镜像删除校验 | `569cef4` |
| R4 | 多副本设备路由（Redis） | `37b9b7f` |
| R2 | WebDAV facade | 已实现（`6fe881d`）；rclone/WinFsp 实测待团队主机 |
| R5 | 设备写租约（默认只读 + 项目侧栏授予） | 已实现 |
| client git | 设备桥 `git` 原语；vcs/changes/init 走设备 RPC；写操作受租约门控 | 已实现 |
| 团队模式 | 登录/建号/改密/禁用/会话吊销/Web 登录/邀请 | 已落地；浏览器 E2E 待验 |

## 9. 剩余任务

### 9.1 P0

1. **R5 设备写授权（已实现）**：client 项目默认**只读**；项目侧栏「只读/可写」显式授予/收回租约（Redis `device:write:lease:*`，Redis 丢失即回到只读）。未授权时回合照常执行但差异不回写，并提示本轮修改不保存；`git init` 同样被租约拦下。企业权限上限与读写审计仍待补。
2. **R2 实测定案**：团队主机安装 rclone+WinFsp，按 §9.2 跑性能/稳定性；通过则删除镜像、`root_path` 指向挂载点；否则用 cgofuse 重试或保留镜像。
3. **团队 Web E2E**：真实域名/反代/TLS 下从登录到 SSE、首登改密、强制下线、分享页的端到端验收（部署手册 + `scripts/team_smoke.py`；脚本已覆盖账密登录、建号、首登改密、邀请闭环、禁用/启用、强制下线、软删除，SSE 与分享页需人工/浏览器复核）。

### 9.2 R2 决策门

| 步骤 | 通过标准 |
|---|---|
| facade 单测（Stat/Readdir/OpenFile/RemoveAll/Rename + PROPFIND/GET） | 已通过 |
| `/v1/device-webdav/{projectID}/` 鉴权（internal token + client provider） | 已通过 |
| rclone mount + WinFsp：fs/git/run_code 可用；rename/delete 生效 | 团队主机实测 |
| 单文件读/写 P95 ≤50/150ms；10 分钟稳定；批量小文件不打满网关 | 团队主机实测 |

**决策（已定）**：团队主机实测通过后**切换挂载并删除镜像**；实测前保留镜像为默认。

### 9.2.1 依赖获取（已定：不进安装包）

rclone / WinFsp 只装在**服务端**（运行网关+引擎的主机），不随应用安装包分发，也不要求客户端安装。团队模式启用挂载时按需管理：

- 已钉版本：rclone **v1.76.0**（windows-amd64 zip，SHA256 `8b4fd2…a1dc`）、WinFsp **v2.2.26323**（msi，SHA256 `36ce01…7e19`）；升级必须同时更新版本与校验值。
- 存放：`<userData>/deps/rclone/<version>`、`<userData>/deps/winfsp/<version>`；取不到校验值就不下载。
- **rclone**：管理员在「设置 → 运行模式 → 挂载依赖」点击后下载 → 校验 → 解压；支持离线放置同版本文件。
- **WinFsp**：内核驱动/安装器，**必须管理员确认**（UAC），不做静默安装；也可用 winget/choco 预装，我们只检测版本。
- 下载失败 / 校验失败 / 版本不满足：挂载明确不可用并报错，**回退镜像**；绝不执行未经校验的二进制。
- 内网可配置镜像基址（同一套 SHA256 校验），默认官方 releases。

### 9.3 P1

4. **R6 引擎池实测/自动扩缩**：逐实例心跳（在途任务/RSS/CPU）与 `GET /v1/admin/engines` 已落地；剩余为团队主机上 P=2/4 压测、P95 首 token 与空闲回收验证；Prometheus 导出待接。
5. **R7 分层配额**：模型 RPM/TPM（`go-redis/redis_rate`）、工具/沙箱并发、费用熔断；模型预算/多 Key 用 LiteLLM。
6. **团队管理增强**：用户列表分页、审计导出、邀请链接/邮件交付（邀请已落地，邮件为 P1）。

### 9.4 P2

7. **S5 chiron-server BFF spike**：**暂缓**（不排期；现有 `/v1` 直连方案继续用，重启需重新评估）。
8. **R9 可观测性**：OTel GenAI 语义 + Langfuse（OTLP），统一 action/trace id，不自建 UI。
9. **R10 数据生命周期**：租户导出/删除；**软删除 + 默认保留 90 天**（回合表 `TURN_RETENTION_DAYS`、检查点 GC `retention_days` 已改 90）；client 文件默认不落服务端。
10. **MCP/工具沙箱**：MXC/gVisor/agent-sandbox；Casbin/OpenFGA 暂不引入。

## 10. 未决

- 文件桥 v1 是否允许客户端 `git`（**已定：完全允许**，见 §3.1）。

（已定：团队主机随桌面程序启动；rclone/WinFsp 服务端按需获取；R2 实测后切换挂载；R5 项目级租约 + 租户总闸；client git 完全允许；项目换设备重新授权而非内容同步；S5 暂缓；数据软删 + 90 天保留。）

## 11. 参考

- OpenAI Codex Sandbox：<https://tonybai.com/2026/11/19/inside-openai-codex-sandbox/>
- Hosted Agents 边界 / OpenHands V1：<https://xwang.dev/blog/2026/openhands-sdk-claude-managed-agents/>
- Baseten Sandboxes：<https://www.baseten.co/blog/building-the-agent-cloud-introducing-baseten-sandboxes>
- 多租户 Agent 隔离：<https://niteagent.com/blog/multi-tenant-agent-platform-architecture-2026/>
