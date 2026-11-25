# 差距分析 R2：子 Agent 调度与交互 / 侧边栏与轨迹窗格 / 大文件加载与预览

> 对象同上轮：`chiron-desktop`（Go 网关 + Python 引擎 + Vue3/Electron）vs `deepseek-harness`（TS monorepo）。
> 方法：只读代码与文档，逐条给 `文件:行号`；找不到就写"未找到"。仅比较这四条轴。
> 上一轮（核心循环 / 长线调度 / 工具交互）见 [gap-analysis-chiron-vs-dsh.md](gap-analysis-chiron-vs-dsh.md)。

## 0. 结论

1. **子 Agent 是这次最实质的差距，且方向与直觉相反**：Chiron 在"分布式可靠性 + 审批可转交"上更强
   （配额三层拒绝、审批/提问**转发给父界面**并能回传决定），DSH 在"子是一等会话"上更强
   （独立日志/独立 driver/可寻址/可续聊），代价是 **DSH 把人工交互在委派边界整刀切除**
   （审批 `never`、提问 `DELEGATED_CALLER`）。两边各有真缺陷：Chiron 的 `inherit_context` 是**空转**、
   父**看不到也回滚不了**子的写入；DSH 的进程外子只能跑一次、目录状态分不出失败原因。
2. **侧边栏**：DSH 是"可注册 + 停靠树 + 布局快照校验"，Chiron 是"三处硬编码 + 本地宽度记忆"。
   差距是**扩展点**，不是可用性。
3. **轨迹窗格**：DSH 有分页 + 虚拟化 + 折叠集合 + "进行中不虚构耗时"；Chiron 的 `CallChainTimeline`
   **一次渲染整份 trace、无上限、无过滤**，且流式期直接不显示。这是本次最该补的一块。
4. **大文件与预览**：DSH 三条硬约束（分页 offset / `maxFileBytes` 拒绝而非截断 / Worker 与 sandbox iframe 隔离）；
   Chiron 三条路各不相同（KB 整块无上限、workbench 行分屏 2000 行上限、媒体走第三方组件），
   设备文件走 **device-fs WS 分片 base64**（8MB 上限、默认 64KB），**全仓无 Web Worker**。
   Chiron 的 `staleGuard` 代次票据比 DSH 的版本对账更直接，是它该保留的优势。

---

## 1. 子 Agent 调度与交互

### 1.1 DSH：子 Agent = 一等 Session

| 面 | 事实 | 证据 |
|---|---|---|
| 启动路径 | 只有两族：**进程内**（spawn / fork，走 `prepareContinuable`）与**进程外**（acp / codex / claude-code / dsh-sdk，只能跑一次）。选哪条由工具实例的 `provider` 配置决定，不做运行时智能路由 | `packages/subagent/subagent/src/index.ts:274-298`、`tool-subagent/src/index.ts:328-335`、`out-of-process.ts:57-62` |
| fork vs spawn | spawn 无种子、`inheritsParentContext=false`；fork 取父"最后一个 `turn/end` 为止"的**已完成**前缀作 seed（不见在飞轮），种子只在创建时切一次 | `subagent-spawn-in-process/src/index.ts:45-54`、`subagent-fork-in-process/src/index.ts:46-78` |
| 记账 | 子会话 = 独立 Session（自己的日志、工具调用、token）；血统写进子 header（`parentSession`/`isSeeded`/`origin:'subagent'`/`delegationDepth`）；**父日志只多 catalog 事件** | `child-agent.ts:135-154`、`catalog.ts:32-44` |
| 父看到什么 | 只有最后一条非空 assistant 文本 + 可选结构化结果 + diagnostic；模型侧明说"parent does not receive your full transcript, tool output, or reasoning" | `continuation-messages.ts:83-96,141-175` |
| 并发 | 容量按**子树**共享（根不占槽，默认 8），深度默认 1；超限**拒绝**（`ACTIVATION_LIMIT_REACHED`）不排队——理由是"父等子可能等的正是自己占的槽"。同仓 workflow 引擎反而用 FIFO 排队 | `subagent/src/index.ts:197-200`、`activation.ts:28-39`、`subagent/README.md:59` |
| 推进 | 子有自己的 driver（`withInitiator(...).kick()`），**不在父 step 里**；宿主靠 `whenIdle()` + `waitForTreeWork()` 轮转 | `packages/core/agent-loop/src/agent.ts:232-254`、`manager.ts:251-283` |
| 人机交互 | 审批在委派边界钉成 `never`（任何 answerer 之前就 rejected）；提问工具对 `origin==='subagent'` 直接回 `DELEGATED_CALLER`，文案要求"把未决问题写进最终结果" | `child-agent.ts:240,254-285`、`packages/interaction/user-questions/src/index.ts:137-150`、`deepseek-harness/docs/subsystems/approval.md:33,86` |
| steer / cancel | steer 用 `sendMessage`（运行中在下一 step 收，空闲起一轮）；interrupt 需 `{kind:'user'}` 或精确活祖先权威，本地子 `keepInbox` 只停当前轮，进程外子 abort+dispose 永久停 | `manager.ts:194-233,645-658` |
| 生命周期 | 子变 idle 且 inbox 空 → 关准入 → 捕获结果 → dispose → 通知 → 释放槽 → 才 resolve；stopReason ∈ `completed/aborted/error/max-tokens/refusal`；非 completed 在工具层映射为 isError | `manager.ts:987-1133`、`deepseek-harness/docs/subsystems/subagent.md:274-322` |
| 交付模式 | `delivery:'parent'` 走用户角色通知但**只有父仍在线才发**；`delivery:'caller'` 不发通知、调用方自己 await | `manager.ts:1136-1158`、`workflow-ptc/src/host.ts:218-233` |
| 前端 | `ui-subagent` = 目录 + 导航 + 续聊；数据来自**各 Session 的 projection**（`subagentCatalog`/`subagentTiming`），**没有**独立 subagent 事件流；子会话按普通会话打开，所以能看完整轨迹 | `packages/client/ui-subagent/src/client/index.ts:75-116`、`ui-subagent/README.md:28-30` |
| 明确局限 | 进程外子一次性；跨进程不协调；接受但未落日志的 prompt 崩溃即丢；`subagent/end` 只可观察不可干预；目录读取串行、大树累积延迟；名字永久占用；取消收敛窗口内消息排队；UI 分不出 inactive 的具体原因 | `subagent/README.md:250-258`、`ui-subagent/README.md:128` |

### 1.2 Chiron：子 Agent = 合成会话上的第二个 runtime

| 面 | 事实 | 证据 |
|---|---|---|
| 启动 | 同步/后台都是**同进程 asyncio**（`create_task`），每次委派新建 `AgentRuntime`；三种入口共用同一条子会话路径（`profile` / `target=profile:/skill:/workflow:` / 跨实例 HTTP） | `app/tools/subagent.py:191-274,351-435`、`app/agent/subagent_runner.py:786-796,921` |
| 记账 | 子会话 id = `sub:<parent_session>:<run_id>`；**L0 逐事件**落 `subagent_run_steps`；进父上下文的只有 L1 摘要 + L2（默认 2000 字符）；usage 记在**子 run 自己的行**，不叠加进父 | `subagent_runner.py:82,164-165,787-790,1310-1350`、`subagent_result.py:22,98-118` |
| 并发（两套闸门） | ①**分布式配额**：租户 24 / 会话 8 / 全局 96，Lua 原子判定，**超限拒绝**（`retryable:false`，理由写明"排队消耗父 wall budget"）；②**进程内 `WriteScheduler`**（默认关）：按**声明写路径集合**判重，非嵌套冲突 FIFO 排队、嵌套立即失败 | `app/subagent/quota.py:9-13,42-75`、`scheduler.py:28-59,127-188,307-355` |
| 深度/预算 | 全局 `MAX_DEPTH=3`、Profile 默认 `max_depth=1`（默认不许孙 Agent）；`SUBAGENT_*` 预算（token 默认 200k，wall/cost 默认不限），越界记 `status=failed, error=budget_exceeded:<轴>` | `subagent.py:42,187-189`、`profile.py:35`、`subagent/budget.py:21-41`、`subagent_runner.py:977-991` |
| 人机交互 | **审批与提问会转发**：子 emit `subagent.approval`/`subagent.ask` → 父会话前端卡片 → 决定经同一 HTTP 通道回传 → 父 runtime 写 Redis 决策键 → 子轮询取走；`unattended` 继承父（即时拒绝） | `subagent_runner.py:1057-1126`、`SubAgentPanel.vue:429-463`、`main.py:2523-2569`、`runtime.py:5659-5673,5744-5761` |
| steer / cancel | **父不能给子追加指令**（`push_steer` 按 `task.session_id`，子会话不在活跃表 ⇒ 直接 `no active agent`）；cancel 走注册表 + Redis `subagent:cancel` 广播 + Go 侧无回执判 `lost` | `runtime.py:3201-3228`、`main.py:2573-2610`、`registry.py:138-160,298-362`、`subagent_cancel.go:84-98,148-212` |
| 生命周期 | 单一出口：状态归一（有产出的失败 → `partial`）→ L1 摘要 → 收据 → `finish_run` → **一个终态事件** → 缓存双面写；取消的终态写库放进独立任务（取消传播会打断 await）；wall 定时器 + 空闲 300s / 总 1800s 看门狗 + `reap_stale_runs` | `subagent_runner.py:1164-1264,1299-1408`、`registry.py:14-20,183-260` |
| 回父三条通道 | ①当轮工具载荷（`result_ref` 必须在 `output` 前，字段序有语义）；②followup 唤起新一轮（幂等 SETNX、3 次/小时、禁级联）；③DB 游标注入 `<subagent-reports>`（兜住②任一跳丢失） | `subagent_runner.py:96-177`、`followup.py:1-25`、`reporting.py:44,97-120`、`main.py:1985-2046` |
| rerun / resume | rerun 只认终态白名单，复用库里的 task/profile/read_only、挂原 run 下；resume 是**分叉新 run**、默认关，原 run 永不 UPDATE | `rerun.py:44,137-192`、`subagent_resume.py:65` |
| 前端 | `subagent.*` 搭在**会话 SSE** 上（前缀过滤、帧去重、50ms 合批、上限 500）；后台/重跑事件由常驻投递器经 Redis → Go relay 补；列表与选中 run **仍有 3s 轮询**兜底；审批/提问卡由面板自己维护（不走 `interactionScope`） | `useSubagentStream.ts:96-205`、`subagent_events_relay.go:30-90`、`SubAgentPanel.vue:672-713,808-818,1176` |

### 1.3 差距

**Chiron 领先（不要照搬 DSH）**

1. **人工交互不被切断**：DSH 在委派边界把审批钉死为 `never`、提问直接拒；Chiron 把审批/提问**转发到父界面并能回传决定**。团队/无人值守场景下，这是"能不能用"的差别，不是风格。
2. **分布式配额 + 明确的拒绝语义**：三层 Lua 原子配额（24/8/96）并带 `retryable:false`；DSH 是**进程内**槽位池（8），跨进程不协调（`subagent/README.md:256` 自述）。
3. **结果回父有冗余通道**：当轮载荷 + followup + DB 游标注入三路，任一跳丢失还能补；DSH 只有"通知（父在线才发）"或"调用方 await"。
4. **可查询的子轨迹**：`GET /v1/subagent/runs/{id}/events` + L0 steps 落库；DSH 明确"父拿不到子的完整轨迹"，只有 UI 能打开子会话。
5. **rerun 复用语义**（终态白名单 + 复用 profile/read_only + 挂原 run 下）在 DSH 是"已结束的子不能再 fork"。

**DSH 领先（值得迁移）**

1. **子是一等会话**：独立日志 + 独立 driver + 可寻址 + 可在 UI 里当普通会话打开续聊。Chiron 的子会话是"合成会话"，**没有 `agent_runs` 行**，因此**不能挂起等人工**（`interactions.py:42-48`），审批超时即该步失败。
2. **拒绝而不是排队，且理由公开**："父等子可能等的正是自己占的槽"；Chiron 的配额拒绝理由写在注释里，但 `WriteScheduler` 又是排队 —— 两套策略并存且**默认关**，行为难以预期。
3. **进程外 provider 抽象**：acp / codex / claude-code / dsh-sdk 统一"只能跑一次 + 能力位声明为 false，父侧**拒绝**而非静默忽略"。Chiron 没有"外部 harness 子 Agent"这条路（只有自己的引擎）。
4. **stopReason 枚举**（completed/aborted/error/max-tokens/refusal）；Chiron 是 `status` + `partial` 归一。
5. **fork 语义写清楚**：种子 = 最后一个 `turn/end` 的完成前缀，之后完全独立；Chiron 的 `inherit_context` 开关**实际是空转**（见下）。

**Chiron 侧的真缺陷（本次读出来的，值得进 backlog）**

| # | 缺陷 | 证据 |
|---|---|---|
| 1 | `inherit_context` 空转：工具路径从不传 `parent_messages`，`inherited_messages` 恒为 0；只有测试直接调 store 才覆盖 | `subagent.py:283-321`、`subagent_runner.py:635,729-733`、`tests/test_subagent_inherit.py:168-181` |
| 2 | 父**回滚不了子的写入**：undo / side-effect 键按 `get_session_id()`，子在 `sub:...` 名下 ⇒ H38 补了 guard 链但没改归账键 | `undo_stack.py:58-61`、`side_effect_ledger.py:96,134-137`、`runtime.py:2359-2365` |
| 3 | 轮数常量漂移：`MAX_TURNS_CAP=40` vs runner 硬夹 10（实际生效 10，注释说"与主路径同基准"） | `subagent.py:41,423,621-625`、`subagent_runner.py:794` |
| 4 | `PHASE_RUNNING` 从不发出（有定义、有测试要求 7 个阶段，但 emit 点缺 running） | `lifecycle.py:37,44-52`、`subagent_runner.py:677-686,1457-1475` |
| 5 | 两套子 Agent 实现并存：`multi_agent.SubAgent` 无落库/无配额/无取消语义，但 `unified_executor` 仍在用 | `multi_agent.py:84-290`、`api/unified_executor.py:772-799` |
| 6 | 事件丢弃不可观测：`EventSink.dropped` / `delivery_dropped` 既不进 UI 也无指标，面板"过程缺行"只能靠 `truncated` | `event_sink.py:219,224,490-497`、`SubAgentPanel.vue:1215` |
| 7 | 实时链路仍靠 3s 轮询兜底；跨实例取消无回执时只能"尽力" | `SubAgentPanel.vue:813-817`、`subagent_cancel.go:214-244` |

### 1.4 可迁移的具体改法

| # | 改法 | 落点 | 成本 | 风险 |
|---|---|---|---|---|
| 1 | **子会话补 `agent_runs` 行**，让子的审批/提问能**挂起等人工**（而不是超时即失败） | `interactions.py` + `subagent_runner.py` | 中 | 中（要处理挂起期间的配额占用） |
| 2 | **修 `inherit_context`**：工具路径把 `parent_messages` 真传下去，或删除该参数（不留空转开关） | `subagent.py` | 低 | 低 |
| 3 | **子副作用并入父账本**：写入时同时记父会话键（或记 lineage），让"整轮回滚"覆盖子 | `undo_stack.py` + `side_effect_ledger.py` | 中 | 中（去重要做对） |
| 4 | **事件丢弃进指标**：`dropped`/`delivery_dropped` 上报 `/metrics`，面板显示"过程事件缺失 N 条" | `event_sink.py` + 前端 | 低 | 低 |
| 5 | **父 → 子 steer 通道**：让父能给运行中的子追加指令（DSH 有，Chiron 无） | `steer.py` 按 run 维度存键 | 中 | 中（子没有 session 级 run 表） |

---

## 2. 侧边栏交互

### 2.1 DSH：三套模型，各有归属

| 面 | 事实 | 证据 |
|---|---|---|
| 左侧栏 | **固定槽位 + 注册式面板列表**（`sidebar.panellist`），选中项是单值 `activePanelId` ⇒ 互斥单选，reload 复位 | `packages/client/ui-sidebar/README.md` |
| 右侧栏 | `ui-dockkit` 停靠套件 + `ctx.sidebarRightTabs` **两阶段注册**；tab 可多开、可两个水平 pane、可浮层；**per-session localStorage + zod 校验** | `ui-sidebar-right/README.md`、`ui-dockkit/README.md` |
| 会话面板 | sections list 注册 + localStorage 排序 | `ui-session-panel/README.md` |
| 布局解算 | `ui-layout/columns.ts` **纯函数**：侧栏 264–420（默认 280、收起 56）；断点 768 drawer、1024 自动收起；右侧 300–70%（首开 45%、保 400px 中区） | `packages/client/ui-layout/src/columns.ts` |
| 拖拽/多窗口 | pointer capture + **rAF 节流**；desktop **无跨窗口布局广播**（只有"最后保存布局"共享） | `ui-layout`（AppFrame）、报告 |

### 2.2 Chiron：三处侧栏，全部硬编码

| 面 | 事实 | 证据 |
|---|---|---|
| 三处侧栏 | ①顶栏品牌菜单 + 标题栏工作台按钮组（**字面量数组**）；②`ChatSidePanel` **五视图互斥 union**（轨迹/会话/统计/…）；③项目树 `ProjectSidebar` 嵌在会话视图内 | `components/AppLayout.vue:96-123,192-199,330-356`、`chat/ChatSidePanel.vue:26,421,503,509-515` |
| 并存能力 | 有：工作台 / 参考栏 / 子 Agent 栏**同时挂载** | `AppLayout.vue`、`SubAgentPane.vue:44-66` |
| 注册机制 | **无**（`registerPanel|panelRegistry|PLUGIN_SLOT` 零命中） | 同报告 |
| 偏好持久化 | 每栏独立 localStorage 键（`chiron:chat-side-panel:v1`、workbench、todoPanel、swapLayout、splitWidth） | `views/ChatView.vue:1704-1707,212-230`、`useWorkbench.ts:15,46,79-86`、`useSplitWidth.ts:31-57` |
| 宽度 | 4px 死区 + 指针捕获 + Escape 撤销 + 双击重置；MIN 260 / MAX 容器×0.6 | `useSplitWidth.ts:16-19,73-137` |
| 断点 | **字面量散落**：侧栏 1024、抽屉 768/576、工作台默认展开 1280；`useResponsive.ts` 零引用 | `usePanelCollapse.ts:45,77-85`、`useWorkbench.ts:40`、报告 |
| 窗口状态 | Electron **不记忆窗口 bounds**（硬编码 1440×900、min 960×640） | `electron/main.cjs:1892-1896` |

### 2.3 差距

| 维度 | DSH | Chiron | 判断 |
|---|---|---|---|
| 面板扩展点 | 注册表 + 停靠树 + tab 类型 | 三处硬编码 | **DSH 领先**（真扩展点） |
| 布局持久化 | per-session + zod 校验 | 每栏独立键，无结构校验（够用） | DSH 领先（轻量可抄一点：给快照加 schema 校验） |
| 布局解算 | 纯函数 + 单测 | 常量散落、`useResponsive` 死代码 | **DSH 领先**（这层成本很低，值得抄） |
| 并存/互斥 | 左互斥 / 右并存（明确分工） | 五视图互斥 + 多栏并存（产品决定） | 打平 |
| 窗口状态 | desktop 无跨窗口广播（明确取舍） | 不记忆 bounds（更退一步） | DSH 略强 |

---

## 3. 轨迹窗格

### 3.1 DSH：投影 + 分页 + 虚拟化 + "不虚构"

| 面 | 事实 | 证据 |
|---|---|---|
| 数据源 | **纯投影**自 session 事件走廊（view target `'trajectory'`），不另存 | `packages/client/ui-trajectory/README.md` |
| 层级/分组 | turn → group → cell；User / Assistant / Tool / **嵌套 Subtool** / compaction，标 turn 与 step 边界 | 同 README、`src/client/TrajectoryTable.tsx` |
| 折叠 | turns / calls / subcalls **三组独立折叠集合**；折叠内的 subcall 仍可被搜索命中 | 同 README |
| 长历史 | 尾部 **50 节点窗口** + 手动向前翻页；**>100 行启用虚拟化**（`@tanstack/react-virtual`，overscan 12，`anchorTo:'end'`） | `TrajectoryTable.tsx:7,52,2227-2235` |
| 流式 | 跟随尾部直到用户**向上滚动**；进行中记录只显示"已开始"，**不虚构耗时** | 同 README（Summary） |
| 搜索/深链 | 自建增量索引 + 3s 节流；无深链 | 报告 |

### 3.2 Chiron：两个"轨迹"，都不是事件投影

| 面 | 事实 | 证据 |
|---|---|---|
| 轨迹 A：提问锚点 | `ChatSidePanel.trajectory` = **用户提问索引**（搜索 + 右侧轨道百分比定位 + 列表跳转；摘要截 40 字；**无上限、无虚拟化**）；跳转走几何偏移不走 `scrollIntoView` | `ChatSidePanel.vue:144-165,437-498`、`ChatView.vue:3568-3574`、`MessageList.vue:512-522` |
| 轨迹 B：调用链 | `CallChainTimeline` 数据来自 **`GET /v1/traces/{id}`**（HTTP 一次拉全量，**非 SSE 增量**）；时间线 / 执行图两种视图；点击详情就地展开 | `CallChainTimeline.vue:160-176,48-68` |
| 可见时机 | 只在回合结束后出现（`currentTraceId` 来自 SSE `done`，且 `!loading`）⇒ **流式期不显示** | `ChatView.vue:708,3547-3551,3589-3593` |
| 上限 | 只有 `max-height: 400px`；**无分页 / 无 span 上限 / 无过滤 / 无跳转** | `CallChainTimeline.vue:345-349` |
| 口径问题 | `totalDurationMs` 直接求和（父子 span **重复计**）；`emit('select')` 模板**无消费点** | `CallChainTimeline.vue:224-225,153-155` |

### 3.3 差距

1. **长历史三件套 Chiron 全缺**：分页、虚拟化、折叠集合。几十轮任务下 `CallChainTimeline` 一次性渲染全部 span，且没有"只看最近"的退路。
2. **"进行中不虚构耗时"**：DSH 明确只标"已开始"；Chiron 的 span 有 `duration_ms`，进行中节点会被当成 0 或最后值；且工具层 `totalDurationMs` 父子重复求和。
3. **流式期不可见**：Chiron 要等 `done` 才有 trace，看不了"正在跑什么"；DSH 的轨迹是实时投影。
4. **Chiron 有、DSH 没有**：执行图（graph）视图 + 时间线/图切换 + 提问锚点轨道 —— 这是差异优势，别丢。
5. **数据源差异决定了改法**：DSH 能"纯投影"是因为日志是唯一真相；Chiron 的 trace 在 PG、按 `traceId` 拉一次。所以 Chiron 不能照搬投影，只能做**分页 + 增量**。

---

## 4. 大文件加载与预览

### 4.1 DSH：分页 / 限额 / 隔离三条硬约束

| 面 | 事实 | 证据 |
|---|---|---|
| 文本 | **按 offset 分页读**（`read(sessionId, path, { offset })` 直到 eof），Markdown/代码**增量渲染累积前缀**；新版本页 >1 时**从头重读**不混版本 | `packages/client/ui-sidebar-documentpreview/README.md`（How it reads） |
| 完整字节 | PDF / HTML / 图片 / 表格走 `readBytes`；Host `maxFileBytes` **拒绝而不是截断**（默认 32 MiB，可配） | 同 README、`packages/api/workspace-files/src/index.ts:110,220,312` |
| 渲染隔离 | 文本/md/代码在 renderer；PDF 与 Excel 进 **Worker**；HTML 进 `sandbox="allow-scripts"`（无 `allow-same-origin`）的 Blob iframe | 同 README + 报告 |
| 资产限额 | HTML 预览：每资产 4 MiB / 总 32 MiB / 64 个；超限**失败而不是半个包** | 同 README |
| 生命周期 | 换模式/卸载释放 Blob URL、中止相关读取；SVG 走 `<img>`（markup 不进 DOM） | 同 README |
| 文件树 | 逐层懒加载 + Host **条目上限 2000** + `truncated` 标记 | 报告（ui-sidebar-files） |
| 上传 | 单次 `POST /api/session/uploadFileBinary`（**非分片、非 BFF**），Blob 走 Worker XHR 取进度；有进度/取消，**明确不可续传** | 报告（file-upload） |

### 4.2 Chiron：三条路各不相同

| 面 | 事实 | 证据 |
|---|---|---|
| KB 文档 | 单次 `GET /v1/kb/{kb}/documents/{id}/preview` **整块** `content` 塞 `<pre>`；**前端无大小上限、无分片**（仅 CSS 400px 视觉裁切） | `KBDocumentPreview.vue:29-38,125-151,224-225` |
| 工作台文件 | **服务端按行 offset/limit 分屏** + `PREVIEW_LINE_LIMIT=2000` 截断 + `loadMorePreview` 追加分块（只增强新块，旧块跳过高亮）；截断**必须告知** | `WorkbenchPanel.vue:534-625`、`previewCode.ts:17,60-80`、`projectFiles.ts:132-157` |
| 媒体库 | 第三方 `@file-viewer/vue3`（preset-all）吃签名 URL；缩略图 `loading="lazy"`；灯箱**仅图片**，视频/音频/Office 无内联预览 | `MediaView.vue:2124-2128,1724-1730,583` |
| 设备侧文件 | **不经 Electron IPC 读内容**；由**服务端**经 device-fs WebSocket 反向取：一次性票据 + `offset`/`max_bytes` 分片 + **base64**（默认 64KB，上限 8MB，`truncated` 标记）；前端**无分片循环** | `electron/device-fs.cjs:2-28,44-56,413-426`、`preload.cjs:174-189` |
| 渲染位置 | **全在主线程 Vue 组件**（PDF 用 iframe，Office/PDF 交 file-viewer），**全仓无 Web Worker** | 报告（grep `new Worker` 零命中） |
| 过期防护 | `staleGuard` 代次票据：KB 预览 / InteractionInbox / MediaView / workbench 已接；`SessionStatsPanel` **未接**（裸 watch+await） | `KBDocumentPreview.vue:117-151`、`staleGuard.ts:21-63`、`SessionStatsPanel.vue:19-36` |

### 4.3 差距

| 维度 | DSH | Chiron | 判断 |
|---|---|---|---|
| 大文本 | 分页 + 增量渲染 | KB 整块（无上限）/ 工作台行分屏（2000 行） | **DSH 领先**；Chiron 的 KB 预览是最薄的一环 |
| 二进制上限 | Host 拒绝不截断 + 前端明确提示 | device-fs 8MB 上限 + `truncated`，前端无提示 | DSH 领先（提示层） |
| 渲染隔离 | PDF/Excel 进 Worker、HTML 进 sandbox iframe | 全主线程；PDF 靠 iframe、Office 靠第三方组件 | **DSH 领先**（大 PDF/表格会卡主线程） |
| 过期响应防护 | 版本对账（版本变了从头读） | **代次票据**（`staleGuard`），更简单可靠 | **Chiron 领先** |
| 本地文件通路 | 走 Host `workspaceFiles`（同一套分页 API） | 走 device-fs WS（分片 base64，指令式） | 打平（Chiron 多了 NAT 穿透能力） |
| 上传续传 | 明确不可续传 | 单请求 + ticket，同样不可续传 | 平手 |

---

## 5. 不可照搬

1. **停靠树 / 浮动面板 / 布局快照校验**：Chiron 是单窗口桌面应用，收益远低于 DSH 那种"多面板并存"的 Web 产品。
2. **纯投影轨迹**：Chiron 的 trace 在 PG，纯投影会变成 N+1 查询；只能做"分页 + 增量"。
3. **DSH 的"委派边界切除人工交互"**：Chiron 的审批转发是优势，别为对齐而退化。
4. **子 Agent = 一等会话的重写**：Chiron 的子会话没有 `agent_runs` 行是结构性的，重写成本高；更划算的是补"可挂起"这一条（1.4 #1）。

## 6. 优先级建议

**立刻可做（小改动，直接消除风险）**
1. `CallChainTimeline` 加**上限 + 按 turn 折叠**（默认只展开最近 N 轮，超限提示"只看最近"），并修 `totalDurationMs` 的父子重复求和。
2. KB 预览加**大小上限与明确提示**（超限说"文件过大，无法预览"，而不是整块塞进 `<pre>`）。
3. 修 **`inherit_context` 空转**（要么真传 `parent_messages`，要么删掉这个开关）。
4. `SessionStatsPanel` 接上 `staleGuard`（唯一没接的异步面板）。

**中期（需设计）**
5. 轨迹窗格**分页 + 虚拟化 + 折叠集合**，并立"进行中不虚构耗时"的规则；流式期也能看到正在跑的调用。
6. **子副作用并入父账本**（让整轮回滚覆盖子 Agent 的写入）。
7. **事件丢弃进指标 + UI 提示**（`dropped`/`delivery_dropped`）。
8. 预览的**渲染隔离**：PDF/Excel 进 Worker（当前全主线程）。

**不做**
- 停靠树、布局快照校验、纯投影轨迹、DSH 式"无人工交互子 Agent"。
