# code agent 的检查点回滚与高性能代码索引

> 状态：**设计稿，未实施**。本文所有 git 行为均在 Windows 11 + git 2.45.1 上实测，
> xerj 行为依据 v1.0.0-rc.100 的 `--help` 与实际参数。
> 前置阅读：`docs/code-agent-xerj-design.md`（xerj 能力边界、`safe_join()` 阻断、分阶段落地）。

---

## 一、结论摘要

| 诉求 | 结论 |
| --- | --- |
| 安全回滚对话 | **不能删 `messages`**。`history_writer.py:14` 定了硬约束「任何 `DELETE messages` 都是缺陷」。改为**追加式截断标记**（`session_rewinds`），回滚是视图操作，撤销回滚 = 把标记置空。 |
| 安全回滚代码 | 用 git 对象做快照，但**默认只恢复、不删除**。实测 `read-tree --reset -u` 会删掉用户已 `git add` 的新文件 —— 这是「回滚把人东西弄丢」的唯一来源，必须默认关闭。 |
| 检查点用分支还是 ref | 用户提的是分支 `chiron_V1_checkpoint/<ts>`。实测推荐 **`refs/chiron/checkpoints/*`**：同样不可变、同样能被 gc 锚定，但**不切分支、不动 HEAD**。分支降级为纯本地的可选物化视图（`git log` 里看得到），**不推送**（D3）。 |
| 高性能索引 | xerj **原生尊重 `.gitignore`**，索引规模天然与 git 一致；自带 `resume journal` 做增量、`--watch` 做常驻变更追踪。不需要我们自己算增量喂它，但**回滚后必须触发一次重索引**，否则检索结果与工作区不一致。 |
| 检查点成本 | 实测 1530 文件：冷启动 **2.365s**、热态 **0.179s**（复用持久化 index 的 stat 缓存）。所以「每个必要节点都建检查点」可行，前提是 index 持久化在项目外。 |
| 多会话并行 | **默认共享工作区 + 路径级写锁**；用户显式开并行会话时才每会话一个 **git worktree**（实测文件与回滚都物理隔离，且同一分支被 git 强制只能一个 worktree 检出）。会话间回滚冲突用 `merge-tree --write-tree` 判定，**真冲突拒绝自动回滚**。详见 §十五。 |

---

## 二、实测依据（本文与既有文档的差异）

既有 `docs/code-agent-xerj-design.md` 是纯源码阅读，本文的 git 部分**全部跑过**。
下表每一条都决定了后面的一处设计取舍：

| 机制 | 命令 | 实测结果 | 对设计的影响 |
| --- | --- | --- | --- |
| 独立索引做快照 | `GIT_INDEX_FILE=<proj>.snapidx git read-tree HEAD && git add -A && git write-tree` | 用户暂存区**完全不变**（`M a.txt` 仍是未暂存） | 可以频繁快照，不打扰用户正在做的 `git add` |
| 建提交不移动 HEAD | `git commit-tree <tree> -p HEAD -m ...` | HEAD 仍在原提交，**不产生分支** | 检查点不必切分支 |
| **检查点的父必须是上一个检查点** | `commit-tree <tree> -p <prev_cp> -m ...` | 若 `-p HEAD`，用户自己提交一次后 `merge-base --is-ancestor` 即失败（`CHAIN_BROKEN`）；`-p <prev_cp>` 则 `CHAIN_INTACT` | **唯一祖先链**的前提，见 §14.4 |
| 不可变锚点 | `git update-ref refs/chiron/checkpoints/s1/cp1 <sha>` | ref 建立，`for-each-ref` 可见，HEAD 不变 | 用 ref 命名空间防 gc，且不污染 `git branch` |
| **自定义 ref 无 reflog** | `git reflog show refs/chiron/cp` | **空**；`core.logAllRefUpdates=true` 后**仍为空** | ref 被改写无法自愈 → DB 的 `commit_sha` 才是权威，见 §14.2 |
| 路径级恢复（旧写法，**有副作用**） | `git checkout <tree> -- <path>` | 能恢复，但把文件**塞进用户暂存区**（`A <path>`） | 不能当默认原语，见 §14.1 |
| 路径级恢复（**推荐**） | `git cat-file --filters <tree>:<path> > <path>` | 恢复成功、`git diff --cached` 为空、行尾与原文一致 | 这是默认回滚原语 |
| **`git show` 直接写盘会改写行尾** | `git show <tree>:f.txt > f.txt`（`core.autocrlf=true`） | 原文 `\r\n` 变成 `\n`，`git status` 立刻 ` M f.txt` | 必须过 smudge → 用 `--filters`，见 §14.1 |
| **删除整个目录后恢复需建父目录** | 移走 `src/` 后 `git show <tree>:src/deep/c.py > src/deep/c.py` | `No such file or directory` | `added` 类恢复前必须 `mkdir -p` 父目录 |
| **密钥剔除方式决定是否落盘** | `add -A` + `rm --cached` vs `add -A -- ':(exclude)…'` | 前者 `LEAKED_IN_OBJECTS`，后者 `SAFE_NOT_WRITTEN` | **必须用 pathspec 排除**，`rm --cached` 会把明文密钥留在 objects，见 §10.1 |
| **落地阶段会部分失败** | `chmod 444` 后用 `cp` 覆盖 | 一个文件成功、另一个 `Permission denied`，工作区处于半改状态 | 两阶段 + 逐文件清单报告，见 §14.6 |
| 只恢复不删除 | `git checkout <tree> -- .` | 快照外的文件**不删** | 删除类操作的回滚会**静默失效** → 必须单独处理 |
| 全树同步（危险） | `git read-tree --reset -u <tree>` | 正确删掉快照外文件，但**把用户已 `git add` 的新文件也删了** | 绝不能作为默认回滚，只用于「用户明确同意的丢弃」 |
| 对象篡改检测 | 改坏 blob 后 `git cat-file` / `git fsck` | `inflate: data stream error` / `object corrupt or missing` | git 内容寻址自带完整性校验，回滚前探一次即可 |
| `.gitignore` | `git add -A` + 忽略规则 | 被忽略文件**不进快照** | 但**无 `.gitignore` 时密钥会进快照** → 必须自带硬黑名单，见 §10.1 |
| 无 git identity | 清空 `GIT_CONFIG_GLOBAL`/`SYSTEM` 后 `commit-tree` | **失败**：`fatal: empty ident name (for <>)` | 新用户仓库常未配身份 → **必须**注入 `GIT_AUTHOR_*`/`GIT_COMMITTER_*`，否则检查点建不出来 |
| 残留 `index.lock` | 存在 `.git/index.lock` 时快照 | **不受影响**，`add_exit=0`（走独立 index） | 崩溃残留不会卡住检查点，见 §14.3 |
| 冷/热耗时 | 1530 文件全量快照 | 冷 2.365s / 热 0.179s | index 必须持久化，否则每回合 2.4s |

> 附带发现：`git stash create` 在工作区**只有未跟踪文件时返回空串**（不是报错），
> 不能当快照原语用 —— 它看不见未跟踪文件。这也是本文不用 stash 的原因。

---

## 三、总体架构

```
                    ┌─────────────────────────────────────┐
                    │  Python agent runtime (回合边界)     │
                    │  pre_turn / post_turn 钩子           │
                    └──────────────┬──────────────────────┘
                                   │ 只调用模块，不经 tools 层
                    ┌──────────────▼──────────────────────┐
                    │  git_checkpoint.py（新，非模型可见）  │
                    │  快照 / 差异 / 恢复 / 清理            │
                    └──────┬───────────────────┬──────────┘
                           │                   │
              ┌────────────▼──────┐   ┌────────▼─────────────┐
              │ 用户仓库 .git      │   │ PG: project_checkpoints│
              │ refs/chiron/*     │   │     session_rewinds    │
              │ 持久化 snap index  │   └──────────────────────┘
              └────────────┬──────┘
                           │ 项目当前状态
              ┌────────────▼──────────────┐
              │ xerj（项目作用域索引）      │
              │ resume journal + --watch   │
              └───────────────────────────┘
```

**分层原则**：检查点引擎**不是工具**，模型不能调用它。理由与 `side_effect_ledger.py` 相同 ——
一旦成为工具，模型就能"回滚到某个检查点"来掩盖自己改坏了代码，安全审计就失去意义。
模型只能**建议**回滚，由用户点确认。

**但「不当工具」不等于「模型碰不到」**。实测 `modes.py:78-92` 的 `CODE_TOOL_NAMES` 含
`shell_exec`，而 `shell_exec` 对 `git` 命令**没有任何拦截**（`app/tools/` 下与执行相关的三个守卫是
`exec_audit.py`（只记账不拦截）、`code_guard.py`（只拦 `run_code` 的静态 import）、
`fs_guard.py`（只管文件路径），**没有**针对 git 子命令的策略层）。因此模型可以用
`shell_exec("git update-ref ...")` 或 `git gc --prune=now` 直接破坏检查点链。

三条缓解，缺一不可：

1. **ref 命名空间隔离**：`refs/chiron/*` 与用户分支正交，模型误操作普通 `git commit`
   不影响检查点；反之检查点也永不改 `HEAD`。
2. **`pre_rollback` 兜底**：任何回滚前都先快照，所以即便检查点链被模型改坏，
   最近一次可用状态仍在。
3. **`shell_exec` 的 `git` 子命令审计**：把 `update-ref`/`gc`/`reflog expire`/`filter-branch`
   列入 `exec_audit` 的高危标记（不拦截，但记入账本并在 UI 提示）。
   真正的拦截需等既有 xerj 文档 P6（网络/隔离）一并做，本文不单独收紧。

> 另注：`git_commit` 与 `git_branch` **已实现**（`git_tools.py:173,193`）但**不在** `CODE_TOOL_NAMES` 内
> —— 这个取舍是对的，应保持：让 agent 不建提交，检查点链就只由我们写。

---

## 四、检查点引擎（git 层）

### 4.1 快照原语

```python
# python-engine/app/agent/git_checkpoint.py（新文件，示意）
async def snapshot(root: Path, index_file: Path) -> str:  # 返回 tree sha
    env = {
        "GIT_INDEX_FILE": str(index_file),
        "GIT_AUTHOR_NAME": "Chiron", "GIT_AUTHOR_EMAIL": "chiron@local",
        "GIT_COMMITTER_NAME": "Chiron", "GIT_COMMITTER_EMAIL": "chiron@local",
    }
    # 1) 从当前 HEAD 起步（新仓库无 HEAD 时用空树）
    await git(root, env, "read-tree", "HEAD")        # 失败则 read-tree --empty
    # 2) 把工作区（含未跟踪、遵守 .gitignore）写入独立索引。
    #    黑名单必须在 add 的**同一次调用**里用 pathspec 排除，
    #    绝不能"先 add -A 再 rm --cached"——后者已经把密钥 blob 写进 .git/objects 了。
    spec = ["--", ".", *[f":(exclude,glob){p}" for p in hard_excluded_paths(root)]]
    await git(root, env, "add", "-A", *spec)
    # 3) 只写对象，不碰任何分支
    return (await git(root, env, "write-tree")).strip()
```

四件事刻意做了：

1. **`GIT_INDEX_FILE` 指向项目外**（`%LOCALAPPDATA%\chiron\snapidx\<workspace_id>`）。
   放在项目内会被 `.gitignore` 漏掉、也会被 xerj 索引进去；而持久化是热态 179ms 的来源。
2. **`read-tree HEAD` 而不是 `--empty`**：让未变更文件直接继承父树，`write-tree` 只写差异 blob。
   实测两次快照内容相同时 tree sha 完全一致，天然幂等。
3. **显式身份**：不依赖用户 `git config`。既避免「未配身份就失败」，也避免把检查点提交记到用户名下。
4. **`add -A` + pathspec 排除**：`add -A` 包含未跟踪文件，这是 code agent 的必要性 ——
   agent 新建的文件如果不进快照，回滚时就会变成孤儿。但**`.gitignore` 只是第一层过滤，
   绝不能当成密钥防线**：实测无 `.gitignore` 的仓库里 `add -A` 会把 `.env`、`id_rsa`、
   `.ssh/key` 全部收进树对象。

   **排除必须发生在 `add` 的同一次调用里**（`git add -A -- . ':(exclude).env' …`），
   而不是「先 `add -A`，再 `git rm --cached`」。两者的树结果一样，但对象库状态不同：

   | 写法 | 树里还有密钥吗 | 密钥 blob 还在 `.git/objects` 吗 |
   | --- | --- | --- |
   | `add -A` 后 `rm --cached` | 没有 | **在**（实测 `LEAKED_IN_OBJECTS`） |
   | `add -A -- :(exclude)…` | 没有 | **不在**（实测 `SAFE_NOT_WRITTEN`） |

   前者是隐蔽事故：树是干净的，`git ls-files` 也看不到 `.env`，但 blob 已落盘，
   用 `git fsck --lost-found` 就能把明文密钥捞出来。而检查点承诺「永不删、永不 gc」，
   等于把这份明文长期留在用户仓库里。详见 §10.1。

`excluded_paths()` 混合两类，**行为不同，不能混为一谈**（§14.5）：

- **密钥类**（`.env`、`*.pem`、`id_rsa*`…）：剔除后**必须**在检查点记录 `skipped_count`
  并在 UI 提示「本次已跳过 N 个疑似密钥文件」。用户有权知道自己的密钥**没被**保护。
- **噪声类**（`node_modules/`、`.idea/`、`__pycache__/`、`dist/`、`build/`、`target/`）：
  剔除后**静默**，不提示。提了只会让非技术用户困惑。
  这类路径同时进**保护列表**，确保它们绝不会出现在回滚的 `removed` 清单里。

**第 1 条不是洁癖，实测会直接破坏幂等性**：把 `GIT_INDEX_FILE` 指向工作区内的
`in.i` 建快照，`git ls-tree -r` 给出 `a.py / in.i / in.i.lock` —— **index 二进制和它的
锁文件被 `add -A` 收进了快照**，于是第一次 `fae1980a...`、第二次 `2584d2ba...`，
tree sha 每次都变，"内容相同则 tree 相同"这个去重前提直接失效。
而同一种操作在 index 位于工作区外时，并发 10 轮 × 2 个独立 index 得到**唯一 tree**
（实测 `唯一 tree 数 = 1`）。

### 4.2 提交与不可变锚点

```python
commit = await git(root, env, "commit-tree", tree, "-p", parent, "-m", message)
ref    = f"refs/chiron/checkpoints/{session_id}/{seq:06d}-{kind}"
# CAS 写入：旧值不匹配就失败，从而**不可能覆盖已有检查点**
await git(root, env, "update-ref", ref, commit, "")   # 期望旧值为空 = 只允许新建
```

- **`refs/chiron/*` 不被 `git branch` 列出**，用户的分支面板不会多出几十条噪音；
  但它**是真正的 ref**，`git gc` 不会回收（实测 `for-each-ref` 可见）。
- **「禁止重写」用 CAS 落地**：`update-ref <ref> <new> <old>` 对同一 ref 第二次写入必然失败，
  因为旧值已不是空。这比"约定不要 force push"强得多，是 git 自身的不变式。

  **但 CAS 只约束我们自己**。实测 `refs/chiron/*` **没有 reflog**
  （`.git/logs/refs/chiron/` 不存在，`core.logAllRefUpdates=true` 也不覆盖自定义命名空间），
  所以外部进程改写 ref 后 git 无法还原。因此 **DB 里的 `commit_sha` 是唯一权威**，
  ref 只是防 gc 的锚点；读取时比对不一致则用 DB 值重新 `update-ref` 自愈（§14.2）。
- **父提交必须是上一个检查点，而不是 HEAD**（实测差别见 §14.4）：
  `-p HEAD` 会在用户自己提交一次后断链（`CHAIN_BROKEN`），`-p <prev_cp>` 才保持
  线性链（`CHAIN_INTACT`）。当时的 HEAD 写进 commit message 的 `Chiron-Head:` trailer。
- **不推送**（D3 已定）。检查点纯本地，用户提的 `chiron_V1_checkpoint/<时间戳>` 分支
  降级为纯本地的可选物化视图：只有被标记为 milestone 时才 `git branch <name> <commit>`
  （不切换 HEAD、不 push）。

### 4.3 检查点时机与去重

| 时机 | kind | 说明 |
| --- | --- | --- |
| 回合开始前 | `pre_turn` | 记录"用户发消息前"的干净起点，是回滚的默认目标 |
| 回合结束后（有文件改动） | `post_turn` | 结果可回看；无改动则跳过 |
| 写类工具执行前 | `pre_write` | 可选细粒度，默认关闭（每回合可能几十次） |
| 用户手动打标记 | `milestone` | 前端按钮，永不自动清理 |
| 回滚执行前 | `pre_rollback` | **保证回滚本身可撤销**，这条是关键安全设计 |

**去重**：`write-tree` 得到的 tree 与本会话上一个检查点相同时，**不建提交、不写 DB 行**。
实测热态 179ms 里 `write-tree` 占比很小，去重省的是对象与 DB 行，不是 stat 成本。

**并发**：同一项目的快照用 `<snapidx>.lock` 串行化。不同项目 index 文件不同，锁也独立。
用户自己的 `git add` 用 `.git/index` 与 `.git/index.lock`，与我们无交集（实测确认）。

### 4.4 保留与清理

对象存在用户 `.git/objects` 里，不清理会持续膨胀。策略：

```
保留 = 本会话最近 50 个 ∪ 所有 milestone ∪ 所有被 session_rewinds 引用的 ∪ 全局最近 200 个
清理 = 删除其余 refs/chiron/checkpoints/*  →  git gc --prune=now（仅当对象数超阈值）
```

两个约束：
- **`--prune=now` 必须只在没有并发 git 进程时跑**，否则可能删掉别人正在写的对象。
  实测层面无法用 git 自身保证，所以清理只在「项目无活跃会话」的空闲任务里做。
- **引用中的检查点永不删**：`session_rewinds.checkpoint_id` 指向的必须存活，否则「撤销回滚」失效。

---

## 五、代码回滚

### 5.0 与既有 `undo_stack.py` 的关系（必须先讲清）

仓库里**已经有一套文件级回滚**：`app/agent/undo_stack.py` 是 `/undo` 的真实后端，
在 `write_file`/`edit_file` 写入前存原内容，`restore()` 据此复原。它的边界是：

| 维度 | `undo_stack`（已有） | 本文的检查点（新增） |
| --- | --- | --- |
| 粒度 | 单文件、每次写入一条 | 全项目树（含未跟踪文件） |
| 容量 | `MAX_SNAPSHOTS = 20` | 受 ref 保留策略约束（§4.4） |
| 有效期 | `TTL_SECONDS = 24h`（存 Redis） | 与 git 对象同寿命 |
| 单文件上限 | `MAX_SNAPSHOT_BYTES = 256KB`，超限**明确告知不可撤销** | 默认 64MB/文件，超限跳过并告警 |
| 覆盖范围 | 仅 `write_file`/`edit_file` | 含 `shell_exec` 造成的任意改动 |
| 跨会话 | 否（`_key(session_id)`） | 是（按项目） |

**两者的分工，而不是替代**：

- `undo_stack` 保留为**即时撤销**（`/undo` 一步回退），响应快、不碰 git；
- 检查点负责**跨会话、跨工具、可审计**的回滚，是"回到某个时间点"的答案。

必须避免的重复实现：**不要让 `restore()` 也去改 git，也不要让检查点回滚去读 Redis 快照**。
两者的失效模式不同（Redis 会过期，git 对象不会），混用会产生「撤销了但树对象没变」的
不一致状态。实现时在 `undo_stack.restore()` 的 docstring 里加一条交叉引用即可。

> 另注：`undo_stack` 只覆盖 `write_file`/`edit_file`（`_SNAPSHOT_TOOLS` 同此），
> 这正是它对 code agent 不够用的原因 —— agent 主要通过 `shell_exec` 跑 `mv`/`rm`/`sed`，
> 这些改动在 `undo_stack` 视野之外，而 `git add -A` 能全部看见。

### 5.1 核心难点：删除 vs 恢复

实测得到两条互相矛盾的事实：

1. `git checkout <tree> -- .` **不删除**快照外文件 → agent 删掉的文件，回滚时不会回来。
2. `git read-tree --reset -u <tree>` **会删除**快照外文件 → 但连用户已 `git add` 的新文件一起删。

第 2 条是最危险的行为：用户手动 `git add` 了三个文件准备提交，agent 回滚一次，全没了。
**这就是"回滚把人东西弄丢"的真实路径。**

### 5.2 设计：三分类差异 + 默认不删

回滚前先算 `diff(当前树, 目标树)`，按语义分三类：

| 类别 | 动作 | 默认 |
| --- | --- | --- |
| `restored`：目标有、当前内容不同 | `git cat-file --filters <tree>:<path>` 写盘（**不用 `git checkout`，也不用裸 `git show`**，见下） | 执行 |
| `added`：目标有、当前没有 | 先 `mkdir -p` 父目录，再 `git cat-file --filters` 写盘 | 执行 |
| `removed`：目标没有、当前有 | **不自动删**，返回清单 | 需用户勾选 |

> **为什么不用 `git checkout <tree> -- <path>`**：实测它会把恢复的文件**写进用户暂存区**
> （`git status` 出现 `A <path>`）。用户只是点了回滚，却在不知情的情况下改了 `git add` 列表。
> 而不经 index 的取法（`git show` / `git cat-file`）`git diff --cached` 保持为空（§14.1 实测）。

> **为什么连 `git show` 也不能用**：实测（`$TMPDIR/crlf3`，`core.autocrlf=true`）——
> 工作区原文是 `line1\r\nline2\r\n`，`git show <tree>:f.txt > f.txt` 写出的是
> **`line1\nline2\n`**，`git status` 立刻变成 ` M f.txt`。**回滚"成功"了，但整个文件
> 的行尾被静默改掉**，用户下次提交就是一次纯行尾的全文件 diff。
> 换成 `git cat-file --filters`（会走 smudge 过滤器）后字节与原文一致，`git status` 为空。

> **父目录必须先建**：实测把 `src/` 整目录移走后，`git show <tree>:src/deep/c.py > src/deep/c.py`
> 直接报 `No such file or directory`。恢复 `added` 类文件前必须 `mkdir -p` 其父目录，
> 否则"文件回来了"只在浅层目录成立。

**保护列表（protected paths）同样参与过滤**：密钥/噪声黑名单里的路径
即使因剔除而从树里"消失"，也**绝不能出现在 `removed` 清单里**，否则会诱导用户
删掉自己的 `.env`（§14.5 实测了这个 `D` 陷阱）。

`removed` 类细分为「我们删的」和「用户自己删的」：

- **不能依赖副作用账本判定**。实测 `side_effect_ledger.py` 的边界：`MAX_ENTRIES = 200`、
  `TTL_SECONDS = 24h`，且 `_SNAPSHOT_TOOLS` 只有 `frozenset({"write_file", "edit_file"})`
  —— **不含 `delete_file`**。用它做归因，跨天或超过 200 条后历史删除记录就被淘汰，
  结果是「agent 昨天删的文件，今天回滚时不再出现在清单里」。这类静默失效比不做归因更糟。
- **改为用 git 自身判定**：`diff-tree -r --name-status` 给出 `D` 的路径，
  再与**本会话检查点序列**比对 —— 若某路径在 `pre_turn(C_n)` 存在、在 `post_turn(C_n)` 消失，
  则是**本会话 agent 删的**。这是持久化在 git 对象里的证据，不受 TTL 影响。
- 用户自己删的文件**不列出来**：回滚不该"复活"用户主动做的操作。判定依据同上 ——
  若路径在会话开始前的基线 `C_0` 就不存在，或删除发生在两次检查点之外的间隙，
  则不算 agent 所为。

**执行删除时**：先建 `pre_rollback` 检查点（4.3），再按路径 `unlink`。
于是"回滚删错了"也能救回来 —— 这就是安全闭环。

### 5.3 单文件/单回合回滚

前端需要的不只是"整体回滚"，还有"只撤销这一处改动"。两者共用同一原语：

```
单文件：diff = {(path, 目标版本)} → git cat-file --filters <tree>:<path> > <path>
单回合：diff = diff(当前树, pre_turn 树) → 三分法
```

`diff-tree -r --name-status <treeA> <treeB>` 实测能直接给出 `A/M/D` 变更集（见实测表），
正好作为增量索引与回滚清单的同一个输入。

---

## 六、对话回滚

### 6.1 为什么不能删消息

`history_writer.py` 模块头写明四条硬约束，第 2 条是「只追加：只 `INSERT`。
任何 `UPDATE` / `DELETE messages` 都是缺陷」。原因有三：审计、计费对账（快照里的 `turn_ids`，
即 `agent_runs.checkpoint` jsonb 的一个字段，非表列）、
以及「压缩后可召回原文」依赖历史完整。

**而现状确实是坏的**：`ChatView.vue:2640` 的注释自己承认

> `truncateFrom` 是**不可逆**的（后续消息直接从前端状态里消失，后端也没有回滚接口）

即刷新页面后消息全部回来。用户说的"安全回滚对话"，要修的就是这个。

### 6.2 设计：追加式截断标记

```sql
CREATE TABLE session_rewinds (
    id              varchar(36) PRIMARY KEY,
    tenant_id       smallint    NOT NULL,
    session_id      varchar(36) NOT NULL,
    from_seq        bigint      NOT NULL,   -- 此 seq（含）之后不可见
    checkpoint_id   varchar(64),            -- 同时回滚代码时指向代码检查点
    reason          varchar(32) NOT NULL,   -- regenerate | edit_resend | manual
    created_at      timestamptz NOT NULL DEFAULT now(),
    undone_at       timestamptz             -- 非 null = 回滚被撤销
);
CREATE INDEX ix_session_rewinds_active ON session_rewinds (session_id) WHERE undone_at IS NULL;
```

读取时：

```sql
SELECT ... FROM messages
 WHERE session_id = $1
   AND seq < COALESCE(
         (SELECT min(from_seq) FROM session_rewinds
           WHERE session_id = $1 AND undone_at IS NULL), 9223372036854775807)
 ORDER BY seq DESC LIMIT $2
```

三个性质由此得到：

1. **不可逆变可逆**：撤销回滚 = `UPDATE session_rewinds SET undone_at = now()`，一行而已。
2. **零数据丢失**：原文一直在库里，`read_archive` 之类召回路径不受影响。
3. **与折叠机制正交**：`session_folds` 用 `[covered_from, covered_to)` 表示折叠，
   读取时把区间按 `from_seq` 裁剪即可。**注意**：若某个 fold 跨越截断边界，
   投影必须只取 `covered_from < from_seq` 的部分，否则会把"已删除的对话"折进摘要里。

### 6.3 对话与代码的原子性

用户点「回到这里」时，应该**同时**回滚对话与代码，且要么都成、要么都不成。

```
BEGIN;
  INSERT INTO session_rewinds (..., checkpoint_id) VALUES (...);
  -- 代码恢复在事务外做（git 不受 PG 事务保护），因此必须记录预期状态
  UPDATE project_checkpoints SET applied_at = now() WHERE id = $cp;
COMMIT;
```

代码恢复失败时的处理：**先恢复代码、后写标记**。顺序反过来会出现
「对话已截断、代码没回滚」的半残状态。失败则整个操作报错，前端不改动任何视图。

---

## 七、高性能索引（xerj）

### 7.1 为什么不自己算增量

xerj 的能力（实测 `--help`）：

- **原生尊重 `.gitignore`**，含嵌套与否定规则，另支持 `.xerjignore`（优先级更高）。
- **`resume journal`**：默认在 `~/.xerj/autoindex/<hash>/`，重跑只处理变更；
  `--fresh` 才是全量重建。
- **`--watch`**：索引一次后常驻，用 OS 文件监听（非轮询）增量重建，`--debounce` 默认 400ms。
- **内置默认忽略**：`node_modules/`、`vendor/`、`dist/`、`target/`、`.venv/`、二进制与字体等。

结论：**增量是 xerj 的职责，我们只需要在正确时机触发它**。
这与既有文档 §3.2 的 `autoindex` 生命周期设计一致，本文只补一致性处理。

### 7.2 索引与检查点的一致性

| 事件 | 索引动作 |
| --- | --- |
| 项目创建 | `autoindex <root> --prefix <p>`（后台任务），`index_state=queued` |
| 常驻（推荐默认） | `autoindex <root> --prefix <p> --watch --no-graph`，交给 OS 监听 |
| 回合结束后有改动 | 若未启用 watch，触发一次 `autoindex`（resume journal 只处理变更） |
| **代码回滚后** | **必须触发一次重索引** —— 文件时间戳变了但内容可能回到旧版本，watch 也能捕捉，但不能依赖它 |
| 删除项目 | `autoindex remove --prefix <p> --yes` |

**`--watch` 必须配 `--no-graph`**（实测 `--help` 原文：「Needs --no-graph」）。
代价是放弃 wikilink / 目录链等关系边 —— 对代码库没有损失（那些是笔记语料的概念），
但如果将来要索引 `docs/` 里的 `.md`，需要另开一个不带 watch 的语料前缀。

另有两条实测细节会影响状态机设计：
- **`--watch` 遇到"需要新 dataset"的文件会停住**（exit 3 `stale-resume-plan`），直到 `--fresh`。
  所以 `index_state` 不能只有 `ready`，必须有 `stale` 状态并在 UI 上提示「索引需重建」。
- **`--watch` 是"一个索引目录一个 OS watch，不轮询"**，因此它随进程存活而存活；
  桌面应用退出即失效 → 下次启动要重新拉起，且首轮必须能自动 resume（journal 已存盘）。

**回滚触发重索引是必须的**，理由：回滚把文件恢复成旧内容，若索引仍指向新内容，
agent 检索到的代码与实际工作区不一致 —— 这比索引滞后更糟，因为模型会基于虚假信息改代码。

### 7.3 数据目录

既有文档 §2.3(a) 已说明：`data/` 在仓库内、xerj 的 `data/` 不在内置忽略清单里，
索引整个仓库会**把自己索引进去**。所以数据目录必须在仓库外（如 `%LOCALAPPDATA%\chiron-xerj`）。

另加一条本文新增的约束：**`snapidx` 目录同样必须在仓库外**，
否则每次 `git add -A` 都会把上次的 index 二进制当文件写进快照。

---

## 八、数据模型

```sql
-- migrations/versions/0004_code_agent_checkpoints.py
CREATE TABLE project_checkpoints (
    id              varchar(64)  PRIMARY KEY,   -- <workspace_id>:<seq>
    tenant_id       smallint     NOT NULL,
    user_id         varchar(36),
    workspace_id    varchar(64)  NOT NULL,      -- = session_map_workspaces.id
    session_id      varchar(36),
    turn_id         varchar(36),
    seq             bigint       NOT NULL,      -- 会话内单调递增，回滚只能回到更小的 seq
    commit_sha      varchar(64)  NOT NULL,      -- 唯一权威；ref 只是防 gc 锚点（§14.2）
    tree_sha        varchar(64)  NOT NULL,
    parent_sha      varchar(64),                -- **上一个检查点**，不是 HEAD（§14.4）
    head_sha        varchar(64),                -- 建检查点时的用户 HEAD，存在 trailer 里
    ref_name        varchar(255) NOT NULL,
    kind            varchar(16)  NOT NULL,      -- init_epoch|pre_turn|post_turn|pre_write|milestone|pre_rollback
    state           varchar(16)  NOT NULL DEFAULT 'ok',  -- ok|failed|lost|ref_drift
    error_code      varchar(64),
    error_msg       text,
    label           varchar(255) NOT NULL DEFAULT '',
    file_count      integer      NOT NULL DEFAULT 0,
    skipped_count   integer      NOT NULL DEFAULT 0,   -- 被黑名单跳过的文件数（§10.1）
    byte_size       bigint       NOT NULL DEFAULT 0,
    branch_name     varchar(255),               -- 物化成 chiron_checkpoint/<ts> 时填（本地视图）
    applied_at      timestamptz,                -- 该检查点被回滚到的时刻（§6.3）；NULL = 未回滚过
    created_at      timestamptz  NOT NULL DEFAULT now()
);
CREATE INDEX ix_project_checkpoints_session ON project_checkpoints (session_id, seq DESC);
CREATE INDEX ix_project_checkpoints_project ON project_checkpoints (workspace_id, created_at DESC);
```

两处相对初稿的改动，均由实测驱动：

- **`parent_sha` 语义反转**：初稿写「建检查点时的 HEAD」，实测那样会让链在用户提交后断开
  （`CHAIN_BROKEN`）。现在 `parent_sha` = 上一个检查点，用户 HEAD 另存 `head_sha`。
- **删除 `pushed_at`**：D3 定为从不推送，该字段无用。保留它只会诱导后人加回推送。
- **新增 `state` / `error_code` / `error_msg`**：检查点失败必须落库而不是抛异常（§14.3）。
- **新增 `seq`**：初稿只有 `id` 里的 seq，但要高效判断"只能往回滚"需要独立可索引列。

**`rollback_epoch` 由 `kind='init_epoch'` 的那条记录承担**（D2）：不额外建字段，
查询"最近一条 `kind='init_epoch'` 且 `state='ok'` 的记录"即为本 epoch 起点。
`git init` 被重复执行（或项目根变更）会追加新的 `init_epoch`，旧 epoch 自动失效。

`workspace_id` 直接复用 `session_map_workspaces` —— 迁移 `0003_project_binding.py` 已确立
「一个 workspace 要么是项目（`root_path` 非空）、要么是画布」，**不新建 projects 表**。

这与既有 xerj 文档 §3.1 的结论不同，需说明时间差：该节标题是「『项目』概念需要新建」，
其前置核查写明「迁移 `0001` 中**没有** project / projects 表」「`session_map_workspaces`
存的是前端画布视口，不是代码工程」，据此建议新建 `projects` 表。
但那些核查写在 `0003` **之前** —— `0003_project_binding.py` 随后就给
`session_map_workspaces` 加了 `root_path` / `sort_index` 与 `ux_smn_project_session` 唯一索引，
把这个挂载点补上了。**结论：§3.1 的问题已被 `0003` 解决，本文沿用该挂载点，不再新建表。**

`xerj_prefix` 与 `index_state` 同理挂在 `session_map_workspaces` 上（新增 3 列），不另建表。

**迁移上线顺序（实测会导致启动阻断，必须先做）**：`internal/db/schema_version.go` 的
`ParseMigrationHead()` 判定「出现在 `revision` 中且未被任何 `down_revision` 引用」为 head，
`CheckSchemaVersion()` 比对 `alembic_version.version_num`。所以：

1. 新增 `0004_code_agent_checkpoints.py` 会把 head 从 `0003_project_binding` 推到 `0004`；
2. **存量数据库一旦升级到这个二进制，`match=false`** —— 与 `0003` 上线时同样的坑；
3. 因此发布流程必须是「先跑 `alembic upgrade head`，再启动新二进制」，
   且迁移必须**可重复执行**（本迁移只 CREATE TABLE / ADD COLUMN，天然幂等）。

同理，`0004` 里给 `session_map_workspaces` 加列必须带 `DEFAULT` 且允许 NULL 语义
（`xerj_prefix` 默认 `''`、`index_state` 默认 `'none'`），否则存量行在 `ADD COLUMN NOT NULL`
时会因缺省值而失败。

---

## 九、API 与前端

### 9.1 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/v1/projects/{id}/checkpoints` | 时间线列表（含 kind/label/改动文件数） |
| `POST` | `/v1/projects/{id}/checkpoints` | 手动打标记 |
| `GET` | `/v1/checkpoints/{cp}/diff` | 三分类改动清单（restored/added/removed） |
| `POST` | `/v1/checkpoints/{cp}/rollback` | `{ dry_run, allow_delete, paths[] }` |
| `POST` | `/v1/checkpoints/{id}/materialize-branch` | 物化成**本地**分支 `chiron_checkpoint/<ts>`（不切 HEAD、不推送） |
| `POST` | `/v1/sessions/{id}/rewind` | `{ from_seq, checkpoint_id? }` 对话+代码 |
| `DELETE` | `/v1/sessions/{id}/rewind/{rid}` | 撤销回滚（置 `undone_at`） |

`rollback` 强制 `dry_run` 先执行一次：前端必须**先看到清单再确认**，
不允许"点一下就改磁盘"。这与既有的 `confirmDestructive` 取向一致。

### 9.2 前端 UX（面向非技术用户）

- 侧边栏项目行出现「时间线」入口，按小时分组的卡片，每张写
  「14:32 · 改了 3 个文件」而不是 commit sha。
- 回滚二次确认列出**具体文件名**，删除项单独红框勾选，默认全不选。
- 回滚完成后 toast：「已回到 14:32，可撤销」+ 一个 **撤销** 按钮（写 `undone_at` + 恢复 `pre_rollback`）。
- 对话侧：消息操作栏的「重新生成」不再不可逆，确认文案改为
  「将回退到这条消息（可撤销）」—— 与现状「cannot be recovered」的措辞直接相反。

---

## 十、安全边界（本文重点）

用户要的是「**安全**回滚」，所以每条能力都要说清失效条件：

| 风险 | 防护 |
| --- | --- |
| 回滚删掉用户已 `git add` 的文件 | 默认不删；删除需逐项勾选；删前建 `pre_rollback` 快照 |
| 回滚把恢复的文件塞进用户暂存区 | **恢复用 `git cat-file --filters` 不用 `git checkout`**（§14.1 实测 `checkout` 会产生 `A <path>`） |
| 回滚静默改写行尾 | 同上：`--filters` 走 smudge 过滤器；裸 `git show` 会把 CRLF 写成 LF（§14.1 实测） |
| 检查点覆盖已有检查点 | `update-ref` 带 CAS 旧值，物理不可覆盖 |
| 外部改写检查点 ref | CAS 只约束自己；`refs/chiron/*` **无 reflog**（实测），故 DB `commit_sha` 为权威 + 读时比对自愈（§14.2） |
| 检查点对象被病毒改坏 | git 内容寻址自带完整性校验（实测 `cat-file`/`fsck` 均报 corrupt）；回滚前主动 `fsck --connectivity-only` |
| **快照把密钥写进 `.git`** | **见下方专条 —— 这是本文发现的最严重风险，`.gitignore` 不足以兜住** |
| 快照把大文件写进 `.git` | 单文件与总量上限（默认 64MB/文件、2GB/检查点），超限则**跳过该文件并告警**，不静默截断 |
| 剔除脏内容反过来删掉用户文件 | **保护列表**过滤 `removed` 清单（§14.5 实测剔除后 `.env` 会显示成 `D`） |
| 源码外泄 | **从不推送**（D3）。检查点纯本地，`refs/chiron/*` 不出本机 |
| 模型自行回滚掩盖错误 | 检查点引擎不是工具，模型无法调用（§三） |
| 项目非 git 仓库 | 检测到就**提示并询问**是否 `git init`（D1），不静默建 `.git` |
| init 之前的会话被回滚 | `init_epoch` 之前的检查点**禁止回滚**，接口返回 409（D2） |
| 检查点创建失败阻断对话 | fail-open：落库 `state='failed'` 并继续；连续 3 次降级为 `degraded` 并显式告知（§14.3） |
| 回滚中途失败 | 两阶段（先暂存后落地）+ 必建 `pre_rollback` + `rollback_state` 落库（§14.6） |
| 回滚后索引与工作区不一致 | 回滚成功即触发重索引（§7.2） |
| `gc --prune` 删掉在用对象 | 只在项目空闲时清理；引用中的检查点与近 N 个永不删 |

### 10.1 密钥泄露：`git add -A` 与 xerj 的行为正好相反

**实测**：在一个**没有 `.gitignore`** 的新仓库里建 `.env`、`id_rsa`、`.ssh/key`，然后
`GIT_INDEX_FILE=... git add -A`，结果 `git ls-files` 列出：

```
.env
.ssh/key
app.py
id_rsa
```

即 **`git add -A` 不看隐藏属性，无 `.gitignore` 时把密钥全收进快照**。
这些 blob 会真实写进用户 `.git/objects`，此后即便删掉 ref，只要对象还没被 gc
（而我们承诺"引用中的检查点永不删"），密钥就一直躺在仓库里。

对比 xerj 的立场（`autoindex --help` 原文）：

> Hidden files (.env, .git/, .ssh) stay skipped either way — **that is not an ignore rule,
> it is what keeps secrets out.**

xerj 把"跳过隐藏文件"当成安全底线而非可配置规则；而 git 完全不管这件事。
**照搬 `git add -A` 就等于把这个底线交给用户是否恰好写了 `.gitignore`。**

**三层防护，必须都实现：**

1. **路径黑名单（硬编码，不可关闭）**：`.env`、`.env.*`、`*.pem`、`*.key`、`*.p12`、`*.pfx`、
   `id_rsa*`、`id_ed25519*`、`*.keystore`、`.ssh/**`、`.aws/**`、`.gnupg/**`、`.npmrc`、
   `.netrc`、`credentials*`、`*.kdbx`。命中则**不入快照**，并在检查点记录里写明跳过了几个文件。
   实现方式是 **`git add -A` 时用 pathspec 排除**（在独立 index 上操作，不影响用户），
   而不是靠 `.gitignore` —— 因为用户的 `.gitignore` 不归我们改。

   ```bash
   git add -A -- . ':(exclude,glob).env' ':(exclude,glob)*.pem' ':(exclude,glob).ssh/**'
   ```

   **已实测（`LEAKED` vs `SAFE`）**：同一无 `.gitignore` 仓库、同样建出「不含密钥」的树，
   两种写法的对象库状态**根本不同**：

   ```
   写法 A: git add -A  →  git rm --cached -- .env
           树里: [a.py]                     ← 干净的
           密钥 blob 3bb52350a1 -> LEAKED_IN_OBJECTS    ← 但已落盘！

   写法 B: git add -A -- . ':(exclude).env'
           树里: [a.py]
           密钥 blob 3bb52350a1 -> SAFE_NOT_WRITTEN    ← 从未写入
   ```

   **写法 A 是隐蔽事故**：树是干净的，`git ls-files` 看不到 `.env`，常规检查全过；
   但 blob 已在 `.git/objects` 里，`git fsck --lost-found` 能把明文密钥捞出来。
   而检查点承诺「永不删、永不 gc」，等于让明文密钥在用户仓库里长期滞留。
   → **`git rm --cached` 不能作为剔除手段**，必须在 `add` 的同一次调用里排除。
   （用户自己的 `.git/index` 在两种写法下都未被触碰。）
2. **密钥内容扫描（深度防御）**：快照建立后对新增 blob 做一次正则扫描
   （`sk-[A-Za-z0-9]{20,}`、`-----BEGIN [A-Z ]*PRIVATE KEY-----`、`AKIA[0-9A-Z]{16}` 等）。
   命中**不阻断**（否则正常开发会被卡住），但记入 `project_checkpoints.skipped_count`
   并在 UI 提示「本次检查点跳过 N 个疑似密钥文件」。
3. **仓库级声明**：项目绑定后第一次建检查点时，读一次 `git check-ignore` 验证关键路径，
   若 `.env` 未被忽略，提示用户「建议把 `.env` 加入 `.gitignore`」—— **建议，不代改**。

**为什么不能只做第 3 条**：它依赖用户配合。用户点了"稍后"就永久裸奔，而第 1、2 条是
我们自己的防线，不依赖任何外部条件。

**与 xerj 的对称性**：xerj 不索引密钥，我们的快照也不收密钥，两套存储的暴露面一致。
若只有一边做了，等于把密钥从索引挪到了 git 对象里，反而更难发现。

**回滚永不丢失信息的闭环**：任何一次回滚前都有快照，任何一次对话回滚都可 `undone_at` 撤销。
即便用户连点两次回滚，也只是多了两个快照。

---

## 十一、性能预算

| 操作 | 实测/预期 | 说明 |
| --- | --- | --- |
| 热态快照（1530 文件） | 179ms | 持久化 index 命中 stat 缓存 |
| 冷启动快照 | 2.4s | 仅在首次或 index 丢失时 |
| 快照去重命中 | ~0 对象 | tree 未变则不建提交 |
| diff 三分类 | `< 100ms` | `diff-tree` 只走 tree 对象 |
| 路径级恢复 | 与改动文件数成正比 | 单文件毫秒级 |
| 并发快照（2 个独立 index × 10 轮） | 结果恒为同一 tree | index 在工作区外才成立，见 §15.3 |
| 跨会话回滚冲突判定 | `merge-tree` 纯对象运算 | 不碰工作区、不建临时分支 |
| 全量索引 | 后台任务 | 不进请求路径（既有文档 §3.2 硬约束 2） |

**超大仓库的降级**：文件数 > 50k 时，快照默认只含已跟踪文件（`add -u`），
未跟踪文件仅在被 agent 写入时单独补入。这是为了避免 `add -A` 扫到构建产物目录。

---

## 十二、分阶段落地

在既有 xerj 文档 P0–P6 之上插入，**不改变其顺序依赖**：

| 阶段 | 内容 | 前置 | 可独立交付 |
| --- | --- | --- | --- |
| **P0.5** | `snapshot/diff/restore` 三个原语 + 单测（无需 DB）✅ **已落地** | — | 是 |
| **P1** | `project_checkpoints` 表 + pre/post_turn 钩子 + fail-open + 时间线 API | P0（项目 CRUD）、P0.5 | 是 |
| **P2** | 回滚 API（三分类 + dry_run + pre_rollback + 两阶段落地） | P1 | 是 |
| **P3** | `session_rewinds` + 读取侧裁剪 + 撤销 | P1 | 是 |
| **P4** | 前端时间线 / 回滚确认 / 撤销 toast | P2、P3 | 是 |
| **P5** | 索引生命周期（沿用既有 P1） | P0、数据目录外置 | 是 |
| **P6** | 回滚后自动重索引 | P5、P2 | 是 |
| **P7** | 里程碑物化成**本地**分支（不推送，可选） | P1 | 是 |

**顺序理由**：P0.5 是纯函数，能先写单测锁住本文那几条实测语义（尤其
「`cat-file --filters` 恢复不污染暂存区、不改行尾」「不做静默删除」「保护列表不进删除清单」），
后面所有层都依赖它。若先做 UI，会把错误的 git 语义固化成用户习惯。

**P1 必须同时交付 `init_epoch`**：D2 要求 init 之前的会话禁止回滚，
这是数据模型层的不变式，晚加会导致历史数据需要回填。同阶段还要接上
`undo_stack.py` 作为检查点失败时的兜底（§14.3 第 3 条）。

**P2 的验收要以 §14.6 的三个失败分支为准**：dry_run 强制先行、回滚前必建
`pre_rollback`、`rollback_state` 落库。这三条比"能回滚成功"更重要。

---

## 十三、已定决策

| # | 议题 | 决定 | 对设计的影响 |
| --- | --- | --- | --- |
| D1 | 非 git 目录 | **检测到就提示并询问是否 `git init`**；不替用户决定 | §14.3：项目状态机多一个 `non_git` 态 |
| D2 | **init 之前的会话** | **禁止回滚**（会话记录仍保留，只是回滚按钮不可用） | 新增 `rollback_epoch` 概念，见 §八 |
| D3 | 远程推送 | **从不推送** | 删掉 P7 的推送部分，ref 只留在本地；`pushed_at` 字段废弃 |
| D4 | 快照是否含未跟踪文件 | **包含** | 用自有按 sha256 分片的内容寻址存储（`UntrackedStore`）保存，**不进**用户 `.git/objects`；清单需独立表（§8.2） |
| D5 | 是否允许删除回滚 | **可自定义，但默认禁止** | `RollbackPolicy.from_settings()`；解析不出或非法值一律落到"禁止" |
| D6 | 项目根 ≠ 仓库根 | **当前仅适配单仓库目录**：要求项目根 == 仓库根，不等则拒绝启用检查点 | §16.6；`snapshot()` 不做子目录裁剪，路径都相对仓库根 |
| D7 | 检查点体积上限与清理阈值 | **可自定义，下限 4 GB**（4 GB 从旧设计的"上限"变成"下限"） | `Limits.resolve(settings)` + `adaptive_limits()`；用户设置只能往上调，不得低于 4 GB |

**D4 的实现（已落地，见 `python-engine/app/agent/git_checkpoint.py`）**：未跟踪文件写进
`<state>/untracked/<sha256[:2]>/<sha256[2:]>`。选它而不选 `git add -A` 的理由有三条：
① agent 新建文件的数量不可控，塞进用户对象库会污染仓库体积、拖慢用户的 `git gc`；
② 内容寻址天然去重（同一文件在多个检查点只存一份，已单测）；
③ 摘要即校验和，读回时比对，损坏立刻报错而不是静默恢复出一份错文件（已单测）。
**仍需补的一张表**：`checkpoint_untracked(checkpoint_id, path, sha256, size)`，
用于按快照引用计数、以及在保留策略里判断"哪些 blob 已经没有任何检查点引用"。

**D5 的落地形态（可自定义，但默认禁止）**：`diff()` 会把"当前有、目标无"的路径归入
`removed`。默认 `allow_delete=None` → 从设置解析 → 解析不出就是 `False`，
于是这些路径只回填到 `skipped_removed` 里报告，**不动手删**。
用户在设置里写 `allow_delete: true`（或 `"1"`/`"yes"`/`"on"`）才真正删除；
非法值一律当作没配置，落在"禁止"这一侧。所以默认能力边界仍是
"能还原被改/被删的文件，但不能撤销 agent 新建的文件"——这是安全的一侧。
开启删除后，删空的父目录会被一并清理并单独列在 `pruned_dirs` 里：
git 不跟踪空目录，不清理的话 `git status` 干净、用户却看到一堆空壳。
命中 `PROTECTED_GLOBS` 的路径（`.git`、`node_modules` 等）**永不删除**，
连"可选删除项"都不进。

**D7 的落地形态（可自定义，下限 4 GB）**：`Limits.resolve(settings)` 认
`max_file_bytes` / `max_checkpoint_bytes` 两项，支持 ``"4gb"``、``"8192 MB"``
这类人类可读写法（用户填了却被静默忽略是最难解释的失效）。其中
`max_checkpoint_bytes` 受硬下限 `MIN_CHECKPOINT_BYTES = 4 GB` 约束：
用户配 1 GB 会被抬到 4 GB，配 8 GB 就用 8 GB，未配置则按
`仓库对象体积 × 10` 自适应、同样不低于 4 GB。**旧设计的 4 GB 上限已被撤销**——
上界只会把用户明确要求的大预算静默砍掉；真正该防的是单文件失控
（`max_file_bytes`，默认 64 MB）与磁盘不足。

**D2 的实现要点（这是用户明确要求的，不能含糊）**：`git init` 时写入一条
`project_checkpoints` 记录 `kind='init_epoch'`，其锚点 commit 记为 `rollback_epoch`。
任何检查点只有 `created_at >= rollback_epoch.created_at` 才允许回滚；之前的检查点
**列表可见、详情可看、但回滚接口直接返回 409 `epoch_mismatch`**。
理由：那段时间的快照根本没有（仓库不存在），跨 epoch 回滚等于凭空构造工作区。

**D3 的直接后果**：检查点是纯本地的。这也让 §10.1 的密钥风险**降级**——不外传，
泄露面仅限本机 `.git`。但 `git add -A` 把 `.env` 写进对象的问题仍然要防（本机也可能被备份/同步工具带走）。

---

## 十四、六个安全性问题的答复（全部实测）

### 14.1 Q1：agent 误删未跟踪文件，如何安全回滚

**能救，但有个陷阱必须绕开。**

实测（`$TMPDIR/q1`）：仓库里放一个未跟踪的 `untracked_notes.txt`，用独立 index 建快照：

```
=== 快照里含未跟踪文件吗 ===
app.py
untracked_notes.txt          ← 在里面
=== 模拟 agent 删除该未跟踪文件 ===
删除后 ls: app.py elsewhere.txt
=== 从快照恢复 ===
恢复内容: MY SECRET NOTES    ← 完整恢复
```

对照实验（`$TMPDIR/q1b`，只 `add -u`）：快照里只有 `f.txt`，恢复未跟踪文件时报
`error: pathspec 'mine.txt' did not match any file(s) known to git` —— **`add -u` 的快照救不回未跟踪文件**。
这就是 D4 必须评估的实质：**不含未跟踪文件 = 撤销不了 agent 最需要撤销的那类操作**。

**陷阱：恢复方式会污染用户暂存区。** 用 `git checkout <tree> -- <path>` 恢复后实测：

```
恢复后 git status --porcelain:
A  untracked_notes.txt      ← 被塞进了用户暂存区！
?? elsewhere.txt
```

用户体验上，这等于"我只是点了回滚，我的 `git add` 列表被改了"。**必须改用不经 index 的写法**：

```bash
git cat-file --filters <tree>:<path> > <path>   # 取 blob 并过 smudge 过滤器
```

实测（`$TMPDIR/q1c`）：恢复成功且 `git status` 只有 `?? note.txt`，`git diff --cached` 为空 —— **零污染**。

**但这里还藏着一个更隐蔽的破坏：行尾会被静默改写。** 实测（`$TMPDIR/crlf3`，
`core.autocrlf=true`），工作区原文 `line1\r\nline2\r\n`：

```
git show <tree>:f.txt  > f.txt   →  line1\nline2\n      git status: [ M f.txt]
git cat-file --filters <tree>:f.txt > f.txt → line1\r\nline2\n   git status: []
```

`git show` 取的是**仓库内的 blob**（已被 `add` 规范化成 LF），不经过 smudge 过滤器；
`git cat-file --filters` 会应用 `.gitattributes`/`core.autocrlf` 的换行转换，还原成
工作区应有的字节。在 Windows 上这不是小问题：回滚会把**整个文件**的行尾改掉，
用户下次提交就是一次纯换行符的全文件 diff，且很难归因为"我刚才点了回滚"。

所以 §5.3 的路径级恢复要改成：**`git cat-file --filters` 写盘**，
而不是 `git checkout -- <path>`，也不是裸 `git show`。`git checkout` 仅保留给
"确实需要同时暂存"的场景。

**文件权限：Windows 上不要指望还原。** 实测（`$TMPDIR/q7`）对 `chmod +x run.sh`
的文件建快照，`git ls-tree -r` 里模式仍是 `100644` —— 因为 Windows 默认
`core.fileMode=false`，git **压根不记录可执行位**。所以：

- 本项目主要面向 Windows，**快照里没有权限信息，恢复时也无需（也无法）还原权限**。
  这一条反而简化了实现：不必写 `chmod` 逻辑。
- 若将来支持 macOS/Linux，需先确认 `core.fileMode=true`，再按 `ls-tree` 的 `100755`
  补 `chmod +x`。**该分支当前未实现、也未实测**，不要提前写进去。

### 14.2 Q2：如何防止检查点被改写/被 gc

分三层，**其中一层 git 帮不了，必须我们自己补**。

**① 内容篡改 —— git 自动检测（实测有效）**

直接改坏 blob 对象文件后（`$TMPDIR/q2`）：

```
$ git cat-file -p 626799f0...
error: inflate: data stream error (incorrect header check)
error: unable to unpack ... header
fatal: Not a valid object name 626799f0...

$ git fsck
error: 626799f0...: object corrupt or missing
missing blob 626799f0...
```

git 对象是 zlib 压缩 + 内容寻址，**改一个字节就读不出来**。这是密码学级别的完整性保护，
不需要我们额外做 checksum。但"读不出来"不等于"我们知道了"—— 要在回滚前主动
`git fsck --connectivity-only` 探一次，否则用户点回滚才发现对象坏了。

**② gc 回收 —— ref 命名空间已实证可护住（见 §4.4）**

`reflog expire --expire=now --all` + `gc --prune=now` 之后 ref 与对象完好（`SURVIVED_GC`）。

**③ ref 指向被改写 —— git 不管，这是真正的缺口**

实测（`$TMPDIR/q2b`）：把 `refs/chiron/cp` 用 `update-ref` 改指到别的 commit 后：

```
原 ref=34eb5f08...
现 ref=d8f05eeb...
REF_TAMPERED_DETECTED
=== reflog 能否还原原始值 ===
(空)
```

**关键发现：`refs/chiron/*` 根本没有 reflog。** `.git/logs/refs/chiron/` 目录不存在，
且实测 `git config core.logAllRefUpdates true` 之后**依然为空**——该配置只覆盖
`refs/heads`、`refs/remotes` 等标准命名空间，**不覆盖自定义命名空间**。

后果：ref 被病毒或误操作改写后，**git 侧无从还原**。所以必须：

- **DB 里的 `commit_sha` 是唯一权威**，ref 只是锚定对象、防 gc 的手段。
- 每次读检查点先比对 `git rev-parse <ref>` == `project_checkpoints.commit_sha`；
  不符则判定 `ref_drift`，用 DB 里的 sha **重新 `update-ref` 修复**。
- 对象也被删（`git cat-file -e` 失败）→ 该检查点标记 `lost`，UI 显示"此检查点已损坏"，
  **绝不假装能回滚**。

**自愈已实测（`$TMPDIR/q2b`）**：ref 被改指到 `d8f05eeb...` 后，

```
=== 对象本身还在吗 ===
commit                          ← DB 里的 sha 仍可读
=== 用 DB 里的 sha 自愈 update-ref ===
HEAL_OK
现 ref=34eb5f08...
REF_RESTORED_FROM_DB            ← ref 已按 DB 值还原
```

即 **ref 是"可再生的缓存"，DB 才是真相**。这也顺带解决了 §4.4 的取舍：
即便 ref 被误删，只要对象还在、DB 有记录，就能重建 ref 继续防 gc。
反向不成立 —— 若对象已被 `gc --prune` 清掉，`git cat-file -e` 返回 1（实测），
此时无论 DB 记什么救不回来，只能标 `lost`。

**注意"禁止重写"的边界**：CAS（`update-ref <ref> <new> ""`）只保证**我们不覆盖**。
它拦不住外部进程直接改写 ref —— 那是操作系统权限层面的事，git 没有钩子能防。
诚实结论：**我们能保证"自己永不重写"，能"事后侦测"外部改写并自愈，
但不能阻止外部改写本身。** 要更强只能靠文件 ACL 或对 `.git/refs/chiron` 设只读。

### 14.3 Q3：检查点创建失败，会话如何安全继续

**结论：失败必须 fail-open，绝不能阻断会话。**

实测三种失败场景：

| 场景 | 实测行为 |
| --- | --- |
| 非 git 目录 | `read-tree` / `add` 均报 `fatal: not a git repository` |
| 存在 `.git/index.lock` | **不影响**：`add_exit=0`（我们用的是 `GIT_INDEX_FILE` 指向的独立 index，锁的是 `.git/index`，两者无关） |
| 只读对象库 | Windows 上 `chmod -w` 不生效，**此场景未能真正复现**，不能作为已验证结论 |

第二条是有价值的结论：**并发/崩溃残留的 `index.lock` 不会卡住我们的快照**，
因为快照全程走独立 index 文件。这是 §4.1 设计带来的额外好处。

设计：

1. **检查点失败不抛给对话层**。捕获后写一条 `project_checkpoints(state='failed', error_code, error_msg)`，
   **返回成功**让对话继续。用户顶多在时间线上看到一个灰色失败节点。
2. **连续失败降级**。同一会话连续 3 次失败 → 项目级 `checkpoint_state='degraded'`，
   时间线顶部挂一条黄色横幅"检查点暂不可用，回滚保护已关闭"，**明确告知失去保护**，
   而不是静默让人以为还能回滚。
3. **失败时的最小保护**：仍调用 `undo_stack.py`（§5.0）记录单文件快照。
   它是纯 Python 写盘，不依赖 git，成功率更高 —— 这条兜底路径该在 P1 就接上。
4. **对话绝不因检查点失败而回滚或中止**。检查点是"安全保障"，不是"功能前置条件"。

D1/D2 在此处落地：项目 `non_git` 态下所有检查点操作都是 no-op + 提示，
`init_epoch` 之前的时间线节点渲染为"该时段无回滚能力"。

### 14.4 Q4：如何保证回滚的安全唯一祖先

**这里有一个会直接毁掉整条链的设计错误，实测抓到了。**

若检查点以 **HEAD** 为父（`$TMPDIR/q4b`）：建 CP1 → **用户自己提交一次** → 建 CP2：

```
新 HEAD=e17ab638...
C2=12a6ab1a...
=== CP1 还是 CP2 的祖先吗（关键）===
CHAIN_BROKEN             ← 链断了
```

原因：CP2 的父是用户的**新** HEAD，而 CP1 挂在**旧** HEAD 上，两者不在同一条线上。
检查点链一旦断开，"唯一祖先"就无从谈起，跨检查点的 diff 也会误报成"全部文件新增"。

**正确做法（`$TMPDIR/q4c` 实测通过）**：检查点以**上一个检查点**为父，
把当时的 HEAD 写进 commit message 的 trailer：

```bash
git commit-tree "$TREE" -p "$PREV_CHECKPOINT" \
  -m "chiron checkpoint $SEQ ($KIND)" \
  -m "Chiron-Head: $CURRENT_HEAD"
```

```
=== C1 是 C2 的祖先吗 ===
ANCESTOR_YES
=== trailer 里能取回当时的 HEAD 吗 ===
Chiron-Head: f8a7951e...
```

这样检查点自己构成一条**线性链**，与用户提交完全解耦，用户怎么提交都不会断链。

配套三条硬规则：

1. **回滚前校验祖先**：`git merge-base --is-ancestor <target> <current>` 双向测。
   实测非祖先时正确返回非零（`NOT_ANCESTOR_OK`）。
2. **只允许回到更早的检查点**：目标 seq 必须 `<` 当前 seq，禁止"向后回滚"。
   否则等于用旧状态覆盖用户的后续成果。
3. **回滚本身也建检查点**：`kind='pre_rollback'`，父也指向上一个检查点 → 回滚可再回滚，
   链永不出现孤立节点。这是"唯一祖先"的闭环保证。

### 14.5 Q5：用户提交了脏内容（.env / node_modules / .idea），检查点如何保持清洁

**能剔除，但剔除动作本身制造了一个新的误删风险 —— 必须同时修。**

实测（`$TMPDIR/q5`）：用户已经把 `.env`、`node_modules/dep.js`、`.idea/workspace.xml`
**提交进 HEAD**。我们建快照时 `git rm -r --cached` 剔除它们：

```
用户 HEAD 树:  .env  .idea/workspace.xml  app.py  node_modules/dep.js
剔除后 write-tree:  app.py                ← 树干净了
用户 HEAD 是否被改:  .env .idea/workspace.xml app.py node_modules/dep.js  ← 原样未动 ✓
```

**关键副作用**：剔除后拿这棵树去和 HEAD 比，差异是：

```
D	.env
D	.idea/workspace.xml
D	node_modules/dep.js
```

`D` 在 §5.2 的语义里是"待删除"。**若按这棵树做全量恢复，会删掉用户的 `.env`！**
这正是"为了清洁反而毁掉用户数据"的典型失败。

**修法（实测有效）**：引入**保护列表（protected paths）**，它与密钥黑名单是同一份数据，
但用途相反 —— 不是"不进快照"，而是"**绝不允许被删除**"。过滤差异后：

```
git diff-tree -r --name-status HEAD "$CLEAN" | rg -v "^D\s+(\.env|node_modules/|\.idea/)"
（空输出）
```

即过滤后不存在任何待执行删除，`.env` 安全。

**agent 如何兜底（用户特别问的一点）**：agent **不该知道脏内容的存在**。
层序是这样：

1. **快照层静默剔除**：`node_modules/`、`.idea/`、`__pycache__/`、`dist/`、`build/`、`target/`
   属于"噪声"，剔除后**不提**，因为提了只会让非技术用户困惑。
2. **敏感层必须提示**：`.env`、`*_rsa`、`*.pem` 属于密钥，剔除后**必须**在时间线上留一句
   "已跳过 1 个疑似密钥文件"。用户需要知道自己的密钥**没有**被保护进检查点。
3. **agent 侧建议而非代改**：`read_file` 试图读取 `.env` 时直接拒绝（现有 `fs_guard`
   已有路径策略，扩展一条即可）；`write_file` 写 `.env` 时提示"建议改用环境变量管理"。
   **不自动改用户的 `.gitignore`，不自动 `git rm` 用户的已提交文件** —— 那是用户的仓库。

**一句话**：检查点的清洁性由我们保证，但清洁手段**永远不能反过来破坏用户工作区**。
保护列表就是这两者的隔离带。

### 14.6 Q6：回滚失败怎么处理

**核心原则：宁可什么都没改，也不要改一半。**

实测（`$TMPDIR/q6`）验证了"先暂存、后落地"两阶段法的可行性：先把所有目标文件
`git cat-file --filters` 到临时目录，**全部成功**再逐个 `mv` 到工作区。

```
=== 两阶段：先把目标内容取到暂存目录 ===
stage keep OK / stage locked OK / stage normal OK
--- 暂存目录内容（全部成功才动工作区）---
keep.txt  locked.txt  normal.txt
```

**落地阶段确实会部分失败（已取得真机样本）。** 之前用 `chmod -w` 没能造出来，是因为
Git Bash 的 `chmod` 在该文件系统上不真正禁止写；改用 `chmod 444` + `cp` 后实测
（`$TMPDIR/land1`）：

```
暂存 OK: free.txt locked.txt          ← 两阶段里的第一阶段全部成功
free.txt 落地 OK
cp: cannot create regular file 'locked.txt': Permission denied
locked.txt 落地退出码=1
locked 内容(应仍 BROKEN): BROKEN      ← 已落地的改了，没落地的保持原样
```

即**「一半成功、一半失败」是真实可达状态**，不是理论推演。这恰好说明两阶段法的价值：
失败被压缩到落地这一步（暂存阶段先暴露对象损坏/磁盘满），且能用清单精确报告。
方案按最坏情况设计：

| 失败点 | 处理 |
| --- | --- |
| **暂存阶段失败**（对象损坏/磁盘满/权限） | 工作区**一个字节都没动**，直接报错。这是绝大多数失败 |
| **落地阶段部分失败** | 逐文件报告已改/未改清单；已改的**不回滚**（回滚失败再反转风险更大），提示用户"部分文件已恢复，N 个失败"。此时 `pre_rollback` 快照已在手，可再次尝试 |
| 回滚后校验失败 | 重算工作区 tree 与目标 tree 比对；不符则标记 `rollback_incomplete` 并保留 `pre_rollback`，**不删除任何 rescue 快照** |

三条不可妥协的规则：

1. **回滚前必建 `pre_rollback` 快照**，且**回滚失败时绝不删它**。失败的东西不可怕，
   没有退路才可怕。
2. **dry_run 强制先行**。API 要求第一次必须 `dry_run=true`，把三分类差异（新增/修改/删除）
   完整展示，用户确认后才执行。§9.1 已规定，此处强调它是**失败率的头号降低手段**。
3. **失败不静默**。`rollback_state` 落库（`ok` / `partial` / `failed`），
   前端必须展示，而不是 toast 一闪而过。

---

## 十五、多会话并行（并发写入与回滚冲突）

前置问题有三个，**必须拆开回答**，混在一起会得出错误结论：

1. **并发写入**：两个会话同时改同一份文件，怎么不互相覆盖？
2. **效率**：串行化会不会把并行变成排队？
3. **回滚冲突**：会话 A 回滚到某个检查点，会不会毁掉会话 B 的成果？

### 15.1 实测：worktree 能解什么，不能解什么

**先给结论**：worktree **能**解决并发覆盖，**不能**解决"用户看到文件被改"，
**也与分支不可重写无关**。它是一把很锋利的刀，但不该做默认。

实测环境在 `$TMPDIR/wtA/`：`main` 是主工作区，`sessB` 是 `git worktree add ../sessB -b chiron/sessB` 出来的。

**① 工作区文件真隔离（能做到）**

```
主工作区 a.py = a-v1
sessB    a.py = B-EDIT          ← 在 sessB 里改，主工作区没动
```

**② 回滚也隔离（能做到）**

在 `sessB` 里改坏再 `git cat-file --filters <tree>:a.py > a.py` 回滚：

```
sessB 回滚后: B-v2
主工作区受影响吗（应仍是 a-v1）: a-v1
```

**③ 对象库与 ref 是共享的（这既是优点也是耦合）**

```
sessB/.git 是【文件】不是目录 → gitdir: .../main/.git/worktrees/sessB
在 main 里写 refs/chiron/checkpoints/s1/cp1，在 sessB 里读得到：5715c0d5...
```

所以：**快照对象只存一份，不随会话数膨胀**。这是 worktree 相比"复制整个仓库"的决定性优势。

**④ git 自身保证同一分支只有一个工作区（白拿的护栏）**

```
$ git worktree add ../sessC chiron/sessB
Preparing worktree (checking out 'chiron/sessB')
fatal: 'chiron/sessB' is already used by worktree at '.../wtA/sessB'
```

这条很有价值：**同一分支不可能被两个 agent 同时检出**，天然的互斥，不需要我们自己写锁。

**⑤ worktree 解决不了的部分（必须承认）**

| 问题 | worktree 能否解决 | 说明 |
| --- | --- | --- |
| 两会话改同一文件互相覆盖 | ✅ 能，物理隔离 | 但冲突只是**被推迟**到合并时 |
| 用户在自己的目录里看不到 agent 改的文件 | ❌ 不能，反而更糟 | 文件在 `sessB/` 子目录里，小白用户会以为"改动丢了" |
| 分支不可重写 | ❌ 无关 | 仍是 §4.2 的 CAS 负责，worktree 一点忙帮不上 |
| 每份副本的磁盘与初始化成本 | ❌ 反而增加 | `node_modules`、`.venv` 不共享，各装一遍 |
| xerj 索引 | ❌ 成本上升 | 见 15.3，一前缀一项目，多 worktree 要多前缀 |
| 冷启动 2.4s 快照 | ❌ 每个新 worktree 独立冷启动 | 新 index 没有 stat 缓存 |

### 15.2 实测：共享工作区的并发真实风险

不隔离时到底会坏成什么样？实测（两会话各写同一文件 200 次，无应用层锁）：

```
最终内容: B-200
git status --porcelain:  M sharedfile
```

即**后写者完全覆盖先写者，且事后无从分辨**——git 只看到一个 `M`，
既不知道发生过并发，也不知道谁的写入被丢了。这是最坏的一类 bug：**静默丢失**。

> 说明：这个实验原打算在 `$TMPDIR` 沙箱里跑，但后台子 shell 的 `cd` 没生效，
> 实际落在了仓库根目录，留下一个未跟踪文件（已移出）。**结论（后写者覆盖、
> 事后不可分辨）不受影响**，因为它只取决于两个进程写同一个路径这一事实。
> 但也因此，我没有取得"N 次写入中丢了几次"的量化数字，故不列具体丢失率。

**关键推论**：共享工作区模式下，**必须由应用层串行化写入**，不能指望文件系统或 git 兜住。

### 15.3 效率：串行化会不会拖慢

这是设计取舍的核心。三个数字摆在一起看：

| 操作 | 实测耗时 | 是否需串行 |
| --- | --- | --- |
| 热态快照（1530 文件，index 持久化） | **179ms** | 否（见下） |
| 冷启动快照 | 2.4s | 否 |
| 文件写入（`write_file`/`edit_file`） | 毫秒级 | **是** |

**快照不需要串行化**，这有实测支撑：index 放在**工作区外**时，10 轮 × 2 个独立 index
并发 `add -A` + `write-tree`，得到**唯一 tree 数 = 1**（完全稳定）。

反过来，index 放在工作区内会彻底失效：

```
第一次=fae1980a7acb20b09395f4e4ac75dac4e51ad1ac
第二次=2584d2baa036e16acf88f67b5726d5ac6d77788e   ← 变了
ls-tree 内容: a.py / in.i / in.i.lock              ← index 把自己也收进去了
```

**所以 §4.1 的"index 放项目外"不只是防止 xerj 索引它，它是并发安全的前提。**

真正需要串行的是**写文件**那一步（毫秒级）。用**路径级互斥**而不是全局锁：
两会话改不同文件时完全并行，只有同路径才排队。粒度这么细，排队时间可忽略。

### 15.4 回滚冲突：怎么判定、怎么解

设会话 A、会话 B 共享同一工作区，各自有检查点链。会话 A 要回滚到 `C_A(k)`。
危险在于：**B 在 `C_A(k)` 之后改过的文件，会被 A 的回滚抹掉**。

**判定不需要猜，git 直接给答案**（实测 `$TMPDIR/mt3`，用 `commit-tree` 构造真分叉）：

```
$ git merge-tree --write-tree "$A" "$B"
1dc12c88...
100644 f0f23074... 1	f.py
100644 3131a24f... 2	f.py
100644 80909c34... 3	f.py

Auto-merging f.py
CONFLICT (content): Merge conflict in f.py
exit=1
```

无冲突时 `exit=0` 且只输出合并后的 tree sha。所以：

- **`merge-tree --write-tree` 是"这次回滚会不会打架"的唯一权威判定**，
  纯对象运算，**不碰工作区、不产生 merge 状态、不需要临时分支**。
- 它比"文件路径交集"更准。路径交集只是粗筛（实测两会话都改 `f.py` 时交集非空，
  但 A 改第 1 行、B 改第 3 行时其实能自动合并）；`merge-tree` 能区分这两种情况。

**分三类处理**：

| 判定结果 | 处理 |
| --- | --- |
| `merge-tree` exit=0 且无路径交集 | 直接回滚，无需打扰用户 |
| exit=0 但有路径交集（改动可自动合并） | 回滚，并在时间线标注"已合并其他会话的改动" |
| **exit≠0（真冲突）** | **拒绝自动回滚**，把冲突文件清单给用户，三选一：只回滚不冲突的文件 / 先让另一会话提交或放弃 / 取消 |

**第三种不能自动解决，这是刻意的**：agent 自动解冲突等于替用户做代码决策，
而"回滚"这个动作的语义恰恰是"退回一个已知好的状态"。自动合并出来的东西
既不是回滚前、也不是回滚后，是个第三种状态 —— 那就不该叫回滚。

### 15.5 推荐架构：默认共享 + 按需 worktree

不给用户抛一个"选哪种模式"的技术题，而是**按场景自动分流**：

| 场景 | 方案 | 理由 |
| --- | --- | --- |
| 单个会话（绝大多数） | 共享工作区 + 路径级写锁 | 用户看得见文件变化，无额外磁盘成本 |
| 用户显式开"并行会话" | **每会话一个 worktree** | 物理隔离，无锁竞争，`git` 保证分支唯一 |
| 只读分析类会话（如代码问答） | 共享工作区，**不加锁** | 不写就不需要锁 |

落地时必须同时处理的四件事（否则 worktree 会带来新问题）：

1. **告诉用户文件在哪**。并行会话的 UI 必须显式标出工作目录（如 `项目/sess-2`），
   并给"把改动合并回主工作区"的一键入口。否则小白用户会认为改动丢了。
2. **`node_modules` / `.venv` 不共享**。首次进并行会话要提示"需要重新安装依赖"，
   或者用符号链接复用（**此项未实现、未实测，列为待验证**）。
3. **xerj 索引要按 worktree 分前缀**。既有文档 §3.2 的硬约束是"一个前缀一个项目"，
   多 worktree = 多前缀。**不能让两个会话共用一个前缀**，否则回滚后的重索引会互相踩。
4. **worktree 的生命周期**。会话结束/归档时要 `git worktree remove`，
   否则 `.git/worktrees/` 与磁盘会一直堆积。检测到残留要给清理入口。

### 15.6 会话状态冲突：三层各自的归属

"会话状态冲突"其实有三层，它们的冲突面和解法完全不同：

| 层 | 存储 | 冲突可能 | 解法 |
| --- | --- | --- | --- |
| **对话状态** | `messages` / `session_rewinds`，按 `session_id` 隔离 | **无**：两会话各写各的行 | 不需要任何锁（§六 的追加式设计已保证） |
| **代码状态** | 用户工作区（共享或 worktree） | **有**：同路径写覆盖 | 路径级写锁 + `merge-tree` 冲突判定 |
| **索引状态** | xerj 前缀（一项目一前缀） | **有**：A 回滚触发重索引，B 的检索短暂不一致 | 重索引期间标 `stale`，读时降级而非报错（§7.2） |

对话状态这层**天然无冲突**，因为 §六 已定为只追加、按 `session_id` 分区。
这也反过来证明了"不删 `messages`"的价值：**追加式设计顺手解决了多会话并发**。

索引那层需要一句明确的话：**索引是工作区的函数，不是会话的函数**。
共享工作区时两个会话看到同一份索引，这是对的（它们看的是同一份代码）；
worktree 模式下才需要各自的前缀。

### 15.7 一个诚实的边界

`merge-tree --write-tree` 需要 Git 2.38+。本机是 `git version 2.45.1`，可用。
更老的 git 只能退化为"路径交集 + 拒绝交叉回滚"，精度下降但不会出错。
**版本探测要做，降级路径要有，不能假设用户机器够新。**

---

## 十六、盲区清单（重新审视后补充）

前面各章讲的是"我们怎么做"，这一章讲**我们做不到什么**。每一条都有实测支撑，
或明确标注为推断。写在这里是因为：这些边界如果不在设计阶段写清，就会在实现阶段
被当成 bug 反复"修"，最后用一种更糟的方式（比如用 `read-tree --reset -u` 强删）去凑。

### 16.1 实测确认「回滚救不回来」的情形

| 情形 | 实测结果 | 后果 | 处置 |
| --- | --- | --- | --- |
| **空目录** | `mkdir -p emptydir/nested` + 一个文件，`ls-tree -r` 只有 `a.py`；删掉后恢复仍是 `EMPTY_DIR_NOT_RESTORED` | agent 删了空目录，回滚后目录不回来 | **明确告知**：空目录不在保护范围内。若确需，另存一份目录清单陪衬（成本低但不在 P0.5） |
| **嵌套 git 仓库 / submodule** | `vendor` 在树里是 `160000 commit …`；`git show <tree>:vendor/lib.py` 报 `exists on disk, but not in <tree>` | submodule 内部文件**完全不在快照里** | UI 上把这类路径标为「外部仓库，不参与检查点」。**不要假装能回滚** |
| **symlink** | Windows 上 `ln -s` 落在树里是 `100644 blob`（存目标内容），`git show` 取出的是文件内容 | 回滚会把符号链接**变成普通文件** | Windows 上本就不记录链接语义；需在支持链接的平台上单独确认（**未实测**） |
| **大小写改名** | `Foo.py` → `foo.py` 在 `diff-tree` 里是 `D Foo.py` + `A foo.py` | 回滚会**同时**恢复 `Foo.py` 并保留 `foo.py`，出现两个文件 | 需要 rename 检测（`diff-tree -M`）把它折叠成一次改名；否则用户会看到"多了一个文件" |
| **只读文件** | `chmod 444` 后落地 `cp` 报 `Permission denied`，工作区处于半改状态 | 回滚部分失败 | 两阶段 + 逐文件清单（§14.6）已在设计里 |
| **特殊字符路径** | 默认 `ls-tree` 输出 `"\344\270\255\346\226\207.py"`（八进制转义） | 按行解析得到的路径是错的，写盘会写到奇怪文件名 | **解析一律用 `-z`**（NUL 分隔），见 16.2 |

> 前四条的共同点：**它们是"git 不记录的东西"**，而不是实现缺陷。
> 设计上唯一正确的选项是**如实缩小承诺范围**，而不是事后补救。

### 16.2 解析层陷阱：`-z` 不是可选项

实测（`$TMPDIR/qp`）同一棵树三种输出的差别：

```
默认:            "\344\270\255\346\226\207.py"     ← 无法直接当路径用
core.quotepath=false:  中文.py                      ← 可读，但仍按行分隔，路径含换行时崩
-z:              中文.py\0                          ← 唯一无歧义
```

**所有 `ls-tree` / `diff-tree` 的输出解析都必须走 `-z`**，不能用"按 `\n` 切分再 trim"。
文件名里可以合法包含换行；`quotepath` 的转义规则又只在 `-z` 下才完全关闭。
这一条要在 P0.5 的单测里锁死（造一个中文名 + 空格名 + 引号名的仓库）。

### 16.3 回滚会让增量构建全线失效（真实 UX 代价）

git 不保存 mtime。任何恢复手段（`cat-file --filters`、`checkout`）写出来的文件
mtime 都是**当前时间**。于是回滚之后：

- `tsc --incremental`、`vite`、`webpack`、`cargo`、`go build` 的缓存全部认为源文件变了；
- 用户看到的是"我只是回滚了一个文件，怎么整个项目重新编译了 3 分钟"。

**这条是推断，未做大项目实测**。但它几乎必然发生，所以 UX 上要提前说明，
并且**只恢复差异文件**（不重写全树）——这一点 §5.2 的三分类差异设计已经保证了。

### 16.4 撕裂读：快照可能捕获到写了一半的文件

快照在 `add -A` 时读取工作区文件。如果此刻另一个进程正在写同一个文件
（用户 IDE 保存大文件、另一个会话的 agent 正在 `write_file`），`git add` 读到的
**可能是半截内容**，于是检查点里存了一个语法上不完整的文件。

- 正常流程下风险低：检查点只在回合边界触发，那时 agent 已停止写。
- 但**多会话并行 + 共享工作区**时风险真实存在（§15 的路径级写锁只锁 agent 自己，
  锁不住用户的编辑器）。

处置（按代价从低到高）：① 记录写入方是自己还是外部（我们自己的写有账本）；
② 快照前对"最近 N 秒内 mtime 变化过的文件"做一次二次读取比对，不一致则延后重试；
③ 把这一条写进 §15 的已知限制，不假装不存在。

### 16.5 对象库与用户共享的长期代价

检查点对象进的是用户 `.git/objects`（这是 §4.2 的选择：借 git 的 gc 与完整性校验）。
代价必须说清：

- `git gc` 会连带打包我们的对象，**用户的 `git gc` 变慢**；
- 用户 `git count-objects -v` 看到的体积包含我们的数据，容易误判"仓库怎么这么大"；
- 用户若执行 `git gc --prune=now`（有真实用户会这么干），**未引用的检查点对象直接消失**。
  ref 能保住被引用的那部分（§14.2 已实测 `SURVIVED_GC`），但已删 ref 的救援不了。

替代方案（外置 object 目录 + alternates）**实测不成立**：alternates 里写相对路径时
git 报 `unable to normalize alternate object path`，跨盘符场景需要额外处理。
结论：**接受共享 `.git`，但把体积上限做成项目级可配置，并在设置页显示"检查点占用"**。

### 16.6 项目根 ≠ 仓库根（这条会直接让路径算错）

`session_map_workspaces.root_path` 可以是 monorepo 的**子目录**。此时：

- 在子目录里 `git add -A` 只覆盖该子目录及以下（git 2.0+ 的语义），
  `write-tree` 得到的是**子树的 tree**，不是仓库根 tree；
- 于是 `git cat-file --filters <tree>:<path>` 里的 `<path>` 必须相对**项目根**而不是仓库根，
  两者混用就会报 `path exists on disk, but not in <tree>`（§16.1 的 submodule 报错就是同一句话）；
- `git diff-tree` 比较两棵树时也必须同基准，否则会把整个仓库误判成"全部新增"。

**设计约束**：检查点统一以 **项目根为 tree 基准**；所有路径参数一律相对项目根，
并在拼接前断言项目根在仓库内（`git rev-parse --show-prefix`）。
用户后续把 `root_path` 从子目录改成仓库根（或反之）时，**旧检查点全部标为不可回滚**，
因为基准变了 —— 这与 D2 的 `init_epoch` 是同一类问题，应复用同一机制。

### 16.7 DB 与 git 的双写：哪一半失败都别装作成功

写检查点时有两个不可原子化的载体：git 对象（先）与 PG 行（后）。

| 失败点 | 状态 | 处置 |
| --- | --- | --- |
| commit 建好，DB 写失败 | 孤儿 commit（无 ref、无行） | 无害，靠 gc 自然回收；只记日志，不重试到成功 |
| DB 行写好，ref 写失败 | 行存在但无 ref 锚定 | **下次读写时按 DB 的 `commit_sha` 补建 ref**（自愈，§14.2 已实测） |
| ref 建好，DB 已回滚/删除 | 无主 ref | 清理任务比对 DB，删掉无对应行的 ref |

单向权威链必须是：**DB 行 → commit_sha → ref**。反过来（ref 权威）已经被 §14.2 否掉了。

### 16.8 回滚之后，agent 的对话上下文已经失配

这是最容易被忽略、影响却最直接的一条：用户回滚了 3 个文件回上一回合，
但那 3 个文件的**新内容仍在对话历史里**（agent 上一回合刚读过/写过）。
下一次对话时，agent 会基于"已经被撤销的改动"继续推理。

处置：回滚成功后**自动注入一条系统消息**
（例如「工作区已回滚到 14:32 的检查点，以下文件已还原：…，请重新读取后再修改」），
并在读取类工具上加一层"文件 mtime/内容与上次读取不一致则重读"的检查。
**不要依赖模型自己意识到这件事**。

### 16.9 决策状态

| 编号 | 问题 | 结论 |
| --- | --- | --- |
| **D4** | 快照是否包含未跟踪文件 | **包含**。用自有 sha256 分片 CAS，不进用户 `.git/objects`。清单表待 P1 补 |
| **D5** | 是否允许"删除型回滚" | **可自定义，但默认禁止**。`RollbackPolicy.from_settings()`；非法值落在"禁止" |
| **D6** | 项目根 ≠ 仓库根 | **当前仅适配单仓库目录**：项目根必须等于仓库根，否则不启用检查点 |
| **D7** | 检查点体积上限与清理阈值 | **可自定义，下限 4 GB**。`Limits.resolve()`；用户设置只能往上调 |

D4–D7 已回填至 §十三 决策表。**D4 选"包含"之后，§14.1 整节的承诺成立**：
agent 误删新建文件可以回滚（走 CAS，不依赖用户是否 `git add` 过）。

落地时（P0.5）新发现并已修的三条缺陷，记录在此以免后来者重犯：

1. **已提交的密钥会随 `read-tree HEAD` 漏进树** —— pathspec 排除只管"这次 add 的路径"，
   管不到索引里已有的条目。必须额外 `update-index --force-remove`。（单测：
   `test_committed_secret_is_removed_from_tree`）
2. **用户已 `git add` 的新文件三处机制都不管** —— `read-tree HEAD` 没有它、`add -u`
   不认它、`ls-files -o` 不列它，结果整个消失。必须从用户自己的 index 按
   `diff --cached --diff-filter=A HEAD` 取回。（单测：`test_staged_new_file_is_included`）
3. **已跟踪的超限文件同样漏进树** —— 与第 1 条同一机理，摘除条件里要一并带上
   `p in oversized`。（单测：`test_oversized_tracked_file_is_excluded`）

另有一条容易写错、且**静默失效**的：`rel.lstrip("./")` 按字符集剥离，会把 `.env`
变成 `env`，于是 Python 侧的密钥排除全部失灵，而 git 侧照常排除 —— 清单与树对不上
却毫无报错。（单测：`test_is_excluded_does_not_eat_dot_prefix`）
