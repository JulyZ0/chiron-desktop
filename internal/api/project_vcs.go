// project_vcs.go —— 项目目录的版本控制状态与初始化。
//
// **为什么需要这一层**
//
// 还原点（检查点）建立在 git 对象库之上：`snapshot` 把工作区写成 tree，
// `commit-tree` 把它包成 commit，ref 落在 `refs/chiron/checkpoints/<session>/`。
// 项目根如果不是 git 工作树，这条链路的第一步就失败（`fatal: not a git repository`），
// 于是每一轮都留下一个 `state='failed'` 的还原点 —— 列表里看得见，点回滚必然 409。
// 用户看到的正是「还原点功能从不正确工作」。
//
// 所以这里回答两个问题：
//   1. 这个项目目录**现在**是不是处于版本控制之下（只读，随时可问）；
//   2. 如果不是，能不能**当场**把它纳入版本控制（写操作，必须用户明确同意）。
//
// **为什么检测要区分「自己就是仓库根」和「位于某个仓库的子目录」**
//
// 两者都让检查点可用（`git -C <子目录>` 会找到祖先仓库，快照只覆盖该子目录），
// 但初始化时的动作完全不同：已经在仓库里就**什么都不该做** —— 在子目录里再
// `git init` 会造出一个嵌套仓库，把该目录从父仓库的工作树里"抠"出来，
// 用户的提交历史会因此断掉。这个区别必须由服务端判断，前端只看布尔值分不出来。
//
// **为什么初始化不顺手做一次提交**
//
// `git init` 之后仓库里没有任何提交，`git status` 会把所有文件列为未跟踪 ——
// 那是事实，不是缺陷。顺手 `add . && commit` 会把用户全部源码（可能含大文件）
// 写进对象库，而那是一个**远超用户同意范围**的动作：他同意的是"启用还原点"，
// 不是"把整个项目提交一遍"。

package api

import (
	"context"
	"errors"
	"net/http"
	"strings"
)

// ProjectVCS 是端点的响应体：项目根此刻的版本控制状态。
type ProjectVCS struct {
	// Repo 表示项目根处于某个 git 工作树内（自身是仓库根，或位于仓库子目录）。
	// false 时检查点无法创建 —— 界面据此给出「启用版本跟踪」入口。
	Repo bool `json:"repo"`
	// RepoRoot 是工作树根目录（`git rev-parse --show-toplevel`），Repo 为 false 时为空。
	RepoRoot string `json:"repo_root,omitempty"`
	// OwnRoot 表示仓库根**就是**项目根本身。Repo 为 true 而 OwnRoot 为 false 时，
	// 项目是某个更大仓库的子目录：检查点可用，但不能再初始化。
	OwnRoot bool `json:"own_root"`
	// GitMissing 表示机器上没有 git 可执行文件。与 Repo=false 分开：
	// 这种情况下「启用」按钮点不动，用户该去装 git 而不是点按钮。
	GitMissing bool `json:"git_missing,omitempty"`
}

// projectVCS 读出项目根的版本控制状态。**从不返回错误**：读不出来就是"不处于
// 版本控制之下"，这与 `collectProjectChanges` 的 `repo: false` 同一口径 ——
// 普通文件夹当项目用是正常用法，把它报成 5xx 只会让界面天天弹红条。
func projectVCS(ctx context.Context, root string) ProjectVCS {
	out, err := runProjectGit(ctx, root, "rev-parse", "--is-inside-work-tree")
	if err != nil {
		if errors.Is(err, errGitMissing) {
			return ProjectVCS{GitMissing: true}
		}
		return ProjectVCS{}
	}
	if strings.TrimSpace(out) != "true" {
		return ProjectVCS{}
	}

	// 仓库根是 best-effort：读不到时 `Repo` 仍然为 true（检查点可用），
	// 只是分不清"自身是根"还是"子目录"，此时按"不是自身根"处理 ——
	// 宁可少给一个初始化入口，也不能在别人的仓库里再造一个。
	top, err := runProjectGit(ctx, root, "rev-parse", "--show-toplevel")
	topRoot := strings.TrimSpace(top)
	if err != nil {
		topRoot = ""
	}
	return ProjectVCS{
		Repo:     true,
		RepoRoot: topRoot,
		OwnRoot:  topRoot != "" && sameRootPath(root, topRoot),
	}
}

// initProjectRepo 把项目根纳入版本控制，返回初始化之后的状态。
//
// **幂等**：已经在仓库里（自身是根或位于子目录）就直接返回现状，不执行任何写操作。
// 这一点由服务端保证而不是靠前端先查一次 —— 前端那次查询与这次写之间隔着用户
// 点确认的时间窗，期间目录完全可能被别的程序 `git init` 过。
func initProjectRepo(ctx context.Context, root string) (ProjectVCS, error) {
	cur := projectVCS(ctx, root)
	if cur.GitMissing {
		return cur, errGitMissing
	}
	if cur.Repo {
		return cur, nil
	}
	// `init -q`：只创建 `.git`（含默认配置与 hooks 样例），不 add、不 commit。
	// staticGitFlags 一并带上：`core.pager` 之类的覆写对 init 无实际影响，
	// 但让"所有 git 调用都经过同一套受保护前缀"这条规则没有例外，评审时少一个判断。
	if _, err := runProjectGit(ctx, root, "init", "-q"); err != nil {
		return cur, err
	}
	return projectVCS(ctx, root), nil
}

// runProjectGit 用受保护前缀执行一条 git 命令。
//
// 与 `gitReadWith` 的区别只有一处：这里不计算 filter 覆盖。调用方是"检测/初始化"，
// 它们不读工作区内容 —— 检测只跑 `rev-parse`，初始化时仓库还不存在（没有任何
// `.gitattributes` 可生效）。为这两条命令再各跑一次 `git config --local --list`，
// 只会让"点一下启用"多等一个进程。
func runProjectGit(ctx context.Context, root string, args ...string) (string, error) {
	full := make([]string, 0, 8+len(args))
	full = append(full, staticGitFlags()...)
	full = append(full, args...)
	return gitRun(ctx, root, full...)
}

// GetProjectVCS 返回项目根的版本控制状态（只读）。
func (h *SessionMapHandler) GetProjectVCS(w http.ResponseWriter, r *http.Request) {
	root, ok := h.requireProject(w, r)
	if !ok {
		return
	}
	OK(w, projectVCS(r.Context(), root))
}

// InitProjectVCS 在项目根执行 `git init`（**写操作**，必须由用户明确同意后才调用）。
//
// 只创建 `.git` 目录，不暂存、不提交 —— 见文件头对"不做提交"的说明。
// 幂等：已在仓库内直接返回现状。
func (h *SessionMapHandler) InitProjectVCS(w http.ResponseWriter, r *http.Request) {
	root, ok := h.requireProject(w, r)
	if !ok {
		return
	}
	state, err := initProjectRepo(r.Context(), root)
	if err != nil {
		if errors.Is(err, errGitMissing) {
			JSONWithCode(w, http.StatusConflict, CodeServiceUnavailable, nil, "git is not installed")
			return
		}
		// git 的 stderr 常带绝对路径：只进日志，不进响应体。用户能拿到的是
		// "没成功"这件事本身 —— 具体原因去引擎/网关日志里看。
		logAndRespond(w, err, http.StatusInternalServerError, "could not initialize version control")
		return
	}
	if !state.Repo {
		// init 返回 0 但目录仍不在工作树内（极少见：权限导致的空操作）。
		// 说清楚"没成功"比返回一个成功状态更诚实。
		logAndRespond(w, errors.New("git init reported success but directory is still not a work tree"),
			http.StatusInternalServerError, "could not initialize version control")
		return
	}
	OK(w, state)
}
