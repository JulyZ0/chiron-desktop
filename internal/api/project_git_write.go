// project_git_write.go —— 项目 Git **写操作**的安全基座（N5 P0）。
//
// ## 为什么不能直接用只读那套
//
// `gitRead` 的前缀里带 `--no-optional-locks`、关掉 fsmonitor/pager —— 这些是为"绝不
// 写盘"服务的。写操作（add/commit/stash/switch）必然要写 index 与 refs，复用那份
// 前缀只会让命令行为变得难以解释。
//
// 但写操作打开的**攻击面**比只读大得多：仓库自带的 hook、clean/smudge filter、
// 签名配置都会在 `git add` / `git commit` 时**执行程序**。一个从网上下载的项目
// 只要绑定成工作台项目，用户点一次"提交"就可能在本机运行它指定的脚本。
//
// 所以这里给出独立的写前缀：
//   - `core.hooksPath=<空目录>`：pre-commit / commit-msg / post-commit 全部不执行；
//   - 仓库里声明的每个 filter driver 强制覆写成 `cat`，并清空 `process`：
//     `.gitattributes` 里 `*.x filter=evil` 不会再触发任何外部程序；
//   - `commit.gpgsign=false`：不弹 pinentry、不调 gpg/ssh-sign；
//   - `core.fsmonitor=false` / `core.pager=cat`：与只读侧同一理由。
//
// 这些 `-c` 的优先级高于仓库 config，是"调用点覆写"而不是"信任仓库"。
//
// ## 路径
//
// 写操作的路径只允许来自**当前 status 的条目**（`allowedGitPaths`），并在拼命令前
// 统一校验：必须是 POSIX 相对路径、不能以 `-` 开头、不能含 `..`/反斜杠/NUL。
// 参数一律以 `--` 收尾，杜绝 pathspec 注入。
package api

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path"
	"regexp"
	"strings"
	"sync"
	"time"
)

const (
	// gitWriteTimeout 是单条写命令的上限：commit/stash 在大仓库上比只读慢一个量级。
	gitWriteTimeout = 30 * time.Second
	// gitWriteMaxPaths 限制一次写操作的路径条数：请求体不能被无限放大。
	gitWriteMaxPaths = 500
)

// errGitPathInvalid 表示请求里的路径不是"当前改动里的合法相对路径"。
var errGitPathInvalid = errors.New("invalid git path")

var (
	noHooksDirOnce sync.Once
	noHooksDir     string
	noHooksDirErr  error
)

// gitNoHooksDir 返回一个**空目录**，用作 `core.hooksPath`。
//
// 用进程级单例而不是每次 mkdir：写在请求热路径上，而空目录内容永远不变。
// 创建失败时返回错误 —— 宁可拒绝这次写操作，也不要带着"hook 可能执行"继续。
func gitNoHooksDir() (string, error) {
	noHooksDirOnce.Do(func() {
		dir, err := os.MkdirTemp("", "chiron-git-nohooks-")
		if err != nil {
			noHooksDirErr = err
			return
		}
		// hooks 目录只需存在且为空；权限收窄，避免被同机其它用户塞进脚本。
		_ = os.Chmod(dir, 0o700)
		noHooksDir = dir
	})
	return noHooksDir, noHooksDirErr
}

// filterKeyRe 解析 `git config --get-regexp '^filter\.'` 的输出键。
// driver 名允许点号（git 不限制），取最后一个 `.clean/.smudge/.process` 作为后缀。
var filterKeyRe = regexp.MustCompile(`^filter\.(.+)\.(clean|smudge|process)$`)

// driverNameRe 只接受能安全拼进 `-c key=value` 的名字。
var driverNameRe = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)

// localFilterDrivers 列出仓库本地配置里声明的 filter driver。
//
// 读失败（没有本地配置、git 不可用）按"没有 driver"处理：这不影响 hook/签名那几条
// 防护，而一个读不出来的配置本来也无法作为覆写依据。
func localFilterDrivers(ctx context.Context, root string) []string {
	out, err := gitRead(ctx, root, "config", "--local", "--get-regexp", `^filter\.`)
	if err != nil {
		return nil
	}
	seen := map[string]bool{}
	var drivers []string
	for _, line := range strings.Split(out, "\n") {
		fields := strings.Fields(strings.TrimSpace(line))
		if len(fields) == 0 {
			continue
		}
		match := filterKeyRe.FindStringSubmatch(fields[0])
		if match == nil {
			continue
		}
		name := match[1]
		if !driverNameRe.MatchString(name) || seen[name] {
			continue
		}
		seen[name] = true
		drivers = append(drivers, name)
	}
	return drivers
}

// gitWriteOverrides 组出写操作的 `-c` 前缀。抽成独立函数是为了能单测"防了哪些面"，
// 不必真的构造一个恶意仓库才能断言覆写存在。
func gitWriteOverrides(ctx context.Context, root string) ([]string, error) {
	hooks, err := gitNoHooksDir()
	if err != nil {
		return nil, fmt.Errorf("prepare empty hooks dir: %w", err)
	}
	flags := []string{
		"-c", "core.quotepath=false",
		"-c", "core.fsmonitor=false",
		"-c", "core.pager=cat",
		"-c", "core.hooksPath=" + hooks,
		"-c", "commit.gpgsign=false",
		"-c", "tag.gpgsign=false",
	}
	for _, name := range localFilterDrivers(ctx, root) {
		flags = append(flags,
			"-c", "filter."+name+".clean=cat",
			"-c", "filter."+name+".smudge=cat",
			// 空 process 让 git 回落到 clean/smudge；required=false 保证即使
			// 某个 driver 仍要求外部程序，也不会把整个操作判失败。
			"-c", "filter."+name+".process=",
			"-c", "filter."+name+".required=false",
		)
	}
	return flags, nil
}

// gitWrite 执行一条**写** git 命令，带完整的仓库配置隔离。
//
// 调用方只传代码里写死的子命令与**已校验**的路径（见 validateGitPath），
// 不接受渲染层直接拼的命令片段。
func gitWrite(ctx context.Context, root string, args ...string) (string, error) {
	overrides, err := gitWriteOverrides(ctx, root)
	if err != nil {
		return "", err
	}
	full := make([]string, 0, len(overrides)+len(args)+1)
	full = append(full, overrides...)
	full = append(full, args...)
	return gitRunTimeout(ctx, root, gitWriteTimeout, full...)
}

// validateGitPath 校验一条"相对于项目根"的 POSIX 路径。
//
// 与 `project_files` 的路径校验同口径（拒绝绝对路径/.. /反斜杠/NUL），但这里更严：
// 写操作会把路径交给 git 当 pathspec，`-` 开头的值可能被解释成选项。
func validateGitPath(raw string) (string, error) {
	value := strings.TrimSpace(raw)
	if value == "" || strings.HasPrefix(value, "-") {
		return "", errGitPathInvalid
	}
	if strings.ContainsRune(value, '\x00') || strings.Contains(value, "\\") {
		return "", errGitPathInvalid
	}
	if path.IsAbs(value) {
		return "", errGitPathInvalid
	}
	// Windows 盘符（`C:/x`）在 POSIX 视角下不是绝对路径，但同样不可以。
	if len(value) >= 2 && value[1] == ':' {
		return "", errGitPathInvalid
	}
	clean := path.Clean(value)
	if clean == "." || clean == ".." || strings.HasPrefix(clean, "../") {
		return "", errGitPathInvalid
	}
	return clean, nil
}

// allowedGitPaths 把请求里的路径收敛成"当前改动确实包含的路径"。
//
// **必须**先过 status：否则渲染层可以让网关 stage 任何路径（包括 `.git/config`），
// 而"用户看得到的改动"与"实际会被写进索引的东西"必须一致。
func allowedGitPaths(requested []string, changes ProjectChanges) ([]string, error) {
	if len(requested) == 0 {
		return nil, errGitPathInvalid
	}
	if len(requested) > gitWriteMaxPaths {
		return nil, fmt.Errorf("%w: too many paths", errGitPathInvalid)
	}
	allowed := make(map[string]bool, len(changes.Changes))
	for _, change := range changes.Changes {
		if change.Path != "" {
			allowed[change.Path] = true
		}
	}
	seen := make(map[string]bool, len(requested))
	out := make([]string, 0, len(requested))
	for _, raw := range requested {
		clean, err := validateGitPath(raw)
		if err != nil {
			return nil, err
		}
		if !allowed[clean] {
			return nil, fmt.Errorf("%w: %s is not a current change", errGitPathInvalid, clean)
		}
		if seen[clean] {
			continue
		}
		seen[clean] = true
		out = append(out, clean)
	}
	if len(out) == 0 {
		return nil, errGitPathInvalid
	}
	return out, nil
}

// gitPathsArgs 把已校验路径拼成 `-- path...` 形式，杜绝 pathspec 选项注入。
func gitPathsArgs(paths []string) []string {
	args := make([]string, 0, len(paths)+1)
	args = append(args, "--")
	args = append(args, paths...)
	return args
}
