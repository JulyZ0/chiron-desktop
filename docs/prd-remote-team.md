# PRD：Chiron 远程执行与团队服务模式 —— 剩余工作

> 本文件只保留**未完成**的项。已完成项（H1–H86 中已验收的 78 项）与逐条过程描述已移除：
> 它们的结论在 `git log` 的提交信息里（每条都带 ID 与"修前会怎样"），证据在对应测试文件里。
> 契约版本见 `contracts/versioned.json`，外部验收入口见 `scripts/team_acceptance.py`。

## 1. 当前状态（一段话）

S0–S4 与 R1/R3/R4/R5 已实现；加固审计 H1–H86 的**可本机验证部分已全部收口**，
本机可做的 W1–W3 与对齐 deepseek-harness 的第一批（G1–G9）已全部完成。剩下的是
**需要真机/外部服务**（H2）、**需要外部系统选型**（H20）、**需要产品决策**（H19/H22）
以及**契约扩容**（H16b）。

## 2. 剩余项

### 2.1 与 deepseek-harness 的可追平差距（`G*`，**按实践容易程度排序**）

来源：[gap-analysis-chiron-vs-dsh.md](gap-analysis-chiron-vs-dsh.md)、[gap-analysis-r2-subagent-frontend.md](gap-analysis-r2-subagent-frontend.md)。
"完成定义"必须是**可执行判据**；每项落地时要有一条"修前会失败"的回归。

**第一批：容易（单点改动 / 本机可验证 / 无迁移）**

| ID | 事项 | 完成定义 | 落点 |
|---|---|---|---|

**第二批：中等（需设计 / 多文件 / 迁移）**

| ID | 事项 | 完成定义 | 落点 |
|---|---|---|---|
| **G11** | 子副作用并入父账本 | 子 Agent 的写入同时记父会话键（或按 lineage 解析），整轮 `/undo` 覆盖子 Agent 写过的文件；有跨会话回滚测试 | `undo_stack.py`、`side_effect_ledger.py` |
| **G14** | 工具结果 spill 统一 | 超过阈值一律落盘 + 上下文只放头尾 + locator（复用 `read_tool_result` 分段取回）；所有工具统一走同一出口 | 新增 `app/tools/spill.py`、`registry.py` |
| **G15** | 并发安全由工具声明 | 并行从"按动作级推断"改为"工具显式声明 `is_concurrency_safe`"，未声明一律串行；漏声明只降速不放错 | `app/tools/registry.py` + 各写工具 |
| **G17** | 预览按 offset 分页 | 文本类预览支持 `offset` 分页 + 前端增量渲染（对齐工作台已有做法），KB 预览不再整块 | Go 预览接口 + `KBDocumentPreview.vue` |
| **G18** | 前端布局解算纯函数化 | 断点与宽度常量集中到一处纯函数（现状字面量散落 1024/768/576/1280），`useResponsive` 死代码删除；有单测 | `frontend-vue/src/composables/` |
| **G19** | 父 → 子 steer | 父 Agent 能给运行中的子追加指令（现在 `no active agent for this session`）；有"父改子指令、子下一轮生效"的测试 | `app/agent/steer.py`、`subagent_runner.py` |
| **G20** | 子 Agent 可挂起等人工 | 子会话补 `agent_runs` 行，审批/提问在超时前**可被回答**（现在合成会话没有 run 行 ⇒ 超时即失败）；待确认（见 3.6） | `app/agent/interactions.py` |
| **G21** | PDF/表格渲染进 Worker | 大 PDF/Excel 不再占主线程（现状全仓无 Worker）；有"主线程不阻塞"的时延断言 | `frontend-vue/src/views/MediaView.vue` 等 |

**第三批：高（新组件 / 架构前提，需决策）**

| ID | 事项 | 完成定义 | 卡点 |
|---|---|---|---|
| **G22** | MCP 并入统一注册表 | MCP 工具与内置同形状（`output` schema + 渲染）走同一 pipeline，去掉 `use_capability` 代理双路径 | 见 3.7 |
| **G23** | 常驻 LSP | 按项目根挂一个语言服务器 + 串行查询队列 + 进程回收 | 新常驻组件 |
| **G24** | 定时触发原语 | cron/rrule → 写目标会话 durable inbox（at-least-once 写明） | 新组件 + 迁移 |
| **G25** | 沙箱缺后端即拒跑 | POSIX 侧"无命名空间"标为不可用而非静默降级（与 H2 同一约束） | 见 3.3；会让现有 Linux 部署不可用，需开关 |

### 2.2 原有未完成项（`H*`）

| ID | P | 事项 | 完成定义 | 卡点 |
|---|---|---|---|---|
| **H2** | P0 | Linux 内核级执行隔离 | 不可用即**拒绝执行**（不降级）；有"沙箱不可用即拒"探针 | 需 Linux 真机 |
| **H20** | P1 | Provider 密钥加密存储 | 密文落库 + 信封加密 + 可插拔 KMS + 轮换；有"明文永不落盘"回归 | 需先定 KMS（3.2） |
| **H19** | P3 | ACP 完整 Agent 协议 | 编辑器侧拿到工具调用/审批/终态 | 需产品决策（3.4） |
| **H22** | P3 | 只读子 Agent 强隔离 | **构造性**禁止写（非提示词），越权返回明确错误 | 需产品决策（3.4） |


### 2.3 并行推进计划（worktree + 多 agent）

剩余 16 个 `G*` 按**文件所有权互斥**分组，每组一个 git worktree 并行推进；
合并顺序按"冲突面从小到大"。未列出的 `H*` 依赖外部条件（见 §3）。

| 波次 | worktree / 分支 | 项 | 独占文件 | 状态 |
|---|---|---|---|---|
| W-A | `wt-g13` / `task/g13-g16` | ~~G13、G16~~ ✅ 已合入（含 main.go relay 接线） | — | 完成 |
| W-A | `wt-g14` / `task/g14-g15` | G14 结果 spill 统一、G15 并发显式声明 | `app/tools/**` | 进行中 |
| W-A | `wt-g10` / `task/g10-g12` | ~~G10、G12~~ ✅ 已合入 | — | 完成 |
| W-A | `wt-g17` / `task/g17-g18` | G17 预览 offset 分页、G18 布局断点纯函数化 | `KBDocumentPreview.vue`、`composables/layout*`、`api/knowledge.py` | 进行中 |
| W-A | `wt-g21` / `task/g21` | G21 PDF/大文件解析进 Worker | `MediaView.vue`、`workers/**` | 进行中 |
| W-A | `wt-h16` / `task/h16b` | ~~H16b 契约 v2 四族~~ ✅ 已合入（7 族 55 例） | — | 完成 |
| W-B | `wt-g11` / `task/g11-g20` | G11 子副作用并入父账本、G20 子 Agent 可挂起 | `app/agent/undo_stack.py`、`side_effect_ledger.py`、`interactions.py` | 进行中 |
| W-C | `wt-g23` / `task/g23-lsp` | G23 常驻 LSP（G19 待 W-B 合入后开，二者同改 agent/**） | `app/tools/lsp/**`(新) | 进行中 |

合并纪律：① 每个分支自己跑完窄回归；② 合并后我在 main 上跑跨包回归（Go `./internal/...`、
Python 相关套件、前端 `src/components` + `check:ui`）；③ 冲突时以"语义正确"优先，不保留两套实现；
④ 每项合入后**立即**从本文件删除对应行。

## 3. 待确认决策

1. **H51 签名根**：发布签名私钥谁持有、公钥如何内嵌进安装包？（决定组件/更新清单验签
   是否真正生效 —— 代码已按"公钥内嵌 + 缺失即拒绝"实现，缺的是这把钥匙的归属）
2. **H20 KMS 选型**：「进程内信封加密 + 可插拔 KMS 接口」还是直接接云 KMS SDK？
3. **H2 / G25 隔离承诺**：是否接受「bwrap 不可用即拒绝执行」作为首个可发货版本？
   （G25 会让现有 Linux 部署直接不可用，需不需要先加默认关的开关？）
4. **H19 / H22 产品承诺**：ACP 完整集成、严格只读子 Agent 是否本版本目标？
5. **H16b 时机**：`queue` / `checkpoint` 字段还在动；等稳定再纳入，还是先纳入 `tool_approval` / `mcp`？
### 3.1 已定（2026-11-25）

- **G20 做**：补 `agent_runs` 行，允许子 Agent 挂起等人工（不再超时即失败）。
- **G22 不做**：MCP 暂不并入统一注册表（动 registry 风险中高，双路径当前可用）。
- **G23 做**：常驻 LSP。
- **G24 不做**：定时触发原语不进本版本。
- **G25 合并进 H2**：沙箱"缺后端即拒跑"与 Linux 内核级隔离一次做完，不单独立项。


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
