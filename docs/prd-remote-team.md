# PRD：Chiron 远程执行与团队服务模式 —— 剩余工作

> 本文件只保留**未完成**的项。已完成项（H1–H86 中已验收的 78 项）与逐条过程描述已移除：
> 它们的结论在 `git log` 的提交信息里（每条都带 ID 与"修前会怎样"），证据在对应测试文件里。
> 契约版本见 `contracts/versioned.json`，外部验收入口见 `scripts/team_acceptance.py`。

## 1. 当前状态（一段话）

S0–S4 与 R1/R3/R4/R5 已实现；加固审计 H1–H86 的**可本机验证部分已全部收口**，
本机可做的 W1–W3 与对齐 deepseek-harness 的第一批（G1–G9）已全部完成，第二轮差距
**G10–G30 已全部收口**（G22/G24 明确不做、G25 并入 H2）。`H*` 剩下的都需要
**真机/外部服务**（H2）、**需要外部系统选型**（H20）或**需要产品决策**（H19/H22）。

## 2. 剩余项

### 2.1 与 deepseek-harness 的差距（`G*`）

来源：[gap-analysis-chiron-vs-dsh.md](gap-analysis-chiron-vs-dsh.md)、[gap-analysis-r2-subagent-frontend.md](gap-analysis-r2-subagent-frontend.md)。
可本机验证的 G 项已全部合入；以下三项**不是待做项**，而是记录当前决定（见 §3.1）。

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
