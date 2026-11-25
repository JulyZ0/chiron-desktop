# DSH 核心 Agent 循环：逐项代码考古（只读）

对象：`D:\desket\gh\clone\reasonix\deepseek-harness`（DSH 本体）。核心实现集中在 `packages/core/agent-loop/src/{index,agent,inbox,assistant-stream,runtime-context}.ts`，配套 `packages/core/tools/src/{pipeline,batch}.ts`、`packages/llm/llm/src/*`。
说明：仓库是 Cordis 插件树，`packages/core` 下是**子包目录**（`agent-loop/`、`agent/`、`tools/`、`session/`…），不是单一包。

---

## 1. 一次 turn 的主循环

**结论**：入口是 `ReactLoopAgent.kick()`，形式为 `while (await this.turn()) {}`；`turn()` 内部再来一层 `while (true)` 走 step。**没有任何迭代上限**——仓库里不存在 `maxSteps/maxTurns/iterationLimit`，README 明确把"无 turn 预算"列为已知限制。

- `packages/core/agent-loop/src/agent.ts:271-273` `private async kick()` → `while (await this.turn()) {}`；错误在 `274-276` 被吞（`catch (_error) {}`，已在 `agent/error` 报过）。
- `packages/core/agent-loop/src/agent.ts:323-340` `turn()` 先 `session.append('turn/start')`，再 `while (true)` 提 step。
- 无上限：`packages/core/agent-loop/src/agent-loop README`→`packages/core/agent-loop/README.md:238` "No built-in turn budget — tool calls or steering continue the current turn"。
- **正常结束**：`agent.ts:366-369` 记录 `stepEnd`（`completed`/`max-tokens`），`agent.ts:391-395` 当 `turnEnds !== null && inbox.nextStep.length === 0` 时跑一次 `agent/turn-stopping` 并 `break`。
- **工具继续**：`agent.ts:587` `result.concluded ? {completed} : null`；返回 `null` 表示"工具债还欠一次请求"，循环不退出（`turnEnds` 保持 null）。
- **工具/扩展失败**：`agent.ts:370-381` 先补写缺失 `tool/result` 再抛；`agent.ts:398-414` 归为 `{kind:'error', error: LlmFailure | {code:'UNKNOWN'}}`。
- **用户中断**：`agent.ts:400-403` 从 `AbortSignal.reason` 复制 `AgentCancelCause` → `{kind:'aborted', reason}`，然后仍重抛。
- **预算耗尽**：只有模型层 `max-tokens`——`agent.ts:364-369` 做成 **sticky**（后续正常完成的 step 不得把 turn 结局降级），`agent.ts:574` 直接 `return {kind:'max-tokens'}`。
- **拒绝进入**：`agent.ts:344-347` `agent/pre-step` 返回 reject → `{kind:'blocked'}` 且 `return false`（不花 step）；首步批次被改成空同样关闭 turn（`agent.ts:349-355`）。
- `turn/end` 在 `finally` 里写入（`agent.ts:415-418`），异常路径也一定落盘；`agent.ts:423-428` 只有 `inbox.hasPending` 才开下一 turn 并重置 `AbortController`。

## 2. 流式与事件模型

**结论**：provider 只吐 `StreamChunk`，适配器抛错被 `LlmRuntime` **归一成唯一的终态 `finish` chunk**，所以消费端永远拿到"一条流 + 一个终态"；实时帧只在同一进程内发 `start/chunk/end`，终态 end 由 `settle()/abandon()` 二选一，持久化事件在 end 之前提交。

- 事件联合：`packages/llm/llm/src/types.ts:496-508`（`block-start|text-delta|reasoning-delta|tool-call-delta|block-end|usage|finish`）；终态语义 `types.ts:163-172`（`stop|tool-calls|max-tokens|aborted|error`）。
- 消费：`packages/core/agent-loop/src/agent.ts:480-487` `const stream = preparedCall?.stream(request) ?? this.loopCtx.llm.stream(request)` 然后 `for await (const chunk of stream)`，每 chunk 前后 `signal.throwIfAborted()`（`485/488`）。
- 归一化：`packages/llm/llm/src/index.ts:1104-1138` 适配器 selection/dispatch/iteration 失败 → `yield adapterFailureChunk(...)` 后 `return`；`index.ts:1170-1178` 该函数产出唯一 `finish`（aborted 或 error）。中间件/消费方异常仍**照常抛出**（`1129-1131` 注释）。
- 实时帧：`packages/core/agent-loop/src/assistant-stream.ts:50-58` start、`61-72` chunk、`79-98` settle（先 `append()` 拿到 seq，再发 `end{committed,eventType,seq}`）、`101-110` abandon（无落地事件时）。
- 终态唯一性：`assistant-stream.ts:23` `terminal` 标志 + `get ended()`；调用侧守卫在 `agent.ts:596` `if (!live.ended) live.abandon()`；成功路径只在 `agent.ts:564-573` settle 一次，错误路径只在 `495-521` settle 一次，二者互斥。
- 持久化：成功 = `assistant/message`（含 `stream` 紧凑记录与 `usage`，`agent.ts:556-573`）；失败/重试/取消 = `assistant/attempt`（`agent.ts:511-520/534-537`）。
- 半截 JSON 参数：装配层**不解析**，只拼原始字符串（`packages/llm/llm/src/assembler.ts:69-75`、`113-118`）；解析在工具批里，非法 JSON **原样保留**给工具做诊断（`packages/core/tools/src/batch.ts:99-106` 注释 "Preserve malformed model input"）。
- 截断/中断的块策略：`assembler.ts:135-150` `max-tokens` 时**丢弃 tool-call 块**（避免执行不安全的半截调用）；`assembler.ts:169-179` `interruptedBlocks()` 只保留 text/reasoning（丢 tool-call，因为补假结果没意义）。
- 断流（未开始/已开始）：`agent.ts:489-530` 未开始直接抛；已开始则按 `signal.aborted` 决定把可见前缀落成 `interrupted: true` 的 `assistant/message`，否则落 `assistant/attempt`，落地失败抛 `AggregateError`（`522-528`）。

## 3. 工具调用的编排

**结论**：循环本身不调度——每 step 把**整个原始批次**交给 `ctx.tools.execute()`；执行器按 `isConcurrencySafe` 把调用切成 `exclusive` 屏障与 `parallel` 滚动池（默认 10），**并行只并行工具本体**：pre-execute/审批/结果提交全部严格 model-order 串行。

- 单点入口：`packages/core/agent-loop/src/agent.ts:580-583` `this.loopCtx.tools.execute({calls, signal, agent, log:{turn,step}, maxParallelToolCalls, toolSet})`；注册表转发 `packages/core/tools/src/index.ts:376-377`。
- 默认并发：`packages/core/agent-loop/src/constants.ts:6` `DEFAULT_MAX_PARALLEL_TOOL_CALLS = 10`；配置 `index.ts:295`（volatile，下次批次生效）。
- 分组：`packages/core/tools/src/batch.ts:76-91` 逐段取 `pipeline.executionMode(first.exec).kind`，`parallel` 取剩余全部，`exclusive` 取 1 条；分类 fail-closed：`packages/core/tools/src/pipeline.ts:106-115`（只有 `=== true` 才 parallel，未知/抛错一律 exclusive）。
- 并行实现：`batch.ts:186-201` `fillPool()` 受 `maxParallelToolCalls` 约束；`batch.ts:206-218` `Promise.race` 收割后 `commitReady()`。
- 审批/权限：**每调一次都在自己的 `prepare()` 里串行做**——`pipeline.ts:191-234`：`tools/pre-execute` 瀑布（`200-203`）→ 需要时 `serviceAsk()`（`204-206`）→ `checkGuards()`（`214`）；`pipeline.ts:442-490` 把 ask 解析成 allow/deny，缺审批通道=deny（`457-467`），拒绝/取消都有独立 code。审批本身是 turn 内、成对审计的：`packages/interaction/user-approval/src/index.ts:243-262`（`approval/asked`+`approval/decided`，无 open turn 直接抛），`294-334` 与取消信号竞速、`never` 策略在 `302` 提前短路。
- 结果顺序：`batch.ts:138-150` 只按 model order 提交**连续**已就绪槽；`batch.ts:243-266` 开始时写 `tool/call`，完成时写 `tool/result` 并带 `sourceEventSeqs:[callSeq]`。
- 失败语义：工具体抛错 → `isError` 结果，**不结束 turn**（`packages/core/tools/README.md:10` "without ending a turn on ordinary tool failures"）；调度器内部失败 → `ToolBatchExecutionError`（`batch.ts:93-95`）并排空在飞调用（`219-223`），循环侧再补写恢复结果（`agent.ts:370-381`，`packages/core/session/src/repair.ts:105-198`）。
- 重试：核心循环**没有**工具体重试；重试是消费 `error.code` 的策略（如超时被映射成 `TOOL_TIMEOUT`：`packages/guard/timeout-policy/src/index.ts:55-80`）。
- 已知限制：并发分类是"一元"的，需要跨兄弟比较的调用必须 exclusive（`agent-loop/README.md:233`）。

## 4. 上下文管理与压缩

**结论**：循环里**没有**窗口裁剪逻辑：历史每次由 `session.deriveMessages()` 从日志派生并冻结；计量在 `tokenMeter`（4 字符/token 启发式 + provider usage 复用），压缩由 `compaction-basic` 挂在 `agent/pre-step` / `agent/request-error` 上，用"surface 区间替换成一条摘要节点"实现，**两侧必须 tool-pairing 平衡**。

- 派生+冻结：`agent.ts:718-733` `deriveMessages()` → 逐条 `deepFreeze` → `Object.freeze(boundaryMessages)`。
- 计量：`packages/llm/token-meter/src/estimate.ts:13` `CHARS_PER_TOKEN = 4`；`packages/llm/token-meter/src/index.ts:122-140` `contextPressure()`；`measure()` 走 session 事件重放折叠。
- 触发 A（步前压力）：`packages/compaction/compaction-basic/src/index.ts:172-190` 监听 `agent/pre-step` → `compactIfNeeded(agent,'pressure')`；阈值 = `thresholdRatio(0.8) × contextWindow − headroom(65536) − reservedCompletion`（`config.ts:20-23`、`75-76`、`172-192`）。
- 触发 B（真溢出）：`index.ts:228-270` 只在 `failure.code === CONTEXT_WINDOW_EXCEEDED` 时介入，`maxOverflowRetries` 默认 1（`config.ts:106`）；只有真的产生了 surface 替换（`replaceGeneration` 前进）才返回 `{kind:'retry'}`（`index.ts:265-271`）。
- 触发 C（手动）：`index.ts:425-469` `compactNow()` 走 `agent.runMaintenance()`，idle 才能跑。
- 实现：`packages/compaction/compaction-basic/src/region.ts:513` `surfaceOp: { op:'replace', startSeq, endSeq }`；先做无模型裁剪（`toolResultPruner`，`index.ts:321`、`344-368`），再按 `retainTokens` 选区间（`index.ts:372`），失败重试 `compactionRetries` 默认 1（`index.ts:371-388`），摘要必须更小（`region.ts:424-426`）。
- 不拆对：`packages/compaction/compaction/src/tool-pairing.ts:112-126` 提供 `toolPairingBalancedBefore/After`；`region.ts:355-361` 双边界校验，`region.ts:146` 选区间时向前回退到平衡点；抽象契约写在 `packages/compaction/compaction/src/index.ts:168-179`（"Both edges must be balanced so assistant tool calls remain paired with their results"）。
- 压缩后系统提示重建：`agent.ts:450-452`（`assembly.systemUpdates(!firstAttempt)`）+ `agent-loop/README.md:124`。

## 5. 中断与 steer

**结论**：steer 是"步边界输入"，不是抢占：running 时只入队 `next-step`，当前 step 收敛后在同一 turn 内开下一 step 时被 claim。cancel 才是抢占：清/留 inbox + abort 该 phase 的 `AbortController`；"当前步可收敛"靠三件事——流前缀落成 interrupted 消息、在飞工具体不抛弃、被跳过调用补合成结果。

- 入队：`agent.ts:177-187` `followup→('next-turn',wake)`、`steer→('next-step',wake)`、`inject→('next-step',no wake)`；`agent.ts:168-175` 若在已中止活动上唤醒，改投 `next-turn`。
- claim：`packages/core/agent-loop/src/inbox.ts:108-113` 步边界一次拿走**全部** `next-step` + （turn 边界时）1 条 `next-turn`，纯删除 splice + 逐条 `agent/inbox/claimed`。
- 步内延续：`agent.ts:391-396` 只有 `nextStep.length === 0` 才 `turn-stopping`/`break`，否则 `target='next-step'` 继续（这就是 steer 生效点）。
- cancel：`agent.ts:189-199` 先 `inbox.clear()`（除非 `keepInbox`），再 `phase.abort.abort(cause)`；`agent.ts:232-243` 中止期间的唤醒被 latch，收敛后重放。
- 收敛点：`agent.ts:446/481/485/488/641` 每个 await 前后 `throwIfAborted()`；流异常分支 `492-509` 把已交付文本落成 `interrupted:true` 的 assistant 消息（"Cancellation preserves streamed text already delivered"，`agent-loop/README.md:12`）。
- 工具侧：`packages/core/tools/src/pipeline.ts:244-252` 完成后不被抛弃（body 到 quiescence 才判 ABORTED）；`batch.ts:225-230` 中止后对未启动调用 `settlement.skip()`；`batch.ts:271-280` 合成 `ABORTED_BEFORE_DISPATCH` 结果。
- 生命周期闭合：`agent.ts:384` `step/end`、`agent.ts:418` `turn/end` 都必写；`kick()` 的 `finally`（`277-283`）保证 phase 回到 idle 并处理 latch。

## 6. 子 Agent

**结论**：子 agent 通过 `ctx.subagents` 的 provider 启动。进程内两种（`spawn` 全新子会话、`fork` 带父已完成 turn 前缀），都走 `ctx.agents.create()/resume()`，即**同一条 AgentLoop + 同一套工具流水线 + 同一套 guard/审批接缝**，只是各自独立 Session、独立 scope、并把策略收窄为 `never`。进程外 provider（dsh-sdk / acp / claude-code / codex）是真子进程，跑的是各自部署的 harness，不复用父进程的循环。

- 创建点：`packages/subagent/subagent/src/manager.ts:865-882` —— `this.ownerCtx.agents.resume({resumeSessionId,...})` 或 `agents.create({sessionId, seed, parentSource, inheritedEventCount, agentOptions, setup})`。
- 进程内 provider：`packages/subagent/subagent-spawn-in-process/src/index.ts:38-55`（`inheritsParentContext = false`）、`packages/subagent/subagent-fork-in-process/src/index.ts:46-78`（seed = 父日志到最后一个 `turn/end`，`prepareContinuable` 一次捕获）。
- 同链证明：`setup` 接收子 `agentCtx`，用 `childCtx.systemPrompt.*`、`childCtx.tools.restrict()` 注册（`packages/subagent/subagent/src/child-agent.ts:192-224`）；工具/审批/超时监听都注册在 ctx 层（`pipeline.ts:200`、`user-approval`、`guard/timeout-policy`），子 agent 的 scope 在 `apply` 内被继承。
- 差异是**策略收窄**：`child-agent.ts:226-239` 委派时把审批策略钉成 `'never'`、固定 sandbox mode；`child-agent.ts:171-177` 用 runtime-context 明确告知子 agent "permission scope was fixed…cannot be widened"。
- 递归预算：`child-agent.ts:49-58` `resolveChildDepth()`（`DEPTH_EXCEEDED`），持久化在 session header（`child-agent.ts:135-154`）。
- 进程外：`packages/subagent/subagent/src/out-of-process.ts:52-99`（"The capability advertisement of an out-of-process backend: NONE"）；`manager.ts:821-835` 走 `inputs.start(signal)` 起外部 run。
- 模型可见入口：`packages/subagent/tool-subagent/src/index.ts`。

## 7. 状态与恢复

**结论**：状态 = append-only `SessionEvent` 日志；模型历史只是派生视图。"可恢复"的粒度是 **turn 边界**：重启后 `resume()` 重新拿写句柄、读日志、把断掉的尾部 turn 用合成事件封口，再重建 agent；**不恢复**半截 step / 半截流。

- 单一事实源：`docs/architecture.md:138` "Model-visible means logged"；`agent.ts:718` 每次请求都从日志派生。
- 写句柄获取：`packages/core/agent-loop/src/index.ts:694-698`（create）、`796-801`（resume，先 `persistence.open(id,'write')` 抢占，避免并发恢复）。
- 恢复即修复：`index.ts:806-810` 读全量事件 → `interruptedTurnClosers(persisted)` → 以普通批次 append；实现见 `packages/core/session/src/repair.ts:53-98`（补 `tool/result` → `step/end` → `turn/end{kind:'interrupted'}`），`repair.ts:105-198` 按 `tool/call` 是否记录过区分 `TOOL_OUTCOME_UNKNOWN` / `TOOL_NOT_STARTED`。
- 耐久点：`packages/session/session-checkpoint-policy/src/index.ts:64-68`（派发前 flush 请求前缀）、`70-75`（顶层工具体前 flush `tool/call`）、`79-82`（下一请求前 flush 上一步）。
- 待办入队也持久：inbox 是 durable projection（`agent-loop/src/inbox.ts:215-231` 每次 splice 落 `agent/inbox/spliced`），所以重启后 pending 输入会重放。
- 恢复边界（明确限制）：`agent-loop/README.md:234` 根上下文关闭时最后一个 `turn/end` 可能只在内存；`docs/architecture.md:134` 硬进程丢失在 settlement 之前不留 attempt 流；`agent.ts:201-222` 的 `runMaintenance` 是"非 turn"任务，压缩不会续跑未完 turn。
- 格式演进：`docs/architecture.md:136` 相邻 `vN→vN+1` 迁移链、只写后继代、从不改写已提交代（`packages/session/session-format-*`）。

## 8. 核心循环明确不做的事（边界）

**结论**：循环只做"claim 输入 → 组装 → 请求 → 流 → 分派工具 → 记账 → 决定是否再走一步"，其余全部外置为插件/事件。

- 不做 token 计量与裁剪：`tokenMeter` 独立服务（`packages/llm/token-meter/src/index.ts:87-140`），循环只在 `agent.ts:699-712` 记录 `request/context`。
- 不做压缩策略：`compaction-basic` 通过 `agent/pre-step`（`index.ts:172`）和 `agent/request-error`（`index.ts:228`）挂入；循环对这些无感。
- 不做审批决策：循环只 `tools.execute()`；ask→allow/deny 在 `user-approval`（`index.ts:243-334`）。
- 不做并发调度：`ctx.tools.execute()` 后面是 executor 的 `batch.ts`；循环只传 `maxParallelToolCalls`（`agent.ts:578`）。
- 不做请求重试：`llm-retry` 监听 `agent/request-error`（`packages/llm/llm-retry/src/index.ts:241`），策略定义在 `packages/llm/llm/src/retry-policy.ts:15-58`（默认 5 次、500ms 起、指数退避+抖动）。
- 不做持久化分帧/迁移：`session-persistence{-jsonl,-sqlite}` 拥有（`docs/architecture.md:136`）。
- 不做迭代/预算上限：`agent-loop/README.md:238`；也没有"停止策略"——停止点是 `agent/turn-stopping`（`agent.ts:392`），由 hooks（`packages/experimental/hooks-claude-code/src/index.ts:284`）等实现。
- 不做会话历史检索：`session-query` 是独立查询服务（`packages/session-query/session-query/README.md:10`），不在循环路径上。
- 不做 todo/plan/skill/goal：各自包通过工具与 `agent/*` 事件接入（如 goal 的轮次预算在 `packages/goal/goal/src/fold.ts:235`）。

---

## 这个核心循环的设计取舍

1. **"日志即状态"胜过"内存即状态"**：模型可见的一切都必须先落 `SessionEvent`，请求每次从日志派生并深冻结（`agent.ts:718-733`）。换来可重放、可冷读、可迁移（`docs/architecture.md:136-138`），代价是每步重推导 + 写放大 + 一个"派生必须与日志一致"的强约束。
2. **"终态唯一"胜过"错误可抛"**：适配器异常被归一成唯一 `finish` chunk（`llm/index.ts:1170-1178`），中断只保留文本前缀、丢弃 tool-call（`assembler.ts:169-179`），缺失结果用合成事件补齐（`repair.ts:105-198`）。历史永远可配对、可重放，代价是语义上的"结果未知"要模型自己判断是否重试。
3. **策略外置到四个切点**：`agent/pre-step`、`agent/request`、`agent/request-error`、`agent/turn-stopping` 是循环唯一的策略面（`packages/core/agent/src/runtime-types.ts:351/374/390/418`）。压缩、重试、超时、重复调用提醒、停止钩子全是监听器，组合极其灵活；代价是"循环行为"取决于装了哪些 bundle，理解与排障必须看组合而不是看循环。
4. **无硬上限、以"步边界输入"驱动收敛**：steer/inject 在同 turn 内续步（`agent.ts:391-396`），turn 只在无 owed work 时关闭。长任务不会被硬阈值截断，代价是失控 turn 只能靠 `turn-stopping` 策略或用户 cancel 兜住（这正是 `agent-loop/README.md:238` 自陈的缺口）。
5. **并行只并行"身体"**：审批、策略、结果提交严格 model-order 串行（`batch.ts:138-150`、`pipeline.ts:191-234`），且并发分类一元 fail-closed（`pipeline.ts:106-115`）。可预测性与审批一致性优先于吞吐，代价是并发上限（默认 10）与"需要跨调用比较的工具只能独占"。
