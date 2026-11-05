# code agent 模式与 xerj 接入方案

> 状态：**设计稿，未实施**。本文不含任何配置改动。
> 依据：本机实测（xerj v1.0.0-rc.100 MCP 握手、`autoindex map --dry-run`、真实检索）
> 与源码逐条核对（行号均指当前工作区）。

---

## 一、结论摘要

| 问题 | 结论 |
| --- | --- |
| xerj 能否当 agent？ | **不能**。22 个 MCP 工具里没有一个能写文件、改代码或执行命令。它是搜索引擎，不是执行体。 |
| xerj 能否当编码插件？ | **能，但现在不起作用**。索引指向了临时目录，对本仓库检索返回 `no match`。 |
| 需要新增什么？ | ①「项目」概念（当前不存在）②索引生命周期挂在项目上 ③`code` 运行模式开关 |
| 最大的阻碍是什么？ | **不是 xerj，是 `safe_join()`**。agent 的文件操作被锁死在 `chiron-sandbox/<tenant>/<user>/workspace`，碰不到用户的真实仓库。 |

---

## 二、xerj 能力边界（实测）

### 2.1 22 个工具的实测分类

向 `xerj mcp` 发 `initialize` + `tools/list` 拿到原始 schema：

| 类别 | 工具 | 能否支撑编码 |
| --- | --- | --- |
| 定位 / 检索 | `xerj_def`、`xerj_search`、`xerj_semantic_search`、`xerj_vector_search`、`xerj_hybrid_search`、`xerj_batch_search`、`xerj_code_search`、`xerj_map` | **只读，真实增益** |
| 远端语料 | `xerj_hub_find`、`xerj_hub_def`、`xerj_hub_ask` | 只读，查「别的项目怎么定义这个 API」 |
| 索引运维 | `xerj_index_budget`、`xerj_autoindex_remove`、`xerj_plan` | 运维向 |
| 记忆 / 图谱 | `xerj_memory_store`、`xerj_memory_recall`、`xerj_brain_ego`、`xerj_brain_link`、`xerj_brain_unlink`、`xerj_brain_overview` | 与 chiron 自有记忆**职责重叠** |
| 出站写 | `xerj_field_report`、`xerj_corpus_feedback` | 已由 `deny_tools` 拦掉，正确 |

**缺失的必需项**：读文件、写文件、编辑、执行命令、跑测试、Git 操作 —— 一个都没有。
这些 chiron 已经全套具备（`read_file`/`write_file`/`edit_file`/`shell_exec`/`run_code`/`git_*`/`grep_files`）。

> `xerj_plan` 名字有误导性：它是「自然语言 → 合法 ES query DSL」，**不是任务规划**。
> 不要指望它承担 agent 的 plan 环节。

### 2.2 当前对本项目零贡献（实测）

```
$ xerj autoindex map python-engine/app --dry-run
error: --prefix ax already indexes a different folder.
  already indexed from: ...\Temp\reasonix-session-tmp-...\xerj-corpus

$ xerj search --prefix ax "isolation level"
no match in ax-* for: isolation level
```

`ax-docs` 只有 6 条文档，且来源是临时目录。**在修好索引之前，xerj 不如 grep。**

### 2.3 两个必须知道的坑

**(a) `data/` 在仓库内 —— 索引仓库会把它自己索引进去**

xerj 文档明说：`Keep it outside any folder you will autoindex`，因为 `data/` 不在内置忽略清单里。
当前数据目录是 `D:\desket\gh\JulyZ0\chiron-desktop\data\xerj`，**位于仓库根下**。
若要索引整个仓库，数据目录必须先挪到仓库外（例如 `%LOCALAPPDATA%\chiron-xerj`）。

**(b) 所有 22 个工具的 `annotations` 都是空 `{}`**

撞上我们已实现的 fail-closed 判定：

- `call_tool` **不会拒**（走「没说就放行 + 标注」分支，`mcp/client.py:490-502`）
- `annotations_for()` 会把 22 个**纯只读检索**工具全标成 `destructive: true`（`mcp/client.py:525-529`）

后果：上游确认卡片/审计若依赖该标记，会把检索当成危险操作。
**这是配置隐患，不是阻断性问题** —— 但接入前应在 `builtin.py` 的插件定义里补一层「已知只读」声明，而不是靠 annotations 猜。

### 2.4 不建议接入的部分

`xerj_memory_*` 与 `xerj_brain_*`：chiron 已有 `remember`/`recall`/`memory_search`/`forget`（`app/tools/memory.py`）。
两套记忆并存会分裂上下文，且 `xerj_brain_link` 是**写操作**（`Proxies POST /_graph/{brain}/link`），
与「只读插件」定位冲突。**继续 `deny_tools`。**

---

## 三、xerj 接入方案：项目作用域索引

按你的要求：**xerj 不默认建索引，只在 chiron 里创建项目后，才对目标代码库建索引。**

### 3.1 「项目」概念需要新建

现状核查：

- 迁移 `0001_authoritative_baseline.py` 中**没有** project / projects 表
- `workbench_context.project` 只是一个字符串，仅用于记忆隔离（`runtime.py:1471` 注释：「工具侧记住/召回/遗忘按它隔离记忆」）
- `session_map_workspaces` 表存的是**前端画布视口**（`viewport jsonb`），不是代码工程

所以必须先定义「项目」，否则「创建项目后建索引」没有挂载点。

**建议最小形态**（不为 SaaS 多租户过度设计）：

```
projects
  id            varchar(64)  PK
  tenant_id     smallint
  user_id       varchar(36)
  name          varchar(255)
  root_path     text           -- 绝对路径，指向用户真实仓库
  xerj_prefix   varchar(64)    -- 一项目一前缀，与 root_path 一一对应
  index_state   varchar(32)    -- none|queued|indexing|ready|stale|failed
  indexed_at    timestamptz
  created_at / updated_at
  UNIQUE (tenant_id, user_id, name)
```

`xerj_prefix` 必须唯一且稳定：xerj 的 catalog 以 `ds:<prefix>:<slug>` 为键，同一 prefix 指向不同目录会被拒绝（已实测）。

### 3.2 索引生命周期

| 时机 | 动作 | 实现位置 |
| --- | --- | --- |
| 创建项目 | 投递 `autoindex` 任务，`index_state=queued` | 项目创建 handler |
| 索引执行 | `xerj autoindex <root_path> --prefix <xerj_prefix>` | 后台任务（复用 `queue/` 的 worker 骨架） |
| 进度查询 | `xerj autoindex status` | 项目详情页轮询 |
| 删除项目 | `xerj autoindex remove --prefix <p> --yes` | 项目删除 handler |
| 过期重建 | `xerj code` 30 天后拒绝（`--stale-ok` 可绕） | 定时任务读 `indexed_at` |

三条硬性约束：

1. **一项目一前缀**。xerj 拒绝同一 prefix 指向不同目录；共享前缀会丢仓库级 provenance。
2. **索引不是即时的**。实测本机 32 核，`autoindex` 是全量 hash + 嵌入，属后台任务，不能放在请求路径里。
3. **数据目录必须在仓库外**（见 2.3a），否则索引会把自己吃进去。

### 3.3 插件侧保持只读

保留现有 `builtin.py` 的 `read_only=True` 与 `deny_tools`，只补一条显式的「已知只读工具白名单」，
用于修正 2.3b 的 `annotations` 误判。**不放宽任何出站工具。**

---

## 四、code agent 模式开关

### 4.1 现有机制足够，不需要新造

`app/agent/modes.py` 已是声明式配置表，`ModeConfig` 的字段**正好覆盖需求**：

```python
@dataclass(frozen=True)
class ModeConfig:
    mode: AgentMode
    persona: str | None
    include_context: bool          # 是否注入记忆/skills/RAG/git 上下文
    include_tools: frozenset[str]  # 模式可见工具
    extra_tools: frozenset[str]
    enable_compaction: bool
    compaction: dict | None
```

新增模式只需在 `_BASE_MODES` 加一条（`modes.py:88-97`），并在 `AgentMode` 加枚举值。
`get_mode_config()` 对未知模式**回退 NORMAL**（`modes.py:127-137`）—— 所以前端旧版本传 `normal` 不会炸。

### 4.2 建议的 `code` 模式定义

```python
AgentMode.CODE = "code"

CODE_TOOL_NAMES = frozenset({
    # 文件（读-改-删完整闭环）
    "read_file", "write_file", "edit_file", "delete_file", "ls",
    # 检索（内容是 grep，路径是 glob）
    "grep_files", "glob_files",
    # 执行
    "shell_exec", "run_code", "persistent_shell",
    # 版本控制（5 个 git 工具必须成套，否则模型会改用 shell 拼 git 命令绕过守卫）
    "git_status", "git_diff", "git_log", "git_branch", "git_commit",
    # 协作与选优
    "subagent", "best_of_n",
    # 工具发现（不放进来的话 code 模式只能看到这 18 个）
    "tool_search", "use_capability",
})

_BASE_MODES[AgentMode.CODE] = ModeConfig(
    mode=AgentMode.CODE,
    persona="You are a precise software engineer working inside a real repository. ...",
    include_context=True,        # 与 minimal 相反：code agent 需要 git 状态与项目上下文
    include_tools=CODE_TOOL_NAMES,
    enable_compaction=True,
)
```

**关键取舍说明：**

- `include_context=True` 而非 `False`。minimal 模式关上下文是为了「最短路径」，但 code agent 需要知道当前分支、改动状态。关掉会让模型反复自己 `git status` 探测。
- **不包含** `recall`/`remember`/`skill_*`/`media_create`/web 类。code 模式应聚焦代码；需要时经 `tool_search` 激活。这符合 `modes.py` 顶部「只暴露常用集，其余按需激活」的既有设计。
- `delete_file` 保留在集合内但**依赖既有 delete 级确认**（`core.py` 注释：需人工确认 + 二次校验）。不宜因「code 模式」绕过。

### 4.3 前端改动点

| 位置 | 改动 |
| --- | --- |
| `ChatView.vue:739-740` | 选项列表加 `{ label: t('common.code'), value: 'code' }` |
| `ChatView.vue:743` | `DEFAULT_MODE` 保持 `'normal'`（不改变默认行为） |
| `ChatView.vue:759` | 预设加 `code: { temperature: 0.15, ... }` |
| `locales/{zh-CN,en-US,ar}/common.ts` | 新增 `code` 与一句模式说明 |

`mode` 走 `llm_config.mode` 请求级下发（`ChatView.vue:983` 注释已写明「仅影响后续消息」），**无需服务端存储**。

### 4.4 一个已发现的命名缺陷（顺带修）

`modes.py:48` 的注释说：

```python
"search_files",  # 获取外部信息（原为 "web_fetch"：该名字**从未注册**，等于白列）
```

但 `search_files` 的真实注册是 **glob 文件搜索**（`core.py:327-328`：`"Search files by glob pattern"`），
与 `glob_files`（`glob_tools.py:45-46`：`"Find files matching a glob pattern"`）**功能重复**。

即：注释描述的「获取外部信息」与实际语义不符；`CORE_TOOL_NAMES` 里同时存在两个同义工具。
而真正的外部信息工具 `web_search`/`web_fetch` 已注册（`app/tools/web.py`）却不在核心集里。

**建议**：`code` 模式只保留 `glob_files`，把 `search_files` 与 `glob_files` 的重复在 NORMAL 里另行合并。此项与 code 模式解耦，可单独修。

---

## 五、真正阻碍 code agent 的五件事

这一节比第四节重要。**加一个 mode 开关很容易，但开关打开后 agent 仍然碰不到用户的代码。**

### 5.1 `safe_join()` 把一切锁进沙箱自建目录（阻断性）

```python
# app/tools/sandbox.py:69-86
def safe_join(rel: str) -> Path:
    if re.match(r"^[A-Za-z]:[\\/]", rel) or rel.startswith(("\\\\", "//")):
        raise ValueError(f"path escapes sandbox: {rel}")
    base = workspace_dir().resolve()
    target = (base / rel).resolve()
    ...

# app/tools/sandbox.py:45-56
def get_sandbox_dir() -> Path:
    d = sandbox_root() / tenant / user        # ← 相对路径，且绝对路径被拒
```

`workspace_dir()` = `sandbox_root()/<tenant>/<user>/workspace`，
而 `sandbox_root()` 默认是 `cwd.parent.parent/chiron-sandbox`（**仓库外**）。

后果：`read_file("src/main.ts")` 读的是**沙箱里的空目录**，不是用户的仓库。
`read_file("D:/myrepo/src/main.ts")` 被 `safe_join` 直接抛错。

**这是 code agent 模式的第一个必解题。** 三个候选：

| 方案 | 做法 | 代价 |
| --- | --- | --- |
| **A. 项目后端**（推荐） | 新增 `ProjectBackend` 实现 `BackendProtocol`，root = `projects.root_path`，用 `set_backend()` contextvar 按 run 注入 | 复用既有抽象（`backends/context.py` 已支持按上下文覆盖）；需在工具层把 `safe_join` 直连改成走 `get_backend()` |
| B. 改 `SANDBOX_ROOT` | 把 `sandbox_root()` 指到仓库 | 治标不治本：路径仍被拼成 `<root>/<tenant>/<user>/workspace`，且 `sandboxed_env` 的 `HOME`/`TEMP` 重定向会污染 |
| C. 放宽 `safe_join` 白名单 | 允许绝对路径 + 目录白名单 | **最危险**：这一处是当前唯一挡住「模型读 `~/.ssh`」的判据 |

选 A 的理由：`BackendProtocol` 已经有完整的 read/write/edit/delete/ls/glob/grep/execute 面
（`backends/protocol.py:144-182`），`LocalWorkspaceBackend` 就是现成样板。
**不需要新机制，只需要一个新实现 + 按 run 注入。**

### 5.2 Git 工具的 cwd 硬锁在 workspace（阻断性）

```python
# app/tools/git_tools.py:18
# "**cwd 刻意不可传**：git 命令一律在工作区内执行"

# app/tools/git_tools.py:68-88
async def _ensure_sandbox_repo():
    """校验当前 workspace 的 git repo 根未逃逸出沙箱"""
    ws = os.path.normpath(str(workspace_dir()))
    top = ... # rev-parse --show-toplevel
    if not top.startswith(ws):
        return {"error": f"git repository root escapes sandbox: {top}"}
```

即便 5.1 解决了文件路径，**git 仍然拒绝对用户仓库操作** —— 而且这个守卫是正确的（它体现了「不允许跨出工作区」的一致判据）。

**结论**：5.1 与 5.2 必须用同一个「项目根」概念，且 git 守卫要改成对照 `ProjectBackend.root` 而非 `workspace_dir()`。
两者分开改会导致「能读文件但读不到 git 状态」的半残状态。

### 5.3 Low IL 会给用户仓库打完整性标签（有副作用）

```python
# app/isolation/__init__.py:259
def _ensure_workspace_low_label(...):
    """icacls /setintegritylevel Low"""
```

`resolve_level` 在启用 Low IL 时会给**工作区目录**打 Low 完整性标签。

如果工作区 = 用户的真实仓库，那么**首次运行 code agent 会修改用户项目目录的 ACL**，
并且 `revoke_all()` 只还原「它记录过的」路径。

**这是 code agent 模式最容易踩的坑**：功能看起来正常，但用户的仓库 ACL 被静默改了。
必须在设计里明确：项目根目录**不打标签**，只对沙箱子目录打；或显式在 UI 上告知并征得同意。

### 5.4 完全没有网络隔离（高）

全仓搜索 `deny_network|block_network|netsh|fw_rule`：**零命中**。
唯一的网络防护是 `code_guard` 的静态 import 拦截（`socket` 在黑名单，`code_guard.py:25,78`），
但那只作用于 `run_code` 的解释器代码，**对 `shell_exec` 起的任意命令行进程无效**。

在沙箱空目录里这不算大事（没东西可外带）；**一旦 code agent 指向真实仓库，`shell_exec("curl ...")` 就能把源码发出去**。

**建议**：code 模式的 `include_tools` 里**先不放 `shell_exec`**，或要求该模式必须配合 `tools_mode='ask'`（每次执行确认）。
这与既有 `guards.py` 的 ask/auto/yolo 三档可以组合，不需要新机制。

### 5.5 AppContainer 是死代码（不阻断，但别指望它）

```python
# app/isolation/__init__.py:353-365
def spawn_kwargs(decision):
    ...
    kwargs["_appcontainer"] = True
```

全仓搜索 `spawn_kwargs`：**只有定义与 `__all__` 导出，零调用方**。
`_appcontainer` 这个 kwarg 也没有任何消费者。即 **`Level.APPCONTAINER` 档位实际不生效**。

`resolve_level` 里 AppContainer 失败会降级到 `low_il` 且**不**标 `below_floor`（`__init__.py` 注释），
所以它静默地什么也没做。若 code agent 方案要依赖更强隔离，需要先把这个路径接通或明确删除。

---

## 六、分阶段落地建议

| 阶段 | 内容 | 前置 | 可独立交付 |
| --- | --- | --- | --- |
| P0 | 建 `projects` 表 + 项目 CRUD（不含索引） | — | 是 |
| P1 | 建索引生命周期（`autoindex` 后台任务 + 状态） | P0、数据目录移出仓库 | 是 |
| P2 | `ProjectBackend` + 按 run 注入，打通读/写/检索 | P0 | 是 |
| P3 | git 守卫改对照项目根 | P2 | 是 |
| P4 | `code` 模式（`modes.py` + 前端） | P2、P3 | 是 |
| P5 | xerj 检索接入 `code` 模式（`xerj_def` 优先于 `grep_files`） | P1、P4 | 是 |
| P6 | 网络隔离 / Low IL 标签策略 | P2 | 是 |

**顺序理由**：P2（能碰到真实代码）是 P4（模式开关）的价值前提。先做 P4 会得到「切了没变化」——
这正是 `modes.py` 头部注释记录过的历史教训（2026-09 实测暴露的问题）。

---

## 七、待你决定的三件事

1. **项目根目录的授权方式**：用户手填绝对路径 / 目录选择器 / 只允许 `SANDBOX_PROJECT_ALLOWLIST` 内路径？
2. **code 模式是否放 `shell_exec`**：放（能力完整，需 ask 确认）还是不放（安全，但无法跑测试）？
3. **Low IL 标签策略**：项目根不打标签（安全但隔离弱）还是先征得同意再打（隔离强但动用户 ACL）？

---

## 附录：本文核对过的行号

| 结论 | 位置 |
| --- | --- |
| 模式配置表与字段 | `app/agent/modes.py:73-97` |
| 未知模式回退 NORMAL | `app/agent/modes.py:127-137` |
| 工具可见性过滤 + 执行类剔除 | `app/agent/runtime.py:3010-3040` |
| `EXECUTION_TOOL_NAMES` | `app/agent/runtime.py:187-195` |
| 路径 clamp | `app/tools/sandbox.py:69-86` |
| 沙箱根目录推导 | `app/tools/sandbox.py:30-56` |
| git cwd 不可传 | `app/tools/git_tools.py:18` |
| git 不许逃出工作区 | `app/tools/git_tools.py:68-88` |
| 后端抽象与注入 | `app/backends/protocol.py:144-182`、`app/backends/context.py` |
| Low IL 工作区标签 | `app/isolation/__init__.py:259` |
| AppContainer 死代码 | `app/isolation/__init__.py:353-365`（`spawn_kwargs` 零调用方） |
| MCP 破坏性判定 | `app/mcp/client.py:121-133`、`:481-500`、`:513-530` |
| xerj 内置插件定义 | `app/plugins/builtin.py:74-91` |
| 核心工具集 | `app/agent/modes.py:42-64` |
| search_files / glob_files 重复 | `app/tools/core.py:327-328`、`app/tools/glob_tools.py:45-46` |
