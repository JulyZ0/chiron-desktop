# PRD：Chiron 远程执行与团队服务模式

- 状态：S0–S4、R1/R3/R4/R5 已实现；R6/R7 的观测与告警、R9 span/trace、R10 数据生命周期仍在推进。
- 加固来源：H1–H86 是 2026-11-24 的全量审计结论（前端/桌面壳/后端），后经七轮推进：
  **已完成 70 项**（逐条登记在 9.5）、**待办 12 项**（9.2）。H80/H81/H84/H85 从未立项，不计数。
- **推进方式（唯一入口）**：只看第 9 章。9.1 决定顺序，9.2 是待办清单与可执行判据，
  9.3 是里程碑退出条件，9.4 是待确认决策，9.5 是已完成项的证据索引。
  历次「本轮做了什么」的逐条叙述已压缩，不再按轮次重复。
- 当前波次：**W1、W2 已完成**；下一步进 **W3**（H46/H47/H16/H38/H14/H24/H44/H51，按 9.2 的切片推进）。
- 待团队主机：R2 实测切换、R6 压测、Web E2E。
- 已知未覆盖：LiteLLM 与 Langfuse 实例级验证、R10 租户删除闭环、S5 多实例演练。
- 关键落点：`internal/api/device_fs.go`、`internal/api/project_mirror.go`、
  `internal/api/sessionmap_projects.go`、`frontend-vue/electron/`、`python-engine/app/`。

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

## 8. 外部验收（需团队主机，不在本机闭环）

以下三项依赖真实团队环境，本机只能做静态检查。统一入口 `scripts/team_acceptance.py`
（退出码：0=无 FAIL 且至少一项 PASS、1=有 FAIL、77=全部 SKIP，**绝不把 SKIP 当 PASS**）。

```bat
set CHIRON_OWNER_PASSWORD=<owner 密码>
python scripts/team_acceptance.py --base-url https://<域名> --email owner@<公司> ^
    --r2-root R:\project --sse --strict-web --json-out .artifacts/team-acceptance.json
```

| 项 | 脚本 | 通过判据 |
|---|---|---|
| R2 实测挂载 | `scripts/r2_mount_bench.py` | `r2.read_p95`≤50ms、`r2.write_p95`≤150ms、`r2.git` 的 `rev-parse --is-inside-work-tree` 为 true、`r2.io_errors`/`r2.rename_delete` 全链路有效、10 分钟稳定 |
| 团队 Web E2E | `scripts/team_smoke.py`（`--sse`） | `web.login`/`web.must_change_password`/`web.invite`/`web.force_logout`/`web.share_anonymous`/`web.share_page_html`/`web.sse_first_token` 逐项 PASS |
| R6 压测 | `--r6-report` | P=2/4 的首 token P95、引擎 RSS、空闲回收、`/metrics` 指标齐全（阈值用 `--r6-first-token-p95-ms` 显式给，PRD 不设硬指标） |

遗留扩展：LiteLLM 与 Langfuse 实例级验证、租户删除闭环、S5 多实例演练。

## 9. 加固计划（H 系列）

本章是**唯一**的推进依据：一个波次计划、一份待办清单、一套退出条件、一份证据索引。
历次「本轮做了什么」的叙述已压缩到 9.5（按 ID 一行），不再按轮次重复。

### 9.1 推进波次

排序标准（按权重从高到低）：**能本机完整验证** > **改动自包含** > **对外风险** > **体量小**。
一个波次内的项互不依赖，可并行；跨波次基本单向（W1 完成后再动 W2）。

| 波次 | 范围 | 为什么排这里 | 本机能否闭环 |
|---|---|---|---|
| W1 ✅ | ~~H45、H48、H50、H73、H76、H11~~ **已完成**（证据见 9.5） | 单点改动 + 纯函数/探针即可证明；H45/H50/H48 是**对外**风险（更新、传输、二因素） | 本机已闭环（H48 的非 Windows 分支用注入桩） |
| W2 ✅ | ~~H7、H36、H41、H17、H15、H60~~ **已完成**（证据见 9.5） | 需要在既有链路上加"身份/代次/预占"这类中间层，改动中等但都可测 | H7 的并发场景需真 Redis → 由 CI 的 redis service 闭环；其余能 |
| W3 | H46（服务端）、H47、H38、H14、H24、H44、H51 | 体量大、跨语言，必须拆片推进（见 9.2 的"切片"列） | 能，但每片都要单独验收 |
| W4 | H2、H20、H19、H22 | 外部条件或产品决策未定，做了也无法验收 | 不能（见 9.4） |

### 9.2 待办清单（12 项）

"完成定义"一栏是**可执行判据**：满足它即可从本表删除，并把证据登记进 9.5。
"切片"一栏给出单项可独立验收的第一步，避免"一次改到底、谁也无法证明它对"。

| ID | P | 目标 | 完成定义（可执行判据） | 切片 / 前置 | 波次 |
|---|---|---|---|---|---|
| H46 | P0 | 设备桥服务端授权 | `POST /v1/device-fs/ticket`：校验 Ed25519 设备签名、票据 ≤60s 且一次性；`/ws/device-fs` 只认票据（query token 一律拒）；撤销/登出后立即断开且 close 4401/4403 | 客户端已就绪 ⇒ ①票据端点 ②签名校验 ③WS 只认票据 ④撤销一致 | W3 |
| H16 | P2 | 版本化 Go/Python 契约 | 为 submit/run event、tool call/approval、checkpoint、queue payload、MCP config、error/terminal state 建立版本化 schema 与**双端 conformance 测试**；不兼容变更必须升版本，旧版本读取方要显式拒绝而非按缺省猜 | ①先做 error/terminal 与 run event 两族（影响面最大）②再做 tool/approval ③最后 checkpoint/queue/MCP | W3 |
| H47 | P1 | renderer 权限最小化 | 启用 sandbox 或拆分窄 preload；按窗口/能力授权；精确校验 `event.sender` 与 mainFrame；setup/error 页不加载特权 preload；保留 CSP/导航/window-open 限制并加回归 | ①能力表 ②窄 preload ③sender 校验 ④回归用例 | W3 |
| H38 | P1 | 子 Agent 复用完整执行链 | 子 Agent 走与主 Agent 相同的 ToolGuard/Broker/审批/副作用账本；undo 先验证快照哈希与路径再替换，失败可恢复且不损坏二进制 | ①统一 execute 入口 ②副作用账本 ③undo 校验 | W3 |
| H14 | P2 | 状态事务与 CAS | 消息写入事务化；checkpoint 主记录与 manifest 原子提交；taskboard 用 revision CAS；todo ID 稳定且 `depends_on` 不因 child merge 失效 | ①messages 事务 ②checkpoint 原子 ③taskboard CAS | W3 |
| H24 | P0 | 插件配置与执行授权一致 | 任一执行路径（Go 端点、Python active_servers、broker）都校验总开关 + 每插件授权 + 规范化配置摘要；改 Args/Env/ReadOnly 使旧授权失效；未授权配置不得起进程/连网/注册工具；撤销授权立即断连 | ①Python 侧校验 ②broker 入口 ③配置变更失效 ④撤销断连 | W3 |
| H44 | P2 | 审计/导出/留存与引用完整性 | 审计写入与 ACK 有明确 outbox/事务语义（不再丢）；导出有行/字节/并发上限；用量与留存操作原子可恢复；引用解析失败**拒绝**而非放行；`no_retention` 全链路不落盘 | ①审计语义 ②导出上限 ③用量原子 ④PageIndex fail-closed ⑤no_retention | W3 |
| H51 | P1 | 运行时与组件供应链 | 依赖与 artifact 全量 hash 锁定；组件 manifest 用内嵌公钥验签；仅允许 HTTPS 与批准主机（不静默切镜像）；安装后与执行前复验 | ①uv hash lock ②manifest 验签 ③主机白名单 ④复验；验签根依赖发布基础设施 | W3 |
| H2 | P0 | Linux 内核级隔离 | 默认无网络、工作区外只读、读不到引擎凭证、进程树可回收；bwrap/gVisor 不可用时**拒绝执行**而非降级 setrlimit | 需要 Linux 真机；本机只能做"拒绝降级"的开关与探针 | W4 |
| H20 | P1 | Provider 密钥加密存储 | Redis 不留明文 key；可配置 KMS/信封加密；轮换 + version + 审计；env 仅兜底 | 需先定 KMS 方案（见 9.4） | W4 |
| H19 | P3 | ACP 完整 Agent 协议 | 声明 capabilities；new_session 绑定 workspace/cwd；load_session 可回放；附件/tool updates/permission ask；保留真实 cancel | 需产品确认是否为本版本目标 | W4 |
| H22 | P3 | 只读子 Agent 强隔离 | 若承诺严格只读：动态 capability + host-observed mutation receipts；否则明确只保证"工具级只读" | 同上（产品决策） | W4 |

### 9.3 里程碑与退出条件

| 里程碑 | 范围 | 退出条件 |
|---|---|---|
| M1 对外安全门禁 ✅ | W1 | 六项完成定义全部有回归（见 9.5）；`go build/test`、`vue-tsc`、`check:ui`、electron `node --test` 全绿 |
| M2 执行可靠性 ✅ | W2 | 并发/竞态类探针可复现"修复前失败"（fakeredis 真执行 Lua）；H7 仍由 CI（真 Redis）复核 |
| M3 契约与设备 | W3 前段（H46/H47） | 设备桥从 fail-closed 变为可用且票据为唯一凭据；renderer 特权面收敛并有回归 |
| M4 数据与授权收口 | W3 后段（H24/H38/H14/H44/H51） | 授权/审计/供应链三条链各自有"未授权即拒绝"的探针 |
| M5 暂缓 | W4 | 仅在外部条件或决策落地后启动 |

### 9.4 待确认决策

1. **H19 / H22 产品承诺**：ACP 完整编辑器集成、严格只读子 Agent 是否属于当前版本目标？否则保持 W4 暂缓。
2. **H20 KMS 选型**：接受"进程内信封加密（APP_SECRET 派生密钥）+ 可插拔 KMS 接口"，还是直接引入云 KMS SDK？
3. **H2 Linux 隔离**：是否接受"bwrap 不可用即拒绝执行"作为首个可发货版本（而不是等 gVisor）？
4. **H45/H51 签名根**：发布签名私钥由谁持有、公钥如何内嵌进安装包（决定两项能否一次收口）。
5. **H7 队列语义**：`retry` 是否允许"同一 idempotency key 多 attempt 并存"（影响 fencing 实现）。

### 9.5 已完成验收证据索引

按 ID 一行（58 项），删项时登记于此；细节在对应测试文件里，不在本文重述。

| ID | 结论与证据 |
|---|---|
| H1 | 公共 `/v1/tools/execute` 在网关与引擎两侧都已移除；`tools/client.py` 中的同名调用是死代码 ｜ 证据：`internal/api/tool_broker_test.go`（POST 得到 404）、`python-engine/tests/test_tools_service_api.py`（`registry.execute` 未被 await） |
| H3 | `agent/tool_policy.py` 复用沙箱文本逃逸判据并按平台解析，复合命令一律升级审批 ｜ 证据：`tests/test_tool_policy_command_parse.py` |
| H8 | `agent_submit` 的 `activate_run_fence/deactivate_run_fence` 改为模块级导入（此前是 NameError，围栏从未真正启用） ｜ 证据：`tests/test_h8_run_fence_wiring.py`（名字解析 + 上下文变量往返） |
| H23 | `tools/code_guard.py` 拦截白名单模块的再导出逃逸（`asyncio.base_events.os…`），AST 属性链 + 运行时 capability 双校验；`plugin_runner` 同步 ｜ 证据：`tests/test_code_guard_reexport_escape.py` |
| H26 | 会话写入只接受显式 owner 身份，闭合同租户匿名公共域；空白身份视为缺失 ｜ 证据：`tests/test_h26_session_write_owner.py` |
| H27 | 子 Agent 观测/取消与 trace 按 tenant+user+会话收窄，越权统一 404；引擎 span 写入 `user_id` ｜ 证据：`internal/api/subagent_authz_test.go`、`internal/api/trace_owner_test.go` |
| H28 | 冲突 ID 为随机值，读取/裁决/删除按记录内 scope 与调用者双校验，审计记 actor ｜ 证据：`tests/memory/test_conflict_scope.py` |
| H29 | 媒体对象键由上传会话推导并校验租户前缀；下载强制 `attachment`+`nosniff` 与 private 缓存 ｜ 证据：`internal/api/media_*.go` 回归 |
| H30 | 语义缓存域含 tenant/user/完整上下文 digest/工具与策略/模型 lineage；私有会话默认禁用 ｜ 证据：`tests/test_gateway_cache_tenant.py` |
| H31 | S3 保存路径单次持锁，故障返回明确错误而非挂起 ｜ 证据：`tests/test_media_s3_store.py` |
| H32 | 迁移 DSN 显式环境变量优先、密码只编码一次、默认脱敏输出 ｜ 证据：`tests/test_migrations_dsn.py` |
| H33 | 会话写入用 claims 租户；幂等键含租户；email 规范化与唯一索引一致（迁移 `0013_tenant_identity`） ｜ 证据：`internal/db/tenant_identity_guard_test.go`、`internal/session/session_tenant_guard_test.go`、`python scripts/check_migration_head.py` |
| H34 | 队列不再用裸 `MAXLEN` 裁剪未 ACK 任务；降级裁剪有指标与告警 ｜ 证据：`tests/test_queue_retention_h34.py` |
| H35 | 引擎注册地址过协议/主机/端口校验与可选 HMAC 签名；设置广播只发白名单字段 + 版本 ｜ 证据：`internal/engine/discovery_trust_test.go`、`internal/api/settings_broadcast_test.go` |
| H37 | 审批改 `GETDEL`/Lua 单次消费；Guardian 矛盾判词降级为 unknown（回退人工）；steer 本地副本带 TTL/代数且不承诺跨副本 ｜ 证据：`tests/test_approval_single_consume_failsafe.py`、`tests/test_guardian.py`、`tests/test_steer.py` |
| H39 | SSRF 改为"只放行全球 unicast"，并在连接期钉住已校验 IP（关掉 rebinding 窗口） ｜ 证据：`tests/test_ssrf_connect_pinning.py` |
| H40 | API Key 校验用户状态；RPA 身份与 owner 过滤 fail-closed ｜ 证据：`internal/api/api_keys_handler.go` 回归、`internal/api/rpa_authz_test.go` |
| H58 | 停止生成真正下发 `POST /v1/agent/interrupt`，未确认不标记已停止、可重试 ｜ 证据：`frontend-vue/src/composables/__tests__/useTurnStop.spec.ts`、`ChatView.interaction.spec.ts` |
| H63 | Mermaid 二次消毒改用 DOMPurify SVG/MathML profile + 标签/属性白名单 ｜ 证据：`src/components/chat/__tests__/mermaidSanitize.spec.ts` |
| H66 | 只对可证明幂等的 GET/带幂等键操作重放，POST/上传/派发不再盲重试 ｜ 证据：`src/api/__tests__/nonIdempotentRetry.spec.ts`、`src/utils/__tests__/replaySafety.spec.ts` |
| H68 | 认证代次围栏：响应/401/重试/profile/路由守卫写回前比对 generation ｜ 证据：`src/stores/__tests__/authGeneration.spec.ts`、`src/api/__tests__/authGenerationFence.spec.ts` |
| H74 | 验证码 Provider 最小 `script/frame/connect` 白名单，部署 CSP 与桌面壳同源；未启用不放行 ｜ 证据：`src/utils/__tests__/captchaCspConsistency.spec.ts`、`src/components/__tests__/CaptchaWidget.provider.spec.ts` |
| H4 | `run_code` 嵌套工具改为**显式 scoped capabilities**（白名单 ∩ 注册表 ∩ 会话收窄），主进程侧逐次重新授权；越权拒绝并写 `exec_audit` ｜ 证据：`tests/test_run_code_nested_scope.py`（9 项，含 `shell_exec(rm -rf)` 被拒 + audit 条目） |
| H5 | 市场安装：外部 source 默认关闭、manifest 抓取 + SHA256 + **Ed25519** 信任链、校验先于落盘、原地更新原子替换 ｜ 证据：`internal/market/trust_test.go`、`install_trust_test.go`、`internal/api/market_trust_api_test.go`、`tests/test_skill_install_integrity.py`（缺摘要/错签名/未知 key_id 均断言"未安装"） |
| H6 | WS 与 HTTP 共用 `evaluateSessionRevocation`（纪元→jti→强制改密→存储故障 fail-close）；撤销事件广播并即时断开在连会话 ｜ 证据：`internal/api/ws_revocation_parity_test.go`（7 状态逐行对齐 HTTP/`/ws/rpa`/`/ws/device-fs`）、`TestRevokeUserSessionsBroadcastsEpochEventAndClosesConnections` |
| H9 | 取消传播：模型流/工具/子进程/子 Agent 都在**收敛预算**内停；已产生副作用返回 `outcome=unknown` 而不是成功 ｜ 证据：`tests/test_cancel_propagation.py`（7 项，含真实子进程与"拒绝停下的工具"） |
| H10 | 审批票据四维绑定（调用者/资源范围/规范化参数摘要/工具名）+ 同等级改参数必须重新授权（HEAD 已满足，本轮补独立探针） ｜ 证据：`tests/test_approval_ticket_binding.py`（14 项）、既有 `tests/test_approval_edit.py`（34 项） |
| H12 | 停机等待 detached run 排空（`InFlightRuns`），等不到时**明确**交代由 checkpoint + 租约 TTL 恢复；关键配置非法改 `fatalExit`（非零退出） ｜ 证据：`internal/api/inflight_runs_test.go`（5 项）、`cmd/chiron/fatal_exit_guard_test.go`（AST 守卫，变异复跑会红） |
| H13 | Go toolchain 固定到 `.go-version`（1.26.11），CI `setup-go` 从该文件取版本 ｜ 证据：`scripts/check_ci_gates.py`（10 条门禁静态核对，本地可跑） |
| H18 | CI 补 gofmt（按**新增违规**判定，不追历史债）、固定版本 golangci-lint、govulncheck、`-race`、**Windows job**、PageIndex / sandbox-service 独立 job，且沙箱隔离用例出现 skip 即失败 ｜ 证据：同上 `check_ci_gates.py`；`gofmt` 增量口径实测"0 新增 / 3 历史" |
| H25 | Git 入口统一硬化：`core.fsmonitor/pager/filter/diff.*`/hooks 覆盖 + 参数不可重开配置 + 探测失败即拒绝；`shell_exec` 与长驻 shell 同口径 ｜ 证据：`tests/test_git_entrypoints_execution_guard.py`（6 项，含埋 `filter.clean` 的恶意仓库 0 marker 落盘） |
| H42 | ACP 严格处理 202/JSON/SSE 终态，没有合法终态即失败；`session/prompt` 改异步分发，长回合不再堵住 cancel/权限回执 ｜ 证据：`cmd/chiron-acp/main_contract_h42_test.go`（9 项，含"cancel 不被长 prompt 阻塞"） |
| H49 | `open-path` 改走 guard：canonical 必须在用户批准的项目根内；文档白名单直开；可执行/UNC 需独立确认 ｜ 证据：`electron/__tests__/open-path-guard.test.cjs`（14 项） |
| H52 | 数据目录 ACL 显式审计 + 移除非授权 ACE，有残留即 `ok:false`；加固失败**拒绝**落 token/secret ｜ 证据：`electron/__tests__/data-dir-acl-gate.test.cjs`（15 项，旧码同输入 `ok=true` 且 0 次 `/remove:g`） |
| H55 | WebDAV：`truncated` 使遍历整体失败（PROPFIND 先缓冲再落盘）；写入带 `ExpectedHash`，冲突 412；同内容重试幂等 ｜ 证据：`internal/api/device_webdav_conflict_test.go` |
| H59 | 审批/问题/SSE 按 session/run 建图，票据冻结归属并拒绝错配；只有来源流能关自己；子 Agent 按 run 去重 + Last-Event-ID 回放 ｜ 证据：`src/views/__tests__/ChatView.scopedStreams.spec.ts`、`src/components/chat/__tests__/interactionScope.spec.ts`、`useSubagentStream.replay.spec.ts` |
| H61 | 票据保留到服务端确认（`ok=false` 也算未确认）；失败可重试；权威 `expires_at`/`allow_free_text`；历史与实时对账 ｜ 证据：`pendingReconcile.spec.ts`、`InteractionInbox.authoritative.spec.ts` |
| H69 | SSE 静默看门狗（按字节活性）+ 恢复即重连；gap 重拉带 session/run 代次并 CAS 提交 ｜ 证据：`sseWatchdog.spec.ts`、`useSessionSwitch.resync.spec.ts` |
| H75 | 市场内容装完 **inert**（agent `enabled=false`、插件 `status=inactive`、技能 `enabled=false/auto_inject=false`），显式启用前重做完整性与授权检查 ｜ 证据：`internal/market/install_enable_test.go`、`internal/api/market_enable_api_test.go`、`tests/test_skill_install_inert.py`、`MarketView.spec.ts` |
| H77 | Agent 绑定 KB 与每次检索都按**调用者**授权（自己的/public/同租户），禁止以库主身份绕过；两端同一判据 ｜ 证据：`internal/api/agents_kb_access_test.go`（9 项）、`tests/test_kb_caller_access_h77.py`（4 项） |
| H86 | 受管 CPython 固定到补丁版本（3.14.4），构建期/运行期两处常量由测试守住，并落 `python-runtime.json` 清单（版本 + 解释器 SHA256） ｜ 证据：`electron/__tests__/runtime-version-pin.test.cjs`（3 项） |
| H43 | `run.py` 的进程/端口判定改精确匹配：`tasklist -FO CSV` 按 PID 精确比对、`netstat`/`lsof` 按**端口整段**匹配并只看 LISTENING；新增 `restart()` 校验（PID 存活 + 端口归属一致）与 `--strict`，`main` 用 `sys.exit(0/1)` 反映结论 ｜ 证据：`scripts/tests/test_service_identity_h43.py`（13 项）；变异复跑：端口子串/PID 子串/restart 吞退出码 三种回退**全部变红**，恢复后全绿 |
| H78 | 画布归属以**权威存储**为准：`smLoadWorkspaceOwner` 直读 PG（不再只看 Redis）；`SaveWorkspace` 归属不符/记录不存在一律 404；`smPersist` 的 UPDATE 带 `tenant_id/user_id` 并检查影响行数、节点 DELETE 用 `USING session_map_workspaces` 收敛；`BindProjectSession` 增加 `smOwnsSession` 校验、`force` 不再能删他人归属 ｜ 证据：`internal/api/sessionmap_owner_guard_test.go`（4 项源码约束）+ 既有 `sessionmap_edgekind_guard_test.py` 断言同步更新（取值来源从热层改为权威存储）；`go test ./internal/api` 全绿 |
| H79 | 云端凭据与 API origin 绑定（v2 信封 `{v,origin,token,savedAt}`，规范化 `scheme://host:port`）：写侧无可信 origin 即拒绝落盘；读侧归属不符**清盘**并要求重新登录；`desktop:set-api-url` 按 `sameOrigin` 判定换代并清凭据；旧版裸 token 视为无法证明归属（清盘一次） ｜ 证据：`frontend-vue/electron/__tests__/token-origin.test.cjs`（6 项，含跨域清盘、默认端口等价、legacy 判定）；electron 全量 `node --test` 530/531 通过（唯一失败为需真实 Electron 的既有 `csp-injection`） |
| H21 | 文档可信度：`docs/` 四份关键文档（PRD / 本地契约 / 团队部署 / 贡献指南）全部 tracked；引用的设计文档**零缺失**（`doc_link_baseline.txt` 为空）；CI 已跑断链与表格结构检查，本轮把它们纳入 `scripts/check_ci_gates.py` 的自检清单（12 条门禁） ｜ 证据：`python scripts/check_doc_links.py`（0 债务）、`check_md_tables.py`、`check_ci_gates.py`（新增 2 条并全绿） |
| H53 | 清空数据范围与界面承诺对齐：`wipeLocalData` 从"只删 `local/` + token"扩到 `config.json` / `token.bin` / `secrets.bin` / `device-id` / `approved-roots.json` / `two-factor.json` / `runtime-state.json` / `updates/` / `Partitions/<分区>/` / `logs/`；**删不掉逐项上报**而不是静默跳过；`keepLogs` 与自定义 `partitions` 可配 ｜ 证据：`electron/__tests__/wipe-data-scope.test.cjs`（7 项：范围齐全、递归语义、失败点名上报且其余继续、keepLogs、自定义分区、停后端失败即中止不删半份、缺 userData 明确报错） |
| H56 | Windows 子进程树终止：新增 `electron/proc-tree.cjs`；安装与设备 Git 的取消/超时改 `taskkill /T /F /PID`（POSIX 自成进程组后 `kill(-pid)`），并保底 `SIGKILL` ｜ 证据：`electron/__tests__/proc-tree.test.cjs`（8 项，含"taskkill 非零必须留痕"、"负 PID 而非单进程"、源码守卫禁止裸 `child.kill()`） |
| H57 | 更新渠道与版本语义：`compareVersions` 改完整 semver 优先级（prerelease 逐标识符、build 不参与、非法输入不声称高低）；`pickLatestRelease` 加 channel / withdrawal / pin 与**不降级**规则；主进程从环境变量传入三者（不接受渲染层输入） ｜ 证据：`electron/__tests__/update-semver.test.cjs`（10 项，含 `2.0.0-beta < 2.0.0`、`beta.2 < beta.10`、撤回规范化、pin、空列表不抛） |
| H64 | 附件 URL 来源与协议约束：新增 `utils/safeAttachmentUrl.ts`（默认拒绝；只放行站内 `/media/`·`/v1/`、同源/受信任 origin 的 https、回环 http、`blob:`、图片 `data:`；拒绝 `javascript:`/`file:`/协议相对 `//`）；`MessageItem` 与 `MediaPickerDialog` 两个入口都过同一策略 ｜ 证据：`src/utils/__tests__/safeAttachmentUrl.spec.ts`（14 项）；`src/components/chat` 全量 51 文件 / 495 项通过；`vue-tsc` 通过 |
| H83 | 平台 Owner 专属后台入口三处同口径：路由 `meta.requiresPlatformOwner`（monitor/datastores/diagnostics/deps）、守卫按 `auth.isPlatformOwner` 收敛并回首页、侧边栏按 `platformOwnerOnly` 过滤（空分组一并消失），高亮也改用过滤后的菜单 ｜ 证据：`src/views/admin/__tests__/platformOwnerNav.spec.ts`（10 项：标记存在且不过度收敛、守卫顺序与回落、模板不再直接遍历未过滤分组、过滤行为） |
| H65 | 上传响应契约收口：`file_url` 与分片路径的 `file_url` 都要过**来源/协议**校验（与附件渲染同一策略）；缺 ID/URL 立即失败且不伪造 ID；同时修掉一处自相矛盾 —— `ChatInput` 曾用 50MB 硬拦，而 `uploadFile()` 明确支持 >50MB 走分片，导致分片路径在聊天里永远走不到 ｜ 证据：`src/api/__tests__/mediaUploadUrlContract.spec.ts`（8 项：`javascript:`/`file:`/协议相对/跨站 https/`data:text/html` 五类拒绝、站内路径放行、分片路径同契约、缺 asset_id 失败）；既有 `mediaUpload.spec.ts` 9 项仍绿 |
| H70 | 媒体签名 URL 失败处理从**永久黑名单**改成分类 + 退避状态机：`gone`(404/410) 永久不可用且不再请求、`expired`/`unauthorized` 有上限重签、`transient` 指数退避（永不永久失败）；失败原因**可见**（缩略图角标 + 详情页状态 + 重试按钮），文案走 i18n key ｜ 证据：`src/utils/__tests__/mediaUrlRetry.spec.ts`（14 项，含 exp 秒/毫秒识别、`beta` 不误判、退避上限）；`MediaView.spec.ts`、`src/views` 110 项通过 |
| H71 | 取消粒度改按**请求签名**（method + url + 规范化 params + 非 GET 的 body）：同 url 不同参数不再互相取消；新增 `cancelWhere` 供"只取消某视图"；去重缓存对 rejected promise **立即失效**（旧实现让"重试"在 TTL 内永远拿到同一个失败） ｜ 证据：`src/utils/__tests__/requestCancelPrecision.spec.ts`（8 项：键序无关签名、params/body 参与签名、精确取消、失败后再发一次）；既有 `requestOptimizer.spec.ts` 9 项仍绿 |
| H72 | 上传续传元数据改成带身份信封 `{v, uploadId, owner:{user,tenant,mode,origin}, fingerprint}`：读时逐项比对，任一不符即**丢弃**（宁可重传不可串号）；旧版裸 `upload_id` 判为 `legacy` 不可用；无身份时不落盘 ｜ 证据：`src/utils/__tests__/uploadIdentity.spec.ts`（13 项：换账号/换租户/换部署/换模式/换文件指纹各自拒绝、legacy 拒绝、空值与损坏分类） |
| H82 | 收紧保留期改为**先预览再确认**：新增只读端点 `POST /v1/admin/retention/preview`（旧值→新值、按类型待删行数、是否需确认、生效时机）；`PUT /v1/admin/settings` 的 retention 分组严格解析（非法值报错而非静默夹取），收紧必须带 `confirm=true` 与 `reason`，成功后写审计（旧值/新值/原因/IP）；前端两步式确认，**取消不写配置**并回滚表单 ｜ 证据：`internal/api/retention_settings_test.go`（5 项：仅收紧需确认、严格解析、确认+原因校验、差异描述、无变化不产条目）；前端 `MonitorView` 相关 12 文件 110 项通过；`check:ui`（i18n/RTL/token/可访问性）全绿 |
| H54 | 本机栈**身份挑战**：`/ready` 增补实例 ID / 协议版本 / data dir 指纹（仅本地模式，云端不设 env 就不回显）；外壳写状态时带同一身份，探测逐项比对，**任一不符即拒绝接管**；旧部署（状态里没有 instanceId）与旧网关（不回显）一律不认 ｜ 证据：Go `internal/api/instance_identity_test.go`（4 项：未注入不回显、注入则回显、协议回落、指纹稳定且不可逆）；Electron `runtime-identity-h54.test.cjs`（8 项：ID 不一致/未回显/旧状态/协议不一致各自拒绝），既有 `runtime-state.test.cjs` 12 项同步更新 |
| H62 | 实时输出/错误/日志**出口**统一脱敏：事件 `content`/`tool_arguments`/`error` 与 runtime 的 Provider 原生错误、顶层异常都过 `redact_text`（密钥形态 + 运行时登记凭据双层，幂等）；前端新增 `utils/redact.ts`（同口径 + 剔除 `Authorization`/`Cookie`/`x-api-key` 与 URL 凭据），本地栈未就绪提示里的服务端原文也脱敏 ｜ 证据：`tests/test_redaction_h62.py`（14 项，含事件三个字段、幂等、provider 摘要保留 status）；`src/utils/__tests__/redact.spec.ts`（18 项，含 Axios 错误对象、嵌套响应体、深度折叠、上报摘要） |
| H67 | 私密媒体不再把**裸对象 URL** 交给 renderer：`resolveMediaUrl` 默认拒绝不可信绝对地址（裸 S3/GCS 一律不算），有 asset id 就走 owner-bound 签名、没有就返回空串；签名结果过来源校验；签名缓存改为**按 user/tenant/mode/origin 作用域**隔离，作用域一变整表作废，并与 H68 的认证重置钩子联动清空 ｜ 证据：`src/utils/__tests__/mediaUrlCache.spec.ts`（12 项：换账号/租户/部署/模式各自失效、提前量过期、prune、裸 S3 不可直接渲染、blob/data 白名单） |
| H73 | 服务端签发并校验 `plan_digest`：dry-run 下发、执行前再跑一次只读重算，不一致 409 且不删；摘要覆盖 workspace/保留期/计划内容（键序无关、planned 排序） ｜ 证据：`internal/api/checkpoint_plan.go`、`checkpoint_plan_test.go`（6 项）、`src/utils/__tests__/checkpointPlanDigestSource.spec.ts`（4 项） |
| H76 | 共享层写入按角色分流：admin 就地更新；普通成员改租户共享技能只写**私有 override**（共享层字节不变）；全局共享层成员一律 403；缺角色头按非管理员处理 ｜ 证据：`tests/test_skill_shared_write_h76.py`（9 项） |
| H45 | 更新清单严格 schema（版本/大小/扩展名/域名白名单/架构）+ 缺 `sha512` 立即拒绝；缓存包复验前要求摘要存在；启动安装器前过 Ed25519 验签（未配公钥即拒绝） ｜ 证据：`electron/update-manifest.cjs`、`electron/__tests__/update-manifest.test.cjs`（18 项） |
| H50 | 服务地址传输安全双重校验（保存时 + 建连前）；非 loopback 强制 HTTPS；明文非回环**不发凭据**并给请求打匿名标记；拒 scheme 与内嵌凭据的地址 ｜ 证据：`electron/api-url-policy.cjs`、`electron/__tests__/api-url-policy.test.cjs`（11 项）、`src/utils/apiUrlPolicy.ts` |
| H48 | 非 Windows 平台拒绝破坏性操作（不再"免验证通过"）；2FA 密文不可读时拒绝解锁并返回`second-factor-unreadable` + 显式重置路径（`desktop:reset-two-factor`，状态可读时拒绝重置） ｜ 证据：`electron/__tests__/sensitive-action-h48.test.cjs`（5 项） |
| H11 | Responses 流式解析要求合法终态：畸形帧计数不静默跳过、无 `completed` 即失败、截断回合**不交出**已收集的工具调用（防 partial 执行副作用） ｜ 证据：`python-engine/tests/test_provider_stream_terminal_h11.py`（8 项） |
| H7 | claim/fencing 由 fakeredis（真执行 Lua）在本地闭环：并发 claim 仅一个成功、旧 attempt 的 fail/retry/ack 全部无效、完成后重复投递直接 COMPLETED、重试保留 idempotency/deadline/trace/priority；顺带修掉 `MAXLEN ~ 0` 会把**刚转移的消息**也裁掉（转移成功但两处都查不到） ｜ 证据：`tests/test_queue_h7_fakeredis.py`（5 项，`requirements-dev.txt` 加 `fakeredis[lua]`）；真 Redis 仍由 CI 的 `tests/test_queue_h7_fencing.py` 复核 |
| H36 | 预算改为**原子预占**（判断+占位同一段 Lua）→ 失败 release / 成功按实际用量 commit；并发请求不再靠"读一次 used"放行（超支上限从 N×预估降到 0）；`budget` 读取对 bytes/str 键都成立（此前 decode_responses 一开就静默读成 0）；403 仍按`is_quota_exhausted` 区分配额耗尽与凭证失效 ｜ 证据：`app/gateway/budget_reserve.py`、`tests/test_budget_reserve_h36.py`（7 项）、`tests/test_gateway_router.py`（36 项，含预占/结算口径） |
| H41 | 双管道**并发**排空（顺序读会在子进程写满 4KB stderr 时死锁：父等 stdout EOF、子阻塞在 write）；提权 helper 拒绝从**用户可写**目录/盘符根加载代码（源码部署下 `-m app...` 靠 cwd 进 sys.path ⇒ 条件性本地提权），守卫在触发 UAC 之前 ｜ 证据：`tests/test_isolation_launch_h41.py`（9 项）+ 既有 isolation 套件 80 项 |
| H17 | 复跑确认已满足完成定义：版本协商后发 `notifications/initialized`、未知版本拒绝且不发通知、并发 response/notification/progress/sampling 按 id 正确配对、超时与取消各自发出 `notifications/cancelled`、服务端取消只结算匹配请求、关闭时 pending 全部失败而非挂起 ｜ 证据：`tests/test_mcp_lifecycle.py`（12 项）+ MCP 全套 44 项 |
| H15 | 复跑确认已满足完成定义：fold 记录 provider/model/context_window/covered_from/to/`covered_prefix_digest`；lineage 或前缀变化一律拒绝安装；projection 安装走 CAS（并发只有一个赢家，陈旧快照不改动原数据） ｜ 证据：`tests/test_compaction_digest.py`（35 项，含 `test_projection_rejects_lineage_mismatch` / `test_projection_rejects_changed_covered_prefix` / 两个 CAS 用例） |
| H60 | 重连：`switchSession(id, {force})` 绕开"同 ID 直接返回"，并把 SSE 装配抽成 `attachSessionStream(id, {sinceEventId})`，断线时有回合在跑就按 `Last-Event-ID` 续传（不重放整轮）；异步写回：新增 `utils/staleGuard.ts`（代次 + 复合键），`InteractionInbox`（切会话不再显示上一会话的待办）、`ToolResultBlock`（旧片段不拼到当前块）、`SubAgentPanel`（轮询结果不跨会话）、`WorkbenchPanel`（改项目后不显示上个项目内容）、`KBDocumentPreview`（连开两个文档不串内容）、`MediaView`（签名 URL 不写进错的条目） ｜ 证据：`src/utils/__tests__/streamReattach.spec.ts`（5 项）、`src/utils/__tests__/staleGuard.spec.ts`（9 项，含组件接入的源码守卫）+ `src/components`/`src/utils` 共 869 项回归 |

## 10. 参考

- OpenAI Codex Sandbox：<https://tonybai.com/2026/11/19/inside-openai-codex-sandbox/>
- Hosted Agents 边界 / OpenHands V1：<https://xwang.dev/blog/2026/openhands-sdk-claude-managed-agents/>
- Baseten Sandboxes：<https://www.baseten.co/blog/building-the-agent-cloud-introducing-baseten-sandboxes>
- 多租户 Agent 隔离：<https://niteagent.com/blog/multi-tenant-agent-platform-architecture-2026/>
