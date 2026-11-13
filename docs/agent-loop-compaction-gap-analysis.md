# 核心循环与会话压缩：chiron / deepagents / Reasonix 差距分析（第二轮）

参照物：
- `vendor/deepagents`（langchain-ai/deepagents v0.7.35，MIT）
- `vendor/DeepSeek-Reasonix`（Go，`internal/runtime/agent/compact.go` 等）

只列**能落到 chiron 代码上的**差距，每条给对位实现的位置。

---

## 一、核心循环：结构差异（不是差距，是约束）

| | chiron | deepagents | Reasonix |
| --- | --- | --- | --- |
| 循环形态 | 单文件 `runtime.run()` 里的大循环，压缩块内联 | LangGraph 图 + 中间件链，`wrap_model_call` 逐层包裹 | `Agent` 结构体方法 + `contextWindow` |
| 压缩挂点 | 请求前 + 工具批末 + `on_overflow` | `_DeepAgentsSummarizationMiddleware.wrap_model_call` | `planCompaction` 在投影层 |
| 状态存放 | `messages` 局部变量 + checkpoint | `AgentState.messages` + `_summarization_event` | `contextWindow` + projection |

结论：**结构不同不构成差距**。chiron 的"每轮重估"等价于 `wrap_model_call`，已确认过。
真正的差距全在**压缩判定与保留策略的细节**上。

---

## 二、仍存在的差距（按影响排序）

### D1. 重试判据只判"变小"，没判"仍超预算"

- deepagents `_check_reduction`（`summarization.py:1559`）：
  ```python
  if error is not None and count >= _count_tokens(original...):
      self._raise_recovery_exhausted(error)      # 绝不重发未变化的被拒请求
  if budget is not None and count > budget:
      raise ContextOverflowError(...)            # 绝不重发已知超标的请求
  ```
- chiron `_force_compact` 只保证"token 确实变小了"，变小之后**仍可能超预算**，于是再发一次请求、再溢出一次。
- 影响：每个溢出回合多一次注定失败的往返（延迟 + 计费）。
- 落点：`runtime._force_compact` 之后加"仍超 `_input_budget` → 直接放弃并报错"，不再重试。

### D2. 尾部保留没有 "trigger/2" 天花板 —— 折叠可能永远无事可做

- Reasonix `recentTailBudget`（`compact.go:143`）三层夹：
  ```go
  n = clamp(window×10%, 32K, 96K)
  if n > window/2      { n = window/2 }
  if n > trigger/2     { n = trigger/2 }   // ← chiron 缺这条
  ```
  注释直接点明原因：*"A tail reaching the trigger leaves nothing to fold: every message is
  still held verbatim when the threshold arrives, so maintenance folds nothing for the
  rest of the session."*
- chiron 只有 `window // 2`（`KEEP_RECENT_RATIO` 那一组），**缺 trigger/2**。
- 后果：窗口大而触发线低时，尾部预算可能 ≥ 触发线，压缩反复触发却压不掉任何东西 ——
  表现就是用户说的"频繁压缩但上下文没变"。
- 落点：`compaction._compact_with_notice` 里算 tail budget 时补 `min(budget, trigger//2)`。

### D3. 切点未按"本轮起点"对齐 —— 会把正在做的事折进去

- Reasonix `activeTurnStart`（`compact.go:264`）用 `activeTurnCreatedAt` 时间戳定位本轮起始的用户消息，
  `keepIndexes` 里硬性 `keep[i] = true`：*"The request that began the running turn states the
  work the rest of the turn is executing. Its closed transactions may fold; it may not."*
- chiron 的 `_prune_messages` 只按"最近 N 条 / 最近 token"切，**不知道哪条是本轮起点**。
  长回合（几十次工具调用）里，本轮的任务描述本身可能被折进摘要 —— 模型随即"忘了自己在干什么"。
- 落点：`_prune_messages` 增加 `turn_start_seq` 参数，切点不得越过它。

### D4. 折叠边界没有"工具调用必须闭合"的约束

- Reasonix `closedPrefixEnd`（`compact.go:318`）：折叠只能在**所有 tool call 都有结果**的点结束。
  注释：*"anywhere else hands the provider an assistant turn whose calls never resolve"*。
- chiron 是**事后**修：`_ensure_valid_tool_sequence` 在组消息时删孤立 tool / 从
  `assistant.tool_calls` 里摘掉未配对的 id。
- 差别：事后修能发出请求，但**被摘掉的调用在模型眼里凭空消失**（它调了工具，却看不到结果，
  也没看到"这次没执行"的解释）。事前对齐则根本不会产生这种残骸。
- 落点：`_prune_messages` 选起点时，若 `processed[start]` 处于未闭合的工具组内，向前退到闭合点。

### D5. 折叠摘要本身没有"可再折叠"与"摘要链合并"的语义

- Reasonix `isCompactionSummary` + `pinnedPrefixLen`（`compact.go:278-296`）：
  - 摘要打 `<summary>` 前缀（`role=user`，与 chiron 一致）；
  - **摘要永不 pin**：*"any digest in the transcript enters the fold region and is merged
    into the next one, so a session cannot accumulate a chain of them."*
- chiron 的 `session_folds` 是区间记录，多次折叠会累积多条 fold；
  `_apply_folds` 投影时按区间替换 —— 但**旧 fold 的 digest 是否会被新 fold 覆盖**取决于区间是否重叠。
  若新折叠区间从旧 digest 之后开始，就会留下"摘要套摘要"，且旧的永不消失。
- 落点：`_merge_progress` 已有跨 fold 合并的思路，digest 也应同构 —— 新 fold 的源区间从
  上一条 digest 的消息位置起算，或在 `_apply_folds` 时丢弃被完全覆盖的旧 fold。

### D6. 手动压缩没有准入门槛

- deepagents `_is_eligible_for_compaction`（`summarization.py:2340`）：手动压缩要求
  对话已到**自动触发线的 50%**，否则直接返回 "nothing to compact"。
- chiron 上一阶段实现的 `/compact` 与 `compact_conversation` 是**无条件压**
  （`force=True` 把阈值压到 0 并把 `max_messages` 砍半）。
- 后果：短对话里模型调一次 `compact_conversation` 就会**无谓丢掉一半历史**，还多烧一次摘要调用。
  模型很容易在"阶段边界"这个模糊判断上过早调用。
- 落点：`_compact_with_notice(force=True)` 之前加门槛判定；不达标如实返回"没可压的"。

### D7. 折叠摘要没有经济性门槛，且多条 fold 里只有最新一条被升级

- Reasonix `foldEconomics`（`compact.go:199`）：`region < 100 tokens` 就不值得调摘要
  （省下的量抵不过一次 LLM 往返的代价与延迟）。
- chiron `_upgrade_fold_digests`（`runtime.py:1275`）的实际行为是**只升级 `folds[-1]`**，
  其余直接 `pop("_source")` 保留提取式 —— 注释给的理由是"串行做多条摘要会累加首 token 延迟"，
  这个取舍本身合理。但它带来一个未被处理的后果：
  **较早的 fold 永久停留在提取式，而它们同样顶替原文参与此后每一次上下文重建**。
  overflow 连发时（长会话常见），模型看到的最新摘要是好的、更早的却是"每条取首行"。
- 另一处缺口：升级前不看 fold 大小。几十 token 的区间也发一次摘要调用，纯亏。
- 落点：升级前用 `_estimate_tokens(source)` 过滤过小的 fold；较早的 fold 可考虑在
  下一次折叠时并入新 fold（与 D5 合并解决）。

### D8. 剪点用 seq 坐标，没有消息 id 兜底

- deepagents 同时记 `cutoff_index` 与 `cutoff_message_id`，
  `_resolve_cutoff_index` 优先按 id 找回（*"stays correct when earlier messages are removed"*）。
- chiron 的 fold 用 `[covered_from, covered_to)` 的 `seq` 区间。
  一旦原文被清理/重编号，区间会**整体漂移** —— 指向错的段落，且没有自检。
- 落点：fold 记录同时存 cut 点消息 id，应用投影时优先按 id 定位。

### D9. 前缀缓存诊断已有，但没有和"压缩"建立因果

- Reasonix 有 `cache_diagnostics.go` / `cache_shape.go`：把"这一轮前缀是否与上一轮一致"
  显式诊断出来（压缩、系统段变动、模型切换都会打破前缀，导致整段重算）。
- chiron 的 runtime 已有 `_last_prefix_fp: tuple[str, str]`（system 段 + 工具表两个哈希）
  并**已经会返回诊断** `{"prefix_changed": [...], "first_turn": bool}`（`runtime.py:1039`）。
  所以这条**不是"只存不诊断"**，比我先前判断的要好。
- 剩下的真缺口是两条：
  1. 指纹只覆盖 system 段与工具表，**不含模型名**——切模型同样导致前缀全废，这里看不见；
  2. 压缩**必然**改写消息区（摘要在最前），但诊断不会把"这次变慢是刚压过缩"关联起来，
     用户看到的仍是一次没有解释的延迟。
- 落点：指纹加上模型名；压缩发生时在诊断里带一个 `cause="compaction"` 标注。

---

## 三、chiron 已经对齐的（不要重做）

| 差距 | 对位实现 | chiron 现状 |
| --- | --- | --- |
| 输入预算 = 0.95×窗口 − 输出额度 | deepagents `_input_budget` | ✅ `compaction._input_budget` |
| 工具表计入压缩分子 | deepagents `_over_budget`（messages+system+tools） | ✅ `CompactionConfig.tools_tokens` |
| 重试必须严格变小 | deepagents `_check_reduction` | ✅ 按 token 判（`_force_compact`） |
| 压缩摘要由模型生成 | deepagents `_acreate_summary` | ✅ `runtime._upgrade_fold_digests` + 形状校验 |
| 摘要需锚定技术细节 | — | ✅ `_validate_digest` 四道校验（chiron 更严） |
| 模型可主动压缩的工具 | deepagents `SummarizationToolMiddleware` | ✅ `compact_conversation` |
| 每轮重估压缩 | deepagents `wrap_model_call` | ✅ 回合头 + 工具批末 |
| 保留决策用固定估算 | Reasonix `fixedTokenEstimate` | ✅ `_fixed_tokens` |
| 「已完成工作」投影 | Reasonix `compact_progress.go` | ✅ `_fold_progress` / `_merge_progress` |

---

## 四、状态：九条已全部修复

| 差距 | 落点 | 关键取舍 |
| --- | --- | --- |
| D1 | `runtime._chat_stream_with_retry` | 压缩前后按 token 比，没变小就抛错；**事实先记进 `overflow_flags` 再抛**，前端仍能看到"试过但没救回来" |
| D2 | `compaction._recent_tail_start` | 尾部预算再夹一道 `trigger // 2` |
| D3 | `compaction._prune_messages` | 切点回退到本轮请求之前；`turn_start == 0`（整个区间都是本轮）时不生效，否则等于禁止压缩 |
| D4 | `compaction._prune_messages` | 切点前移到整组工具调用之前 |
| D5 | `compaction._apply_folds` | 摘要链总长上限 `DIGEST_CHAIN_MAX_CHARS`，从新往旧装，最新一条完整 |
| D6 | `runtime._compact_with_notice` | 低于自动触发线 50% 直接返回"没有可压缩的内容" |
| D7 | `runtime._upgrade_fold_digests` | 区间 < `DIGEST_MIN_FOLD_TOKENS` 不发摘要调用 |
| D8 | `compaction._relabel_by_ids` | 缺 `seq` 时用 `covered_ids` 重建坐标 |
| D9 | `runtime._prefix_fingerprint` | 指纹加入模型名，诊断可报 `prefix_changed: ["model"]` |

顺带修掉的两个真实缺陷（D1 验证时暴露）：

- `_force_compact` 的第二刀原本是 `max_messages // 2`（相对**配置值** 200），实际条数
  只有几十条时这一刀什么都没砍 —— 救援机会被白白用掉。改成相对**当前条数**减半。
- 溢出事件的 `messages_after == messages_before` 时仍然重发，已由 D1 拦下。

回归测试见 `tests/test_compaction_gap_fixes.py`（14 条，每条对应一个真实失败模式）。

