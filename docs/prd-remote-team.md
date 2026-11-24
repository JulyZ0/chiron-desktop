# PRD：Chiron 远程执行与团队服务模式

- 状态：S0–S4、R1/R3/R4/R5 已实现；R6/R7 的观测与配额、R9 span/trace、R10 导出已落地。
- 待团队主机：R2 实测切换、R6 压测、Web E2E。
- 暂缓：LiteLLM、Langfuse 实接、R10 租户删除、S5。
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
  - 边界：每 op 带 root，设备侧 realpath+词法包含校验；读 8MiB、walk 5000 文件/200MiB、回写 4MiB、写带 `expected_hash`；git 参数由服务端构造（≤64 项、单项 ≤2048 字节），输出 8MiB、超时 30s，非零退出码按结果返回。
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
| 代码执行 | 服务端沙箱（Windows 低完整性/Job Object；Linux bwrap/gVisor）+ 租户并发槽 | 暖池/会话复用、CPU/内存/时长硬限 | 超时杀进程 |
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

## 9. 参考

- OpenAI Codex Sandbox：<https://tonybai.com/2026/11/19/inside-openai-codex-sandbox/>
- Hosted Agents 边界 / OpenHands V1：<https://xwang.dev/blog/2026/openhands-sdk-claude-managed-agents/>
- Baseten Sandboxes：<https://www.baseten.co/blog/building-the-agent-cloud-introducing-baseten-sandboxes>
- 多租户 Agent 隔离：<https://niteagent.com/blog/multi-tenant-agent-platform-architecture-2026/>
