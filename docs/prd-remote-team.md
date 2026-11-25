# PRD：Chiron 远程执行与团队服务模式

- 状态：S0–S4、R1/R3/R4/R5 已实现；R6/R7 观测告警、R9 span/trace、R10 数据生命周期在推进。
- 加固来源：H1–H86 为 2026-11-24 全量审计（前端/桌面壳/后端）。**已完成 76 项**（9.5）、
  **待办 6 项**（9.2）。H80/H81/H84/H85 从未立项，不计数。
- **推进只看第 9 章**：9.1 顺序、9.2 待办与可执行判据、9.3 退出条件、9.4 待决策、9.5 证据。
- 当前：W1/W2 已完成，**W3 进行中**（剩 H38/H44/H51/H16）；W4（H2/H20/H19/H22）等外部条件。
- 待团队主机：R2 实测切换、R6 压测、Web E2E；未覆盖：LiteLLM/Langfuse 实例级、租户删除闭环、S5 演练。

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

## 8. 外部验收（需团队主机）

本机只能做静态检查，三项依赖真实环境。统一入口 `scripts/team_acceptance.py`
（退出码 0=无 FAIL 且至少一项 PASS、1=有 FAIL、77=全部 SKIP；**SKIP 绝不当 PASS**）。

```bat
set CHIRON_OWNER_PASSWORD=<owner 密码>
python scripts/team_acceptance.py --base-url https://<域名> --email owner@<公司> ^
    --r2-root R:\project --sse --strict-web --json-out .artifacts/team-acceptance.json
```

| 项 | 脚本 | 通过判据 |
|---|---|---|
| R2 实测挂载 | `r2_mount_bench.py` | read_p95≤50ms、write_p95≤150ms、`rev-parse` 为 true、rename/delete 有效、10 分钟稳定 |
| 团队 Web E2E | `team_smoke.py`（`--sse`） | 登录/改密/邀请/强制下线/匿名分享/分享页 HTML/首 token 逐项 PASS |
| R6 压测 | `--r6-report` | P=2/4 首 token P95、引擎 RSS、空闲回收、`/metrics` 齐全（阈值显式给） |

## 9. 加固计划（H 系列）

本章是**唯一**的推进依据：一个波次计划、一份待办清单、一套退出条件、一份证据索引。
历次「本轮做了什么」的叙述已压缩到 9.5（按 ID 一行），不再按轮次重复。

### 9.1 推进波次

排序权重：**能本机验证** > **改动自包含** > **对外风险** > **体量小**。波次内互不依赖可并行。

| 波次 | 范围 | 状态 |
|---|---|---|
| W1 | H45、H48、H50、H73、H76、H11（对外风险 + 单点改动） | ✅ 完成 |
| W2 | H7、H36、H41、H17、H15、H60（加"身份/代次/预占"中间层） | ✅ 完成 |
| W3 | H46、H47、H24、H14、H38、H44 已完成；剩 **H51、H16** | 进行中 |
| W4 | H2、H20、H19、H22（外部条件/产品决策未定） | 暂缓（见 9.4） |

### 9.2 待办清单（6 项）

"完成定义"一栏是**可执行判据**：满足它即可从本表删除，并把证据登记进 9.5。
"切片"一栏给出单项可独立验收的第一步，避免"一次改到底、谁也无法证明它对"。

| ID | P | 目标 | 完成定义（可执行判据） | 切片 / 前置 | 波次 |
|---|---|---|---|---|---|
| H16 | P2 | 版本化 Go/Python 契约 | 为 submit/run event、tool call/approval、checkpoint、queue payload、MCP config、error/terminal state 建立版本化 schema 与**双端 conformance 测试**；不兼容变更必须升版本，旧版本读取方要显式拒绝而非按缺省猜 | ①先做 error/terminal 与 run event 两族（影响面最大）②再做 tool/approval ③最后 checkpoint/queue/MCP | W3 |
| H51 | P1 | 运行时与组件供应链 | 依赖与 artifact 全量 hash 锁定；组件 manifest 用内嵌公钥验签；仅允许 HTTPS 与批准主机（不静默切镜像）；安装后与执行前复验 | ①uv hash lock ②manifest 验签 ③主机白名单 ④复验；验签根依赖发布基础设施 | W3 |
| H2 | P0 | Linux 内核级隔离 | 默认无网络、工作区外只读、读不到引擎凭证、进程树可回收；bwrap/gVisor 不可用时**拒绝执行**而非降级 setrlimit | 需要 Linux 真机；本机只能做"拒绝降级"的开关与探针 | W4 |
| H20 | P1 | Provider 密钥加密存储 | Redis 不留明文 key；可配置 KMS/信封加密；轮换 + version + 审计；env 仅兜底 | 需先定 KMS 方案（见 9.4） | W4 |
| H19 | P3 | ACP 完整 Agent 协议 | 声明 capabilities；new_session 绑定 workspace/cwd；load_session 可回放；附件/tool updates/permission ask；保留真实 cancel | 需产品确认是否为本版本目标 | W4 |
| H22 | P3 | 只读子 Agent 强隔离 | 若承诺严格只读：动态 capability + host-observed mutation receipts；否则明确只保证"工具级只读" | 同上（产品决策） | W4 |

### 9.3 里程碑与退出条件

| 里程碑 | 退出条件 |
|---|---|
| M1 对外安全门禁 ✅ | W1 六项全部有回归；`go build/test`、`vue-tsc`、`check:ui`、electron `node --test` 全绿 |
| M2 执行可靠性 ✅ | 并发/竞态探针可复现"修复前失败"（fakeredis 真跑 Lua）；H7 由 CI 真 Redis 复核 |
| M3 契约与设备 ✅ | 设备桥票据为唯一凭据；renderer 特权面为"主框架 + 登记窗口 + 能力表"且有回归 |
| M4 数据与授权收口 | H51/H16：供应链与契约各自有"未授权即拒绝"的探针 |

### 9.4 待确认决策

1. **H19 / H22 产品承诺**：ACP 完整编辑器集成、严格只读子 Agent 是否本版本目标？否则维持暂缓。
2. **H20 KMS 选型**：「进程内信封加密 + 可插拔 KMS 接口」还是直接引入云 KMS SDK？
3. **H2 Linux 隔离**：是否接受「bwrap 不可用即拒绝执行」作为首个可发货版本？
4. **H38 undo 语义**：快照恢复失败时，是「整轮回滚」还是「逐文件尽力 + 明确报告失败项」？
5. **H51 签名根**：发布签名私钥谁持有、公钥如何内嵌（决定 H45/H51 能否一次收口）。

### 9.5 已完成验收证据索引

74 项（H1–H86 中已验收部分）。每项一行：**做了什么 + 去哪看**；完整理由在 `git log` 提交信息与测试文件里。新增完成项**加行**，不要另起小节。

| ID | 做了什么 | 证据（文件） |
|---|---|---|
| H1 | 公共 `/v1/tools/execute` 在网关与引擎两侧都已移除… | `tool_broker_test.go`、`test_tools_service_api.py` |
| H3 | `agent/tool_policy.py` 复用沙箱文本逃逸判据并按平台解析… | `test_tool_policy_command_parse.py` |
| H4 | `run_code` 嵌套工具改为显式 scoped capabilities… | `test_run_code_nested_scope.py` |
| H5 | 市场安装：外部 source 默认关闭、manifest 抓取 + SHA256 + E… | `trust_test.go`、`install_trust_test.go` 等4 |
| H6 | WS 与 HTTP 共用 `evaluateSessionRevocation`… | `ws_revocation_parity_test.go` |
| H7 | claim/fencing 由 fakeredis（真执行 Lua）在本地闭环… | `test_queue_h7_fakeredis.py`、`test_queue_h7_fencing.py` |
| H8 | `agent_submit` 的 `activate_run_fence/deactiv… | `test_h8_run_fence_wiring.py` |
| H9 | 取消传播：模型流/工具/子进程/子 Agent 都在收敛预算内停… | `test_cancel_propagation.py` |
| H10 | 审批票据四维绑定（调用者/资源范围/规范化参数摘要/工具名）+ 同等级改参数必须重新授权… | `test_approval_ticket_binding.py`、`test_approval_edit.py` |
| H11 | Responses 流式解析要求合法终态… | `test_provider_stream_terminal_h11.py` |
| H12 | 停机等待 detached run 排空（`InFlightRuns`）… | `inflight_runs_test.go`、`fatal_exit_guard_test.go` |
| H13 | Go toolchain 固定到 `.go-version`（1.26.11）… | `check_ci_gates.py` |
| H14 | taskboard revision CAS + 合并保留 ID 重映射 depends… | `checkpoint_atomicity_h14_test.go`、`messages_atomicity_h14_test.go` 等3 |
| H15 | 复跑确认已满足完成定义：fold 记录 provider/model/context_w… | `test_compaction_digest.py` |
| H17 | 复跑确认已满足完成定义：版本协商后发 `notifications/initialize… | `test_mcp_lifecycle.py` |
| H18 | CI 补 gofmt（按新增违规判定… | `check_ci_gates.py` |
| H21 | 文档可信度：`docs/` 四份关键文档… | `check_md_tables.py`、`check_ci_gates.py` |
| H23 | `tools/code_guard.py` 拦截白名单模块的再导出逃逸… | `test_code_guard_reexport_escape.py` |
| H24 | 授权不再只看"有没有点过同意"，而是绑定到具体执行参数… | `plugin_approval.go`、`plugin_approval_test.go` 等3 |
| H25 | Git 入口统一硬化：`core.fsmonitor/pager/filter/diff… | `test_git_entrypoints_execution_guard.py` |
| H26 | 会话写入只接受显式 owner 身份，闭合同租户匿名公共域；空白身份视为缺失 | `test_h26_session_write_owner.py` |
| H27 | 子 Agent 观测/取消与 trace 按 tenant+user+会话收窄… | `subagent_authz_test.go`、`trace_owner_test.go` |
| H28 | 冲突 ID 为随机值，读取/裁决/删除按记录内 scope 与调用者双校验… | `test_conflict_scope.py` |
| H29 | 媒体对象键由上传会话推导并校验租户前缀… | `media_*.go` |
| H30 | 语义缓存域含 tenant/user/完整上下文 digest/工具与策略/模型 lin… | `test_gateway_cache_tenant.py` |
| H31 | S3 保存路径单次持锁，故障返回明确错误而非挂起 | `test_media_s3_store.py` |
| H32 | 迁移 DSN 显式环境变量优先、密码只编码一次、默认脱敏输出 | `test_migrations_dsn.py` |
| H33 | 会话写入用 claims 租户；幂等键含租户… | `tenant_identity_guard_test.go`、`session_tenant_guard_test.go` |
| H34 | 队列不再用裸 `MAXLEN` 裁剪未 ACK 任务；降级裁剪有指标与告警 | `test_queue_retention_h34.py` |
| H35 | 引擎注册地址过协议/主机/端口校验与可选 HMAC 签名… | `discovery_trust_test.go`、`settings_broadcast_test.go` |
| H36 | 预算改为原子预占（判断+占位同一段 Lua）→ 失败 release / 成功按实际用量… | `budget_reserve.py`、`test_budget_reserve_h36.py` 等3 |
| H37 | 审批改 `GETDEL`/Lua 单次消费… | `test_approval_single_consume_failsafe.py`、`test_guardian.py` 等3 |
| H39 | SSRF 改为"只放行全球 unicast"… | `test_ssrf_connect_pinning.py` |
| H40 | API Key 校验用户状态；RPA 身份与 owner 过滤 fail-closed | `api_keys_handler.go`、`rpa_authz_test.go` |
| H41 | 双管道并发排空（顺序读会在子进程写满 4KB stderr 时死锁… | `test_isolation_launch_h41.py` |
| H42 | ACP 严格处理 202/JSON/SSE 终态，没有合法终态即失败… | `main_contract_h42_test.go` |
| H43 | `run.py` 的进程/端口判定改精确匹配… | `test_service_identity_h43.py` |
| H45 | 更新清单严格 schema（版本/大小/扩展名/域名白名单/架构）+ 缺 `sha512… | `update-manifest.cjs`、`update-manifest.test.cjs` |
| H46 | 设备桥服务端半补全：`POST /v1/device-fs/ticket` 校验 Ed2… | `device_fs_ticket.go`、`device_fs_ticket_test.go` 等3 |
| H47 | renderer 特权面收敛：IPC 来源判定改为三道… | `ipc-guard-h47.test.cjs` |
| H48 | 非 Windows 平台拒绝破坏性操作（不再"免验证通过"）… | `sensitive-action-h48.test.cjs` |
| H49 | `open-path` 改走 guard：canonical 必须在用户批准的项目根内… | `open-path-guard.test.cjs` |
| H50 | 服务地址传输安全双重校验（保存时 + 建连前）；非 loopback 强制 HTTPS… | `api-url-policy.cjs`、`api-url-policy.test.cjs` 等3 |
| H52 | 数据目录 ACL 显式审计 + 移除非授权 ACE，有残留即 `ok:false`… | `data-dir-acl-gate.test.cjs` |
| H53 | 清空数据范围与界面承诺对齐：`wipeLocalData` 从"只删 `local/` … | `wipe-data-scope.test.cjs` |
| H54 | 本机栈身份挑战：`/ready` 增补实例 ID / 协议版本 / data dir 指… | `instance_identity_test.go`、`runtime-identity-h54.test.cjs` 等3 |
| H55 | WebDAV：`truncated` 使遍历整体失败（PROPFIND 先缓冲再落盘）… | `device_webdav_conflict_test.go` |
| H56 | Windows 子进程树终止：新增 `electron/proc-tree.cjs`… | `proc-tree.test.cjs` |
| H57 | 更新渠道与版本语义：`compareVersions` 改完整 semver 优先级… | `update-semver.test.cjs` |
| H58 | 停止生成真正下发 `POST /v1/agent/interrupt`… | `useTurnStop.spec.ts`、`ChatView.interaction.spec.ts` |
| H59 | 审批/问题/SSE 按 session/run 建图，票据冻结归属并拒绝错配… | `ChatView.scopedStreams.spec.ts`、`interactionScope.spec.ts` 等3 |
| H60 | 重连：`switchSession(id, {force})` 绕开"同 ID 直接返回… | `streamReattach.spec.ts`、`staleGuard.spec.ts` |
| H61 | 票据保留到服务端确认（`ok=false` 也算未确认）；失败可重试… | `pendingReconcile.spec.ts`、`InteractionInbox.authoritative.spec.ts` |
| H62 | 实时输出/错误/日志出口统一脱敏… | `test_redaction_h62.py`、`redact.spec.ts` |
| H63 | Mermaid 二次消毒改用 DOMPurify SVG/MathML profile … | `mermaidSanitize.spec.ts` |
| H64 | 附件 URL 来源与协议约束：新增 `utils/safeAttachmentUrl.t… | `safeAttachmentUrl.spec.ts` |
| H65 | 上传响应契约收口：`file_url` 与分片路径的 `file_url` 都要过来源/… | `mediaUploadUrlContract.spec.ts`、`mediaUpload.spec.ts` |
| H66 | 只对可证明幂等的 GET/带幂等键操作重放，POST/上传/派发不再盲重试 | `nonIdempotentRetry.spec.ts`、`replaySafety.spec.ts` |
| H67 | 私密媒体不再把裸对象 URL 交给 renderer… | `mediaUrlCache.spec.ts` |
| H68 | 认证代次围栏：响应/401/重试/profile/路由守卫写回前比对 generatio… | `authGeneration.spec.ts`、`authGenerationFence.spec.ts` |
| H69 | SSE 静默看门狗（按字节活性）+ 恢复即重连… | `sseWatchdog.spec.ts`、`useSessionSwitch.resync.spec.ts` |
| H70 | 媒体签名 URL 失败处理从永久黑名单改成分类 + 退避状态机… | `mediaUrlRetry.spec.ts`、`MediaView.spec.ts` |
| H71 | 取消粒度改按请求签名（method + url + 规范化 params + 非 GET… | `requestCancelPrecision.spec.ts`、`requestOptimizer.spec.ts` |
| H72 | 上传续传元数据改成带身份信封 `{v, uploadId, owner:{user,te… | `uploadIdentity.spec.ts` |
| H73 | 服务端签发并校验 `plan_digest`… | `checkpoint_plan.go`、`checkpoint_plan_test.go` 等3 |
| H74 | 验证码 Provider 最小 `script/frame/connect` 白名单… | `captchaCspConsistency.spec.ts`、`CaptchaWidget.provider.spec.ts` |
| H75 | 市场内容装完 inert（agent `enabled=false`、插件 `statu… | `install_enable_test.go`、`market_enable_api_test.go` 等4 |
| H76 | 共享层写入按角色分流：admin 就地更新… | `test_skill_shared_write_h76.py` |
| H77 | Agent 绑定 KB 与每次检索都按调用者授权（自己的/public/同租户）… | `agents_kb_access_test.go`、`test_kb_caller_access_h77.py` |
| H78 | 画布归属以权威存储为准：`smLoadWorkspaceOwner` 直读 PG… | `sessionmap_owner_guard_test.go`、`sessionmap_edgekind_guard_test.py` |
| H79 | 云端凭据与 API origin 绑定… | `token-origin.test.cjs` |
| H82 | 收紧保留期改为先预览再确认：新增只读端点 `POST /v1/admin/retenti… | `retention_settings_test.go` |
| H83 | 平台 Owner 专属后台入口三处同口径… | `platformOwnerNav.spec.ts` |
| H86 | 受管 CPython 固定到补丁版本（3.14.4），构建期/运行期两处常量由测试守住… | `runtime-version-pin.test.cjs` |
| H38 | 子 Agent 不再绕过执行链：`SubAgent` 支持注入 `tool_executor`，生产调度器（`AgentDispatcher(runtime=…)`）注入**走 guard 链**的执行器，与主 Agent 同口径的 ToolGuard / 服务端授权 / 副作用账本；后台不可交互，「需要确认」降级为**显式拒绝**。undo 三处收口：快照存 **base64 原始字节**（二进制不再被替换坏）、`pop_and_restore` 失败**回栈可重试**、写入用 `O_NOFOLLOW` 关掉 TOCTOU 窗口 | `test_subagent_chain_undo_h38.py`（10 项）、子 Agent/undo 相关 39 项 |
| H44 | 四块收口：① 租户导出**先落临时文件**再决定能否给（行/字节上限 + 每租户单并发，超限返回 413 而不是半截 zip）；② 审计 sink 失败批**回队重试**、缓冲有界、丢弃计数可见（不再一次 PG 抖动就整批丢）；③ turn 用量 `DELETE + INSERT` **同事务**（不再少记半轮）；④ 记忆写入（条目/摘要/画像）统一走 `memory_writes_allowed()`，`no_retention` 全链路不落 PG；PageIndex 引用**解析不出即拒绝**（原 fail-open 放行） | `admin_export_limits_h44_test.go`、`audit_sink_retry_h44_test.go`、`usage_ingest_atomicity_h44_test.go`、`test_citation_scope_h44.py`、`test_memory_retention_h44.py` |

## 10. 参考

- OpenAI Codex Sandbox：<https://tonybai.com/2026/11/19/inside-openai-codex-sandbox/>
- Hosted Agents 边界 / OpenHands V1：<https://xwang.dev/blog/2026/openhands-sdk-claude-managed-agents/>
- Baseten Sandboxes：<https://www.baseten.co/blog/building-the-agent-cloud-introducing-baseten-sandboxes>
- 多租户 Agent 隔离：<https://niteagent.com/blog/multi-tenant-agent-platform-architecture-2026/>
