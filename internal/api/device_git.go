package api

// ── client 项目的 git（设备侧 RPC）──
//
// client 项目的文件在用户设备上，服务端没有工作树。版本控制状态、改动列表与
// `git init` 都必须回到设备执行：
//
//	网关 ──► deviceHub.Call("git") ──► Electron 主进程 ──► git（cwd=项目根）
//
// 安全边界：
//   - 参数**只由服务端构造**（staticGitFlags + 本文件里的固定子命令），渲染层无法下发；
//   - 设备端校验 cwd 在项目根内，参数数量/长度受限，输出 8MB、超时 30s；
//   - 读操作（vcs/changes）不要求写租约；`git init` 会建 `.git`，必须持有设备写租约。
//
// 与 server 项目同一套响应结构（ProjectVCS/ProjectChanges），前端不区分 provider。
// 行数统计（insertions/deletions）不为 client 计算：那需要两条 diff，回合镜像本身
// 也不带行级信息；返回 nil 与"git 没有统计"同义。

import (
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"
)

const clientGitTimeout = 30 * time.Second

// clientGit 在设备上执行一条 git 命令。
//
// 非零退出码通过 deviceOpResponse.Code 返回而不是 error —— "not a git repository"
// 是正常结果。只有设备离线/超时/参数非法才是 error。
func clientGit(r *http.Request, loc projectLocator, args ...string) (deviceOpResponse, error) {
	if len(args) == 0 {
		return deviceOpResponse{}, errors.New("missing git args")
	}
	full := make([]string, 0, 8+len(args))
	full = append(full, staticGitFlags()...)
	full = append(full, args...)
	resp, err := clientCall(r, loc, deviceOpRequest{
		Op:        "git",
		Path:      loc.RootPath,
		Args:      full,
		TimeoutMS: int(clientGitTimeout / time.Millisecond),
	})
	if err != nil {
		return resp, err
	}
	if resp.Truncated {
		return resp, errors.New("git output truncated")
	}
	return resp, nil
}

func clientGitMissing(resp deviceOpResponse) bool {
	return resp.Code == 127 || strings.Contains(strings.ToLower(resp.Stderr), "git not found")
}

// clientProjectVCS 是 projectVCS 的设备版本，语义与判定完全一致。
func clientProjectVCS(r *http.Request, loc projectLocator) (ProjectVCS, error) {
	resp, err := clientGit(r, loc, "rev-parse", "--is-inside-work-tree")
	if err != nil {
		return ProjectVCS{}, err
	}
	if clientGitMissing(resp) {
		return ProjectVCS{GitMissing: true}, nil
	}
	if resp.Code != 0 || strings.TrimSpace(resp.Stdout) != "true" {
		return ProjectVCS{}, nil
	}

	top, err := clientGit(r, loc, "rev-parse", "--show-toplevel")
	topRoot := ""
	if err == nil && top.Code == 0 {
		topRoot = strings.TrimSpace(top.Stdout)
	}
	return ProjectVCS{
		Repo:     true,
		RepoRoot: topRoot,
		OwnRoot:  topRoot != "" && sameRootPath(loc.RootPath, topRoot),
	}, nil
}

// clientInitProjectRepo 是 initProjectRepo 的设备版本：幂等，已在仓库内不做任何写操作。
func clientInitProjectRepo(r *http.Request, loc projectLocator) (ProjectVCS, error) {
	cur, err := clientProjectVCS(r, loc)
	if err != nil {
		return ProjectVCS{}, err
	}
	if cur.GitMissing {
		return cur, errGitMissing
	}
	if cur.Repo {
		return cur, nil
	}
	resp, err := clientGit(r, loc, "init", "-q")
	if err != nil {
		return cur, err
	}
	if resp.Code == 127 {
		return cur, errGitMissing
	}
	if resp.Code != 0 {
		// stderr 常带设备上的绝对路径：只进日志，不进响应体（与 server 版本同口径）。
		return cur, fmt.Errorf("%w: exit %d", errGitFailed, resp.Code)
	}
	return clientProjectVCS(r, loc)
}

// clientProjectChanges 是 collectProjectChanges 的设备版本。
//
// 只跑 rev-parse + status + symbolic-ref/rev-parse，不跑 diff numstat —— client 项目
// 的行数统计留空（nil），界面按"git 没有统计"处理。
func clientProjectChanges(r *http.Request, loc projectLocator) (ProjectChanges, error) {
	result := ProjectChanges{Changes: []ProjectChange{}}
	resp, err := clientGit(r, loc, "rev-parse", "--is-inside-work-tree")
	if err != nil {
		return result, err
	}
	if clientGitMissing(resp) {
		result.GitMissing = true
		return result, nil
	}
	if resp.Code != 0 || strings.TrimSpace(resp.Stdout) != "true" {
		return result, nil
	}
	result.Repo = true

	status, err := clientGit(r, loc, "status", "--porcelain=v1", "-z")
	if err != nil {
		return result, err
	}
	if status.Code != 0 {
		return result, fmt.Errorf("%w: status exit %d", errGitFailed, status.Code)
	}
	result.Changes = parseGitStatusZ(status.Stdout)
	if len(result.Changes) >= maxProjectChanges {
		result.Truncated = true
	}

	// symbolic-ref 在空仓库（还没有首个提交）上也能给出分支名；rev-parse HEAD 会失败，
	// 此时 Head 保持空串 —— 与 server 版本一致。
	if br, err := clientGit(r, loc, "symbolic-ref", "--short", "-q", "HEAD"); err == nil && br.Code == 0 {
		result.Branch = strings.TrimSpace(br.Stdout)
	}
	if head, err := clientGit(r, loc, "rev-parse", "--short", "HEAD"); err == nil && head.Code == 0 {
		result.Head = strings.TrimSpace(head.Stdout)
	}
	return result, nil
}

// respondClientGitError 把设备侧失败统一成 502：不是用户错误，重试/重连设备即可。
func respondClientGitError(w http.ResponseWriter, err error) {
	JSONWithCode(w, http.StatusBadGateway, CodeRequestFailed,
		map[string]interface{}{"reason": err.Error()}, "device git failed")
}
