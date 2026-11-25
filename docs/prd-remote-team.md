# PRD：Chiron 远程执行与团队服务模式

- 状态：S0–S4、R1/R3/R4/R5 已实现；R6/R7 的观测与配额、R9 span/trace、R10 导出已落地。
- 新增加固：H1–H86 为 2026-11-24 后端全覆盖与前端/桌面壳复核后的原始问题编号，按 P0–P3 排序；已完整验收的条目从待办表删除并保留编号空洞，不重排其它问题。
- 2026-11-24 第二轮加固：**H1/H3/H8/H23/H26–H35/H37/H39/H40/H58/H63/H66/H68/H74 已完整验收并从待办表移除**，证据见 9.12。
- 2026-11-24 第三轮加固：**H4/H5/H6/H9/H10/H12/H13/H18/H25/H42/H49/H52/H55/H59/H61/H69/H75/H77/H86 已完整验收并从待办表移除**，证据见 9.13。
- 只剩两半的项：**H46**（设备桥客户端已收紧，服务端票据端点与签名校验待实现）、**H73**（前端已绑定不可变计划，服务端 `plan_digest` 校验未做）；两条都留在表里并写清了缺哪一半。
- 2026-11-25 第四轮加固：**H43/H78/H79 已完整验收并从待办表移除**，证据见 9.14。
- 待团队主机：R2 实测切换、R6 压测、Web E2E。
- 暂缓：LiteLLM、Langfuse 实接、R10 租户删除、S5；ACP 完整协议见 H19，按产品目标排期。
- 关联：`internal/api/device_fs.go`、`project_mirror.go`、`sessionmap_projects.go`、`frontend-vue/electron/device-fs.cjs`、`docs/desktop-local/contracts.md`、`docs/team-domain-deployment.md`。

## 1. 三种形态

| 形态 | 执行位置 | 界面 | 身份/数据 |
|---|---|---|---|
| 桌面本地 | 本机（引导器 + Go 网关 + Python 引擎） | Electron | 本机数据目录；OS 账户免口令会话 |
| 桌面远程 | 服务端（网关 + 引擎池 + PG/Redis）；文件经设备桥回桌面 | Electron（只界面） | 服务端；账号密码/API Key |
| 团队 Web | 同远程，浏览器直连域名 | 网关托管的 SPA | 服务端；httpOnly Cookie + JWT |

Agent 循环、工具与代码执行**始终在引擎侧**；桌面端不执行工具。

## 2. 项目身份与文件访问

```
ProjectLocator { project_id, provider: server|client, device_id?, root, root_key }
```

- 身份是 `project_id`，不是路径字符串；判重键 `(tenant, user, provider, device_id, root_key_hash)`。
- `root_key` 由归属方规范化（server 解析 symlink；client 由设备按本机 OS 计算），sha256 入唯一索引（迁移 `0011_project_locator`）。
- **设备桥**：桌面主动外连 `/ws/device-fs?token=…&device_id=…`；连接归属写 Redis `device:fs:owner:*`，多网关经 req/resp 路由。
  - 原语：`canonicalize/stat/list/read/walk/hash/write/mkdir/remove/rename/git`。
  - 边界：当前每 op 带 root，设备侧 realpath 失败时回退词法包含校验；H46 要求改为 realpath-only 并拒绝 symlink/junction 逃逸。读 8MiB、walk 5000 文件/200MiB、回写 4MiB、写带 `expected_hash`；git 参数由服务端构造（≤64 项、单项 ≤2048 字节），输出 8MiB、超时 30s，非零退出码按结果返回。
- **client 文件与 git**：浏览/搜索/读取、`vcs/changes/vcs/init` 都经设备桥；读不要求租约，`git init` 等写操作要求项目写租约。行数统计对 client 项目留空。
- **回合镜像**：回合前 walk/read 到服务端临时目录并记录基线 sha256；回合后按差异回写（冲突不覆盖；删除前校验设备哈希仍为基线）。镜像**不含 `.git`**，引擎内 git 工具需要 R2 挂载。
- **R2 方向**：网关 WebDAV facade（已实现）+ rclone/WinFsp 挂载成普通目录，Python 零改动；团队主机压测通过后切换并删除镜像。

## 3. 多用户执行与配额

- **引擎池**：`CHIRON_ENGINE_POOL_SIZE`（1–4）；引擎自注册 `engine:instance:*`，网关发现；session 一致性哈希/run 归属；池副本空闲自停（退出码 75）。心跳携带 `active_tasks/cpu_percent/memory_mb`，管理端与 `/metrics` 可见。
- **并发**：全局 `AGENT_MAX_CONCURRENCY`（20）、每用户 `AGENT_MAX_CONCURRENCY_PER_USER`（2，429）；run token 预算 `AGENT_RUN_MAX_TOKENS`（400k）。
- **分层配额**（0 = 关闭；后台保存需重启引擎生效）：

| 配额 | 配置 | 行为 |
|---|---|---|
| 日费用熔断 | `quota.tenant_daily_cost_cents` / `user_daily_cost_cents` | 按 `usage_events` 当日 UTC 汇总；submit 与 OpenAI 兼容入口 429；读库失败放行 |
| 模型调用 RPM/RPS | `GATEWAY_MODEL_{,USER_}RPM/RPS`（python 分类） | 每次 LLM 调用计数，缓存命中不计数；用户身份取运行时上下文 |
| 模型 TPM | `GATEWAY_MODEL_{,USER_}TPM`（python 分类） | 调用前判已用、调用后按实际 token 累计；并发窗口可小幅超出 |
| 沙箱并发 | `SANDBOX_MAX_CONCURRENCY_PER_TENANT` | run_code/execute_python/shell_exec/persistent_shell 共用 Redis 租户槽；嵌套调用不重复占槽 |

- 限流/配额组件故障均 fail-open；日费用熔断与 TPM 的硬预算口径以 `usage_events` 为准。

## 4. 安全与性能

| 风险层 | 隔离/授权 | 性能策略 | 失败默认 |
|---|---|---|---|
| 项目读 | 项目只读租约、根白名单 | 元数据缓存、按需分块 | 明确报错 |
| 项目写 | 写租约 + hash 冲突 + 设备哈希校验；项目所有者可授予/收回；租户总闸与可写项目数上限 | 批处理、乐观并发、异步审计 | 冲突不覆盖 |
| 代码执行 | Windows 低完整性/Job Object；Linux bwrap/gVisor 为目标，当前仅 POSIX setrlimit；租户并发槽 | 暖池/会话复用、CPU/内存/时长硬限 | 超时杀进程；隔离机制不可用时拒绝执行 |
| 网络/安装 | 默认拒绝 + 域名白名单代理 + 凭证注入 | 连接复用 | 拒绝并给申请路径 |
| MCP/插件 | 独立沙箱、固定版本、默认无网 | 常驻连接 | 单工具失败隔离 |

原则：授权在边界做一次；写/网络 fail-closed；读/状态 fail-fast；不因性能关掉越界检查。
- 设备写审计：授予/收回 `device_write_granted/revoked`；回写 `device_write_synced`（仅数量，不含路径）。
- 数据导出仅平台 owner；排除密码/密钥哈希、邀请令牌、`system_settings` 与二进制原文。

## 5. 团队服务模式（Web）

- 浏览器打开 `https://<域名>`，同源 SPA + `/v1` + SSE；无注册页，账号由 owner/admin 创建。
- 账号闭环：一次性初始密码 → 首登强制改密 → 重置/禁用/启用/软删除/强制下线；管理动作审计。
- 邀请：后台可配 SMTP/晴辰邮件通道（密钥加密），邀请时自动发信；未配置/失败不撤销邀请，仍返回链接与令牌；支持测试发信。
- 登录安全：失败锁定（Redis 5 次/15 分钟）、限流、统一错误文案；改密/禁用写会话纪元使旧 token 失效。
- 角色：owner / admin / user；平台诊断、跨租户监控、邮件配置、数据导出仅 owner。
- 部署：TLS 反代（SSE 关缓冲）、`PUBLIC_BASE_URL`、`COOKIE_SECURE`、CORS 白名单；详见 `docs/team-domain-deployment.md`。
- team 模式隐藏/拒绝本机隔离档位、UAC 助手、内嵌数据栈诊断。

## 6. 运行开关与部署

| 变量 | 默认 | 作用 |
|---|---|---|
| `CHIRON_DEPLOY_MODE` | `desktop` | `team` 强制无注册/无免口令，隐藏桌面能力 |
| `CHIRON_SERVICE_MODE` | 0 | 后台「服务」页开关的部署级等价物 |
| `CHIRON_ENGINE_POOL_SIZE` | 1 | 引擎池 1–4 |
| `CHIRON_ENGINE_IDLE_SECONDS` | 1800 | 池副本空闲自停 |
| `AGENT_MAX_CONCURRENCY` / `_PER_USER` | 20 / 2 | 同时运行回合（全局/每用户） |
| `AGENT_RUN_MAX_TOKENS` | 400000 | 单 run token 预算 |
| `CHIRON_DEVICE_WRITE_ENABLED` | 1 | 租户级写回总闸；关闭时所有 client 项目只读 |
| `CHIRON_MIRROR_ROOT` | 临时目录 | client 项目镜像根 |
| `GATEWAY_MODEL_*` / `SANDBOX_MAX_CONCURRENCY_PER_TENANT` | 0 | 模型/沙箱配额（亦可在后台 python 分类配置） |

部署：新构建自动 `alembic upgrade head`；源码团队主机先手动执行。team 模式要求 HTTPS、强 `APP_SECRET/JWT_SECRET`、精确 CORS。团队主机继续随桌面程序启动（不做 headless）。

## 7. 已定决策

- rclone/WinFsp 只在服务端按需获取（固定版本+SHA256），不进安装包；R2 实测通过后切换挂载并删除镜像。
- R5：项目级租约（项目所有者授予/收回）+ 租户总闸 + 租户可写项目数上限（后台仅平台 owner 配置）。
- client git 完全允许；项目换设备重新授权，不做内容迁移。
- 数据软删除 + 默认保留 90 天；client 文件默认不落服务端。
- LiteLLM 暂不引入；Langfuse 暂缓实接（需要时经 OTel Collector）；S5 暂缓；租户删除暂缓。

## 8. 剩余工作

统一入口 `scripts/team_acceptance.py` 串联下列可自动部分，输出 JSON + 人类摘要；
没有真实主机/挂载点时对应阶段明确 SKIP（退出码 77），**不把 SKIP 记成 PASS**。

```bat
set CHIRON_OWNER_PASSWORD=<owner 密码>
python scripts/team_acceptance.py --base-url https://<域名> --email owner@<公司> ^
    --r2-root R:\project --sse --strict-web ^
    --json-out .artifacts/team-acceptance.json
```

退出码：0=无 FAIL 且至少一项 PASS；1=有 FAIL；77=全部 SKIP（缺主机/挂载点/凭据）。

1. **R2 实测定案**：团队主机安装 rclone+WinFsp，运行 `scripts/r2_mount_bench.py`
   （或验收入口的 `--r2-root`），通过则切挂载、删镜像。门禁 id：
   `r2.read_p95`（≤50ms）、`r2.write_p95`（≤150ms）、`r2.git`（`rev-parse
   --is-inside-work-tree` 必须为 true；`--skip-git` 只记 SKIP）、`r2.io_errors`、
   `r2.rename_delete`。仍待人工：fs/git/run_code 全链路、业务侧 rename/delete 生效、
   10 分钟稳定性。
2. **团队 Web E2E**：真实域名/TLS 下登录、首登改密、邀请一次性、强制下线、SSE 首
   token/`turn_done`、匿名分享页（数据面 + `/s/<token>` HTML）。`scripts/team_smoke.py`
   可单独跑；`--sse` 产生消息后才会判定分享页，没有消息时是 SKIP。门禁 id 见
   `team_acceptance.py` 头部（`web.login` / `web.must_change_password` /
   `web.first_login_password_change` / `web.invite` / `web.force_logout` /
   `web.share_anonymous` / `web.share_page_html` / `web.sse_first_token` 等）。
3. **R6 压测**：P=2/4，P95 首 token、逐引擎 RSS、空闲回收；核对 `/metrics` 的引擎池
   指标。已有结果时由验收入口 `--r6-report` 摄入，报告形状：
   `{"metrics_checked": true, "runs": [{"pool": 2, "first_token_p95_ms": 900,
   "engine_rss_mb": [320, 318], "idle_reclaimed": true}, {"pool": 4, …}]}`。
   缺 P=2/4、缺指标或未确认空闲回收均 FAIL；首 token 阈值可选
   `--r6-first-token-p95-ms`（PRD 暂未给硬指标，不设则只记录）。
4. 暂缓项：LiteLLM、Langfuse 实接、租户删除、S5、企业权限的更多维度（按需）。

## 9. 后端、前端与桌面壳安全加固（H 系列）

- 对照基线：Reasonix 2.61.0；核验日期 2026-11-24。后端按 Go/Python 全生产代码覆盖，前端按 Vue renderer、Electron 主/预加载进程与构建交付链覆盖。
- 目标不是复制本地单用户 Host 架构，而是补齐 Chiron 多租户服务在入口授权、执行隔离、故障接管和跨语言契约上的缺口。
- 优先级：P0 = 对外服务前必须收口；P1 = 可靠性门禁；P2 = 契约与工程化；P3 = 按产品目标排期。
- 所有 H 项默认“待实现”或“待加固”；已有部分能力不等于满足验收。

### 9.1 P0：安全边界

| ID | 需求 | 现状/证据 | 验收标准 |
|---|---|---|---|
| H2 | **Linux 内核级执行隔离** | 非 Windows 当前直接 `Level.NONE`，实际仅 `RLIMIT_*`；独立 sandbox service 复用同一实现。证据：`python-engine/app/isolation/__init__.py:297`、`python-engine/app/tools/sandbox.py:738` | 默认无网络；工作区外文件系统只读；不能读取引擎凭证；进程树可回收；bwrap/gVisor/容器机制不可用时拒绝执行，不能降级为裸 `setrlimit`。 |

### 9.2 P1：执行可靠性与失败语义

| ID | 需求 | 现状/证据 | 验收标准 |
|---|---|---|---|
| H7 | **队列 exclusive claim 与 attempt fencing** | `running` 可被再次 claim，complete/fail 不按 attempt 隔离，XADD/XACK 非原子。证据：`python-engine/app/queue/idempotency.py:27`、`worker.py:602` | 同一任务并发 claim 仅一个成功；旧 attempt 的 complete/fail/ack 不生效；重复投递副作用幂等；retry 保留 idempotency key、deadline、trace、priority 等元数据。 |
| H11 | **Provider clean-attempt 与终态校验** | malformed SSE 可跳过；流结束无 completed/tool calls 时可能无终态；partial content 可掩盖 error。证据：`python-engine/app/providers/responses.py:248`、`agent/runtime.py:3196` | 每个 attempt 有身份与合法终态；截断/取消/失败不得提交为 clean turn；partial attempt 不执行后续工具；重试分类且有上限、可观测。 |

### 9.3 P2：状态、契约与协议

| ID | 需求 | 现状/证据 | 验收标准 |
|---|---|---|---|
| H14 | **状态事务与 CAS** | taskboard 无 CAS 且 child merge 重编号；messages 无 revision；部分消息/checkpoint 写入非原子。证据：`python-engine/app/agent/taskboard.py:113`、`python-engine/app/session_store.py:175`、`internal/session/manager_messages.go:604`、`internal/api/project_checkpoints.go:300` | 消息写入事务化；checkpoint 主记录与 manifest 原子提交；taskboard 使用 revision CAS；todo ID 稳定，`depends_on` 不因 child merge 失效。 |
| H15 | **压缩投影 lineage 与 prefix hash** | 已有 seq/suffix/savings 校验，但未绑定 provider/model lineage 与 covered-prefix hash。证据：`python-engine/app/agent/compaction.py:1147` | 摘要记录模型、上下文窗口、provider 和 prefix digest；lineage 或前缀变化时拒绝安装；projection 安装使用 CAS。 |
| H16 | **版本化 Go/Python 契约** | 跨语言边界较多依赖 `map[string]any` 与约定字段 | 为 submit/run event、tool call/approval、checkpoint、queue payload、MCP config、error/terminal state 建立版本化 schema 与双端 conformance tests；不兼容变更必须升版本。 |
| H17 | **MCP 请求生命周期** | 固定协议版本；stdio 假定“下一行”就是当前响应，未按 ID demultiplex；缺少 initialized 通知。证据：`python-engine/app/mcp/client.py:228` | 并发 notification/progress/sampling 不错配；initialize 后发送 initialized；支持版本协商；取消与超时可传播；破坏性 annotation 继续 fail-closed。 |

### 9.4 P3：按产品目标推进

| ID | 需求 | 验收标准 |
|---|---|---|
| H19 | **ACP 完整 Agent 协议** | 声明 capabilities；new_session 绑定 workspace/cwd；load_session 可回放；支持附件、tool updates、permission ask；保留现有真实 cancel。若编辑器集成非当前目标，则明确标为暂缓。 |
| H20 | **Provider 密钥加密存储** | Redis 不保存明文 key；支持可配置的 KMS/Secret Manager 或信封加密后端；密钥轮换、version 标记、访问审计齐全；env 仅作兜底。 |
| H21 | **关键文档 tracked 化** | 将安全契约、部署拓扑和设计决策从缺失/ignored 的 `vendor/*.md` 迁入 tracked `docs/`；文档链接检查进入 CI。 |
| H22 | **只读子 Agent 强隔离（按需）** | 若产品承诺严格只读，使用动态 capability 与 host-observed mutation receipts；共享 workspace 的 diff 不归因到具体 child；否则明确只保证工具级只读。 |

### 9.5 里程碑与退出条件

| 里程碑 | 范围 | 退出条件 |
|---|---|---|
| M-H0 对外安全门禁 | H2、H24、H45–H46、H76 | P0 自动化测试全部通过；Linux 无隔离时明确拒绝执行；跨用户资源访问、技能路径穿越、插件/Git 执行绕过、更新真实性、设备桥越权与凭据跨源复用均有回归测试。 |
| M-H1 故障接管门禁 | H7、H11、H36、H38、H41、H47–H48、H50–H51、H65、H67、H73 | 故障注入下无双重副作用；stale worker 无写权；预算、审批、提权 helper、renderer 权限、附件/账户切换与交付链失败语义通过测试。 |
| M-H2 契约与工程门禁 | H14–H17、H44、H53–H54、H56、H60、H62、H70–H72、H82–H83 | 状态一致性测试、schema conformance、MCP 并发测试通过；运维验收、数据生命周期、流恢复、缓存、管理权限呈现与构建可复现性证据不得假成功。 |
| M-H3 产品化增强 | H19–H22、H57、H64 | 按确认后的产品目标逐项验收；非目标项保持显式暂缓。 |

### 9.6 待确认决策

1. **H2 Linux 隔离形态**：推荐 bwrap 作为通用基线，gVisor/独立容器作为高安全团队部署选项；需确认团队主机是否允许 user namespace、容器运行时和默认无网。
2. **H1 公共工具 API**：推荐删除公共 `/v1/tools/execute`，仅保留内部入口；若有前端直接调用场景，需列出调用方并统一审批语义。
3. **H5 市场信任策略**：推荐外部 source 默认关闭，完成 digest + 签名后再启用；需确认签名是否对所有外部 source 强制，还是仅对非平台官方 source 强制。
4. **H12 配置恢复边界**：推荐认证、数据库、Redis、迁移头等关键配置 fail-closed；仅展示偏好、主题等可恢复配置使用 LKG。
5. **H19/H22 产品承诺**：需确认 ACP 完整编辑器集成、严格只读子 Agent 是否属于当前版本目标；否则保持 P3 暂缓，避免阻塞 M-H0/M-H1。
6. **H20 密钥后端**：推荐后台可配置的 KMS/信封加密 Provider，env 只兜底；需确认首个落地环境是本地 Windows、团队 Linux，还是两者都要。

### 9.7 全覆盖复核新增项（H23–H44）

- 2026-11-24 对 657 个生产 Go/Python 文件（约 19.4 万行）做全覆盖复核后新增；以下均为待实现/待加固，不代表已有能力。
- 证据分为“已直接确认代码事实”和“特定部署/并发条件下触发”；验收必须覆盖正常路径与失败路径。

| ID | 优先级 | 需求 | 现状/证据 | 验收标准 |
|---|---|---|---|---|
| H24 | P0 | **插件配置与执行授权一致** | Go 仅在 `/test` 检查 `commands_enabled + approved`；Python `active_servers()` 只看 `status=active`，`read_only=true` 直连并启动命令；broker 加载全部配置，Go 授权入口只做通用工具分级。证据：`internal/api/plugin_validation.go:129`、`internal/api/plugin_handler.go:329`、`:334`、`:411`、`python-engine/app/plugins/store.py:96`、`plugins/pool.py:360`、`:395`、`python-engine/mcp_broker.py:111`、`internal/api/tool_broker.go:114` | 任一执行路径都验证用户总开关、每插件授权和规范化配置摘要；改 Args/Env/ReadOnly 使旧授权失效；未经授权的 active 配置不得启动进程、建立连接、注册工具或经 broker 调用；撤销授权立即断开既有连接。 |
| H36 | P1 | **预算、key 重试与 Provider 绑定原子化** | 预算 check 与 deduct 之间存在竞态；403 一律按 key-scoped 冷却；base_url 变化可能沿用旧 key。证据：`python-engine/app/gateway/budget.py:43`、`:53`、`:62`、`gateway/key_retry.py:42`、`:101`、`gateway/router.py:337` | 预算使用 Lua/事务预占与释放；403 先区分配额、模型无权限和凭证失效；key 绑定 provider/base_url/账号域；重试预算不被 SDK 内部重试放大。 |
| H38 | P1 | **子 Agent 与撤销操作复用完整执行链** | 生产子 Agent 直接 `registry.execute`，绕过 ToolGuard/Broker/审批/undo/side-effect ledger；undo 存在 LPOP 后失败、二进制替换与 TOCTOU 风险。证据：`python-engine/app/agent/multi_agent.py:275`、`agent/undo.py` | 子 Agent 使用与主 Agent 相同的授权、审计和副作用账本；undo 先验证快照/哈希/路径，失败可恢复且不损坏二进制。 |
| H41 | P1 | **提权 helper 与子进程资源边界** | 一次性 helper 顺序读取 stdout/stderr，可因管道写满死锁；源码部署且引擎根可写时，UAC helper 从该 cwd 加载 `app`，形成条件性本地提权。证据：`python-engine/app/isolation/win_launch.py:877`、`:1253`、`isolation/elevated_client.py:415`、`:443` | 两根管道并发排空并限制总量；提权代码只从 ACL 受保护安装目录加载；helper 启动验证可执行文件/模块路径与签名，拒绝用户可写 cwd。 |
| H44 | P2 | **审计、导出、留存与引用完整性** | PG 审计失败仍 ACK、通道满丢弃；owner 全表导出无上限；内部用量 DELETE+INSERT 非原子；PageIndex 无前缀引用 fail-open；no_retention 仍可能写 PG。证据：`internal/db/audit*.go`、`internal/api/admin_export.go`、`internal/api/unified_usage.go`、`pageindex-service/app/adapter.py:78`、`:80`、`python-engine/app/memory/**` | 审计写入与 ACK 建立明确事务/outbox 语义；导出有行/字节/并发上限；用量与留存操作原子且可恢复；引用无法解析时拒绝而非放行；no_retention 全链路不落盘。 |

### 9.8 前端与桌面壳新增项（H45–H57）

- 2026-11-24 对 Vue renderer、Electron 主/预加载进程、API 客户端与构建交付链复核后新增；生成资产、lockfile 与第三方 WASM 不作为逐行审计对象，但在供应链入口核对。
- 本地模式与云端模式分开判定：设备桥、远程 API 地址等问题只在对应部署路径触发；更新与安装链按对外交付门禁处理。

| ID | 优先级 | 需求 | 现状/证据 | 验收标准 |
|---|---|---|---|---|
| H45 | P0 | **更新清单与安装器真实性门禁** | 清单允许空 `sha512`，主进程仅在摘要非空时校验，随后直接启动安装器；缓存包无摘要也会再次启动，且无发布者签名。证据：`frontend-vue/electron/update-check.cjs:44`、`:47`、`frontend-vue/electron/main.cjs:2183`、`:2235`、`:2304` | 版本、架构、文件名、大小、摘要必须通过严格 schema；缺摘要立即拒绝；使用内嵌公钥验证签名清单与安装器，缓存包启动前重新验证；重定向仅允许发布域名；state 版本/路径做 semver 与安全组件校验。 |
| H46 | P0 | **云端设备桥能力、根目录与设备身份授权** | **客户端已收紧**（`electron/approved-roots.cjs` 用户批准根 + `device-identity.cjs` Ed25519 设备密钥 + `device-fs.cjs` realpath-only/op 白名单/git 白名单与最小环境/短时票据换取并禁止回退 query token、4401-4403 停止重连）；未授权 root 与 junction 逃逸实测由 ACCEPTED 变 REJECTED。**服务端未实现**：`internal/api/device_fs.go` 仍只要求非空 `device_id`、仍接受 query token，没有 `POST /v1/device-fs/ticket` 端点与设备签名校验。因此桥当前 **fail-closed**（远程项目文件不可用），这是预期而不是缺陷。 | 主进程持有用户批准的 canonical root grant ✅；realpath-only 且拒绝 symlink/junction 逃逸 ✅；操作与 Git 子命令/选项白名单化、最小环境并禁用 aliases/hooks/filters/helpers ✅；设备身份需证明归属（**服务端待实现**签名校验）；使用短时一次性连接票据而非 query token（**服务端待实现**票据端点）；撤销与 HTTP 一致（客户端已对齐，服务端待实现 4401/4403 同语义）。 |
| H47 | P1 | **Electron renderer 权限最小化** | `sandbox:false`，preload 暴露 token、模式切换、重启、清空与打开路径等能力；IPC 仅按页面 URL 判定，setup/error 页也使用同一 preload。证据：`frontend-vue/electron/main.cjs:321`、`:395`、`:1806`、`frontend-vue/electron/preload.cjs:68`、`:204` | 启用 sandbox 或拆分窄 preload；校验精确 `event.sender` 与 mainFrame；按窗口/能力授权；setup/error 不加载特权 preload；保留现有 CSP、导航与 window-open 限制并加入回归测试。 |
| H48 | P1 | **敏感操作、第二因素 fail-closed 与秘密清理** | 非 Windows 清空数据直接返回验证成功；2FA 密文不可读时按未启用放行；注册弹窗取消不清 secret/QR，恢复码弹窗用默认关闭路径也不清数组。证据：`frontend-vue/electron/main.cjs:1691`、`:2786`、`:2963`、`frontend-vue/electron/two-factor.cjs:124`、`:269`、`frontend-vue/src/components/settings/TwoFactorPanel.vue:125`、`:345`、`:362`、`:392` | 不支持原生重认证的平台不得执行破坏性操作；2FA 状态不可读时阻止解锁并提供显式恢复/重置流程；所有降级必须可观测，不得静默跳过安全因素；取消、关闭、成功、失败与卸载都清空 secret/QR/TOTP token/recovery codes/password 等敏感内存值。 |
| H50 | P1 | **远程连接传输安全** | 配置接受任意 HTTP(S) 地址，API 客户端向其附加 Bearer token，CSP 也会放行该 origin。证据：`frontend-vue/electron/config.cjs:56`、`frontend-vue/src/api/index.ts:172`、`frontend-vue/electron/main.cjs:338` | 非 loopback 地址强制 HTTPS；开发/测试需显式不安全开关；禁止向 HTTP/WS 发送 token、Cookie 或私有内容；配置保存与连接建立双重校验。 |
| H51 | P1 | **运行时与组件供应链完整性** | `uv pip install` 未使用 hash lock，core 依赖为版本范围并自动尝试第三方镜像；组件下载允许 HTTP，摘要来自同一来源而非签名信任根。证据：`frontend-vue/electron/runtime-install.cjs:762`、`:793`、`:1023`、`desktop-local/pyproject.toml:11`、`desktop-local/app/components/manager.py:497`、`:598`、`:973` | 完全锁定依赖与 artifact hashes；优先签名离线包/ wheels；组件 manifest 使用内嵌公钥签名；仅允许 HTTPS 与批准主机，不静默切换第三方镜像；安装后与执行前再次校验。 |
| H53 | P2 | **清空数据的范围与残留治理** | 清空只覆盖渲染存储、token 与 `userData/local`，`secrets.bin`、更新状态/下载、配置、runtime state、device id 仍保留。证据：`frontend-vue/electron/wipe-data.cjs:60`、`:90` | 明确“清空会话数据”和“恢复出厂”两种范围；后者覆盖全部敏感残留并验证结果；前者 UI 不得宣称全部本机数据已删除。 |
| H54 | P2 | **本机栈身份挑战** | 仅凭 loopback `/ready` 返回 PostgreSQL/Redis up 就认定现有栈属于 Chiron，无实例密钥、版本或数据目录绑定。证据：`frontend-vue/electron/runtime-state.cjs:102`、`:114`、`:120` | 使用每数据目录随机 secret 的挑战响应，并校验实例 ID/协议版本；未知服务不得附加、配置或发送凭据。 |
| H56 | P2 | **Windows 子进程树终止** | 运行时安装与设备 Git 超时只 kill 直接子进程，Windows 后代进程可能继续运行。证据：`frontend-vue/electron/runtime-install.cjs:660`、`frontend-vue/electron/device-fs.cjs:335` | 使用 Job Object 或 `taskkill /T` 终止完整进程树；超时/取消后验证无残留，资源上限与审计一致。 |
| H57 | P3 | **更新渠道与版本语义** | 版本比较丢弃 prerelease/build 元数据，按数字最大值选版本，无稳定/预发布策略、撤回与 pin 语义。证据：`frontend-vue/electron/update-check.cjs:52`、`:80` | 实现完整 semver 优先级、channel/withdrawal/pin 策略；降级与回滚必须来自新鲜可信清单，不得自动重装被 pin 的版本。 |

### 9.9 聊天、审批与流式恢复新增项（H58–H64）

| ID | 优先级 | 需求 | 现状/证据 | 验收标准 |
|---|---|---|---|---|
| H60 | P2 | **重连、历史恢复与异步竞态守卫** | 重连仅 `/health` 后调用 `switchSession(当前 id)`，同 ID 直接返回；模型/上下文、收件箱、子 Agent、工具输出、工作台与媒体/KB 预览缺少 await 后 generation 校验。证据：`frontend-vue/src/views/ChatView.vue:1872`、`:2066`、`:2115`、`frontend-vue/src/components/chat/InteractionInbox.vue:46`、`frontend-vue/src/components/chat/SubAgentPanel.vue:619`、`frontend-vue/src/components/chat/ToolResultBlock.vue:239`、`frontend-vue/src/components/workbench/WorkbenchPanel.vue:523`、`frontend-vue/src/views/MediaView.vue:318`、`frontend-vue/src/components/KBDocumentPreview.vue:114` | 显式重开当前流、带游标补发并重拉历史；所有异步写回捕获 session/file/run 与 request generation，stale 响应和 finally 状态不得覆盖新视图；关闭/切换时 Abort 并回收 object URL。 |
| H62 | P2 | **实时输出、错误与日志脱敏** | Provider 错误原文直接显示；子 Agent 审批参数、工具参数与结果实时发射未脱敏；Axios 错误整体进 console，可能含 Authorization 头。证据：`frontend-vue/src/views/ChatView.vue:2583`、`python-engine/app/agent/runtime.py:3196`、`python-engine/app/agent/event_sink.py:290`、`:364`、`:401`、`frontend-vue/src/composables/useSessionSwitch.ts:216` | 发射、显示、持久化与日志前统一脱敏；仅记录 status/code/request ID；明文查看需独立权限与显式揭示；测试覆盖 header、key、token、连接串。 |
| H64 | P3 | **附件 URL 来源与协议约束** | `attachmentUrl()` 直接返回解析或持久化 URL 并进入图片/音频/文件绑定；未确认攻击者可控来源。证据：`frontend-vue/src/components/chat/MessageItem.vue:305`、`:846`、`frontend-vue/src/views/ChatView.vue:1912` | 仅允许明确 scheme/origin；记录附件来源；导出 Markdown 不携带签名 URL；新协议或跨域来源需显式策略。 |

### 9.10 API 契约、资源身份与项目操作新增项（H65–H86）

- 本节是前端契约复核与内容/项目视图交叉审计的直接确认项；部署条件与并发条件单独写入现状，避免把条件性风险表述为必然利用。
- 后端新发现的技能路径、私有知识库与会话画布问题重新纳入 M-H0；它们不是前端权限隐藏可以修复的问题。
- H79–H86 为 2026-11-24 补充复核的直接确认项；更新清单缺摘要不重复编号，继续归入 H45。

| ID | 优先级 | 需求 | 现状/证据 | 验收标准 |
|---|---|---|---|---|
| H65 | P1 | **聊天附件上传响应契约** | `uploadFile()` 从 `resp.data.asset` 或 `resp.data` 取字段，但 Go `OK` 返回 `{success,data}`；真实 ID/URL 丢失后伪造 `Date.now()` ID 与 `/v1/media/{id}/download`。证据：`frontend-vue/src/api/index.ts:718`、`:720`、`:722`、`frontend-vue/src/components/chat/ChatInput.vue:800`、`internal/api/media_upload.go:125`、`internal/api/response.go:63` | 严格按 `{success,data}` 解包并做 schema 校验；缺 ID/file_url 立即失败，不得生成伪 ID；上传后附件可签名、下载并随消息发送；本地/S3、≤50MB 与分片路径均有契约测试。 |
| H67 | P1 | **私密媒体强制签名 URL 且缓存按账号隔离** | S3 `file_url` 是裸对象 URL，`resolveMediaUrl()` 对非 `/media/` 前缀直接返回，跳过 owner-bound 签名与过期；公开桶时绕过授权语义，私有桶时图片失败。签名缓存仅以 asset ID 为键。证据：`frontend-vue/src/api/index.ts:1032`、`:1044`、`:1046`、`:1064`、`internal/api/media_crud.go:729`、`internal/storage/s3.go:184`、`frontend-vue/src/views/MediaView.vue:178` | 私密存储禁止把裸 S3 URL 交给 renderer；所有资产经服务端 owner-bound 签名或受控代理；缓存键包含 tenant/user/mode/origin 并在登出清空；私有桶与公开桶配置均有回归测试。 |
| H70 | P2 | **媒体签名过期后重新签名** | 图片签名过期时清缓存并加入 `fallbackIds`，之后直接回退原路径，不再尝试签名。证据：`frontend-vue/src/views/MediaView.vue:187`、`:207`、`:212` | 区分过期、401/403 与永久 404；过期时限频重新签名并重试，不降级到裸路径；重复失败有可见状态且不形成永久黑洞。 |
| H71 | P2 | **请求取消键与生命周期精确化** | 跟踪键仅 `method:url`，不含 params/body/request ID，并覆盖调用方 CancelToken；同 URL 不同参数并发会互相覆盖，首个响应可能删除全部跟踪。证据：`frontend-vue/src/utils/requestOptimizer.ts:166`、`:169`、`:177`、`:183`、`frontend-vue/src/router/guard.ts:34` | 每个请求有独立 ID/AbortSignal；取消只作用于目标视图/资源代次，不误伤同端点并发操作；完成、失败与取消都只清理自身；导航取消测试覆盖不同 params。 |
| H72 | P2 | **上传续传元数据按账号隔离并随登出清理** | `chunk-upload:*` 记录文件名、大小、时间、用途、父级和 uploadId，但未纳入 `privateStorage` 前缀，登出后仍可被下一账号读取并尝试续传。证据：`frontend-vue/src/utils/uploader.ts:57`、`:58`、`:80`、`frontend-vue/src/utils/privateStorage.ts:15`、`:36` | 续传键包含 tenant/user/mode/origin 与文件摘要；登出/切换账号清除所有旧账号续传状态；服务端每次读/写/complete 都复核 upload owner，避免元数据残留升级为跨账号上传。 |
| H73 | P1 | **破坏性预览绑定不可变目标** | 前端已修：预览计划携带不可变 workspace/generation/摘要，确认执行只按该计划落点，切换项目使确认失效（`frontend-vue/src/utils/checkpointGcPlan.ts`、`CheckpointTimeline.vue`、`api/checkpoints.ts` 已带 `plan_digest`）。**未做**：`internal/api/checkpoint_diff.go` 的 `checkpointGcBody` 仍不接收/校验 `plan_digest`，引擎也没有下发"服务端签发的计划摘要"，因此服务端一致校验这一条还不成立。 | 预览计划携带不可变 workspace、generation 与计划摘要；确认执行必须绑定该计划，切换项目使确认失效；**服务端**校验计划/目标一致（dry-run 下发摘要、执行回传并重算比对，不一致 409），破坏性操作不得按当前 UI 状态重新选目标。 |
| H76 | P0 | **技能名路径穿越与共享层越权写入** | install 只检查 name 非空；`_path()` 直接拼接 `{name}.skill.json`，`save()` 创建父目录并原子替换，`../`/绝对路径可覆盖存储目录外或其他用户技能。`update_meta()` 还会就地修改共享层载体。证据：`python-engine/app/api/skills.py:172`、`:183`、`:195`、`python-engine/app/skill/store.py:406`、`:463`、`:469`、`:494`、`:545` | 所有 save/delete/meta/update 入口统一严格 name 校验（白名单、长度、拒绝分隔符/绝对路径）并验证 resolved containment；tenant 共享写入要求 tenant-admin；成员修改共享技能只生成私有 override；跨用户/越界探针不得落盘。 |
| H82 | P1 | **缩短数据保留期需破坏性确认** | 保留期表单保存前不比较新旧值，任何天数下降都直接写入配置；后台清理在后续周期按新阈值删除 usage、分享与检查点，UI 没有展示影响范围、生效时间或不可逆确认。证据：`frontend-vue/src/views/admin/MonitorView.vue:104`、`:109`、`internal/api/admin_extras.go:563`、`internal/api/retention.go:520`、`internal/api/routes_admin.go:170` | 任一保留天数下降时展示旧/新阈值、受影响数据类型、预计生效周期并二次确认；取消不得写配置；服务端记录操作者与变更审计，测试覆盖仅缩短、混合变更与失败回滚。 |
| H83 | P2 | **租户 Admin 可见平台 Owner 专属后台入口** | 路由与守卫允许 `admin/owner` 进入 `/admin`，侧边栏无条件展示监控、Provider 与依赖管理等页面；相应接口使用 `RequirePlatformOwner`，租户 admin 点击后得到 403。后端已阻止越权，但导航承诺与实际权限不一致。证据：`frontend-vue/src/router/index.ts:186`、`:201`、`:209`、`:309`、`frontend-vue/src/router/guard.ts:112`、`frontend-vue/src/views/admin/Layout.vue:101`、`internal/api/routes_admin.go:164`、`:184`、`internal/api/deps_admin.go:587` | 路由与菜单按服务端 capability 区分 tenant-admin 与 platform-owner；无权限入口隐藏或禁用，直接访问给出明确的未授权页面且不发起无权请求；两种角色均有导航与接口契约测试。 |

### 9.11 易推进项实施状态（2026-11-24）

- 以下 6 项仅完成局部修复，不关闭 H 条目，也不改变 M-H0/M-H1/M-H2 的退出条件。
- 实现以工作区代码和自动化回归为准；未执行 Electron GUI、真实媒体存储或团队部署的端到端验收。
- 最终门禁：前端 177 个测试文件、1803 项测试通过；Go 全量测试、PostgreSQL 首设密码/重启/登录实测、认证 store 31 项与路由守卫 21 项、CI 退出码故障注入 8 项、`vue-tsc -b --force --noEmit`、`check:ui`、生产构建、变更文件 ESLint（0 error，2 个既有 `timer`/`pauseAllUploads` warning）、Python `pytest -k skill` 177 项、Ruff 与 compileall 通过。浏览器实测覆盖 Go + PostgreSQL + Redis 链路的首次设置密码、改密后状态及 Provider Key 预览；Python 测试环境注入非空 `JWT_SECRET` 与 `INTERNAL_TOKEN`，模拟真实网关认证。

| ID | 已完成切片 | 回归证据 | 尚未满足的整项验收条件 |
|---|---|---|---|
| H48 | `TwoFactorPanel` 在弹窗关闭、取消、成功、失败、卸载及迟到异步返回时清理 secret/QR/TOTP token/recovery codes/password；确认期间禁止关闭，防止迟到成功丢失一次性恢复码。证据：`frontend-vue/src/components/settings/TwoFactorPanel.vue:63`、`:66`、`:84`、`:118`、`:160`、`:350` | `TwoFactorPanel.spec.ts` 11 项通过，覆盖重新注册、取消、默认关闭、错误密码、异步代次与确认期间关闭。 | 非 Windows 原生重认证 fail-closed；2FA 密文不可读时阻止解锁并显式恢复/重置；降级可观测。 |
| H65 | 普通上传严格解包 `{success,data}`；分片上传要求真实 `asset_id/file_url`，拒绝缺失、错误 envelope 与仅含 `upload_id` 的完成响应，不再伪造 ID/URL。证据：`frontend-vue/src/api/index.ts:693`、`:703`、`:722`、`internal/api/uploads.go:413` | `mediaUpload.spec.ts` 9 项通过，覆盖普通/分片正常与畸形响应。 | 真实本地/S3、≤50MB 与分片上传后的签名、下载、随消息发送 E2E；完整响应 schema 与错误映射。 |
| H70 | 签名缓存按服务端 `exp` 提前 30 秒失效；MediaView 对过期链接限频重签一次，404/永久失败不再回退已移除的公开路径。证据：`frontend-vue/src/api/index.ts:1039`、`:1072`、`frontend-vue/src/views/MediaView.vue:197`、`:208` | `mediaSignLifecycle.spec.ts` 3 项与 `MediaView.smoke.spec.ts` 9 项通过。 | 缓存按 tenant/user/mode/origin 隔离；重复失败可见状态与手动重试；其他媒体消费方及 S3 路径统一治理。 |
| H71 | pending request 改用递增 ID 与精确 config 身份，保留调用方 CancelToken，完成/取消时以原 listener 真正退订，响应/失败只清理自身，导航取消保留认证会话请求。证据：`frontend-vue/src/utils/requestOptimizer.ts:27`、`:58`、`:74`、`:86`、`:105`、`:177` | `requestOptimizer.spec.ts` 9 项通过，覆盖同 URL 并发、调用方取消/退订、真实 Axios token 与 retained auth。 | 取消仍需从 URL 粒度细化到目标视图/资源代次；按验收要求补齐 AbortSignal 或等价语义及不同 params 的导航测试。 |
| H72 | 续传键经 `privateKey` 绑定当前 user，`chunk-upload` 前缀纳入登出清理，旧无账号键也会清除。证据：`frontend-vue/src/utils/uploader.ts:67`、`frontend-vue/src/utils/privateStorage.ts:23` | `uploader.spec.ts` 5 项与 `privateStorage.spec.ts` 3 项通过，覆盖账号切换和旧键清理。 | 键加入 tenant/mode/origin 与内容摘要；服务端查询、写入和 complete 每次复核 upload owner。 |
| H76 | 共享严格技能名白名单与 resolved containment 覆盖保存、目录、删除、meta 与 API 入口；非法名/越界返回 HTTP 400。原子写使用同目录安全临时文件，`SKILL.md` 自身符号链接也会被拒绝。证据：`python-engine/app/skill/store.py:128`、`:149`、`:156`、`:439`、`:513`、`:590`、`python-engine/app/api/skills.py:55`、`:62` | `test_skill_store_validation.py` 40 项通过，技能相关 Python 回归 177 项通过；含 flat/directory/`SKILL.md` symlink 越界探针。 | tenant 共享写入要求 tenant-admin；成员修改共享技能只生成私有 override；补齐共享层授权与跨用户探针。 |

### 9.12 第二轮加固验收（2026-11-24，已从待办表移除）

- 下表 22 项在本轮按"行为改造 + 自动化回归"完成，且**已从 9.1–9.10 的待办表删除**（编号空洞保留，其它问题不重排）。
- 判定口径：每条都有能证明"修复前会失败"的回归用例；没有真实主机的项（S3/PG/团队域名/Electron GUI）不计入本条验收，另行留在 §8。
- 合并后统一门禁：`go build ./...`、`go test ./... -parallel 1`（全包通过）、`python -m pytest` 相关子集 374 项（`test_*` 与 `tests/memory` 邻域）、前端 15 个 spec/133 项、`pnpm run check:ui`、`vue-tsc -b --noEmit`、`vite build --mode desktop`、`gofmt`/`ruff`/变更文件 ESLint（0 error）。

| ID | 落点 | 回归证据 |
|---|---|---|
| H1 | 公共 `/v1/tools/execute` 在网关与引擎两侧都已移除；`tools/client.py` 中的同名调用是死代码 | `internal/api/tool_broker_test.go`（POST 得到 404）、`python-engine/tests/test_tools_service_api.py`（`registry.execute` 未被 await） |
| H3 | `agent/tool_policy.py` 复用沙箱文本逃逸判据并按平台解析，复合命令一律升级审批 | `tests/test_tool_policy_command_parse.py` |
| H8 | `agent_submit` 的 `activate_run_fence/deactivate_run_fence` 改为模块级导入（此前是 NameError，围栏从未真正启用） | `tests/test_h8_run_fence_wiring.py`（名字解析 + 上下文变量往返） |
| H23 | `tools/code_guard.py` 拦截白名单模块的再导出逃逸（`asyncio.base_events.os…`），AST 属性链 + 运行时 capability 双校验；`plugin_runner` 同步 | `tests/test_code_guard_reexport_escape.py` |
| H26 | 会话写入只接受显式 owner 身份，闭合同租户匿名公共域；空白身份视为缺失 | `tests/test_h26_session_write_owner.py` |
| H27 | 子 Agent 观测/取消与 trace 按 tenant+user+会话收窄，越权统一 404；引擎 span 写入 `user_id` | `internal/api/subagent_authz_test.go`、`internal/api/trace_owner_test.go` |
| H28 | 冲突 ID 为随机值，读取/裁决/删除按记录内 scope 与调用者双校验，审计记 actor | `tests/memory/test_conflict_scope.py` |
| H29 | 媒体对象键由上传会话推导并校验租户前缀；下载强制 `attachment`+`nosniff` 与 private 缓存 | `internal/api/media_*.go` 回归 |
| H30 | 语义缓存域含 tenant/user/完整上下文 digest/工具与策略/模型 lineage；私有会话默认禁用 | `tests/test_gateway_cache_tenant.py` |
| H31 | S3 保存路径单次持锁，故障返回明确错误而非挂起 | `tests/test_media_s3_store.py` |
| H32 | 迁移 DSN 显式环境变量优先、密码只编码一次、默认脱敏输出 | `tests/test_migrations_dsn.py` |
| H33 | 会话写入用 claims 租户；幂等键含租户；email 规范化与唯一索引一致（迁移 `0013_tenant_identity`） | `internal/db/tenant_identity_guard_test.go`、`internal/session/session_tenant_guard_test.go`、`python scripts/check_migration_head.py` |
| H34 | 队列不再用裸 `MAXLEN` 裁剪未 ACK 任务；降级裁剪有指标与告警 | `tests/test_queue_retention_h34.py` |
| H35 | 引擎注册地址过协议/主机/端口校验与可选 HMAC 签名；设置广播只发白名单字段 + 版本 | `internal/engine/discovery_trust_test.go`、`internal/api/settings_broadcast_test.go` |
| H37 | 审批改 `GETDEL`/Lua 单次消费；Guardian 矛盾判词降级为 unknown（回退人工）；steer 本地副本带 TTL/代数且不承诺跨副本 | `tests/test_approval_single_consume_failsafe.py`、`tests/test_guardian.py`、`tests/test_steer.py` |
| H39 | SSRF 改为"只放行全球 unicast"，并在连接期钉住已校验 IP（关掉 rebinding 窗口） | `tests/test_ssrf_connect_pinning.py` |
| H40 | API Key 校验用户状态；RPA 身份与 owner 过滤 fail-closed | `internal/api/api_keys_handler.go` 回归、`internal/api/rpa_authz_test.go` |
| H58 | 停止生成真正下发 `POST /v1/agent/interrupt`，未确认不标记已停止、可重试 | `frontend-vue/src/composables/__tests__/useTurnStop.spec.ts`、`ChatView.interaction.spec.ts` |
| H63 | Mermaid 二次消毒改用 DOMPurify SVG/MathML profile + 标签/属性白名单 | `src/components/chat/__tests__/mermaidSanitize.spec.ts` |
| H66 | 只对可证明幂等的 GET/带幂等键操作重放，POST/上传/派发不再盲重试 | `src/api/__tests__/nonIdempotentRetry.spec.ts`、`src/utils/__tests__/replaySafety.spec.ts` |
| H68 | 认证代次围栏：响应/401/重试/profile/路由守卫写回前比对 generation | `src/stores/__tests__/authGeneration.spec.ts`、`src/api/__tests__/authGenerationFence.spec.ts` |
| H74 | 验证码 Provider 最小 `script/frame/connect` 白名单，部署 CSP 与桌面壳同源；未启用不放行 | `src/utils/__tests__/captchaCspConsistency.spec.ts`、`src/components/__tests__/CaptchaWidget.provider.spec.ts` |

遗留（本条不覆盖，仍留在上表）：H39 的连接钉住对代理链路需在真实代理下复验；H35 的注册签名需要引擎侧签发配合（当前为可选开关）。

### 9.13 第三轮加固验收（2026-11-24/25，已从待办表移除）

- 同样口径：行为改造 + 能证明"修复前会失败"的回归（多数条目附了变异复跑记录）。
- 合并后门禁：`go build ./...`、`go test`（api/engine/session/auth/db/storage/market/cmd/config 全绿）、`python -m pytest` 相关子集 120+ 项、前端受影响 spec 全绿、`vue-tsc -b --noEmit`、`pnpm run check:ui`。

| ID | 落点 | 回归证据 |
|---|---|---|
| H4 | `run_code` 嵌套工具改为**显式 scoped capabilities**（白名单 ∩ 注册表 ∩ 会话收窄），主进程侧逐次重新授权；越权拒绝并写 `exec_audit` | `tests/test_run_code_nested_scope.py`（9 项，含 `shell_exec(rm -rf)` 被拒 + audit 条目） |
| H5 | 市场安装：外部 source 默认关闭、manifest 抓取 + SHA256 + **Ed25519** 信任链、校验先于落盘、原地更新原子替换 | `internal/market/trust_test.go`、`install_trust_test.go`、`internal/api/market_trust_api_test.go`、`tests/test_skill_install_integrity.py`（缺摘要/错签名/未知 key_id 均断言"未安装"） |
| H6 | WS 与 HTTP 共用 `evaluateSessionRevocation`（纪元→jti→强制改密→存储故障 fail-close）；撤销事件广播并即时断开在连会话 | `internal/api/ws_revocation_parity_test.go`（7 状态逐行对齐 HTTP/`/ws/rpa`/`/ws/device-fs`）、`TestRevokeUserSessionsBroadcastsEpochEventAndClosesConnections` |
| H9 | 取消传播：模型流/工具/子进程/子 Agent 都在**收敛预算**内停；已产生副作用返回 `outcome=unknown` 而不是成功 | `tests/test_cancel_propagation.py`（7 项，含真实子进程与"拒绝停下的工具"） |
| H10 | 审批票据四维绑定（调用者/资源范围/规范化参数摘要/工具名）+ 同等级改参数必须重新授权（HEAD 已满足，本轮补独立探针） | `tests/test_approval_ticket_binding.py`（14 项）、既有 `tests/test_approval_edit.py`（34 项） |
| H12 | 停机等待 detached run 排空（`InFlightRuns`），等不到时**明确**交代由 checkpoint + 租约 TTL 恢复；关键配置非法改 `fatalExit`（非零退出） | `internal/api/inflight_runs_test.go`（5 项）、`cmd/chiron/fatal_exit_guard_test.go`（AST 守卫，变异复跑会红） |
| H13 | Go toolchain 固定到 `.go-version`（1.26.11），CI `setup-go` 从该文件取版本 | `scripts/check_ci_gates.py`（10 条门禁静态核对，本地可跑） |
| H18 | CI 补 gofmt（按**新增违规**判定，不追历史债）、固定版本 golangci-lint、govulncheck、`-race`、**Windows job**、PageIndex / sandbox-service 独立 job，且沙箱隔离用例出现 skip 即失败 | 同上 `check_ci_gates.py`；`gofmt` 增量口径实测"0 新增 / 3 历史" |
| H25 | Git 入口统一硬化：`core.fsmonitor/pager/filter/diff.*`/hooks 覆盖 + 参数不可重开配置 + 探测失败即拒绝；`shell_exec` 与长驻 shell 同口径 | `tests/test_git_entrypoints_execution_guard.py`（6 项，含埋 `filter.clean` 的恶意仓库 0 marker 落盘） |
| H42 | ACP 严格处理 202/JSON/SSE 终态，没有合法终态即失败；`session/prompt` 改异步分发，长回合不再堵住 cancel/权限回执 | `cmd/chiron-acp/main_contract_h42_test.go`（9 项，含"cancel 不被长 prompt 阻塞"） |
| H49 | `open-path` 改走 guard：canonical 必须在用户批准的项目根内；文档白名单直开；可执行/UNC 需独立确认 | `electron/__tests__/open-path-guard.test.cjs`（14 项） |
| H52 | 数据目录 ACL 显式审计 + 移除非授权 ACE，有残留即 `ok:false`；加固失败**拒绝**落 token/secret | `electron/__tests__/data-dir-acl-gate.test.cjs`（15 项，旧码同输入 `ok=true` 且 0 次 `/remove:g`） |
| H55 | WebDAV：`truncated` 使遍历整体失败（PROPFIND 先缓冲再落盘）；写入带 `ExpectedHash`，冲突 412；同内容重试幂等 | `internal/api/device_webdav_conflict_test.go` |
| H59 | 审批/问题/SSE 按 session/run 建图，票据冻结归属并拒绝错配；只有来源流能关自己；子 Agent 按 run 去重 + Last-Event-ID 回放 | `src/views/__tests__/ChatView.scopedStreams.spec.ts`、`src/components/chat/__tests__/interactionScope.spec.ts`、`useSubagentStream.replay.spec.ts` |
| H61 | 票据保留到服务端确认（`ok=false` 也算未确认）；失败可重试；权威 `expires_at`/`allow_free_text`；历史与实时对账 | `pendingReconcile.spec.ts`、`InteractionInbox.authoritative.spec.ts` |
| H69 | SSE 静默看门狗（按字节活性）+ 恢复即重连；gap 重拉带 session/run 代次并 CAS 提交 | `sseWatchdog.spec.ts`、`useSessionSwitch.resync.spec.ts` |
| H75 | 市场内容装完 **inert**（agent `enabled=false`、插件 `status=inactive`、技能 `enabled=false/auto_inject=false`），显式启用前重做完整性与授权检查 | `internal/market/install_enable_test.go`、`internal/api/market_enable_api_test.go`、`tests/test_skill_install_inert.py`、`MarketView.spec.ts` |
| H77 | Agent 绑定 KB 与每次检索都按**调用者**授权（自己的/public/同租户），禁止以库主身份绕过；两端同一判据 | `internal/api/agents_kb_access_test.go`（9 项）、`tests/test_kb_caller_access_h77.py`（4 项） |
| H86 | 受管 CPython 固定到补丁版本（3.14.4），构建期/运行期两处常量由测试守住，并落 `python-runtime.json` 清单（版本 + 解释器 SHA256） | `electron/__tests__/runtime-version-pin.test.cjs`（3 项） |

遗留（已如实登记，不进本条验收）：H46 服务端票据/签名、H73 服务端计划摘要校验；H9 的"收敛上界"依赖工具可被 cancel（阻塞在同步 C 调用里的工具会走 unknown 分支，已被测试固化为预期行为）。

### 9.14 第四轮加固验收（2026-11-25，已从待办表移除）

- 参与方并行能力受限（worktree 内的长时间任务多次中断），本轮三项均由主线逐条完成并**当场验证**；中断留下的半成品（H36 预算预占、H46 服务端票据）已丢弃，相应条目保持原样在表里。

| ID | 落点 | 回归证据 |
|---|---|---|
| H43 | `run.py` 的进程/端口判定改精确匹配：`tasklist -FO CSV` 按 PID 精确比对、`netstat`/`lsof` 按**端口整段**匹配并只看 LISTENING；新增 `restart()` 校验（PID 存活 + 端口归属一致）与 `--strict`，`main` 用 `sys.exit(0/1)` 反映结论 | `scripts/tests/test_service_identity_h43.py`（13 项）；变异复跑：端口子串/PID 子串/restart 吞退出码 三种回退**全部变红**，恢复后全绿 |
| H78 | 画布归属以**权威存储**为准：`smLoadWorkspaceOwner` 直读 PG（不再只看 Redis）；`SaveWorkspace` 归属不符/记录不存在一律 404；`smPersist` 的 UPDATE 带 `tenant_id/user_id` 并检查影响行数、节点 DELETE 用 `USING session_map_workspaces` 收敛；`BindProjectSession` 增加 `smOwnsSession` 校验、`force` 不再能删他人归属 | `internal/api/sessionmap_owner_guard_test.go`（4 项源码约束）+ 既有 `sessionmap_edgekind_guard_test.py` 断言同步更新（取值来源从热层改为权威存储）；`go test ./internal/api` 全绿 |
| H79 | 云端凭据与 API origin 绑定（v2 信封 `{v,origin,token,savedAt}`，规范化 `scheme://host:port`）：写侧无可信 origin 即拒绝落盘；读侧归属不符**清盘**并要求重新登录；`desktop:set-api-url` 按 `sameOrigin` 判定换代并清凭据；旧版裸 token 视为无法证明归属（清盘一次） | `frontend-vue/electron/__tests__/token-origin.test.cjs`（6 项，含跨域清盘、默认端口等价、legacy 判定）；electron 全量 `node --test` 530/531 通过（唯一失败为需真实 Electron 的既有 `csp-injection`） |

## 10. 参考

- OpenAI Codex Sandbox：<https://tonybai.com/2026/11/19/inside-openai-codex-sandbox/>
- Hosted Agents 边界 / OpenHands V1：<https://xwang.dev/blog/2026/openhands-sdk-claude-managed-agents/>
- Baseten Sandboxes：<https://www.baseten.co/blog/building-the-agent-cloud-introducing-baseten-sandboxes>
- 多租户 Agent 隔离：<https://niteagent.com/blog/multi-tenant-agent-platform-architecture-2026/>
