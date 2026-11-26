# PRD：Chiron 远程执行与团队服务模式 —— 剩余工作

> 本文件只保留**未完成**的项。已完成项（H1–H86 中已验收的 78 项）与逐条过程描述已移除：
> 它们的结论在 `git log` 的提交信息里（每条都带 ID 与"修前会怎样"），证据在对应测试文件里。
> 契约版本见 `contracts/versioned.json`，外部验收入口见 `scripts/team_acceptance.py`。

## 1. 当前状态（一段话）

S0–S4 与 R1/R3/R4/R5 已实现；加固审计 H1–H86 的**可本机验证部分已全部收口**，
本机可做的 W1–W3 与对齐 deepseek-harness 的第一批（G1–G9）已全部完成，第二轮差距
**G10–G30 已全部收口**（G22/G24 明确不做、G25 并入 H2）。2026-11-26 的 A 波
（知识库索引链路、存量状态归一、多租户归属、Provider 路由亲和）也已合入；剩下的
`H*` 需要**真机/外部服务**（H2）、**需要外部系统选型**（H20）或**需要产品决策**（H19/H22）。

## 2. 剩余项

### 2.1 与 deepseek-harness 的差距（`G*`）

历史差距分析与 DSH 循环考古已移除（结论已并入本文件与 git 历史）；可本机验证的 G 项
均已合入。以下三项**不是待做项**，而是记录当前决定（见 §3.1）。

| ID | 事项 | 决定 | 卡点 |
|---|---|---|---|
| **G22** | MCP 并入统一注册表 | 不做：动 registry 风险中高，`use_capability` 双路径当前可用 | — |
| **G24** | 定时触发原语 | 不做：不进本版本 | — |
| **G25** | 沙箱缺后端即拒跑 | 并入 H2：与 Linux 内核级隔离一次做完，不单独立项 | 见 §3 第 3 条；会让现有 Linux 部署不可用，需开关 |

### 2.2 原有未完成项（`H*`）

| ID | P | 事项 | 完成定义 | 卡点 |
|---|---|---|---|---|
| **H2** | P0 | Linux 内核级执行隔离 | 不可用即**拒绝执行**（不降级）；有"沙箱不可用即拒"探针 | 需 Linux 真机 |
| **H20** | P1 | Provider 密钥加密存储 | 密文落库 + 信封加密 + 可插拔 KMS + 轮换；有"明文永不落盘"回归 | 需先定 KMS（3.2） |
| **H19** | P3 | ACP 完整 Agent 协议 | 编辑器侧拿到工具调用/审批/终态 | 需产品决策（3.4） |
| **H22** | P3 | 只读子 Agent 强隔离 | **构造性**禁止写（非提示词），越权返回明确错误 | 需产品决策（3.4） |


### 2.3 并行推进计划（worktree + 多 agent）

本轮分组推进的 12 个 worktree 已**全部合入**（逐项明细与"修前会怎样"见对应 merge / feat
提交信息，证据在各自测试文件）；当前**没有在飞的分支**。未列出的 `H*` 依赖外部条件（见 §3）。

合并纪律（保留给下一轮）：① 每个分支自己跑完窄回归；② 合并后我在 main 上跑跨包回归
（Go `./internal/...`、Python 相关套件、前端 `src/components` + `check:ui`）；③ 冲突时以
"语义正确"优先，不保留两套实现；④ 每项合入后**立即**从本文件删除对应行。

### 2.4 下一批开发任务（建议顺序）

排序原则：**先修"用户被挡在门外"的 → 再做安全/平台（等决策或环境）→ 最后外部验收与体验**。
每个任务都写可执行判据；三条线上故障（会话归项目 404 / OpenCode 400 / 知识库索引失败）
已修复，这里只列它们的**收尾项**。

#### 波次 A：本地可验证的故障收尾（✅ 已合入 2026-11-26）

| # | 事项 | 落地内容 | 证据 |
|---|---|---|---|
| A1 | 知识库端到端可信 | integration 的 KB 探针失败即 FAIL（不再 SKIP）；建索引前等 worker 心跳、轮询中 `worker_alive=false` 立即失败；`/status` 无条件下发 `worker_alive`（10s 缓存，Cloud 为 null）；页面在执行器未运行时提前给 warning 提示（与库级 error 判重） | `scripts/integration/run.sh`、`knowledge/service.py`、`KnowledgeDetailView.vue`、`test_knowledge_worker_alive.py`、`KnowledgeDetailView.index.spec.ts` |
| A2 | 存量索引状态归一 | 读侧未知库状态 → error、大小写归一；迁移 0015 把库级未知值→error、文档级→pending（幂等） | `progress.py`、`0015_kb_status_normalize.py`、`test_knowledge_index_progress.py` |
| A3 | 多租户 / 归属口径统一 | 隐式建会话改用 `tenantIDFromContext(ctx)`（claims 优先，0/无 claims 回退默认）；源码守卫 + ctx 优先级单测；集成带新增 tenant=2 隐式建会话探针 | `internal/session/tenant_context.go`、`manager_messages.go`、`run.sh`、`mint_token.py` |
| A4 | Provider 会话亲和 | `llm_session_scope()`：显式会话优先，否则按 scope+输入哈希+trace 固定路由；覆盖记忆摘要/蒸馏、上下文压缩、媒体分析、技能生成、provider 探活、agents 派发；**静态清单**要求每个直调点在 scoped/self-managed/inherit 中表态 | `providers/session_context.py`、`test_llm_session_affinity.py` |

> **唯一未在本机验证**：`make integration` 需要 docker 守护进程（本机未启动）。
> 已做 `bash -n` + 逐段静态检查，需在 CI 或有 docker 的主机跑一次 A1/A3 的探针。

#### 波次 B：安全与平台能力（P1，卡在决策 / 环境）

| # | 事项 | 完成定义 | 卡点 |
|---|---|---|---|
| B1 | H20 密钥加密 | 密文落库 + 信封加密 + 可插拔 KMS + 轮换；"明文永不落盘"回归 | §3 第 2 条（KMS 选型） |
| B2 | H2 / G25 Linux 隔离 | `bwrap` 不可用即**拒绝执行**（不降级）；提供默认关的开关避免现有部署直接不可用；"沙箱不可用即拒"探针 | 需 Linux 真机 |
| B3 | H51 发布签名根 | 私钥归属 + 公钥内嵌 + 清单验签真正生效（代码已按 fail-closed 实现） | §3 第 1 条（组织决策） |
| B4 | H19 / H22 | ACP 完整协议；构造性只读子 Agent（越权返回明确错误） | §3 第 4 条（产品决策） |

#### 波次 C：外部验收与运维（P1/P2）

| # | 事项 | 完成定义 |
|---|---|---|
| C1 | 团队主机验收 | R2 挂载基准、`team_smoke --sse`、R6 压测三项跑出报告；阈值显式、SKIP 不计 PASS |
| C2 | 租户删除与多实例 | R10 租户删除闭环；S5 多实例演练（含子 Agent 跨实例取消回执） |
| C3 | 观测补齐 | LiteLLM / Langfuse 实例级验证；KB 队列深度、worker 心跳、丢失事件进看板 |

#### 波次 D：体验增强（P2，可插队）

| # | 事项 | 完成定义 |
|---|---|---|
| D1 | 父→子 steer 对外入口 | 把 `steer(run_id)` 暴露为受控入口（HTTP `run_id` 路由或 read 级工具）；只允许**本会话的子 run**，越权 403；有"父改子指令、子下一轮生效"端到端用例 |
| D2 | 子会话并排阅读 | 子 Agent 的完整消息流可在参考栏只读回放（需要新接口，不能复用普通会话 API）；含分页与脱敏 |

**下一批怎么排**

A 波已全部合入并跑过跨包回归（Go `./internal/...`、Python 相关套件、pageindex、前端全量）。
当前**没有在飞的 worktree**；能立即开工的只有：

1. **C1 的一次真实集成跑**（需要 docker 主机）：确认 A1 的失败语义与 A3 的 tenant=2 探针；
2. **D1 父→子 steer 对外入口**（不依赖决策，但要动 tool_policy/路由）；
3. **B1–B4 待你拍板**（§3 的四条）；决策到位后按 §2.3 的分支纪律各开一个 worktree。

合并纪律沿用 §2.3。


### 2.5 执行隔离：`container` 挡位（Kiri，2026-11-26 定）

**决定**：新增 `container` 隔离挡位，运行时用 Kiri（Docker Engine API）。Kiri 与 docker
**都不内置**，缺依赖时按需下载。

- **语义**：每条命令放进一次性容器 —— 独立 namespaces/cgroup、只读根文件系统、
  `--network none`、`CAP_DROP=ALL`、`no-new-privileges`、pids/memory 上限、只挂工作区、
  默认 seccomp。规格**不可放宽**（没有 privileged / cap-add / host network 开关）。
- **依赖获取**：`POST /v1/system/isolation/container/prepare` 按平台下载**固定版本**
  `0.2.2` 的官方包到缓存目录，并用**内嵌 SHA-256** 校验（不信任远端校验和）；
  **不自动提权安装** —— 响应里的 `install_hint` 是给人/流水线执行的命令
  （Linux：`dpkg -i`/`rpm -U` + `kiri host-access`；macOS：dmg 手动）。
- **fail-closed**：挡位请求了但运行时不可用 ⇒ `effective=none + below_floor=true`，
  启动门禁拒绝启动、执行侧拒跑（`sandbox unavailable: container isolation requested
  but not available`），**不回退**到无命名空间的本地执行。
- **部署前提**：引擎（或 sandbox 服务）需要能访问 Engine socket
  （`SANDBOX_CONTAINER_SOCKET` / `KIRI_SOCKET`，Linux 默认 `/run/kiri/docker.sock`）；
  镜像需预先拉取，用 `SANDBOX_CONTAINER_IMAGE` 指定（默认 `python:3.12-slim`，
  团队部署应按项目工具链自建）；`SANDBOX_CONTAINER_USER`/`_MEMORY_MB`/`_PIDS` 可调。
- **边界（别夸大）**：Linux 上 Kiri 容器仍跑在**宿主内核**（与 Docker 同一隔离档），
  不是 VM；若目标是"不可信多租户代码"，需要 gVisor/Kata/Firecracker 这一档。
  该挡位目前**只支持 Unix socket**：Windows 的 Kiri 走命名管道，本版没有驱动，
  界面会明确标为不可用。桌面端 Windows 继续用 appcontainer/restricted。
- **验证**：单测覆盖检测/哈希校验/容器规格/失败拒跑/超时杀容器；
  **真实 Kiri 上的端到端（无网络、只写工作区、pids/memory、超时、daemon 停掉即拒）
  仍需在 Linux 主机跑一次**（本机未装运行时）。

## 3. 待确认决策

1. **H51 签名根**：发布签名私钥谁持有、公钥如何内嵌进安装包？（决定组件/更新清单验签
   是否真正生效 —— 代码已按"公钥内嵌 + 缺失即拒绝"实现，缺的是这把钥匙的归属）
2. **H20 KMS 选型**：「进程内信封加密 + 可插拔 KMS 接口」还是直接接云 KMS SDK？
3. **H2 / G25 隔离承诺**：是否接受「bwrap 不可用即拒绝执行」作为首个可发货版本？
   （G25 会让现有 Linux 部署直接不可用，需不需要先加默认关的开关？）
4. **H19 / H22 产品承诺**：ACP 完整集成、严格只读子 Agent 是否本版本目标？

### 3.1 已定（2026-11-25）
- **已验证无需追平**（记录以免重复讨论）：① SSRF——我们已有全球 unicast 白名单 + scheme/端口白名单 + 连接级 IP 钉住 + **重定向逐跳校验**，而 DSH 的 `web-fetch-http` 明确不限制目标地址；② 代码定位——我们已有 `outline`（结构行索引）工具，DSH 没有，故不引入符号索引/LSP 前置依赖。
- **G20 做**（已合入）：补 `agent_runs` 行，允许子 Agent 挂起等人工（不再超时即失败）。
- **G23 做**（已合入）：常驻 LSP。


## 4. 外部验收（需团队主机）

本机只能静态检查。统一入口 `scripts/team_acceptance.py`（退出码 0=无 FAIL 且至少一项
PASS、1=有 FAIL、77=全部 SKIP；**SKIP 绝不当 PASS**）。

```bat
set CHIRON_OWNER_PASSWORD=<owner 密码>
python scripts/team_acceptance.py --base-url https://<域名> --email owner@<公司> ^
    --r2-root R:\\project --sse --strict-web --json-out .artifacts/team-acceptance.json
```

| 项 | 脚本 | 通过判据 |
|---|---|---|
| R2 实测挂载 | `r2_mount_bench.py` | read_p95≤50ms、write_p95≤150ms、`rev-parse` 为 true、rename/delete 有效、10 分钟稳定 |
| 团队 Web E2E | `team_smoke.py`（`--sse`） | 登录/改密/邀请/强制下线/匿名分享/分享页 HTML/首 token 逐项 PASS |
| R6 压测 | `--r6-report` | P=2/4 首 token P95、引擎 RSS、空闲回收、`/metrics` 齐全（阈值显式给） |

## 5. 已知未覆盖

LiteLLM 与 Langfuse 的实例级验证、R10 租户删除闭环、S5 多实例演练。

## 6. 参考

- OpenAI Codex Sandbox：<https://tonybai.com/2026/11/19/inside-openai-codex-sandbox/>
- Hosted Agents 边界 / OpenHands V1：<https://xwang.dev/blog/2026/openhands-sdk-claude-managed-agents/>
