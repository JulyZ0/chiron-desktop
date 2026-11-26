# 差距分析：Chiron vs ZCode v4.0.0（核心循环 / 前端 UI·UX）

> 对象：`chiron-desktop`（Go 网关 + Python 引擎 + Vue3/Electron，main `c7526fe` 起含 Kiri 容器挡位）
> 对比：`zai-org/ZCode` v4.0.0（commit `a4b44451`，2026-11-23 同步）
> 方法：只读两侧源码与文档，逐条给 `文件:行号` 证据；**找不到就写"未找到"**，不猜。
> ZCode 源码只克隆在仓库外（`../zcode`，depth=1），不参与 Chiron 提交，也不引入依赖。

## 0. 结论

ZCode 与 Chiron 的强弱几乎互补：

- **ZCode 强在"一条可恢复的事件流 + 一套严格的协议/契约 + 一个成熟的前端体验层"**。
  它的核心循环把每个流事件都落成持久 `SessionEvent`，用事件投影驱动 UI；前端把"流式浮现、轮虚拟化、
  工具卡注册表、快捷键单一事实源、平台能力注入"做成了可复用资产，并有 816 个桌面 E2E 用例。
- **Chiron 强在"团队与可靠性"**：多租户、审批转交/跨会话收件箱、五轴预算与线账、两套 checkpoint、
  子 Agent 挂起等人、知识库全链路、Windows 原生 + Kiri 容器隔离档位。这些在 ZCode 里**大多不存在**。

按主题看差距大小：

| 主题 | 差距 | 判断 |
|---|---|---|
| 核心循环的**事件/投影模型** | 大 | ZCode 的"事件即事实"值得追；Chiron 现在是消息 + 旁路事件，恢复/多端一致性靠补丁 |
| 核心循环的**工具调度与取消语义** | 中 | ZCode 元数据驱动并行 + Stop 补齐 `ToolCancelled`；Chiron 默认串行、取消写 `not_executed`，方向对但覆盖不足 |
| 核心循环的**预算与失败语义** | **Chiron 领先** | ZCode 主循环**没有轮次/预算硬停**（只声明不产生）；Chiron 五轴预算 + 线账 + 续段 |
| 子 Agent | **Chiron 领先**（能力）/ ZCode 领先（通知唤醒体验） | Chiron 有 fork、嵌套、挂起等人、按 run steer；ZCode 子 Agent 独立上下文、深度硬编码 1、可 `SendMessage` |
| 前端**流式渲染** | 大 | ZCode "文本完整、只动透明度"是当前最值得抄的一条；Chiron 仍逐 chunk 追加 |
| 前端**状态架构** | 大 | Chiron 会话/流式/审批/分屏状态集中在 `ChatView.vue`（4403 行）；ZCode 是 service + projection store |
| 前端**工具卡/diff/工作台** | 中 | ZCode 40 个专用 renderer、side pane 23 种 tab、终端/浏览器/Git 内嵌；Chiron 有工作台但面窄 |
| 前端**设计系统与测试** | 中 | ZCode 有 `DESIGN.md` 硬约束 + testid 契约 + E2E；Chiron 有 token 棘轮 + 213 组件测试，缺 E2E/a11y 门禁 |
| 前端**多端与 RTL** | **Chiron 领先**（RTL）/ ZCode 领先（桌面/Web/分享/手机远控一套 UI） | ZCode 无 RTL；Chiron 三语含阿语 RTL |

## 1. 架构总览对照

| 维度 | ZCode v4.0.0 | Chiron |
|---|---|---|
| 形态 | pnpm monorepo：22 个 `packages/*` + `apps/zcode-cli` 内嵌二级 monorepo（33 包） | 单仓多语言：Go 网关 + Python 引擎 + Vue 前端 + Electron + 独立服务 |
| Agent 核心 | TS：`apps/zcode-cli/packages/core`（628 文件 / ~50k 行），独立版本节奏 | Python：`python-engine/app/agent/runtime.py`（6203 行）为唯一状态机，单进程 |
| 进程边界 | Desktop Main / Window Host / CLI / 扩展进程 / Remote Source Server；RPC + NDJSON | Go 网关 / Python 引擎 / sandbox-service / pageindex / 桌面 Electron |
| 事实源 | CLI 的 SessionEvent + session store；UI 只消费 projection | 消息落 PG + Redis 事件缓存；前端按 SSE 事件增量维护视图 |
| 协议 | V4 command/projection（`baseRevision`/`baseLogEpoch`/`commandId`，ACK 六态） | HTTP + SSE；`/v1/agent/submit`、`/v1/events?Last-Event-ID` |
| 工程门禁 | `architecture-policy.yaml`（2618 行）+ 指纹 ratchet + 规则自测 | `make check`：pytest/ruff/mypy、go test、vue-tsc、`check:ui`（token/i18n/a11y 静态契约）、doc 脚本 |
| 规模 | 19,355 文件；`docs/` 2618 篇 | 前端 568 文件 / 64.7k 行 `.vue`；Python 355 文件；Go 467 文件 |

**判断**：ZCode 的架构复杂度显著更高（多进程、多端、协议版本化），这套结构是为"桌面 + Web + 手机远控 + 远程
Source Server"服务的；Chiron 的"网关 + 引擎 + 前端"更紧凑，团队部署与多租户是其核心。**不建议为了追平而
整体重构 Chiron 的进程/协议模型**，只借其"事件即事实"与"契约机械化"两类做法。

## 2. 核心循环逐项对照

### 2.1 循环驱动与状态机

| 项 | ZCode | Chiron |
|---|---|---|
| 结构 | `executeTurn` → 命令队列 → `executeTurnCommand` → `while (true)`（`turn-loop.ts:68-281`）→ `runModelBackedTurnStep` 返回 `continue/output_continuation/break`（`turn-model-step.ts:105`） | `AgentRuntime.run()` → `for turn in range(max_turns)`（`runtime.py:3128`），每轮一次 LLM + 一批工具 |
| 状态机 | `TurnMachineImpl` 相位严格校验（`turn-machine.ts:68`），但 `getNextPhase()` **无生产调用点** —— 只做记录 | 无显式相位机；以循环变量 + 异常 + 终态写库驱动 |
| 终止 | 文本且无工具调用、`stopTurnAfterResult`、异常、abort | `max_turns`、预算五轴、用户取消、错误、挂起 |
| 评价 | ZCode 的"状态机只记录、控制流靠返回值"是反直觉但自洽的选择；Chiron 的循环更传统 | — |

**差距**：ZCode 的"相位事件"（`turnPhase`）对前端可观测性有用，但**不是必须**。Chiron 的 `done` 事件已带预算/
上下文/缓存快照（`runtime.py:4356-4386`），可观测性不弱。

### 2.2 轮次与预算（**Chiron 领先**）

- ZCode：`TurnResultType` 声明了 `error_max_turns`/`error_max_budget`/`error_max_tool_calls`（`turn-state.ts:144-158`），
  但全仓**从未产生**；主循环无硬停。只有软刹车：输出续写 3 次、断流恢复 10 次、Stop hook 3 次、压缩熔断 3 次；
  15M token 只是提醒，goal 的 `tokenBudget` 无消费点（`turn-loop.ts:86`、`target-continuation-loop.ts:70-98`）。
- Chiron：`TaskBudget` 五轴（turns/steps/tokens/wall/cost，`task_budget.py:60/140`），tokens 撞线可续段、
  cost 由网关单价、线账跨续跑/委派、无进展软暂停（`runtime.py:3169/3210/3875-4088`）。

**结论**：这是 ZCode 的**明确短板**。团队/多租户计费场景不能照搬"无硬停"；Chiron 不应放松预算。

### 2.3 流式与事件持久化（ZCode 领先）

- ZCode：每个流 delta 都作为 `ModelStreaming` `SessionEvent` 落库（`model.ts:229-245`、
  `model-streaming-event-queue.ts:40-73`，128 水位背压），live 只发布"已落库事件"，live/replay/snapshot 的
  `eventSeq` 同源（`events.ts:82-125`）。工具调用只认 final tool-call（`docs/model-tool-call-stream-commit-boundary.md`）。
- Chiron：SSE 逐帧下发，`Last-Event-ID` 补发（`internal/api/events.go`），但事件不构成"可重建投影的唯一日志"；
  消息与旁路事件（thinking/tool/usage）分开存，恢复靠消息 + checkpoint。

**差距**：ZCode 的"事件日志 = 唯一事实源"让多端、冷恢复、投影重建都简单；Chiron 目前是"消息为准 + 事件旁路"，
多端一致性靠补丁。**不必重写存储**，但可引入 `event_seq` + 投影水位（见 §4 P1）。

### 2.4 工具调度（ZCode 领先，但可渐进）

- ZCode：按 `blockIndex` 排序后 `scheduleTools`；元数据决定并发（`destructive` 串行、`concurrentSafe`/readOnly/
  `sideEffectScope=none` 并行），拓扑分组、组内 `Promise.all`、组间串行，默认并发 10
  （`tool/scheduler.ts:85-202`、`batch-runner.ts:30-136`）；`stopTurnAfterResult` 时后续组统一生成 `ToolCancelled`
  闭合 provider 历史（`batch-runner.ts:100-135`）。
- Chiron：`_split_tool_batch` 按 `is_concurrency_safe`（默认 False）+ 同路径降级串行；并发组 `gather` +
  信号量；未执行调用写 `not_executed`（`runtime.py:820-871/3849-4000`）。

**差距**：Chiron 的并发**默认关闭**，实际大量串行；ZCode 的元数据（`sideEffectScope`/`destructive`/`concurrentSafe`）
更完整，且"取消后补齐结果"的语义更彻底。建议扩 `ToolDef` 元数据并提高只读工具默认并发（见 §4 P1）。

### 2.5 审批与权限

| 项 | ZCode | Chiron |
|---|---|---|
| 模式 | plan/build/edit/yolo/guarded/auto（自动模式未实现） | none/job/…（执行隔离）+ ask/auto/yolo（工具授权） |
| 审批链 | 静态模式/规则 → PreToolUse hook → memory 特例 → allow/deny → 工具 `prepareApproval` → UI broker → hook modify 重判（`permission-flow.ts:134-415`） | 解析 → `ToolGuard` 三态 → 服务端 Broker → guardian LLM → 人工/落库 grant（`tool_execution.py:147-293`、`runtime.py:5006/5135`） |
| 跨端/跨回合 | 会话级 ticket；UI broker | **落库 ticket + `/v1/agent/interactions` + 跨会话介入收件箱 + 答完自动续跑** |
| 评价 | ZCode 的 hook modify 后**重建请求**（防迟到响应批准新输入）值得抄 | Chiron 的"审批转交/跨副本/跨回合"更强 |

**建议**：只补 ZCode 的"hook 改写输入后重判 + 请求重建"细节；不引入其模式体系（与 Chiron 的 tools_mode 重叠）。

### 2.6 中断与 steer

- ZCode：
  - Stop 校验 `expectedForegroundExecutionId` 防竞态（`runtime-command-queue.ts:492-525`）；
  - 工具已进历史时**不立刻抛**，给每个调用补 `ToolCancelled` 再抛（`turn-tools.ts:132-135`），并写
    `[Request interrupted by user]` 标记，冷恢复可回放（`interrupt-marker.ts:11-36`）；
  - steer 只在"下一次 provider 请求前"取，且分 **guide**（注入当前 turn）与 **queue**（下一轮）两条通道，
    边界写死（`turn-loop.ts:84-98`、`turn-guide-drain.ts:14-71`）。
- Chiron：
  - interrupt/steer 都只在**轮次边界**消费；工具执行中由 `run_tool_with_cancellation` 与信号赛跑；
  - steer 按会话/按子 run 寻址，Redis + 本地队列、限流、来源落库（`steer.py`）；
  - 取消后未执行工具写 `not_executed`（与 ZCode 的 `ToolCancelled` 同向）。

**差距**：Chiron 缺 **guide vs queue 的双通道语义**（"这条补充是这一轮就用，还是下一轮再用"），也缺
"取消后向 provider 历史补齐每个未执行调用结果"的完整性（现在只写 `not_executed`，未见统一补 provider tool_result）。
建议补这两点，不需要引入命令队列框架。

### 2.7 子 Agent（Chiron 能力更强）

| 项 | ZCode | Chiron |
|---|---|---|
| 上下文 | **不 fork 父对话**，按 agentPrompt 重建 system；只继承执行事实（模型/权限/工具面/MCP） | `inherit_context` 有界脱敏 fork；模型/provider 继承父实际值 |
| 嵌套 | 深度硬编码 1（`subagents.enabled:false`），`maxTurns` 传了但不消费 | `max_depth` 默认 1，可配；子 run 挂起等人、续段 |
| 回传 | `<task-notification>` 唤醒父 Agent；`SendMessage` 可再 steer 子 Agent | 父→子 steer（`steer(run_id)`）、子→父报告、followup 唤醒父会话 |
| 隔离 | 子 Agent 与父同进程、同工具面（剔除 Plan 工具） | 只读工具面、写槽仲裁、独立 session/预算 |
| 评价 | 通知唤醒 + 可回注是体验亮点 | 能力面明显更宽（挂起/续段/嵌套/只读） |

**结论**：不要照搬 ZCode 的"子 Agent 不 fork 历史、深度 1"；Chiron 已有更强模型。可抄"后台完成用通知注入父
下一轮"的表达方式（Chiron followup 已有类似信号，可统一文案）。

### 2.8 压缩 / 召回 / 缓存

- ZCode：microcompact（清旧工具结果）→ auto compact（窗口 − 输出预留 − 13K buffer）→ reactive（每次超窗一次）；
  过快重复压缩熔断；压缩后**恢复已读文件**（5 文件/5K/50K）；cacheControl 只保留一个移动断点；prompt snapshot 固定 system 字节
  （`compact/policy.ts:6-93`、`compact-post-reminders.ts:28-32`、`provider-request-messages.ts:580-602`）。
- Chiron：三层阈值（70% snip / 85% prune）+ 摘要 + `result_store` 召回；prompt 缓存/上下文预算已有；未见
  "压缩后恢复已读文件"与"压缩熔断"。

**建议**：补"压缩后回读最近读过的文件 + 压缩过快熔断"，成本低、收益直接。

### 2.9 checkpoint / 回滚

- ZCode：文件变更 → checkpoint artifact + `CheckpointCreated`；工作区 checkpoint 列表/恢复 + 消息级 rewind + 会话 fork；
  可 fork 的成功边界用 `stableProductStartMessageId` 标记（`runtime/methods/tools.ts:174-256`、`session-fork.ts:163-350`）。
- Chiron：run 现场快照 + git 影子仓库双套，前端时间线预览/恢复/GC（`checkpoint*.py`、`CheckpointTimeline.vue`）。

**结论**：持平偏 Chiron（现场快照 + 挂起接手是 ZCode 没有的）；ZCode 的"消息级 rewind + 明确 fork 边界"更细，
可选择性借鉴。

### 2.10 Provider / 失败处理

- ZCode：AI SDK 全流；provider registry + resolver；创建期 fallback + 请求期 fallback（先结算流式工具再切模型）；
  断流从安全锚点恢复（≤10）；失败分类器 + 指数退避 + Retry-After + 空补全重试；请求准入（并发/冷却/RPM）端口。
- Chiron：Provider 目录 + gateway router；重试/熔断/预算；OpenCode 会话头兜底；响应式错误映射。
- **差距**：ZCode 的"请求期 fallback 前先结算已接受的流式工具"、"空补全重试边界"、"模型请求准入"更细；
  Chiron 可按需补，不是架构级差距。

### 2.11 执行隔离（Chiron 明显领先）

- ZCode：工具 handler 在宿主进程执行，子进程只做进程组/`taskkill`；唯一沙箱是 dynamic workflow 的
  `vm.createContext` + 子进程 + 禁 `Date.now/Math.random` + 256MB/墙钟；MCP 在 Windows 用 Job Object 收尸。
  **未找到容器/namespace 级工具沙箱**。
- Chiron：Windows 原生四档 + **Kiri 容器挡位**（namespaces/cgroup/caps/seccomp、无网络、只读根、只挂工作区，
  依赖缺失即拒跑）；sandbox-service 独立执行；启动门禁 fail-closed。

**结论**：这是 Chiron 的结构性优势，不应为"对齐 ZCode"而弱化。

## 3. 前端 UI/UX 逐项对照

### 3.1 应用外壳与多端

| 项 | ZCode | Chiron |
|---|---|---|
| 技术栈 | React 19 + Tailwind 4 + Radix/shadcn + Zustand + Lexical + Streamdown + pierre | Vue 3 + Pinia + Ant Design Vue + markdown-it/highlight.js |
| 三端共享 | 同一 `@zcode/ui` + 注入 `IPlatformService`（桌面/Web/分享页；手机远控） | 桌面 Vue 应用 + Web 分享页（`web-share` 类能力）；团队 Web 为同一前端 |
| 布局 | `WorkspaceShellLayout`：侧栏 + 会话 + 底部终端 + Side Pane，独立 frame + 4px 可拖间隙（`DESIGN.md:601`） | `ChatView`：工作台 / 参考栏 / 子 Agent 对照栏 / 主会话四栏 + 侧面板 overlay |
| 分屏 | 二叉 pane 树（≤4）但**入口已下线**，只保留渲染；Side Pane 完整视图为当前"停靠"形态 | 三栏并存 + 可拖宽度 + 换边；无通用 pane 树 |
| 多窗口 | 桌面多 workspace 窗口；Web 远控手机单列 | 单窗口 + 会话地图 |
| 评价 | ZCode 更"IDE 化"；Chiron 更"对话 + 对照" | 追平需先做状态抽离，否则加栏会继续堆进 ChatView |

### 3.2 消息与工具呈现

- **ZCode**：
  - 行模型 + turn 分组，**以"轮"为虚拟化单位**（避免 retry/fork 被拆散）；用户消息唯一气泡、纯文本不渲染 markdown；
  - 助手 markdown 走 Streamdown（CJK/代码/数学/Mermaid）；代码块 shiki + 自动换行 + 复制；图片画廊；
  - 工具卡 **注册表分流（约 40 个专用 renderer）** + 统一摘要 + **闭合不建详情 DOM**（`ToolLayout.tsx:121`）；
  - diff 用 pierre（unified/word-alt/bars），聊天内联轻量 diff + "查看 Diff"兜底；
  - 思考块独立组件，`shouldAutoCollapseReasoning` 策略。
- **Chiron**：
  - `MessageItem`（markdown-it + highlight.js + mermaid 沙箱 + 复制/编辑/重试）；
  - `ToolCallCard` 摘要 + 点路径跳工作台；`ToolResultBlock` 分型（diff/read/terminal/search/todos/json/text）；
  - 自研 `diffParse.ts` 行级着色 + 增删统计；`ReasoningBlock` 流式状态。
- **差距**：Chiron 的分型与摘要已接近；差在**renderer 注册表的可扩展性**（新增工具不必改大组件）、**惰性详情**
  与**统一摘要语法**。可渐进改，不必换组件库。

### 3.3 流式渲染（ZCode 最大亮点）

- ZCode 的三代教训写在 `docs/streaming-markdown-reveal.md:1-30`：逐字 `visibleText` 会让高度逐帧增长、撞虚拟滚动测高。
- 现方案：**DOM 文本第一帧就是全文**，只给每个词/字写 `data-zcode-reveal` + `--zcode-reveal-delay`，用 CSS
  `transition + @starting-style` 淡入；`Intl.Segmenter` 词粒度、CJK 再拆字；只对"纯追加且单帧 ≤600 字符"生效，
  否则整条不动画（`:36-170`、`:250-268`）。
- Chiron：逐 chunk 追加 + `ReasoningBlock` 流式状态；长对话靠虚拟列表，但**文本高度仍在变**，滚动跟随容易抖。
- **建议**：这是投入产出比最高的一项。Vue 侧用自定义指令/渲染后处理实现同样的"全文 + 延迟淡入"，不改数据结构。

### 3.4 会话 / 项目 / 未读 / 分享

- ZCode：侧栏 workspace → task 二级；四种视图（项目/分组/最近/未读）；dnd 分组；未读以 tasks-index 为唯一真相源；
  归档二次确认；分享以 `productTurnId` 为身份、默认 `public_importable`、手机/普通 Web 不得发起。
- Chiron：会话 + 项目归属 + 会话地图（分支/fork/合并可视化）；分享；未读/通知较弱（未见同粒度未读真相源）。
- **差距**：Chiron 的"会话地图"是 ZCode 没有的强项；差在**未读状态单一真相源**与**分组/视图切换**。

### 3.5 输入 / 命令 / 快捷键

- ZCode：Lexical 富文本（`/`、`@`、IME 229、长粘贴转附件）；composer 工具条（模型/provider/模式/思考等级/上下文用量）；
  Command Center（`Cmd+K`，Recent + 分组命令 + All/Actions/Tasks/Files）；**快捷键单一事实源**
  `shortcutCommands.ts`（channel: window/menu、scope: global/composer、平台主修饰键隔离、冲突检测、录制归一化）。
- Chiron：`ChatInput`（`/`、`@`、模型/模式/effort、拖拽上传、IME、outbox、steer）；`CommandPalette`；`useKeyboardShortcuts`
  + 冲突检测（已接近）。
- **差距**：Chiron 的输入能力已很全；差在"快捷键定义跨端/跨菜单单一事实源"与命令中心的信息架构（Recent/范围 tab）。

### 3.6 设置 / 主题 / i18n / a11y

| 项 | ZCode | Chiron |
|---|---|---|
| 设置 | 单页分区（23 区，含 plugins/mcp/skill/automations/hooks/browser/CUA/remote） | 多子面板（Isolation/RunMode/Harness/EngineEnv/Shortcut/2FA/Version/About）+ 后台 Admin |
| 主题 | light/dark/zai-light/zai-dark/system，4 主题 + 系统色同步 | 5 组主题 × 明暗 + token 契约棘轮 |
| 语言 | 5 种（en/ja/ko/ru/zh），**无 RTL** | 3 种（zh/en/ar）+ **RTL**（逻辑属性 + 键盘镜像） |
| a11y | 规范 + 手工 ARIA，**无 axe 自动化** | 静态契约 + ARIA，**无 axe 自动化** |
| 设计系统 | `DESIGN.md` 53KB；**字号只允许 `text-ui-*`，禁止任意字号**；间距/圆角/动效/响应式写死 | `style.css` 2560 行 token + `check:ui` 十项契约（token/i18n/a11y 静态/RTL） |
| 评价 | 规则更"硬"、字号单一来源；Chiron 的 RTL 与多主题更强 | 可互补 |

**建议**：把 ZCode 的"字号派生 + 禁止任意字号"补进 `check:ui`（Chiron 现在是检查硬编码颜色/z-index，未检查字号）。

### 3.7 工作台 / IDE

- ZCode：Side Pane 23 种 tab（browser/git/file-manager/code-viewer/terminal/browser-use/subagent-*/workflow-*/plan-detail…），
  文件树（含 watcher/搜索索引/拖拽）、xterm 终端（PTY 模块级保活）、Office/PDF/PPTX 预览、嵌入浏览器（CDP）、
  Git 面板 + 图形、插件面板。
- Chiron：`WorkbenchPanel`（项目文件树 + 只读预览 + agent 写入路径高亮 + checkpoint 时间线）、参考栏/子 Agent 对照栏；
  无内嵌终端/浏览器/Git 图（Git 走工具）。
- **差距**：Chiron 的工作台是"看 agent 改了什么"，ZCode 是"在应用里干活"。追平工作台是产品级投入，优先级低于
  状态抽离与流式。

### 3.8 前端测试与可观测性

- ZCode：**退役 App 业务单测**，保留 Agent CLI/CUA/E2E/工程测试；816 个桌面 E2E（WDIO + Electron）；
  组件测试用"真 React + 真浏览器 + 确定性 fixture"（`node:test` + Playwright）断言 DOM 几何；
  **testid 单一来源 + 禁止截图判定**（`docs/conversation-session-ui-testid-contract.md`）；无视觉快照、无 axe。
- Chiron：213 个 `*.spec.ts` 组件/交互测试（vitest + jsdom），无体系化 Electron E2E；a11y 为静态契约。
- **评价**：Chiron 的单测密度是资产，不应照搬"退役单测"；但应补 **桌面 E2E + testid 契约**，否则四栏/分屏/
  流式这类"几何与滚动"问题只能靠手测。

## 4. 建议追平的顺序（Top 10）

### P0（先做，收益/成本比最高）

1. **前端状态抽离**：把 `ChatView.vue` 的会话/流式/审批/分屏状态抽到 `stores/chat.ts` + composables。
   - 判据：`ChatView.vue` < 1500 行；状态可在不挂载视图的情况下单测；新增一栏/一面不改 ChatView。
   - 落点：`frontend-vue/src/stores/`、`components/chat/use*`。
2. **流式"文本完整、只动透明度"**：渲染后处理给词/字打 `data-reveal` + 延迟，CSS 淡入；只对纯追加生效。
   - 判据：流式期间行高稳定（ResizeObserver 不触发连续重算）；复制/搜索拿到全文；`prefers-reduced-motion` 生效。
   - 落点：`MessageItem.vue` / 新的 markdown 渲染 composable；参考 ZCode `docs/streaming-markdown-reveal.md`。
3. **工具卡注册表 + 惰性详情 + 统一摘要**：按工具名分流 renderer；闭合不建详情 DOM；摘要语法集中一处。
   - 判据：新增工具只加一个 renderer 文件；收起时 DOM 不含详情；同一工具在聊天/工作台/子 Agent 面板渲染一致。
   - 落点：`components/chat/ToolCallCard.vue`、`ToolResultBlock.vue`。
4. **testid 契约 + 桌面 E2E 骨架**：testid 常量单一来源；先覆盖"发送→流式→工具卡→停止→恢复"与四栏布局。
   - 判据：E2E 只认 `data-testid`/`data-state`，不截图判定；CI 可跑。

### P1（结构改进）

5. **工具调度元数据化**：`ToolDef` 补 `side_effect_scope` / `concurrent_safe` / `destructive`；只读默认并行；
   保留同路径串行与信号量。
   - 判据：多工具批次的并行/串行顺序测试；取消时每个未执行调用都有结构化结果。
6. **取消/中断补齐 provider 历史**：停止时给未执行调用写 `tool_result`（`is_error` + 取消原因），再追加
   `[用户中断]` 标记；冷恢复可回放。
7. **guide vs queue 双通道**：在 steer 上区分"当前 turn 下一次请求前注入"与"下一轮队列"，边界写进接口与事件。
   - 判据：工具批次后/文本收口后两条注入测试；Stop hook/断流重试不重复注入。
8. **压缩后回读 + 压缩熔断**：压缩后把最近读过的文件（有界）重新注入；过快重复压缩直接报明确错误。

### P2（体验/工程）

9. **快捷键单一事实源 + 命令中心信息架构**：定义 `channel/scope/defaultBindings`，跨桌面菜单与渲染层共用；
   命令中心补 Recent/All/Actions/Tasks/Files 范围。
10. **设计系统补硬约束**：`check:ui` 增加"字号只允许 `text-ui-*`/语义 token"与"任意字号即失败"。
11. **a11y 自动化**：引入 axe（组件测试或 E2E 之一），先覆盖聊天区、工具卡、审批弹窗、设置页。
12. **工作台扩展（产品级）**：内嵌终端 → Git 面板 → 浏览器预览，按用户价值排序，不一次性追 ZCode 的 23 tab。

### 4.1 落地状态（2026-11-26）

| 项 | 状态 | 证据 / 说明 |
|---|---|---|
| P0-2 流式浮现 | ✅ 已合入 `2ee68f5` | 只给本批新增字符建 span（prefix 纯文本），取消 2000 字总量上限；动画改纯 opacity；5+1 项测试 |
| P0-1 状态抽离（三刀） | ✅ 已合入 `05c9011`/`4b8cca3`/`931b1d0` | 身份与项目归属 → `chatSession` store；多会话运行时 → `useChatRunState`；增量文本投影 → `useChatStreamText`。ChatView 4403 → **4143 行**，新增 15 项脱离视图的单测 |
| P0-1 收尾（onSSEMessage 分发） | ⏳ 未做 | 它依赖审批/路由/工作台十余个局部状态；剩余约 250 行，需要先定义事件投影上下文。**这是下一步唯一的大块** |
| P0-3 工具卡注册表/惰性详情/统一摘要 | ✅ 基本已具备 | 摘要单一来源 `utils/toolFamily.ts`；`ToolCallCard` 用 `v-if` 惰性详情；`ToolResultBlock` 已按类型分流。剩余只是"按工具名注册 renderer"的扩展性优化 |
| P0-4 testid + 桌面 E2E | ⏸ 按决定不做 | 仓库无 Playwright/WDIO；上 E2E 需要先定工具链 |
| P1-1 工具调度元数据 | ✅ 已合入 `eda07be` | `ToolDef.destructive` + `is_parallel_safe`；破坏性动作一票否决并发；2 项测试 |
| P1-2 取消补齐 provider 历史 | ✅ 已具备（复核） | 挂起路径写 `not_executed`、取消路径返回 cancelled 结果；`test_interaction_suspend.py` / `test_cancel_propagation.py` 已覆盖 tool_calls 与 tool 消息配对 |
| P1-3 guide vs queue | ✅ guide 语义已具备；queue 不加 | Chiron 的 steer 在每个模型步开头 drain（即 ZCode 的 guide）；`queue`（无活跃回合时入队等下一轮）与当前"没有活跃 run 即拒绝"的 fail-closed 准入冲突，需产品决策，不擅自放宽 |
| P1-4 压缩回读 + 熔断 | ✅ 已合入 `eda07be` | 3 次过快压缩暂停自动压缩（手动 /compact 重置）；最近 5 个 `read_file` 结果压缩后以 `[recent-files]` 回读；6 项测试 |
| P2 字号硬约束 | ⏸ 暂缓 | 现有 `font-size` 声明 995 处、裸 px 666 处；现在加棘轮基线会是 600+ 条噪音，建议先按文件分批迁移再上闸 |
| P2 快捷键单一事实源 / a11y 自动化 / 工作台扩展 | ⏳ 未做 | 分别属于"统一注册表改造""引入 axe 依赖决策""产品级投入"，排在 P0-1 收尾之后 |

**下一步**：P0-1 收尾（事件投影上下文 + 分发抽离）是唯一剩下的结构性大块；P2 三项需要先做工具链/产品决策。

## 5. 不建议照搬

1. **多进程/多端 V4 协议栈**：ZCode 为桌面/Web/手机远控/远程 Source Server 服务；Chiron 的团队形态是"中心服务 +
   客户端"，照搬会带来协议版本化与跨端一致性的巨大成本。只借"事件即事实 + 投影读数"。
2. **主循环无轮次/预算硬停**：团队计费/多租户不可接受；Chiron 的五轴预算 + 线账是优势。
3. **子 Agent 不 fork 历史、深度硬编码 1**：Chiron 的 fork/嵌套/挂起/只读面更强，不要降级。
4. **退役 App 单测**：ZCode 用 816 E2E 替代，Chiron 的 213 组件测试是资产；应"加 E2E"，不是"删单测"。
5. **容器/沙箱**：ZCode 只有 workflow vm 沙箱；Chiron 的 Kiri 容器挡位与 Windows 原生档位更强。
6. **React 生态细节**：Streamdown rehype 管线/`startTransition` 提交时序/Radix Presence 测量/Lexical 自定义节点/
   `memo` 稳定引用，都不能直接迁到 Vue；要按"效果"重写，而不是"代码"移植。
7. **`architecture-policy.yaml` 式全仓门禁**：ZCode 自己承认 `ui/web/desktop` 三大包 `managed:false`，零违规不等于
   全量合规；Chiron 用 `check:ui` + 小步契约更合适，别再造一套 2600 行策略。

## 6. Chiron 应保留的独特优势

1. 多租户 + 团队账号闭环（邀请/末位 owner/强制下线/计费导出）。
2. 审批转交/跨会话介入收件箱 + 挂起等人工 + 答完自动续跑。
3. 五轴预算 + 线账 + 续段 + takeover/watchdog。
4. 两套 checkpoint（run 现场快照 + git 影子仓库）+ 会话地图（分支/fork/合并可视化）。
5. 知识库/RAG 全链路（上传→索引→引用→检索事件）+ PageIndex worker 心跳可见。
6. Windows 原生隔离四档 + **Kiri 容器挡位（依赖按需下载、失败即拒）** + sandbox-service。
7. 三语含阿语 RTL + 5 组主题 + `check:ui` 十项契约 + 桌面本地栈（内嵌 PG/Redis）。

## 7. 证据索引（关键文件）

ZCode（仓库外 `../zcode`，commit `a4b44451`）：

- 核心循环：`apps/zcode-cli/packages/core/src/runtime/methods/{turn,turn-loop,turn-model-step,turn-tools,tools,steering,turn-guide-drain}.ts`
- 工具：`core/src/tool/{scheduler.ts,executor/{permission-flow,call-runner,batch-runner,tool-result-settlement}.ts}`
- 子 Agent：`core/src/runtime/methods/subagent.ts`、`core/src/subagent/runner.ts`、`send-message.ts`
- 压缩：`core/src/compact/{microcompact,policy,prompt}.ts`、`runtime/helpers/compact-post-reminders.ts`
- 隔离：`packages/dynamic-workflow-runtime/src/{harness,child-source}.ts`、`adapters/src/mcp/windows-job-object.ts`
- 前端流式：`packages/ui/src/components/ai-elements/message.tsx`、`docs/streaming-markdown-reveal.md`
- 布局/工具卡：`packages/ui/src/app-shell/WorkspaceShellLayout.tsx`、`packages/ui/src/ToolCallBlocks/*`
- 设计系统：`DESIGN.md`、`packages/ui/src/styles.css`
- 测试契约：`docs/conversation-session-ui-testid-contract.md`、`packages/desktop/test/e2e/*`

Chiron：

- 核心：`python-engine/app/agent/runtime.py`、`tool_execution.py`、`tool_policy.py`、`subagent_runner.py`、`steer.py`、
  `compaction.py`、`checkpoint*.py`、`task_budget.py`
- 隔离：`python-engine/app/isolation/{__init__,container,kiri}.py`、`tools/sandbox.py`
- 前端：`frontend-vue/src/views/ChatView.vue`、`components/chat/*`、`components/settings/IsolationPanel.vue`、
  `stores/*`、`style.css`
- 工程：`Makefile` 的 `check`/`check-ui`、`scripts/check_tool_policy_parity.py`、`docs/prd-remote-team.md`
