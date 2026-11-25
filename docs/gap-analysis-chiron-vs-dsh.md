# 差距分析：Chiron vs deepseek-harness（核心循环 / 长线任务调度 / 外部工具交互）

> 对象：`chiron-desktop`（Go 网关 + Python 引擎 + Electron 外壳）与 `deepseek-harness`（TS monorepo，55 个包）。
> 方法：只读两侧代码与文档，逐条给"文件:行号"证据；**不猜**，找不到就写"未找到"。
> 口径：只比较这三条轴，不比较 UI、部署、商业模式。

## 0. 结论与总览

**一句话**：两个项目的强弱几乎完全互补 —— DSH 强在**结构**（日志即状态、能力 seam、只减权的守卫），
Chiron 强在**分布式可靠性**（队列 fencing/幂等/DLQ、五轴预算、服务端授权与票据绑定）。
真正的差距只有两处是"能力缺失"：**循环可换装性**（Chiron 单体 6117 行）与**长任务记账一致性**
（Chiron 队列/会话/SSE 三份进度 vs DSH 一份日志）。其余是**取舍差异**，不该互相照搬。

| 轴 | DSH 领先 | Chiron 领先 | 关键证据 |
|---|---|---|---|
| 核心循环 | 契约/实现分离 + 四策略切点；终态两层保证；steer 落点在**步**边界 | `max_turns` + 五轴预算 + 续段（长任务不被静默截断） | `agent-loop/README.md:238`、`runtime.py:3067,3091-3132` |
| 长线调度 | job 生命周期写进**会话日志**；恢复"不发明事实" | Redis Streams + fencing + 幂等 + DLQ + takeover；cron 之外有延迟/死信 | `docs/subsystems/jobs.md:35`、`app/queue/{idempotency,worker}.py` |
| 工具交互 | 三来源同一 `ToolDefinition` + 同一条 pipeline；guard **类型上**只减权；沙箱 fail-closed | **服务端授权**（Tool Broker）+ 票据绑定 `args_hash/turn_id/scope` + 参数级硬拦 + SSRF 钉 IP + 出口脱敏 | `pipeline.ts:63-234`、`internal/api/tool_broker.go:102-158`、`runtime.py:194-238` |

### 0.1 修正一处口径

DSH **有**审批留痕（`approval/asked` + `approval/decided` 成对写进日志，`packages/interaction/user-approval/src/index.ts:243-262`），
缺的是**队列**：单决策槽、无审批专用超时、无"记住该工具"、子 Agent 不能弹审批（`docs/subsystems/approval.md:21,53`）。
Chiron 有 `pending_interactions` 状态机（`pending→answered→consumed`，24h 过期）与票据单次消费，能支撑"人不在也要排队等"。

## 1. 核心循环

### 1.1 DSH 的做法

| 面 | 事实 | 证据 |
|---|---|---|
| 结构 | `packages/core` 是**子包目录**：`agent-loop`（驱动）/ `agent`（公共契约）/ `tools` / `session` / `system-prompt` / `scope`；`agent-loop` 只是 `agent` 契约的**默认实现**，扩展插件从不依赖它 | `packages/core/README.md:38` |
| 入口 | `kick()` = `while (await this.turn()) {}`；`turn()` 内 `while(true)` 逐步推进 | `packages/core/agent-loop/src/agent.ts:271-273,340` |
| 迭代上限 | **没有**（全仓无 maxSteps/maxTurns；README 自陈 "No built-in turn budget"） | `packages/core/agent-loop/README.md:238` |
| 终止分支 | completed / blocked（pre-step 整体 reject，**不消耗 step**）/ max-tokens（sticky，优先于后续继续）/ error / aborted；`turn/end` 在 finally 必写 | `agent.ts:344-347,364-369,366-418,574`、`docs/agent-lifecycle.md:37` |
| 流与终态 | 适配器抛错被**归一成一个 finish chunk**；`AssistantStreamAttempt` 另有 terminal 标志 ⇒ "终态唯一"是两层保证 | `packages/llm/src/index.ts:1104-1138`、`agent-loop/src/assistant-stream.ts:23,79-110` |
| 半截参数 | 装配层只拼字符串；max-tokens 时**丢弃 tool-call 块**，中断只保留 text/reasoning；非法 JSON 原样保留给执行期拒 | `packages/llm/src/assembler.ts:69-75,135-150,169-179`、`core/tools/src/batch.ts:99-106` |
| 工具编排 | 整批交给 `ctx.tools.execute()`，默认并发 10；exclusive 屏障 vs 并行滚动池；**只有工具本体并行**，pre-execute/审批/结果提交按模型顺序串行 | `agent.ts:580-583`、`core/tools/src/constants.ts:6`、`batch.ts:76-91,138-150,186-218` |
| 工具失败 | 普通工具错误**不结束 turn**；调度器失败抛 `ToolBatchExecutionError` 并排空在飞，循环补写恢复结果；无工具体重试（超时 → `TOOL_TIMEOUT`） | `batch.ts:219-223`、`agent.ts:370-381` |
| 上下文 | 循环内**不做**裁剪：每次 `deriveMessages()` 派生并深冻结；压缩靠外挂策略（三触发：pre-step 压力 0.8×window−65536、真溢出重试 1 次、手动） | `agent.ts:718-733`、`compaction-basic/src/index.ts:172-190,228-270` |
| 配对保护 | 压缩前后**必须** tool-pairing 平衡（两侧都要），且摘要必须更小 | `compaction/tool-pairing/src/index.ts:112-126`、`region.ts:355-424` |
| steer / cancel | steer 是**步边界**输入（running 时入队，当前 step 收敛后下一 step 被 claim）；cancel 才抢占：流前缀落 interrupted、在飞工具体到 quiescence 不抛弃、未启动补 `ABORTED_BEFORE_DISPATCH`、`step/end`+`turn/end` 必写 | `agent.ts:177-199,384-396,492-509`、`batch.ts:225-230` |
| 策略切点 | 只有四个：`agent/pre-step`、`agent/request`、`agent/request-error`、`agent/turn-stopping` | `core/agent/src/runtime-types.ts:351,374,390,418` |
| 子 Agent | 进程内 `spawn`/`fork` 都走 `ctx.agents.create()/resume()`，**同一条 AgentLoop + 同一工具流水线 + 同一 guard/审批接缝**，独立 Session/scope；差异只是策略收窄（`approval: never` + 固定 sandbox + 深度预算） | `packages/subagent/subagent/src/manager.ts:865-882`、`child-agent.ts:226-239` |
| 恢复粒度 | **turn 边界**：半截 step/流不续跑；resume 先抢写句柄，再用 `interruptedTurnClosers()` 合成封口事件 | `agent-loop/src/index.ts:796-810`、`session/src/repair.ts:53-198` |

### 1.2 Chiron 的做法

| 面 | 事实 | 证据 |
|---|---|---|
| 结构 | `AgentRuntime.run()` 一个类承担循环 + 审批 + 埋点 + 计划 + 记忆 + 压缩触发；`runtime.py` 6117 行 | `python-engine/app/agent/runtime.py:2340` |
| 迭代上限 | `max_turns` 默认 40；续跑从快照 `turn_index` 起算 | `runtime.py:3067`、`app/config.py:149`、`runtime.py:2994-3010` |
| 终止分支 | 8 条：正常无工具 / 预算五轴（tokens 可续段）/ 线账 / 步数轴 / 用户中断 / 挂起等人 / 无进展 / 异常 | `runtime.py:3091-3173,3179-3199,3838-3864,4079-4102,4335-4342` |
| 工具失败 | **不终止**：分发异常转成 `{"error": …}` 回灌后继续 | `app/agent/tool_execution.py:474-497` |
| 并发批处理 | 有：只把"动作级 read、非 ask_user、非同路径"放组内并发（`gather(return_exceptions=True)` + 信号量，默认 4）；写/删/外部一律串行，收尾按原序落消息 | `runtime.py:791-839,4694-4719`、`app/config.py:158` |
| 流与终态 | 17 种事件；tool_call 在**流结束后**才下发（避免发布被截断的调用）；终态靠各分支 `return` **结构性唯一**，**没有"已发终态"守卫**，`input-guard` 分支只发 `guardrail_blocked` 就 return | `runtime.py:1341-4339`（事件清单）、`2467-2472,2500-2505` |
| 半截参数 | `finish_reason=length` 时丢弃该批 tool_calls、补 assistant+tool 配对并回灌出路；执行前再拒一次非法 JSON | `runtime.py:3558-3600`、`tool_execution.py:118-136` |
| 上下文 | 窗口来自模型窗口表；三级 SNIP/PRUNE + 溢出强制压缩重试；触发线 `min(ratio×window, window−16000)`；估算器按 provider 真实 tokens 逐轮校准 | `runtime.py:2880,2917-2970`、`app/agent/compaction.py:32-73,246` |
| 配对保护 | `_ensure_valid_tool_sequence` **双向**修复；调用前再清孤立 tool 消息 | `compaction.py:1570-1631`、`runtime.py:3272-3286` |
| steer / cancel | steer 在**轮次边界**消费（工具半途插消息会破坏配对），跨副本走 Redis + 本地快路径，RENAME 原子取走 + 代数翻转防残留；中断用 run_token+generation 围栏，工具执行另有"竞速取消"，已可能产生副作用则返回"结果未知" | `runtime.py:3201-3234`、`app/agent/steer.py:169-226`、`tool_execution.py:397-411` |
| 子 Agent | 进程内 asyncio（`create_task` + 新 `AgentRuntime`），**复用同一份执行链代码**（`guarded_execute_tool`，`allow_approval=False`），depth ≤3，子默认只读并剥离 delegate；账本按子会话 id 记、**不并入父账本** | `app/tools/subagent.py:355-423`、`app/agent/subagent_execution.py:37-75`、`subagent_runner.py:1594-1628` |
| 恢复粒度 | **回合末**：尾部窗口 + `done_tools` + usage + task_meta 写 `agent_runs.checkpoint`，带 run_token/generation 围栏；reconciler 每 60s 扫，热窗口 1h 内 CAS 抢占后自动续跑 | `runtime.py:4037-4055`、`app/agent/checkpoint.py:88-316`、`app/agent/resume.py:37,373-392` |

### 1.3 差距

| 维度 | DSH | Chiron | 谁更强 / 差在哪 |
|---|---|---|---|
| 循环可换装 | 契约与实现分离，四切点外置 | 单体 `AgentRuntime.run()` | **DSH**：Chiron 换任何一处都要改 6117 行的函数 |
| 迭代上限 | 无（靠 turn-stopping / cancel 收敛） | `max_turns=40` + 五轴预算 | **Chiron**：长任务不会被静默截断；DSH 的"无上限"在无人值守时是风险 |
| 终态唯一 | 两层保证（归一 + terminal 标志） | 结构性唯一但**无守卫**；guard 分支可能只发 `guardrail_blocked` | **DSH**：Chiron 这条是实际缺口（依赖网关关流兜底） |
| 并行工具 | 只并行本体，审批/结果按模型序串行 | 按动作级分类放行并发（read 才并行），写/外部串行 | 打平：两边都用"分类"控制，Chiron 分类更粗（read/write/delete/external） |
| 压缩完整性 | tool-pairing 双侧强制 + 摘要必须更小 | 双向修复 + 调用前清孤立 | 打平，思路一致 |
| steer 落点 | 步边界 | 轮次边界 | **DSH 更细**：Chiron 的轮内工具半途不能插话（有意，保配对） |
| 子 Agent 账本 | 独立 Session，job/工具记账进各自日志 | 新 `AgentRuntime`，账本按 `sub:<parent>:<run>` 记、**不并入父** | **DSH**：Chiron 的"整体回滚"看不见子 Agent 的副作用 |
| 恢复粒度 | turn 边界 + 不发明事实 | 回合末快照 + reconciler 自动续跑 | 各有所长：Chiron 能**自动**续跑（DSH 不行），DSH **不假装**恢复（Chiron 要做快照对齐） |

## 2. 长线任务调度

### 2.1 DSH 的做法

| 面 | 事实 | 证据 |
|---|---|---|
| 托管形态 | `ctx.jobs` 是**进程内**后台任务 seam（`LocalJobRegistry`），没有持久队列、没有调度守护进程 | `packages/jobs/README.md:14`、`packages/jobs/jobs-local/README.md:12` |
| 状态机 | `running → stopping → completed(killed|failed)`；kill 先把状态置 `stopping` 再落终态 | `packages/jobs/jobs/src/view.ts:19`、`packages/jobs/jobs-local/src/index.ts:500-509` |
| 推进者 | 每个 job 一个 `setTimeout` 轮询（`pumpPollMs=150`），生命周期与进程绑定 | `packages/jobs/jobs-local/src/pump.ts:98`、`README.md:57,61` |
| 背压 | **硬拒绝不排队**（默认 10 个/owner，超限直接拒） | `packages/jobs/jobs-local/src/index.ts:234-237` |
| 会话记账 | job 生命周期作为 `job/registered` / `job/settled` 写进**原 owner 会话日志** | `docs/subsystems/jobs.md:35` |
| 断点恢复 | 不恢复 job 与输出环；恢复靠"日志重放 + 语义检查点 + 合成闭包" | `docs/subsystems/persistence.md:108-112`、`packages/core/session/src/repair.ts:209` |
| 防重复副作用 | **不重试**：已开始未完成 → `TOOL_OUTCOME_UNKNOWN`，未开始 → `TOOL_NOT_STARTED` | `repair.ts:166-193` |
| 定时 | 7 种 timing（after/at/every/daily/weekly/cron/rrule），单 Host timer + latest-only catch-up，语义明确 **at-least-once** | `docs/subsystems/schedule.md:5,24,26-34,127,131` |
| 触发落点 | 写入目标 Session 的 durable inbox 并 flush；receipt ≠ 模型已执行 | `schedule.md:118,131`、`packages/schedule/schedule/src/index.ts:222` |
| plan / todo | **都没有 revision/CAS**：plan 是 log-only 模式状态，todo 整表覆盖 last-write-wins | `docs/subsystems/plan.md:11`、`docs/subsystems/todo.md:16-27` |
| 有 CAS 的地方 | `goal` 与 `team` 的 task board | `packages/goal/goal/src/index.ts:458-464`、`packages/team/agent-team/src/task-board.ts:129-131` |
| 隔离单位 | **Session**，不是 worktree；team 明写 no worktree/lock/merge | `packages/team/agent-team/README.md:196-197,215`、`docs/subsystems/worktrees.md:13` |
| 配额 | job 10/owner、subagent 槽位池 + 深度、goal 轮数、workflow `maxTotalAgents`；**无 token/货币预算** | `packages/subagent/subagent/src/activation.ts:28-31` |
| 上下文 | pre-step 按 pressure/overflow 压缩；工具结果超 `maxInlineTokens` 落盘（spill）只留头尾 + locator | `docs/subsystems/compaction.md:11,19,101`、`docs/subsystems/spill.md:92` |
| 人机协作 | **无审批队列**：waterfall 单决策槽、无审批专用超时、fail-closed，但 `approval/asked`+`approval/decided` 成对留痕；超时问答返回 `{pending,callId}` 可事后补答；子 Agent 不能弹审批 | `docs/subsystems/approval.md:21,86`、`docs/subsystems/user-questions.md:149-158,206` |
| 可观测 | `JobView` 统一投影 + `job.list/follow/kill` 流；进度单行 `progress`、原因 `detail`、`cause=producer|kill|teardown`；**无任务级心跳** | `docs/subsystems/jobs.md:275-320,422-447`、`packages/jobs/jobs/src/types.ts:253,261-287` |
| 明确局限 | job 契约进程内且不恢复；workflow 无 journal/resume；subagent 崩溃丢"已接受未落日志"的消息；goal 无 evaluator/自动重试；plan pending 进程内易失；schedule Host 必须常驻；spill 无保留策略 | 见上述各 README，及 `packages/jobs/tool-jobs/src/index.ts:478,499`（FIXME） |

### 2.2 Chiron 的做法

| 面 | 事实 | 证据 |
|---|---|---|
| 托管形态 | Redis Streams 队列：producer / worker / 消费组 / PEL / DLQ / watchdog / takeover | `python-engine/app/queue/{producer,worker,streams,dlq,watchdog_job,takeover_job}.py` |
| 幂等 | 有幂等键与 Lua 单次消费（本轮补了 `maxlen<=0` 不裁剪的边界） | `python-engine/app/queue/idempotency.py` |
| 必杀/回收 | claim + fencing（旧 attempt 的 fail/retry/ack 全部无效）、死亡任务 takeover | `python-engine/tests/test_queue_h7_fakeredis.py`、`app/queue/takeover_job.py` |
| 预算 | 多轴预算（turns / steps / tokens / cost / wall），越界产出 `budget_exceeded:<轴>`；tokens 轴可"续段" | `python-engine/app/agent/runtime.py:3087-3104` |
| 后台命令 | `run_in_background` 类工具 + `job_runner`（结果存 Redis，TTL 24h，可 kill，单条上限 3600s） | `python-engine/app/tools/job_runner.py:24-30` |
| 断点 | PG `project_checkpoints`（含 untracked 清单，本轮改为同事务）+ git tree/ref；undo 栈（本轮改为二进制安全 + 失败回栈） | `internal/api/project_checkpoints.go`、`python-engine/app/agent/undo_stack.py` |
| 计划/待办 | `plan.py` + `taskboard.py`：本轮补上 **revision CAS**（Lua 比对后 SET）与稳定 ID + `depends_on` 重映射 | `python-engine/app/agent/taskboard.py` |
| 隔离 | 会话/工作区维度隔离；无 worktree 概念 | `internal/session`、`app/workspace` |
| 审批 | 有审批队列与留痕（`approval_audit`），审批票据与服务端授权绑定 | `python-engine/app/agent/approval_audit.py`、`internal/api/tool_broker.go:102` |
| 可观测 | SSE 事件 + usage 记账 + trace span（父子 span，含委派） | `python-engine/app/agent/runtime.py:3068-3073`、`internal/api/usage_events*.go` |

### 2.3 差距

**Chiron 领先（不要照搬 DSH）**
1. **分布式队列**：DSH 的 job 是进程内的，进程退出即丢；Chiron 有 Redis Streams + fencing + 幂等 + DLQ。
   对"团队主机常驻、多实例"这个前提，DSH 这套不成立。
2. **预算体系**：DSH 只有"数量配额"（槽位/轮数），**没有 token/货币预算**；Chiron 有五轴并可续段。
3. **审批留痕与队列**：DSH 明确"fail-closed、无队列、子 Agent 不能弹审批"，这在无人值守时是硬约束；
   Chiron 有审批队列 + 审计，能支撑"人不在也要记账"。

**DSH 领先（值得迁移）**
1. **job 记账进会话日志**：DSH 的 `job/registered`/`job/settled` 与会话同一份日志，前端一个订阅看全；
   Chiron 是队列（Redis）、会话（PG）、进度（SSE）三份，一致性靠约定。**这是最值得抄的一条**。
2. **不恢复就不假装恢复**：DSH 明写"缺 `job/settled` 就标注未完成，不补 finish 时间"；
   Chiron 的 checkpoint 恢复需要"快照 vs 实际"对齐，存在"恢复成功但状态偏"的窗口。
3. **`TOOL_OUTCOME_UNKNOWN` / `TOOL_NOT_STARTED` 的二分**：DSH 用"不重试 + 明确未知"避免重复副作用；
   Chiron 有幂等键，但对"进程崩溃时工具已开始"这一态的表达不如它干脆。
4. **spill 策略**：大工具结果落盘 + 只留头尾 + locator，且**有 inline 上限**；Chiron 的截断策略分散在工具实现里。
5. **schedule 的 7 种 timing + latest-only catch-up + at-least-once 语义声明**：Chiron 无定时触发原语。

**两边都缺**
- 任务级**心跳**（DSH 只有传输层 websocket 心跳；Chiron 有 watchdog 但停在队列层）。
- workflow 级别的 journal/resume（DSH 明确没有；Chiron 无 workflow 概念）。
- 保留策略（spill / 快照 / 产物）。

### 2.4 可迁移的具体改法（按性价比排序）

| # | 改法 | 落点 | 成本 | 风险 |
|---|---|---|---|---|
| 1 | **队列事件进会话流水**：worker 起停/完成/失败写一条会话级事件（与 usage 事件同一条链），前端只订阅一处 | `app/queue/worker.py` → `internal/api/usage_events*` 旁路一条 `job_events` | 中（要加表/迁移） | 低 |
| 2 | **"未知副作用"显式化**：崩溃/超时后可判定的工具调用产出 `outcome=unknown` 而不是失败重试 | `app/agent/tool_execution.py` 收尾处 | 低 | 低（改语义，需前端配合展示） |
| 3 | **工具结果 spill 统一**：超过 N KB 一律落盘 + 上下文只放头尾 + locator | 新增 `app/tools/spill.py`，registry 出口统一 | 中 | 中（要改历史兼容） |
| 4 | **定时触发原语**：cron/rrule → 写目标会话 durable inbox（at-least-once 写明） | Go 侧新 scheduler + PG 表 | 高 | 中 |
| 5 | **任务级心跳**：队列表每行 `heartbeat_at`，前端显示"卡住"而不是"在跑" | `app/queue/worker.py` | 低 | 低 |

> 第 1 条与第 4 条会改变 PRD 的形态（新增迁移 + 新组件），需要决策后再开；第 2/3/5 条可以作为加固项直接进 backlog。

## 3. 外部工具交互

### 3.1 DSH 的做法

| 面 | 事实 | 证据 |
|---|---|---|
| 来源同构 | 只有**一个**注册表 `ctx.tools`：内置（插件在 `apply()` 里注册）、MCP（`tools/list` → `createMcpToolDefinition` → 同一个 `register`）、动态插件全部产出同一个 `ToolDefinition`，走**同一条** pipeline | `packages/core/tools/src/index.ts:128-153`、`packages/mcp/mcp-client/src/tools.ts:225,340-362`、`packages/fs/tool-fs/src/index.ts:71-102` |
| 可见性 | per-step `resolve()` 的 `when` 准入 + scoped 遮蔽 + `restrict({allow,deny})`；on-demand MCP 工具**不受** `restrict` 约束（只能靠 guard 挡） | `index.ts:162-227,264-307`、`packages/mcp/mcp-client/README.md`（Known Limitations） |
| 执行链 | 参数解析 → **无损快照冻结** → `tools/pre-execute`（allow/deny/cancel/ask）→ 审批 → **单调 guard（只减权）** → `tools/execute`（超时/信号）→ 工具体 → output schema 校验 + `render` → `projectContent` → `tools/post-execute` → `finalizeContent` → 冻结 → `tools/result` | `packages/core/tools/src/pipeline.ts:63-234,356-417,557-588`、`docs/tool-execution-pipeline.md:9-62` |
| 失败码 | `UNKNOWN_TOOL` / `INVALID_ARGS` / `INVALID_TOOL_OUTPUT` / `TOOL_TIMEOUT` / `ABORTED` vs `ABORTED_BEFORE_DISPATCH` / `APPROVAL_*`，全部归一成 `isError` | `pipeline.ts:732-805`、`contracts.ts:292-337` |
| 权限粒度 | 工具可见性 + 调用级 allow/deny/ask/cancel + 会话级两个旋钮（`sandbox/mode`、`approval/policy`）；**没有** per-参数/路径 ACL，**没有**"记住选择" | `packages/interaction/permission-presets/src/index.ts:91-107`、`docs/subsystems/approval.md:21` |
| 审批 | 必须处于 open turn；先 `approval/asked` 再 waterfall 再 `approval/decided`（**成对审计进日志**）；请求只带 `toolName/callId/reason`，**不复制参数**；无审批专用超时（一直挂到被取消）；`allowed-once` 一次性 | `packages/interaction/user-approval/src/index.ts:243-262,129-150,294-334`、`approval.md:53` |
| 并发 | 并行 **opt-in**：只有 `isConcurrencySafe(args) === true` 才进并行池，未声明/抛错一律 exclusive；exclusive 断组；滚动池上限 = batch 覆盖 > executor 配置 > 默认 10；审批发生在**有序 prepare 阶段**，会卡住"下一个开始"但不打断已派发 | `pipeline.ts:106-115`、`batch.ts:76-91,139-201,186-218` |
| 沙箱 | 纯策略 + 同内核受限进程（Linux bwrap→Landlock、macOS Seatbelt、Windows 受限令牌+DACL），**非容器**；后端不可用即 **fail closed**（`SANDBOX_UNAVAILABLE`，"refusing to run the command unconfined"）；windows-acl 只 partial，`additionalWritableRoots` 对进程无效 | `packages/sandbox/sandbox/src/index.ts:32-161`、`sandbox-local/src/index.ts:174-206` |
| 结果 | canonical value 出厂校验、**只留在执行内**不进 durable 事件；超 `maxInlineTokens` → head/tail + 落盘 locator（缺省关闭）；二进制进 attachment store；三处可重写点（`projectContent`/`post-execute`/`finalizeContent`） | `contracts.ts:340-350`、`packages/spill/spill-policy/src/index.ts:92-151` |
| 常驻资源 | LSP 每 workspace 一实例 + 串行队列；terminal PTY 会话常驻；browser/computer 只有 seam（真身在 experimental，按 Agent 挂 stdio MCP client）；进程树用 Windows Job 对象 / Linux user-systemd scope | `packages/lsp/lsp-stdio/src/index.ts:136-239`、`terminal/terminal-bash/src/session.ts:1-20`、`subprocess-local/src/windows-job.ts` |
| 安全面 | 命令**不做净化**（bash 就是 shell，防线是策略+审批+沙箱）；路径用 `fs.contains` 夹取；**SSRF 私网校验已移除**（v0.2.1-rc.2 起，要挡须自建 `tools/pre-execute`）；凭证只存引用；外部内容标注 untrusted，但 **MCP 结果没有统一包装** | `packages/sandbox/sandbox/src/index.ts:214-225`、`docs/upgrade-guide/v0.2.1-rc.2/web-fetch-private-destinations/guide.md:8-12`、`packages/web/tool-web/src/trust.ts:7` |

### 3.2 Chiron 的做法

| 面 | 事实 | 证据 |
|---|---|---|
| 来源 | 三类（内置 / MCP / 插件）走**同一条执行链**，但 MCP 属 `HIDDEN_SOURCES`，不进可见清单，只能经 `use_capability` 代理 | `app/tools/registry.py:38-48,154-180`、`app/tools/use_capability.py:34-53` |
| 执行链 | 参数 JSON → schema 归一/校验 → 动作分级 read/write/delete/external → ToolGuard（密钥/逃逸硬拦 + 模式裁决）→ **Tool Broker 服务端授权**（delete/external fail-closed）→ guardian LLM 评审 → 人工审批 → recovery 闸门 → PreToolUse hook（只收紧）→ 分发（写占用 + 沙箱槽）→ 结果压缩/落盘 → **副作用账本** → span/usage | `app/agent/tool_execution.py:1-27,118-500`、`internal/api/tool_broker.go:102-158` |
| 权限粒度 | 会话三模式 `ask|auto|yolo`；**参数级硬拦不受模式影响**；审批票据绑定 `(tool_call_id, args_hash, turn_id, caller_id, resource_scope)`，缺字段 fail-closed，执行前二次校验，单次消费 + 跨副本 TTL | `app/agent/guards.py:97-100,300-314`、`runtime.py:194-238,556-601,5580` |
| 沙箱 | 路径夹取 + 命令白名单（25 个）+ 逃逸正则（`$()`/反引号/换行）+ 文本级准入；Windows 隔离档 `none/job/low_il/restricted/appcontainer`（默认 `restricted`，硬底线 `low_il`）；**POSIX 只有 `setrlimit`**（无命名空间）；可选 `sandbox-service` 明示**无命名空间/seccomp**，不可达且非降级模式则拒绝启动 | `app/tools/sandbox.py:227-292,758,973-1002`、`app/isolation/__init__.py:56-103`、`sandbox-service/README.md:5-8` |
| 结果 | `INLINE_LIMIT` 以上生成结构摘要 + `result_ref` 落 `result_store`，`read_tool_result` **分段取回**（32000/次） | `app/tools/tool_result.py:18-54` |
| 常驻 | 会话级 `persistent_shell`（失败不回退）；浏览器经网关 `/v1/rpa/exec` 转发到用户已连接 Chrome 扩展（未绑定 fail-loud）；**无 LSP** | `app/tools/terminal.py:43,108-230`、`app/tools/browser.py:53-68` |
| 安全面 | 出口统一脱敏（事件/错误/日志 + 运行时登记凭据双层）；SSRF 只放行全球 unicast 并在连接期钉 IP（关掉 rebinding 窗口）；Git 入口统一硬化 | `app/agent/runtime.py`（H62 脱敏链）、`app/tools/net_guard`（H39）、`app/tools/git_guard`（H25） |

### 3.3 差距

**Chiron 领先（不要照搬 DSH）**

1. **服务端授权（Tool Broker）**：危险工具的判定权在**网关**，delete/external **fail-closed**，还能反向收紧会话模式。
   DSH 是单进程，没有"服务端权威"这一层 —— 对多租户团队部署，这是硬差距而不是风格差异。
2. **审批票据绑定**：Chiron 把 `args_hash`/`turn_id`/`caller_id`/`resource_scope` 绑进票据并二次校验；
   DSH 的审批请求**不带参数**（只有 `toolName/callId/reason`），用户看到的是"工具表现层"渲染，且 `allowed-once` 只在本进程。
   Chiron 的抗篡改更强。
3. **参数级硬拦**：Chiron 明确"参数级硬拦不受会话模式影响"；DSH 没有 per-参数 ACL，只能写 `tools/pre-execute`/`tools.guard` 自己实现。
4. **SSRF 与凭证**：Chiron 有"只放行全球 unicast + 连接期钉 IP"和出口统一脱敏；
   DSH **移除了**私网目的地校验（要挡须自建 pre-execute），MCP 结果也没有统一 untrusted 包装。
5. **结果分段取回**：Chiron 的 `read_tool_result` 是**可导航**的分页；DSH 的 spill 只给头尾 + locator（全文在文件里，模型得另开工具读）。

**DSH 领先（值得迁移）**

1. **`ToolDefinition` 单一契约**：三个来源同一形状、同一条 pipeline，连 `output.schema` + `render` 都是必填。
   Chiron 的 MCP 走 `use_capability` 代理、可见性靠 `HIDDEN_SOURCES` 过滤 —— 结果是 **MCP 工具与内置工具走两条路径**，
   策略要么写两遍，要么代理层漏一处。
2. **guard 的"只减权"类型**：`ToolGuard` 的返回类型里**没有 allow**，所以 listener 顺序不可能把 deny 翻回 allow。
   Chiron 的 hook（PreToolUse）虽然"只能收紧"，但那是**约定**（注释 + 实现），DSH 是**类型上不可能**。
3. **沙箱 fail-closed**：DSH 后端缺失直接 `SANDBOX_UNAVAILABLE` 拒跑；Chiron 的 POSIX 侧只有 `setrlimit`，
   `sandbox-service` 也明示无命名空间 —— 这正是 PRD 里 H2 未完成的那条（Linux 内核级隔离）。
4. **`isConcurrencySafe` 的显式声明**：并行需要工具**自己声明**安全（未声明即串行）；Chiron 是按"动作级 read"推断，
   推断错一个动作级就等于放错一个并发。
5. **常驻资源生命周期**：LSP 每 workspace 一实例 + 串行队列 + SIGTERM→SIGKILL 升级、PTY 会话、进程树挂 Job 对象；
   Chiron 只有持久 shell，**无 LSP**，进程回收粒度不如它细。

**两边都缺**
- 工具级幂等/去重（两边都靠"不重试 + 标未知"）。
- 审批"记住这个工具"（DSH 明确 `allowed-once`；Chiron 是单次票据）。DSH 把持久化的只有 `approval/policy` 与会话级模式。

### 3.4 可迁移的具体改法（按性价比排序）

| # | 改法 | 落点 | 成本 | 风险 |
|---|---|---|---|---|
| 1 | **MCP 工具并入统一注册表**：去掉 `use_capability` 代理路径，MCP 工具与内置同形状（`output` schema + 渲染）后走同一 pipeline；可见性仍可用 `when`/allowlist 表达 | `app/tools/registry.py` + `app/plugins/*` | 中高 | 中（MCP schema 不可信，要先做本地校验） |
| 2 | **`ToolGuard` 类型化"只减权"**：把 hook 从"约定只能收紧"改成"返回类型里没有 allow" | `app/agent/guards.py` | 低 | 低 |
| 3 | **`isConcurrencySafe` 由工具显式声明**：默认串行，声明才并行（替代按动作级推断） | `app/tools/registry.py` + 各写工具 | 中 | 中（漏声明会降速） |
| 4 | **常驻 LSP**：按项目根挂一个语言服务器实例 + 串行查询队列 | 新增 `app/tools/lsp/` | 高 | 中（进程管理 + 端口/管道） |
| 5 | **沙箱缺后端即拒跑**（不是降级）：与 H2 同一条约束，可先在 POSIX 侧把"无命名空间"标成不可用而非静默 | `app/isolation/__init__.py` | 中 | 中（会让现有 Linux 部署直接不可用，需开关） |

> 第 1/4/5 条都要动架构或部署前提，应先决策；第 2/3 条是纯加固，可以直接进 backlog。

## 4. 架构性差异（决定"哪些不能照搬"）

| 维度 | DSH | Chiron | 对迁移的含义 |
|---|---|---|---|
| 进程模型 | **单进程**：55 个包在同一个 Node 进程里用 DI（`ctx.*`）组合，`jobs-local`、`plan pending`、`subagent` 槽位都是进程内状态 | **三进程**：Go 网关（鉴权/租户/持久化/RPA/设备桥）+ Python 引擎（循环/工具）+ Electron 外壳；状态跨 PG + Redis | DSH 的 `job`、`plan pending`、`subagent` 语义**不能直接搬** —— 它们的正确性依赖"单进程 + 不重启" |
| 状态载体 | **日志即状态**：append-only `SessionEvent`，历史派生、恢复重放 | 命令式循环 + 外部快照（PG checkpoint / Redis 板 / git tree） | 抄 DSH 的"日志即状态"等于重写引擎；但可以只抄**局部**：把 job 生命周期写进会话流水 |
| 多租户 | 未见租户隔离（单用户/单机假设） | 租户/用户/会话三级隔离是硬约束（`internal/api` 全套鉴权 + 每张表带 `tenant_id`） | DSH 任何"按 owner 字符串"的设计在 Chiron 都要变成"按租户 + RLS 可验证" |
| 可换装性 | 能力 seam（`ctx.llm`/`ctx.tools`/`ctx.sessionPersistence` 都可替换）+ 4 个策略切点 | 单体 `AgentRuntime`，策略内嵌 | Chiron 若要做同类扩展性，成本是"拆 `runtime.py`"，这是最大的结构性欠账 |
| 人机协作 | fail-closed、单决策槽、无队列、子 Agent 不能弹审批 | 审批队列 + `pending_interactions` 状态机（`pending→answered→consumed`，24h 过期）+ 审计 | **不要**把 DSH 的 un-attended 假设引入 Chiron：Chiron 的团队场景需要"人不在也能排队等" |
| 预算 | 无 token/货币预算，只有数量配额 | 五轴预算 + 续段 + 网关级日费用熔断 | Chiron 这块领先，保持 |

## 5. 两处"两边都缺"的共同短板

1. **任务级心跳**：DSH 只有传输层 websocket 心跳；Chiron 的 watchdog 停在队列层，`agent_runs` 没有"最后活跃时刻"给前端判"卡住"。
2. **产物保留策略**：DSH 的 spill 文件无保留策略；Chiron 的 `result_store`/快照/日志同样没有统一 TTL 与容量上限（当前靠零散 TTL）。

## 6. 结论：优先级建议

**立刻可做（低风险、纯加固）**
- Chiron 补 **"终态事件已发"守卫**：`input-guard` / `UserPromptSubmit` 拦截分支现在只发 `guardrail_blocked` 就 return，靠网关关流兜底 —— 与 DSH 的两层保证相比这是实际缺口。
- Chiron 补 **子 Agent 副作用并入父账本**：现在按 `sub:<parent>:<run>` 单独记，"整体回滚"看不见子 Agent 写过的文件。
- **`ToolGuard` 类型化只减权**（3.4 第 2 条）：把"hook 只能收紧"从约定变成类型。

**中期（需设计，不改架构）**
- **job 生命周期进会话流水**（对齐 DSH 的 `job/registered`/`job/settled`），把"队列 / 会话 / SSE 三份进度"收敛成一份。
- **"未知副作用"二分**（DSH 的 `TOOL_OUTCOME_UNKNOWN` / `TOOL_NOT_STARTED`）作为恢复期的正式状态，而不是提示语。
- **工具结果 spill 统一**（DSH 的 `maxInlineTokens` + locator 模式）。
- **`isConcurrencySafe` 显式声明**，替代按动作级推断并发。

**暂不做（成本 > 收益，或与 Chiron 前提冲突）**
- "日志即状态"重写：收益是可重放，代价是重写引擎 + 写放大。
- 单进程化的 DI seam：与"Go 网关 + Python 引擎"的进程边界冲突。
- DSH 式"无迭代上限 + 靠 cancel/turn-stopping 收敛"：Chiron 面向无人值守团队场景，硬上限更安全。
- DSH 式"审批请求不带参数"：Chiron 的票据绑定 `args_hash` 是更强的抗篡改设计，不要退回去。

## 7. 参考附件

- [`appendix-dsh-core-loop-analysis.md`](appendix-dsh-core-loop-analysis.md)：DSH 核心循环的逐项考古（8 节，含 loop/steer/cancel/compaction 的行号级证据）。
