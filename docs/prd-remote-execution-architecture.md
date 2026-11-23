# PRD：远程/团队执行架构（现状与剩余任务）

- 状态：S0–S4 已实现；S5 未开始
- 关联：`internal/api/device_fs.go`、`project_mirror.go`、`sessionmap_projects.go`、`frontend-vue/electron/device-fs.cjs`、`local-stack.cjs`、`python-engine/app/idle_shutdown.py`
- 契约：`docs/desktop-local/contracts.md` §1.1/§1.2

## 1. 架构一页

```
本地模式：Electron + 引导器(PG/Redis) + Go 网关 + Python 引擎 + 项目目录（同机）

远程模式：Electron（仅界面）
   └─ /v1 请求 ──► 服务端 Go 网关 ──► Python 引擎池 ──► PG / Redis
                              │
                              └─ /ws/device-fs ◄── 桌面设备桥 ──► 本机项目目录
```

- **agent 循环、工具、git/run_code 都在引擎侧**；桌面端不执行工具。
- client 项目的文件读取：`@引用` 直读设备；引擎工具走**按回合镜像**（S2 过渡）。
- 多用户：引擎自注册 + 网关发现，按 session 一致性哈希/run 归属分流。

## 2. 项目身份（解决路径冲突）

```
ProjectLocator { project_id, provider: server|client, device_id?, root, root_key }
```

- 项目身份是 `project_id`，不是路径字符串；引擎看虚拟身份 + `project_locator`。
- 判重键：`(tenant, user, provider, device_id, root_key_hash)`。
  - `root_key` 由**路径归属方**规范化：server 侧 `Clean→EvalSymlinks→大小写折叠`；client 侧由设备桥按本机 OS 计算。
  - 唯一索引落 sha256（`root_key_hash`），避免 2KB 中文路径撞 btree 行宽上限。
- 写入前判重 + 数据库唯一索引兜底；`23505` 统一映射 409 `project_duplicate`。
- 允许不同用户/不同设备同名路径；拒绝同设备大小写/`..`/junction 改写。
- 迁移 `0011_project_locator`；历史行由首次判重惰性回填。

## 3. 文件访问

### 3.1 设备桥（只读 + 写入原语）

- 桌面主动外连 `GET /ws/device-fs?token=…&device_id=…`（与 `/ws/rpa` 同鉴权口径）。
- 原语：`canonicalize / stat / list / read / walk / hash / write`。
- 边界：每个 op 带 `root`，设备侧做包含校验（realpath + 词法，拒绝 symlink 逃逸）；
  单文件读 1MiB、walk 上限 5000 文件 / 200MiB、回写单文件 4MiB。
- 写入带 `expected_hash` 乐观并发；冲突返回 `conflict`，不覆盖。

### 3.2 引擎访问 client 项目（S2 过渡：按回合镜像）

| 阶段 | 行为 |
|---|---|
| 回合前 | 设备 `walk+read` → 服务端临时目录，记录设备侧 sha256 基线 |
| 回合中 | 引擎工具在镜像上正常执行（fs/git/run_code） |
| 回合后 | 本地差异 → 设备 `write(expected_hash=基线)`；冲突保留并汇总 |
| 回滚 | 检查点恢复后立即回写镜像差异 |

镜像上限超限时**明确拒绝本轮**，不回退沙箱（避免 agent 改到假项目）。

### 3.3 已知边界

- client 项目“项目文件浏览”接口仍 501（`@引用` 与引擎工具已可用）。
- **删除/重命名不回写**；镜像是按回合副本，不是长期双向同步。
- 后续方向：用逐操作 `ProjectFS` 取代镜像（PRD 原目标），见 R2。

## 4. 多用户执行

- **引擎池**：`CHIRON_ENGINE_POOL_SIZE`（1–4）拉起额外引擎；引擎写
  `ENGINE_ADVERTISE_URL` 自注册 `engine:instance:*`，网关每 15s 发现。
- **路由**：同 session 一致性哈希粘滞（SSE/统一链路）；审批/取消按 `engine:run:*` 归属；
  实例故障回退哈希。
- **空闲回收**：池副本连续空闲 `CHIRON_ENGINE_IDLE_SECONDS` 后退出码 75，启动器缩容不重拉。
- **故障恢复**：primary 由监督器重拉；池副本原端口自愈；会话锁带心跳/TTL。
- **配额**：全局并发 `AGENT_MAX_CONCURRENCY`；每用户并发
  `AGENT_MAX_CONCURRENCY_PER_USER`（默认 2，超限 429）；run token 预算 `AGENT_RUN_MAX_TOKENS`（默认 400k）。

本机空载基线（2026-11-23）：引擎 262MB、网关 40MB、launcher 60MB、pageindex ~123MB、PG ~150MB、Redis 15MB。

## 5. 安全与性能默认值

| 风险层 | 隔离/授权 | 性能策略 | 失败默认 |
|---|---|---|---|
| 读元数据/项目读 | 项目只读租约、根白名单 | 元数据缓存、按需分块、不整仓同步 | 离线明确报错 |
| 项目写 | 写租约 + hash 冲突 | 批处理、乐观并发、审计异步 | 冲突不覆盖 |
| 代码执行 | 服务端沙箱（Windows 低完整性/Job Object；Linux bwrap/gVisor） | 暖池/会话复用、CPU/内存/时长硬限 | 超时杀进程 |
| 网络/安装 | 默认拒绝 + 域名白名单代理 + 凭证注入 | 连接复用 | 拒绝并给申请路径 |
| MCP/插件 | 独立沙箱、固定版本、默认无网 | 常驻连接 | 单工具失败隔离 |

原则：**授权在边界做一次；写/网络 fail-closed；读/状态 fail-fast；不因性能关掉越界检查。**

## 6. 运行开关

| 变量 | 默认 | 作用 |
|---|---|---|
| `CHIRON_ENGINE_POOL_SIZE` | 1 | 引擎池规模 1–4 |
| `CHIRON_ENGINE_IDLE_SECONDS` | 1800 | 池副本空闲自停 |
| `AGENT_MAX_CONCURRENCY` | 20 | 全局同时运行回合 |
| `AGENT_MAX_CONCURRENCY_PER_USER` | 2 | 每用户同时运行回合（0=不限） |
| `AGENT_RUN_MAX_TOKENS` | 400000 | 单 run token 预算 |
| `CHIRON_MIRROR_ROOT` | 系统临时目录 | client 项目镜像根 |

部署：新构建启动自动 `alembic upgrade head`；源码团队主机需先手动执行。

## 7. 实现状态

| 阶段 | 内容 | 提交 |
|---|---|---|
| S0 | ProjectLocator / 迁移 / 引擎上下文 | `258d37b` |
| S1 | 设备隧道、client 项目、`@引用` 只读 | `a77e6b4` |
| S2 | 写入/冲突、按回合镜像、检查点回写 | `09d6693`、`74e48b9` |
| S3 | 引擎池启动、粘滞、空闲缩容 | `7511f1b`、`74e48b9` |
| S4 | 每用户并发配额、故障恢复 | `7511f1b` |
| S5 | chiron-server BFF 评估 | 未开始 |

## 8. 接下来的任务

### P0（先补齐可用性闭环）

1. **R2 WebDAV+rclone spike（先行）**：网关用 `golang.org/x/net/webdav` 把 client 项目暴露为本机 WebDAV；rclone mount（Windows 走 WinFsp）挂成普通目录，Python 工具零改动。决策门见 §8.2。
2. **R1 client 项目文件浏览**：优先复用挂载目录；若 spike 不通过，再把 `ListProjectFiles/ReadProjectFile/RawProjectFile` 接到设备桥（去掉 501）。
3. **R3 删除/重命名与二进制**：走挂载则免费获得；若保留 RPC 则设备桥补 `remove/rename` 并让 sync-back 支持删除。
4. **R4 多副本设备注册表**：当前在进程内；多网关副本时改 Redis pub/sub 路由，避免请求落到不持连接的实例。

### 8.2 R2 spike：WebDAV + rclone 决策门

| 步骤 | 内容 | 通过标准 |
|---|---|---|
| 1 | 网关 `webdav.FileSystem` 实现：Stat/Readdir/OpenFile/RemoveAll/Rename，全部经设备桥 | Go 单测覆盖读/写/列/改名/删除/越界拒绝 |
| 2 | `/v1/device-webdav/{projectID}/` 仅回环 + 内部令牌/设备会话鉴权 | 未授权 401；越界 403 |
| 3 | `rclone mount :webdav:` + WinFsp（团队主机） | 现有 fs/git/run_code 在挂载目录可用；rename/delete 生效 |
| 4 | 性能与稳定性 | 单文件读/写 P95 ≤50/150ms（局域网）；10 分钟连续操作无断连；小文件批量写不把网关打满 |

**决策**：通过 ⇒ 删除按回合镜像，`root_path` 直接指向挂载点；不通过（WinFsp 部署不可接受 / 性能不达标）⇒ 用 `winfsp/cgofuse` 进程内实现重试，仍不通过则保留镜像并回到 RPC 方案。

**当前进度**：facade 已实现（`/v1/device-webdav/{projectID}/`，内部令牌 + 仅 client 项目；Stat/Readdir/OpenFile/RemoveAll/Rename 全走设备桥）。Go 测试覆盖读写/列目录/改名/删除/越界拒绝与 PROPFIND。**决策（临时）**：facade 侧通过；rclone+WinFsp 的实际挂载/性能属团队主机硬件验证，完成前**保留按回合镜像为默认路径**，不切换 `root_path`。团队主机实测通过后再删除镜像。

本机挂载示例（rclone 需 WinFsp；token 取网关 `APP_SECRET` 派生的 internal token）：

```powershell
rclone config create chiron webdav `
  url "http://127.0.0.1:<gatewayPort>/v1/device-webdav/<projectID>" `
  vendor other user dummy pass "$(rclone obscure dummy)" --non-interactive
rclone mount chiron: X: --vfs-cache-mode writes `
  --header "X-Internal-Token: <internal-token>"
```

### P1（团队规模与治理）

5. **R5 设备写授权/审批**：首次写确认、项目/设备级租约、企业权限上限；审计每次设备读写。
6. **R6 引擎池实测与自动扩缩**：P=2/4 压测、P95 首 token、逐引擎 RSS、空闲回收命中；按负载扩容（目前固定规模）。
7. **R7 分层配额**：模型 RPM/TPM、工具并发、沙箱并发、费用熔断（当前只有用户/全局并发 + token 预算）。

### P2（长期方向）

8. **R8 chiron-server BFF spike（S5）**：`/v1`→`/api/v1` 映射 auth/session/run/model，限时 2 周，输出差异清单与 go/no-go。
9. **R9 可观测性**：模型→工具→沙箱/设备桥→持久化统一 action/trace id；失败链路全量、成功采样。
10. **R10 数据生命周期**：团队模式租户导出/删除/保留期；client 文件默认不落服务端。

### 8.1 开源复用评估（避免手搓）

| 任务 | 可复用 | 决策 |
|---|---|---|
| R1/R3 文件浏览/删除 | 无现成（安全边界在我们自己的设备桥）；删除/重命名可由挂载层免费获得 | 不引入；R3 若保留 RPC 只补 `remove/rename` |
| R2 虚拟 FS | `golang.org/x/net/webdav` + `rclone mount`（Windows 走 WinFsp）；或 `winfsp/cgofuse`/`hanwen/go-fuse` 进程内实现 | **先做 WebDAV+rclone spike**：Python 零改动、mtime/rename/delete 齐全；若 sidecar/性能不可接受，改用 cgofuse。不采用 Mutagen（daemon 重）与 rclone bisync（冲突语义不稳） |
| R4 多副本设备路由 | `redis/go-redis` pub/sub；`centrifugal/centrifuge` | 保留自研连接表 + Redis 请求/应答通道（约百行）；Centrifugo 是 pub/sub/presence 服务器，不是 RPC 路由器，当前规模不值得引入 |
| R5 工具/MCP 隔离 | `microsoft/mxc`（Windows）、`google/gvisor`/`bubblewrap`、`kubernetes-sigs/agent-sandbox` | 采用 OS 级沙箱后端；审批/租约仍自研（Casbin/OpenFGA 对当前模型过重） |
| R6 池观测/扩缩 | `shirou/gopsutil` + Prometheus；K8s HPA/KEDA（若上 K8s） | 单机 Windows 保留现有启动器；补 gopsutil RSS/队列指标。不引入 Nomad |
| R7 分层配额 | `go-redis/redis_rate`（GCRA 速率）；模型预算/多 Key 失败转移用 LiteLLM | 并发仍用信号量（并发不是速率）；HTTP/模型 RPM 用 redis_rate；LiteLLM 已在依赖链中，优先启用其 budget/fallback 而非自研 key 池 |
| R8 chiron-server | 已内置租户配额、沙箱档位、审计 | 直接复用其数据面，不再在 Go/Python 栈造多租户 |
| R9 可观测性 | 现有 OpenTelemetry（Go/Python 已接）+ **OTel GenAI 语义约定** + Langfuse（OTLP） | 不自建 trace UI；统一 `gen_ai.*` 属性，失败链路全量、成功采样 |
| R10 数据生命周期 | 无专门 OSS；PG dump/retention 够用 | 不引入 |

原则：**只有“协议/内核级能力”才引入 OSS（WebDAV/FUSE/WinFsp、gVisor/MXC、OTel）；业务边界（设备租约、审批、冲突语义）保持自研**，避免把安全语义交给第三方黑盒。

## 9. 未决

- 文件桥 v1 是否允许客户端 `git`（建议先只做读写/补丁）。
- 项目换设备：重新授权即可，还是需要内容同步（当前：重新授权）。
- chiron-server 走 BFF 还是直连（建议 BFF）。
- 团队主机是否允许 headless 部署（当前仍随桌面程序启动）。

## 10. 参考

- OpenAI Codex Sandbox：<https://tonybai.com/2026/11/19/inside-openai-codex-sandbox/>
- Hosted Agents 边界 / OpenHands V1 Workspace：<https://xwang.dev/blog/2026/openhands-sdk-claude-managed-agents/>
- Baseten Sandboxes（microVM、暂停恢复、缩零）：<https://www.baseten.co/blog/building-the-agent-cloud-introducing-baseten-sandboxes>
- 多租户 Agent 隔离模式：<https://niteagent.com/blog/multi-tenant-agent-platform-architecture-2026/>
- OWASP Top 10 for Agentic Applications 2026：<https://cycode.com/blog/owasp-top-10-agentic-applications/>
