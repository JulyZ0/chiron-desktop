# PRD：Chiron 远程执行与团队服务模式 —— 剩余工作

> 本文件只保留**未完成**的项。已完成项（H1–H86 中已验收的 78 项）与逐条过程描述已移除：
> 它们的结论在 `git log` 的提交信息里（每条都带 ID 与"修前会怎样"），证据在对应测试文件里。
> 契约版本见 `contracts/versioned.json`，外部验收入口见 `scripts/team_acceptance.py`。

## 1. 当前状态（一段话）

S0–S4 与 R1/R3/R4/R5 已实现；加固审计 H1–H86 的**可本机验证部分已全部收口**，
本机可做的 W1–W3（对外安全门禁、执行可靠性、设备与契约）已完成。剩下的是
**需要真机/外部服务**（H2）、**需要外部系统选型**（H20）、**需要产品决策**（H19/H22）
以及**契约扩容**（H16b）。

## 2. 剩余项（5 项）

| ID | P | 事项 | 完成定义（可执行判据） | 卡点 |
|---|---|---|---|---|
| **H16b** | P2 | 契约 v2：把 `tool_approval` / `checkpoint` / `queue` / `mcp` 四族纳入 | `contracts/versioned.json` 的 `families` 含这四族；`contracts/fixtures/conformance.json` 每族 ≥4 例；Go `internal/contract` 与 Python `app/contract` 跑同一夹具全绿 | 无（本机可做，但字段仍在演进，先等稳定） |
| **H2** | P0 | Linux 内核级执行隔离 | bwrap/gVisor 可用时执行、不可用时**拒绝执行**（不是降级到无隔离）；`python-engine/tests` 里有一组"沙箱不可用即拒"的探针 | 需 Linux 真机 |
| **H20** | P1 | Provider 密钥加密存储 | 密文落库、进程内信封加密、可插拔 KMS 接口、轮换；`internal/api` 侧有"明文永不落盘"的回归 | 需先定 KMS 方案（见 3.2） |
| **H19** | P3 | ACP 完整 Agent 协议 | 编辑器侧能拿到工具调用/审批/终态（不只是一次性文本）；协议不缺字段 | 需产品决策（见 3.4） |
| **H22** | P3 | 只读子 Agent 强隔离 | 严格只读时**构造性**禁止写（不是靠提示词），越权尝试返回明确错误 | 需产品决策（见 3.4） |

## 3. 待确认决策

1. **H51 签名根**：发布签名私钥谁持有、公钥如何内嵌进安装包？（决定组件/更新清单验签
   是否真正生效 —— 代码已按"公钥内嵌 + 缺失即拒绝"实现，缺的是这把钥匙的归属）
2. **H20 KMS 选型**：「进程内信封加密 + 可插拔 KMS 接口」还是直接接云 KMS SDK？
3. **H2 隔离承诺**：是否接受「bwrap 不可用即拒绝执行」作为首个可发货版本？
4. **H19 / H22 产品承诺**：ACP 完整集成、严格只读子 Agent 是否本版本目标？
   若不承诺，两项维持暂缓而不是"做一半"。
5. **H16b 时机**：四族里的 `queue` / `checkpoint` 字段还在动；是等稳定再纳入，
   还是先纳入 `tool_approval` / `mcp` 两族？

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
