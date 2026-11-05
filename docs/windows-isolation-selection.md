# Windows 执行隔离：选型调研与结论

> 调研对象：`vendor/DeepSeek-Reasonix`（一个已发布的成熟开源 agent，含完整 Windows 沙箱实现与 100+ 测试）
> 目的：为 chiron 的 code agent 模式确定「项目根授权 / shell 执行 / Low IL」三项技术选型
> 状态：**待用户确认**，本文不含改动

---

## 一、Reasonix 的三层架构

它把「能不能做」和「做得到哪」拆成两个独立问题：

| 层 | 包 | 职责 |
|---|---|---|
| 策略 | `internal/safety/permission` | 纯函数规则求值，**无 I/O**，决定 Allow / Ask / Deny |
| 审批 | 同上（`Gate` + `Approver`） | 把 `Ask` 交给真人；无人接手时的兜底 |
| 强制 | `internal/safety/sandbox` | OS 级 jail：策略允许了，也出不了圈 |
| 判定 | `internal/safety/shellsafe` | 命令是否只读（策略与证据层共用同一张表） |

**关键设计**：`permission` 包的 Policy 是纯的（`permission.go:1-6` 包注释明说），所以规则可以单测到穷尽，而与「Ask 如何被解答」解耦。

### 1.1 三值决策，保守默认

```go
const ( Allow Decision = iota; Ask; Deny )

// ParseDecision: 未知 / 空输入 → Ask
func ParseDecision(s string) Decision {
    switch strings.ToLower(strings.TrimSpace(s)) {
    case "allow": return Allow
    case "deny":  return Deny
    default:      return Ask   // ← 保守
    }
}
```

### 1.2 无人值守 = 一律拒绝（与 super-agent 接管直接相关）

```go
// unattendedAsk 回答一个没有 approver 的 Ask。
// Autonomy 是「没人看着时的姿态」，不是「替人做决定的许可」。
// 一个没人回答的问题就是「不行」—— 否则这个姿态就在替缺席的人做答，
// 而 YOLO（构造时根本没有 approver）会把每一条都静默放行。
func unattendedAsk(toolName string, args json.RawMessage) (bool, string, error) {
    if subjectRequiresHuman(toolName, Subject(args)) {
        return false, "this decision needs a person, and no approver is attached to this session", nil
    }
    return true, "", nil
}
```

必须真人的四类（`write_fence.go`）：

| 工具 | 为什么只有人能答 |
|---|---|
| `extend_write_paths` | 移动围栏本身，不是「放行一次写」 |
| `network_egress` | 出站到未声明主机 |
| `paid_search` | 只有用户能同意被计费 |
| `mcp_signin` | 目标主机是 MCP server 选的 |

> **对 chiron 的直接启示**：接管期间无人在场，遇到这四类必须拒，不能沿用 `tools_mode='auto'` 静默放行。chiron 现有 `safe_tools_mode()` 已把 `yolo` 降为 `auto`，方向对，但 `auto` 仍会自动放行——需要补一条「无人值守白名单外的工具一律拒」。

### 1.3 只读判定的两个问题

`shellsafe` 明确区分：

- **表成员**回答「这个程序能不能只读」→ `ReadOnlyCommands`（cat/ls/grep/rg/git status...）
- **参数规则**回答「这一次调用是不是只读」→ `ArgsMakeReadOnlyCommandWrite`（`find -delete`、`sort -o`、`git tag v1.0`）

注释点明了为什么必须分开：`sed`/`awk` 能通过脚本语言写入，所以**故意不在只读表里**。

### 1.4 强制人工的六种命令形态

`BashApprovalBlocker` 枚举的是「静态分析读不懂」的形状：

| 枚举 | 含义 |
|---|---|
| `Unparsable` | 根本解析不了，什么都不能信 |
| `NestedExecution` | `$(...)` / `` `...` `` / `<(...)` |
| `DynamicName` | 命令名是变量 |
| `InlineCode` | `python -c` / `node -e` / `bash -c` |
| `IndirectExecution` | `eval` / `source` / `xargs` / `find -exec` |
| `HereDocBody` | here-doc 的 body 是文件内容，不是参数 |

> 这张表可以**直接照搬**成 chiron 的 `tools_mode` 判定输入。

---

## 二、Windows 强制机制的真实实现

### 2.1 不是 AppContainer，是受限令牌 + 合成 SID + DACL

`win_launch_windows.go:73-104`：

```go
// restrictedToken 造出 WRITE_RESTRICTED 令牌：除 traverse 检查外所有特权移除
// （backup/restore 会绕过访问检查），restricting SIDs 只留
// 合成 SID、logon SID、Everyone；只有被授权的根目录带合成 SID。
// 注意：delete 或 rename 不查询 restricting SIDs，所以不受此约束。
func restrictedToken(sidText string) (windows.Token, error) {
    list := []sidAndAttributes{{sid: synthetic}, {sid: logon}, {sid: everyone}}
    procCreateRestrictedToken.Call(cur, disableMaxPrivilege|writeRestricted,
        0, 0, 0, 0, len(list), &list[0], &out)
    setDefaultDACL(out, synthetic, system, user.User.Sid)
}
```

授权掩码（`win_grants_windows.go:15`）：

```go
// 读/写/执行/删除，但**不含 WRITE_DAC 与 WRITE_OWNER** ——
// 合成 SID 是写边界，它不能让命令改写用户自己文件在根目录里的 ACL。
const grantMask = FILE_GENERIC_READ | FILE_GENERIC_WRITE |
                  FILE_GENERIC_EXECUTE | 0x10000 | 0x40
```

两个防护细节值得抄：

1. **`refuseLink(root)`**：root 是 junction/symbolic link 就拒绝授权，否则 SID 落到别处。
2. **`~0.7s / 20,000 files`** 的实测开销 ⇒ 授权是「一次」而非每次执行。

进程树收束：`CREATE_SUSPENDED` → `AssignProcessToJobObject` → `ResumeThread`，Job 设 kill-on-close。

### 2.2 默认关闭，理由写得很清楚

```go
// ConfinesByDefault (windows): false
// Windows 有后端，但只有用户打开 windows_integrity 后才运行，
// 所以默认配置下不强制。

func (c *Config) BashModeForGOOS(goos string) string {
    if goos == "windows" {
        if c.Sandbox.WindowsIntegrity { return "enforce" }
        return "off"
    }
    ...
}
```

配置项注释（`config.go:903-907`）：

```go
// WindowsIntegrity: 命令跑在写受限的 Low 完整性令牌下，
// workspace 与 cache 根目录被一次性授予（**在你磁盘上留下 ACL 条目**，
// 用 `reasonix sandbox` 列出）。默认关，且只限用户级配置；
// 它约束写入，不约束读取和网络。
```

`BashModeUnderOverride` 进一步堵死「调用方自作主张」：

```go
// Windows 上，enforce 请求只有在用户打开 windows_integrity 后才被采纳：
// 在那里强制会编辑用户文件夹的权限，调用方不得以自己的名义这么做。
if override == "enforce" && runtimeGOOS == "windows" && !c.Sandbox.WindowsIntegrity {
    return c.BashMode()
}
```

### 2.3 enforce 但未就绪 → fail closed，不降级

```go
func bwrapFaultFor(spec Spec) error {
    if spec.Mode != "enforce" { return nil }
    if spec.GrantFault != nil { return spec.GrantFault }
    if spec.GrantSID == "" { return ErrWinNotPrepared }  // ← 拒绝执行
    return nil
}
```

### 2.4 它自己承认的边界

| 边界 | 出处 |
|---|---|
| **删除、重命名不受 restricting SIDs 约束** | `win_launch_windows.go:75-76` |
| Windows 无 OS 级 shell 沙箱（多处） | `docs/ACP.md:229`、`docs/GUIDE.md:1409` |
| 只约束写，不约束读与网络 | `config.go:903-907` |
| 无网络隔离后端 | `seatbelt_windows.go`: `EgressSupported() = false` |
| Git for Windows bash 在受限令牌下**起不来**（`couldn't create signal pipe, Win32 error 5`） | `shell_confine_windows.go` 注释 |

最后一条是硬伤：它必须先探测 bash 能否在受限令牌下启动（`BashConfine` + `startsConfined`），起不来就跳过强制。

---

## 三、chiron 现状对比

| 维度 | Reasonix | chiron（当前） |
|---|---|---|
| 降权手段 | 受限令牌（restricting SIDs）+ 合成 SID | `SetTokenInformation(TokenIntegrityLevel, LOW)` |
| 授权方式 | 在根目录加一条 ACE 给合成 SID | `icacls /setintegritylevel LOW` 改整个目录完整性级别 |
| 撤销 | `RevokeGrants()` 精确撤销 ACE | `revoke_all()` + 记录还原 |
| 默认 | 关（`windows_integrity`） | `low_il` 默认开 |
| fail-closed | ✅ `ErrWinNotPrepared` | ✅ `apply_in_child` 失败即 `os._exit(9)` |
| 策略层 | 纯函数 Policy + 六类人工形态 | `_has_escape` 正则 + 命令白名单 + Python AST |
| 无人值守 | Ask → Deny | `yolo` → `auto`（仍会放行） |
| `shell_exec` 接线 | ✅ 全量 | ❌ **完全无隔离**（`sandbox.py:587-688` 只有 cwd+env+rlimit+超时） |
| 长驻 shell | — | ❌ 无隔离（`terminal.py:60-90` 注释自认） |
| AppContainer | — | ❌ 死代码（`spawn_kwargs` 零调用方） |
| 网络隔离 | 无后端（仅策略） | 仅 `code_guard.DANGEROUS_MODULES` 拦 `socket` import |

### 3.1 chiron Low IL 的硬约束（实测注释）

`app/isolation/__init__.py:259-300`：

```python
def _ensure_workspace_low_label(path: str) -> tuple[bool, str]:
    """完整性检查是 no-write-up，Low IL 子进程写不了 Medium 对象
    —— 包括它自己的工作区。"""
    ok, err = win.set_path_integrity(path)
    if not ok:
        # 根因（本机实测）：工作区从父目录继承的 ACE 对 Authenticated Users
        # 只给 M，而 M 不含 WRITE_OWNER —— 改完整性标签恰恰需要它。
        # 于是读 ACL 成功、所有者就是当前用户、也没提权，但 /setintegritylevel
        # 一律返回 5（拒绝访问），low_il 就静默降级成 job。
        granted, gerr = win.grant_current_user_full_control(path)  # ← 授完全控制
        if granted:
            ok, err = win.set_path_integrity(path)
```

**这就是关键分歧点**：chiron 的 Low IL 要作用于真实仓库，就必须

1. 给**整个项目目录**打 Low 完整性标签，且
2. 先给当前用户授 `完全控制`（因为继承的 `M` 不含 `WRITE_OWNER`）

两步都是对用户仓库的实质性权限改动。而 Reasonix 只加**一条可精确撤销的 ACE**，不动完整性级别。

---

## 四、四项选型结论

### Q1 项目根授权 → 采用 Reasonix 的 `ProjectGrantStore` 模式

```go
// ProjectGrant 是用户对一个 workspace 文件夹的授权：
// 回答了 "always" 的 allow 规则，以及额外的可写目录。
// 它存在用户 home 下，**以文件夹自身路径为键**：
// grant 没有 digest 可绑定，所以不能跟随一个 checkout 的 .git 文件
// 所声称的仓库身份。
type ProjectGrant struct {
    Allow      []string
    AllowWrite []string
    Trust      WorkspaceTrust   // ""(undecided) | "trusted" | "declined"
}
```

默认根解析（`boot.go:230-241`）：

```go
func resolveWorkspaceRoot(explicit string) string {
    if explicit != "" { return explicit }
    wd, _ := os.Getwd()
    if root, ok := nearestGitRoot(wd); ok { return root }
    return wd
}
```

**建议 chiron 落地**：
- 存 `<user home>/chiron/project-grants.json`
- 键 = 文件夹**绝对路径**（不用 repo id —— `.git` 文件可伪造）
- 三态信任：未决首次进入时弹一次（对应你要求的「目录选择器」）
- 不需要服务端白名单：桌面端单用户，这张表本身就是白名单

> **为什么不建议「用户手填绝对路径」**：Reasonix 证明「当前目录的最近 git 根」是自然默认值，用户不必打路径；而手填路径在桌面端是反模式。

### Q2 shell_exec → 放，但三层设防

Reasonix 的答案是「策略层决定 allow/ask/deny，强制层保证 allow 了也出不了圈」。chiron 的强制层在 Windows 上目前是空的，所以**必须先补强制层，再放 shell_exec**。

具体三层：

| 层 | 做法 |
|---|---|
| 只读豁免 | 照搬 `ReadOnlyCommands` 表（cat/ls/grep/rg/git status/git diff/wc/head/tail） |
| 强制人工 | 照搬 `BashApprovalBlocker` 六类：`python -c` / `node -e` / `$(...)` / `eval` / `xargs` / here-doc |
| 无人值守 | `unattendedAsk` 语义：接管期间遇 Ask 一律拒 |

> **关于「主流开源 agent」**：我只深入读了 vendor 里的 Reasonix（它本身成熟且有完整 Windows 实现与测试）。Claude Code、Codex CLI、OpenHands 等的具体源码我**没有核实**。基于通用理解，它们的共同做法是「策略层 + 用户确认」为主，OS 级隔离只在 macOS/Linux 做（Seatbelt/bubblewrap），Windows 普遍缺位——Reasonix 的 `EgressSupported()=false` 和「Windows has no OS-level shell sandbox」正是这一现实的写照。**不构成对具体实现的断言。**

### Q3 Low IL 标签 → code 模式不沿用 `_ensure_workspace_low_label`

**理由**：给真实仓库打 Low 完整性标签 + 授完全控制，是两处对用户资产的实质性改动；而 Reasonix 的合成 SID 方案只加一条可精确撤销的 ACE。

两个可选路径：

**路径 A（推荐，需实现受限令牌）**
- 新增 `app/isolation/win_token.py`：`CreateRestrictedToken` + restricting SIDs
- 在项目根加一条 ACE 给合成 SID（不含 `WRITE_DAC`/`WRITE_OWNER`）
- `refuseLink` 拒绝 junction/symlink 根
- 撤销：`RevokeGrants()` 精确删 ACE
- **必须承认的缺陷**：删除/重命名不受约束（Reasonix 自己写明）⇒ 需要策略层额外拦 `rm`/`del`/`mv`

**路径 B（短期，不改 ACL）**
- 项目根**不打标签**，只给 `<workspace>/.chiron-tmp` 打
- 接受「对仓库内文件读写不受 Low IL 约束」
- 靠策略层 + 只读/人工判定兜底
- 代价：隔离强度只剩 Job Object 层，README 的「执行隔离是默认值」承诺在 code 模式上打折

> 路径 B 与你「必须设计全新方案、不能像 docker vs local 那样二态」的定调一致：它不是一个掩盖，而是一个**明确标注强度等级**的中间档，且可随时升级到路径 A。

### Q4 实施顺序 → 先强制层，再 code 模式

理由：没有强制层的 code 模式 = 现有 `_run_in_sandbox_impl` 的逃逸正则 + 白名单，而后者已被 `code_guard` 的设计者自己承认「不是完备隔离（可混淆绕过）」。先上模式开关会得到「切了没变化」——正是 `modes.py` 头部注释记录过的历史教训。

建议顺序：
1. `_run_in_sandbox_impl` 接 Job Object（低风险、立即生效）
2. `terminal.py` 长驻 shell 接同一套
3. 策略层：只读表 + 六类人工形态
4. 项目根授权（`ProjectGrantStore` + 三态信任）
5. `ProjectBackend`（突破 `safe_join` 绑定）
6. code 模式开关

---

## 五、微软官方文档核实

### 5.1 `WRITE_RESTRICTED` 的确切语义（证实删除缺口是设计后果）

来源：[CreateRestrictedToken](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-createrestrictedtoken)

> **WRITE_RESTRICTED 0x8**：The new token contains restricting SIDs that are considered **only when evaluating write access**.

所以「删除/重命名不受 restricting SIDs 约束」**不是 Reasonix 的实现疏漏，而是这个标志的文档化语义**。同理 `Restricted Tokens` 页：

> The system performs two access checks: one using the token's enabled SIDs, and another using the list of restricting SIDs. Access is granted only if **both** access checks allow the requested access rights.

推论：想 bound 删除只有一条路——**去掉 `WRITE_RESTRICTED`**，让 restricting SIDs 参与全部访问检查。但那样**读也被 bound**：agent 将无法读取任何未被显式授权的文件，包括系统 DLL、Python 运行时、Git 安装目录。实务上不可行。

⇒ **删除只能在策略层解决，没有 OS 级方案。**

### 5.2 Reasonix 漏掉的官方要求：桌面隔离（对 chiron 影响最大）

`CreateRestrictedToken` 与 `Restricted Tokens` 两页都有这段 **Warning**：

> Applications that use restricted tokens should run the restricted application on **desktops other than the default desktop**. This is necessary to prevent an attack by a restricted application, using **`SendMessage` or `PostMessage`**, to unrestricted applications on the default desktop.

**Reasonix 的代码里没有这个处理**（`runRestricted` 只设 `STARTF_USESTDHANDLES`，未设 `lpDesktop`）。

它对 CLI 影响小，对 chiron 影响大：

| | Reasonix | chiron |
|---|---|---|
| 进程形态 | CLI，前台单进程 | Electron 主进程 + 渲染进程 + Python 引擎 |
| 默认桌面上有什么 | 一个终端 | **未受限的整个应用** |
| 受限进程能做什么 | 消息打不到有价值的靶子 | `PostMessage` 到 Electron 主进程窗口 |

⇒ chiron 若照抄 Reasonix，会留下一个它自己不需要担心的攻击面。**必须补 `CreateDesktop` + `STARTUPINFO.lpDesktop`**。

### 5.3 `DISABLE_MAX_PRIVILEGE` 印证了 traverse 例外

> **DISABLE_MAX_PRIVILEGE 0x1**：Disables all privileges in the new token **except the `SeChangeNotifyPrivilege`** privilege.

这正是 Reasonix 注释里说的「every privilege but the traverse check removed」——`SeChangeNotifyPrivilege` 是目录遍历检查的绕过特权，保留它是为了让受限进程仍能穿过目录找到被授权文件。

### 5.4 AppContainer 的真实价值

来源：[AppContainer isolation](https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation)

官方列出的六类隔离：

| 类别 | 受限令牌是否覆盖 |
|---|---|
| Credential isolation（不能借用户凭据登录他处） | ❌ |
| Device isolation（摄像头/麦克风/GPS 默认拒绝） | ❌ |
| File isolation | ✅ |
| **Network isolation**（Internet/Intranet/server 三类粒度授权） | ❌ |
| Process isolation | 🟡 仅 Job Object |
| **Window isolation**（影响其他应用界面） | ❌ |

**网络隔离**和**凭据隔离**是受限令牌完全没有的，这是 AppContainer 不可替代之处。代价是 legacy 兼容性——chiron 已实测单独使用报 `STATUS_DLL_NOT_FOUND`（`3221225781`）。

> 注：`appcontainer-for-legacy-applications` 页面已 404，未取到 legacy app 迁移细节。

---

## 六、最终意见

### Q1 Windows 降权 → 三档并存，不做单一选型

| 档 | 机制 | 覆盖 | ACL 改动 | 落地成本 |
|---|---|---|---|---|
| **L1** | Job Object + 沙箱子目录 Low IL | 进程树收束、资源上限、沙箱内写边界 | 无（只碰 `chiron-sandbox/`） | 低，**立刻可做** |
| **L2** | 受限令牌 + `WRITE_RESTRICTED` + 合成 SID + **独立桌面** | 项目根写边界、可精确撤销 | 一条 ACE（不含 `WRITE_DAC`/`WRITE_OWNER`） | 中，需 Win32 绑定 |
| **L3** | AppContainer | 上述 + 网络/凭据/窗口隔离 | 走 capability SID | 高，legacy 兼容性未解 |

**建议：L1 + L2，L3 暂不做。**

理由：L3 的唯一独占优势是网络隔离，而 chiron 已有 `code_guard` 拦 `socket` import——虽然不完备（可被混淆绕过、拦不住外部命令），但在 code 模式初期够用。先花力气在 L2 上，把 delete 缺口用策略层补掉。

**L2 必须在 Reasonix 之上加两条它没有的**：
1. 独立桌面（§5.2）
2. `refuseLink` 拒绝 junction/symlink 根（它有，但要照抄）

### Q2 删除/重命名 → 策略层硬拦，且必须覆盖包装器形态

官方文档已证明 OS 级无解（§5.1）。所以：

| 层级 | 做法 |
|---|---|
| 直接命令 | `rm` / `del` / `erase` / `rd` / `rmdir` / `mv` / `move` / `ren` / `rename` |
| **等价破坏** | `git clean -fdx` / `git reset --hard` / `git checkout -- .` / `truncate` |
| **包装器** | `xargs rm` / `find -delete` / `find -exec rm` / `for f in *; do rm` |
| 无人值守 | 上述任一，即使 `tools_mode=yolo` 也拒（接管期间无人在场，无法事后追认） |

实现方式照搬 Reasonix 的 `BashApprovalBlocker`：静态分析读不懂的形状（`$(...)`、`eval`、`xargs`、here-doc）**一律升级为需人工**，而不是尝试解析。

> 这一层与 `undo_stack` 快照是**互补而非替代**：快照只能救回被 `write_file` 改过的文件，救不回 `git clean -fdx` 删掉的未跟踪文件。

### Q3 默认开还是关 → 分层默认

| 层 | 默认 | 理由 |
|---|---|---|
| 隔离机制本身 | **开** | 守住 README「执行隔离是默认值」的承诺 |
| 对**真实仓库**的 ACL 改动 | **关**，首次进入该项目时弹一次 | 这是对用户资产的实质性改动，必须知情同意 |
| 用户同意后 | 记住，写进 `project-grants.json` | 不必每次询问 |

这与现有 `FLOOR_LEVEL` / `SANDBOX_ISOLATION_ALLOW_DOWNGRADE` 的分离思路一致：**「接受更弱但仍隔离」不等于「关闭隔离」**。

具体交互：
- 用户在 chiron 里新建/打开一个项目 → 检测该项目是否已有授权记录
- 无记录 → 弹一次：「chiron 将在 `<path>` 添加一条可撤销的访问控制条目，使 Agent 只能写入此目录。随时可在设置中撤销。」
- 拒绝 → 该项目降为 L1（沙箱内），UI 明示「此项目未启用 OS 级写保护」

---

## 七、本文的可信度声明

| 结论 | 依据 |
|---|---|
| Reasonix 的实现细节（受限令牌、DACL、Job Object、fail-closed） | ✅ 逐行读源码 |
| `WRITE_RESTRICTED` 语义、桌面隔离警告、六类 AppContainer 隔离 | ✅ 微软官方文档原文 |
| 「删除只能策略层解决」 | ✅ 由 §5.1 的官方语义推出 |
| 其他开源 agent（Claude Code / Codex CLI / OpenHands）的做法 | ❌ **未核实** |
| AppContainer legacy 迁移细节 | ❌ 官方页面 404，未取到 |
