// project_git.go —— 项目改动文件（只读 Git 视图）。
//
// **为什么由后端跑 git**
//
// 「这个项目现在和上次提交差了什么」只有 git 知道。渲染层既拿不到项目根，也不该
// 直接执行外部进程（Electron 渲染进程里跑命令要先开 nodeIntegration，等于把整个
// shell 暴露给页面）。所以执行点落在已经持有 root_path 的这一层。
//
// **为什么是只读**
//
// 本文件只执行 status / diff / rev-parse 三类**只读**子命令，且子命令与参数全部
// 由代码写死 —— 请求里没有任何一项会变成命令行参数，因此不存在注入面。暂存、
// 丢弃、提交一律不提供：丢弃会真的回退用户的工作区，写操作需要二次确认与撤销
// 设计，不在这一版范围内。
//
// `--no-optional-locks` 不是可选项：`git status` 默认会**刷新并写回 index**
// （更新 stat 缓存），那就是写盘 —— 一个"只读"面板不该拿用户的 index 锁。
//
// **为什么 cwd 是项目根而不是仓库根**
//
// 项目可以被绑成某个仓库的子目录。在子目录里跑 `git status`，git 只报告该子目录
// 内的改动，这正是用户对"这个项目的改动"的预期；从仓库根跑会把兄弟目录的改动也
// 列进来，点开时才发现路径不在项目内。
//
// 分工与 project_files.go 一致：下半部分是纯函数（只解析文本，不执行、不碰 HTTP），
// 上半部分是取数外壳。这样 porcelain 解析可以脱离 git 直接单测。

package api

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	// maxProjectChanges 是一次返回的改动条数上限。
	//
	// 一个刚 `npm install` 完又忘了 .gitignore 的仓库可能有上万个未跟踪文件。
	// 截断必须**说出来**（响应带 truncated），否则前端会把"只看到前 N 条"
	// 误认成"只改了 N 个文件"。
	maxProjectChanges = 2000
	// gitTimeout 是单条 git 命令的时间上限。
	//
	// git status 在大仓库上可能慢，但慢到这个数就说明仓库状态有问题（或磁盘在
	// 掉链子）；继续等只会让面板一直转圈，不如超时并明确说"没读出来"。
	gitTimeout = 5 * time.Second
)

var (
	// errNotGitRepo 表示项目目录不是一个 git 工作树 —— 这是**正常状态**，
	// 不是错误：画布、普通文件夹都可能没有版本控制。
	errNotGitRepo = errors.New("not a git work tree")
	// errGitMissing 表示机器上没有 git 可执行文件。
	//
	// 与 errNotGitRepo 分开：前者用户该去装 git，后者什么都不用做。
	errGitMissing = errors.New("git is not installed")
	// errGitFailed 是其余失败（仓库损坏、权限、safe.directory 拒绝…）。
	errGitFailed = errors.New("git command failed")
)

// ProjectChange 是一个文件相对上次提交的改动。
//
// 与 Reasonix 的 WorkspaceChange 同构，但去掉了暂存操作相关的字段：没有写操作，
// 就不需要"能不能暂存 / 能不能丢弃"的判断。
type ProjectChange struct {
	Path string `json:"path"`
	// OldPath 只在重命名/复制时非空（porcelain 的 R/C 条目带原路径）。
	OldPath string `json:"old_path,omitempty"`
	// Status 是单字母状态：M 改 / A 新增 / D 删除 / R 重命名 / C 复制 / ? 未跟踪 / U 冲突。
	Status string `json:"status"`
	// Staged 表示索引里有这个文件的改动（`git diff --cached` 看得到）。
	Staged bool `json:"staged"`
	// Unstaged 表示工作区与索引不同（`git diff` 看得到）。
	Unstaged bool `json:"unstaged"`
	// Insertions / Deletions 为 nil 表示 **git 没有统计**（二进制文件、重命名简写、
	// 未跟踪文件）。这与"改了 0 行"是两件事：二进制文件不是按 0 行改的，
	// 界面因此什么都不该打印。
	Insertions *int `json:"insertions,omitempty"`
	Deletions  *int `json:"deletions,omitempty"`
}

// ProjectChanges 是端点响应体。
type ProjectChanges struct {
	// Repo 为 false 时 Changes 一定是空数组而不是 null：界面要能区分
	// "没有改动"和"这里没有版本控制"，两者给的提示完全不同。
	Repo bool `json:"repo"`
	// GitMissing 为 true 时界面提示"未检测到 git"而不是"没有改动"。
	GitMissing bool            `json:"git_missing,omitempty"`
	Branch     string          `json:"branch,omitempty"`
	Head       string          `json:"head,omitempty"`
	Changes    []ProjectChange `json:"changes"`
	Truncated  bool            `json:"truncated"`
}

// gitOutputCap 是单条 git 命令收集的输出上限。
//
// 为什么需要：`maxProjectChanges` 只在**解析之后**截断条目数，而 `bytes.Buffer`
// 会先把整份输出收进内存。一个没配 `.gitignore` 的仓库里 `git status -z` 能吐出
// 几十 MB，每次刷新改动页签都是一次可复现的内存放大。
// 8MB 远大于 2000 条路径所需（约 200KB），正常仓库永远碰不到这个上限。
const gitOutputCap = 8 << 20

// cappedBuffer 是带上限的 bytes.Buffer。
type cappedBuffer struct {
	buf       bytes.Buffer
	truncated bool
}

// Write 在达到上限后丢弃多余字节，但**始终返回 len(p), nil**。
//
// 返回短写会让 os/exec 判定管道出错并停止读取，git 随后因管道写满而阻塞，
// 一直卡到 gitTimeout —— 白白多等 5 秒才拿到一个本可以立刻就有的结果。
func (c *cappedBuffer) Write(p []byte) (int, error) {
	if room := gitOutputCap - c.buf.Len(); len(p) > room {
		if room > 0 {
			c.buf.Write(p[:room])
		}
		c.truncated = true
		return len(p), nil
	}
	c.buf.Write(p)
	return len(p), nil
}

// String 返回已收集的内容；被截断时退到最后一个完整的分隔符。
//
// 截断点几乎必然落在半条记录中间。直接返回半条会让 porcelain 的解析把残缺路径
// 当成一个真实文件（`M src/very-lo`），也会让 numstat 记错行数。退到最后一个
// NUL（status 的分隔符）或换行（numstat 的分隔符）就能保证只丢掉不完整的尾巴。
func (c *cappedBuffer) String() string {
	s := c.buf.String()
	if !c.truncated {
		return s
	}
	cut := strings.LastIndexAny(s, "\x00\n")
	if cut < 0 {
		return ""
	}
	return s[:cut+1]
}

// gitRun 执行一条 git 命令并返回 stdout。
//
// 做成变量是为了单测能替换成假实现 —— 但**不要**为了方便而让它接受调用方给的
// 任意参数：调用方只传代码里写死的子命令。
var gitRun = func(ctx context.Context, dir string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, gitTimeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, "git", args...)
	cmd.Dir = dir
	cmd.Env = gitEnv()

	var out, errOut cappedBuffer
	cmd.Stdout = &out
	// stderr 同样设上限：仓库损坏时 git 可以刷出成百上千行。
	cmd.Stderr = &errOut

	if err := cmd.Run(); err != nil {
		if errors.Is(err, exec.ErrNotFound) {
			return "", errGitMissing
		}
		var execErr *exec.Error
		if errors.As(err, &execErr) && errors.Is(execErr.Err, exec.ErrNotFound) {
			return "", errGitMissing
		}
		if ctx.Err() != nil {
			return out.String(), fmt.Errorf("%w: timed out after %s", errGitFailed, gitTimeout)
		}
		// stderr 可能含绝对路径，只留在错误里给日志，不透给页面（见 handler）。
		return out.String(), fmt.Errorf("%w: %s", errGitFailed, strings.TrimSpace(errOut.String()))
	}
	return out.String(), nil
}

// gitEnv 组出执行 git 用的环境变量。
//
// **用白名单而不是黑名单。** 早期版本只是丢掉 `GIT_DIR` / `GIT_INDEX_FILE` 这几个
// "会把 git 指到别的仓库"的变量，但那远不是全部：git 会读几十个 `GIT_*`，
// 其中一批能直接让 git **执行一个程序**或**注入任意配置**：
//
//   - `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n`：注入任意
//     配置项。`core.fsmonitor`、`core.pager` 之类都指向可执行程序 —— 等于命令执行。
//   - `GIT_EXTERNAL_DIFF` / `GIT_DIFF_OPTS`：diff 的外部实现。
//   - `GIT_SSH_COMMAND` / `GIT_ASKPASS` / `GIT_PROXY_COMMAND`：拉远程或取凭据时执行。
//   - `GIT_CEILING_DIRECTORIES` / `GIT_DISCOVERY_ACROSS_FILESYSTEM`：改变"哪个目录
//     算仓库"，能把读取导向项目外。
//
// 黑名单只能拦住想得到的那几个，白名单拦住一切没想到的。保留下来的每一项都有
// 明确理由：找到 git 自己（PATH/PATHEXT）、Windows 上让进程能起来（SYSTEMROOT
// 等）、读全局配置（HOME/USERPROFILE/XDG_CONFIG_HOME）。
func gitEnv() []string {
	allowed := map[string]bool{
		// 找到可执行文件
		"PATH": true, "PATHEXT": true,
		// Windows：缺 SYSTEMROOT 时进程无法启动；TEMP 等供 git 写临时文件
		"SYSTEMROOT": true, "SYSTEMDRIVE": true, "WINDIR": true, "COMSPEC": true,
		"TEMP": true, "TMP": true, "TMPDIR": true,
		// 全局配置的位置（只读）
		"HOME": true, "USERPROFILE": true, "HOMEDRIVE": true, "HOMEPATH": true,
		"XDG_CONFIG_HOME": true, "APPDATA": true, "LOCALAPPDATA": true, "PROGRAMDATA": true,
		// 语言只影响 stderr 措辞；真正的输出语言由下面覆写的 LC_ALL 固定
		"LANG": true, "LC_CTYPE": true, "TERM": true,
	}
	env := make([]string, 0, 24)
	for _, kv := range os.Environ() {
		name, _, _ := strings.Cut(kv, "=")
		if allowed[strings.ToUpper(name)] {
			env = append(env, kv)
		}
	}
	return append(env,
		// 绝不停在凭据输入上：只读命令理论上不需要凭据，但子模块/credential
		// helper 仍可能插话，而这个进程没有终端可交互。
		"GIT_TERMINAL_PROMPT=0",
		// 命令行选项之外再关一次：见文件头对 index 写回的说明。
		"GIT_OPTIONAL_LOCKS=0",
		"GIT_PAGER=cat",
		// 报错措辞稳定，便于归类；也避免非 ASCII 输出干扰解析。
		"LC_ALL=C",
	)
}

// gitRead 是带固定前缀的 git 调用。
//
// 前缀分两类。
//
// **一、只读**：`--no-optional-locks` 保证不写 index；`core.quotepath=false` 保证
// 非 ASCII 路径不被转义成八进制（否则中文文件名在列表里会变成一串 \344\275\240）。
//
// **二、不执行仓库自带的程序** —— 这是安全边界，不是风格偏好。
//
// git 会读**仓库自己的** `.git/config`，其中若干项直接指向可执行文件。最典型的是
// `core.fsmonitor`。实测（git 2.52）：
//
//	git config core.fsmonitor "$PWD/hook.sh"   # hook.sh 里 touch pwned.txt
//	git status --porcelain=v1                  # → pwned.txt 出现
//
// 也就是说：用户只要绑定一个从网上下载的项目，刷新一次改动页签，就会在本机执行
// 项目里指定的程序。`gitEnv()` 的白名单挡的是**环境变量**这一路（`GIT_CONFIG_COUNT`
// 等），完全是另一个注入面 —— 环境变量干净不代表仓库配置干净。
//
// 命令行 `-c` 的优先级高于仓库 config，所以在调用点覆写即可。覆盖 `core.pager`
// 是同一类考虑：输出不该被接到仓库指定的程序上。
func gitRead(ctx context.Context, root string, args ...string) (string, error) {
	return gitReadWith(ctx, root, repoFilterOverrides(ctx, root), args...)
}

// gitReadWith 用调用方已算好的 filter 覆盖执行一条 git 命令。
//
// 存在的理由**纯粹是进程数**：`repoFilterOverrides` 自己要走一次 git，而
// `collectProjectChanges` 一批要跑 6 条命令。每条都重算一次，等于把 git 进程数
// 翻倍（6 → 12），而这段代码当初就是为了减少等待才改成并行的。按批算一次即可：
// 同一批命令之间仓库配置不会变。
func gitReadWith(ctx context.Context, root string, overrides []string, args ...string) (string, error) {
	full := make([]string, 0, 16+len(overrides)+len(args))
	full = append(full, staticGitFlags()...)
	full = append(full, overrides...)
	full = append(full, args...)
	return gitRun(ctx, root, full...)
}

// staticGitFlags 是每条 git 调用都要带的固定前缀。见 gitRead 的安全说明。
func staticGitFlags() []string {
	return []string{
		"--no-optional-locks",
		"-c", "core.quotepath=false",
		// 关掉 fsmonitor：它会执行仓库配置里指定的程序。
		"-c", "core.fsmonitor=false",
		// rev-parse 会执行它，同为仓库可控的可执行入口。
		"-c", "core.alternateRefsCommand=",
		// diff 的外部实现。
		"-c", "diff.external=",
		"-c", "core.pager=cat",
	}
}

// repoFilterOverrides 把仓库**本地配置**里的 filter 驱动全部改成恒等变换。
//
// 这是第二类"仓库配置指向可执行文件"，与 core.fsmonitor 同类但更隐蔽：入口不在
// `.git/config` 的某个固定键，而是分成两半 —— `.gitattributes` 里写
// `*.txt filter=<任意名>`，`.git/config` 里再把这个名字绑到一条命令上。实测
// （git 2.52）：
//
//	git config filter.evil.clean "$PWD/evil.sh"   # 脚本里 touch PWNED
//	echo '*.txt filter=evil' > .gitattributes
//	git status --porcelain=v1                     # → PWNED 出现
//	git diff --numstat                            # → PWNED 出现
//
// 也就是说固定的 `-c` 列表挡不住它：filter 名是仓库随便起的，写死几个 key 只能
// 挡住写死的那几个。必须按仓库**实际存在的** filter 名动态生成覆盖。
//
// 只读 `--local`：全局/系统级配置属于用户自己的机器，可信；仓库目录是从网上下载
// 来的，不可信。少读一层也更不容易误伤用户装的 git-lfs。
//
// 返回空表示仓库没有本地 filter（绝大多数情况）—— 此时不增加任何参数。
func repoFilterOverrides(ctx context.Context, root string) []string {
	// 用 `--list` 而不是 `--get-regexp '^filter\.'`。
	//
	// 差别是**可判性**，不是风格：`--get-regexp` 在没有匹配项时以 exit 1 结束，
	// 而"探测本身失败"（仓库读不了）也是非零。`gitRun` 把两者都包成同一个错误，
	// 于是我们无法区分"这个仓库确实没有 filter"和"我没读到它的配置" ——
	// 后者一旦被当成前者，后续命令就在**未覆盖**状态下执行，正是本函数要防的事。
	//
	// `--list` 只在仓库真的读不了时才失败，正常情况（有/没有 filter）都是 exit 0，
	// 结论唯一。
	//
	// 改名而不是走 gitRead：走 gitRead 会递归调用本函数。这条命令只读配置、
	// 不碰工作区，也不经过任何 filter。
	out, err := gitRun(ctx, root, "config", "--local", "--list")
	if err != nil {
		// 到这里说明本地配置读不出来。调用方已先确认过"这是个 git 仓库"，
		// 所以这属于异常；返回 nil 是安全的 —— 后续命令会以同样的原因失败，
		// 不会跑到"未覆盖却继续执行"的分支上。
		return nil
	}
	names := filterDriverNames(out)
	if len(names) == 0 {
		return nil
	}
	overrides := make([]string, 0, len(names)*6)
	for _, name := range names {
		// clean/smudge 改成 cat：即使 .gitattributes 标了 required=true，恒等
		// 变换也不会让 git 报"过滤器失败"。
		overrides = append(overrides,
			"-c", "filter."+name+".clean=cat",
			"-c", "filter."+name+".smudge=cat",
			// process 是长驻协议，置空即禁用它（否则 git 会优先走它）。
			"-c", "filter."+name+".process=",
		)
	}
	return overrides
}

// filterDriverNames 从 `git config --local --list` 的输出里取出 filter 驱动名。
//
// 每行形如 `filter.<name>.<key>=<value>`。键名取最后一段（clean/smudge/process/
// required…），前面整体就是驱动名 —— 所以 `filter.foo.bar.clean` 的驱动名是
// `foo.bar`，不能简单地按第一个点切。
func filterDriverNames(out string) []string {
	seen := map[string]struct{}{}
	names := make([]string, 0, 4)
	for _, line := range strings.Split(out, "\n") {
		key, _, _ := strings.Cut(line, "=")
		rest, ok := strings.CutPrefix(strings.TrimSpace(key), "filter.")
		if !ok {
			continue
		}
		dot := strings.LastIndex(rest, ".")
		if dot <= 0 {
			continue
		}
		name := rest[:dot]
		if _, dup := seen[name]; dup {
			continue
		}
		seen[name] = struct{}{}
		names = append(names, name)
	}
	return names
}

// ── 纯函数：解析 ──

// parseGitStatusZ 解析 `git status --porcelain=v1 -z`。
//
// -z 而不是默认换行格式：路径里可以有空格和引号，换行模式下 git 会给它们加引号
// 并转义，解析就得反着做一遍；NUL 分隔时路径原样输出。
//
// 条目形如 `XY <path>\0`；重命名/复制（R/C）后面紧跟一段 `<原路径>\0`。
func parseGitStatusZ(out string) []ProjectChange {
	changes := []ProjectChange{}
	if out == "" {
		return changes
	}
	parts := strings.Split(out, "\x00")
	for i := 0; i < len(parts); i++ {
		entry := parts[i]
		// 最短是三个字符（XY + 空格）加至少一个路径字符
		if len(entry) < 4 {
			continue
		}
		x, y := entry[0], entry[1]
		change := ProjectChange{
			Path:     entry[3:],
			Status:   changeLetter(x, y),
			Staged:   x != ' ' && x != '?',
			Unstaged: y != ' ',
		}
		if x == 'R' || x == 'C' || y == 'R' || y == 'C' {
			i++
			if i < len(parts) {
				change.OldPath = parts[i]
			}
			// 重命名条目里 Path 是**新**路径（git 的约定），OldPath 是旧的。
		}
		changes = append(changes, change)
		if len(changes) >= maxProjectChanges {
			return changes
		}
	}
	return changes
}

// changeLetter 把两个状态字符收敛成一个给界面看的字母。
func changeLetter(x, y byte) string {
	switch {
	case x == '?':
		return "?"
	case x == 'R' || y == 'R':
		// 重命名优先于工作区状态：列表里 `R` 比 `M` 更能解释"为什么路径变了"
		return "R"
	case x == 'C' || y == 'C':
		return "C"
	case y != ' ':
		// 工作区状态优先：用户看到的是磁盘上的样子
		return string(y)
	default:
		return string(x)
	}
}

// addNumstat 把 `git diff --numstat` 的行数统计**累加**到已有条目上。
//
// 同一个文件可能既有已暂存又有未暂存改动（porcelain 的 `MM`）。两次调用分别传入
// `git diff`（索引→工作区）与 `git diff --cached`（HEAD→索引）的输出，累加起来
// 恰好是"相对 HEAD 的总改动"。
//
// 匹配不上的条目一律跳过而不是猜：重命名的简写形式（`dir/{a => b}/c`）与二进制
// 文件的 `-\t-` 都在这里被留下 nil —— 界面据此什么都不打印。
func addNumstat(changes []ProjectChange, out string) {
	if len(changes) == 0 || out == "" {
		return
	}
	byPath := make(map[string]int, len(changes))
	for i := range changes {
		byPath[changes[i].Path] = i
	}
	for _, line := range strings.Split(out, "\n") {
		first := strings.IndexByte(line, '\t')
		if first < 0 {
			continue
		}
		rest := line[first+1:]
		second := strings.IndexByte(rest, '\t')
		if second < 0 {
			continue
		}
		ins, del, path := line[:first], rest[:second], rest[second+1:]
		at, ok := byPath[path]
		if !ok {
			continue
		}
		if ins == "-" || del == "-" {
			continue // 二进制：git 没有数，不能当成 0
		}
		n, err1 := strconv.Atoi(ins)
		m, err2 := strconv.Atoi(del)
		if err1 != nil || err2 != nil {
			continue
		}
		if changes[at].Insertions == nil {
			changes[at].Insertions = new(int)
			changes[at].Deletions = new(int)
		}
		*changes[at].Insertions += n
		*changes[at].Deletions += m
	}
}

// ── 取数外壳 ──

// collectProjectChanges 读出项目根下的改动。
//
// 没有版本控制时返回 Repo=false 而**不是**错误：普通文件夹是正常用法，
// 把它当失败会让界面天天弹红条。
//
// **为什么并行**：这条路径是"打开/刷新改动页签"，用户就在等。串行时要依次启动
// 6 个 git 进程（Windows 上每个 60~120ms 的进程创建开销），而且每个都有独立的
// gitTimeout —— 一个卡住的 diff 会把后面两条也拖住，最坏叠加到几十秒。
// 这些命令互不依赖、都只读，且都带 `--no-optional-locks`，并发访问同一仓库安全。
func collectProjectChanges(ctx context.Context, root string) (ProjectChanges, error) {
	empty := ProjectChanges{Repo: false, Changes: []ProjectChange{}}

	// filter 覆盖按批算**一次**：它自己要走一次 git，而这一批有 6 条命令。
	// 逐条重算等于把进程数翻倍（6 → 12），而这段代码当初改成并行就是为了少等。
	// 同一批命令之间仓库配置不会变，所以算一次就够。
	overrides := repoFilterOverrides(ctx, root)

	out, err := gitReadWith(ctx, root, overrides, "rev-parse", "--is-inside-work-tree")
	if err != nil {
		if errors.Is(err, errGitMissing) {
			return ProjectChanges{Repo: false, GitMissing: true, Changes: []ProjectChange{}}, nil
		}
		return empty, nil
	}
	if strings.TrimSpace(out) != "true" {
		return empty, nil
	}

	// 第一波：工作区状态 + 分支 + 提交。三者互不依赖。
	//
	// 分支与提交是 best-effort：空仓库（还没有第一个提交）时 rev-parse 会失败，
	// 那不是问题 —— 界面只是少显示一处"对照于哪个提交"。
	var (
		statusOut, branchOut, headOut string
		statusErr                     error
		wg                            sync.WaitGroup
	)
	wg.Add(3)
	go func() {
		defer wg.Done()
		statusOut, statusErr = gitReadWith(ctx, root, overrides, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
	}()
	go func() {
		defer wg.Done()
		branchOut, _ = gitReadWith(ctx, root, overrides, "rev-parse", "--abbrev-ref", "HEAD")
	}()
	go func() {
		defer wg.Done()
		headOut, _ = gitReadWith(ctx, root, overrides, "rev-parse", "--short=8", "HEAD")
	}()
	wg.Wait()

	if statusErr != nil {
		return ProjectChanges{}, statusErr
	}
	changes := parseGitStatusZ(statusOut)

	result := ProjectChanges{Repo: true, Changes: changes}
	result.Truncated = strings.Count(statusOut, "\x00") > len(changes)
	result.Branch = strings.TrimSpace(branchOut)
	result.Head = strings.TrimSpace(headOut)

	// 没有改动就不必再跑两条 diff。
	if len(changes) == 0 {
		return result, nil
	}

	// 第二波：两条 numstat。必须等 status 出来才知道有没有改动，但彼此可并行 ——
	// 它们都要把整个仓库的改动算一遍，是这条路径上最贵的两步。
	var worktree, index string
	wg.Add(2)
	go func() {
		defer wg.Done()
		worktree, _ = gitReadWith(ctx, root, overrides, "diff", "--numstat")
	}()
	go func() {
		defer wg.Done()
		index, _ = gitReadWith(ctx, root, overrides, "diff", "--cached", "--numstat")
	}()
	wg.Wait()

	// 累加在主 goroutine 上做：`addNumstat` 要改 result.Changes，两个 goroutine
	// 同时改同一个切片就是数据竞争。取数并行、合并串行。
	addNumstat(result.Changes, worktree)
	addNumstat(result.Changes, index)
	return result, nil
}

// ListProjectChanges 返回项目内的改动文件（只读 Git 视图）。
//
// 没有版本控制、没有装 git 都是**正常结果**（`repo: false`），不是错误：
// 普通文件夹当项目用是允许的，把它报成 4xx 会让前端天天弹红条。
func (h *SessionMapHandler) ListProjectChanges(w http.ResponseWriter, r *http.Request) {
	root, ok := h.requireProject(w, r)
	if !ok {
		return
	}
	result, err := collectProjectChanges(r.Context(), root)
	if err != nil {
		if errors.Is(err, errGitMissing) {
			OK(w, ProjectChanges{Repo: false, GitMissing: true, Changes: []ProjectChange{}})
			return
		}
		// 错误里可能带绝对路径（git 的 stderr 常这样），只进日志不进响应体。
		logAndRespond(w, err, http.StatusInternalServerError, "could not read git changes")
		return
	}
	if result.Changes == nil {
		result.Changes = []ProjectChange{}
	}
	OK(w, result)
}
