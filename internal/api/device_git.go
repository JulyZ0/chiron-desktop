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
	"strconv"
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

// clientProjectDiff 是 collectProjectDiff 的设备版本。
//
// 与 server 版本同一套判定（repo / untracked / binary / truncated），差异只在
// 执行位置：git 在设备上跑，参数仍由服务端构造，路径先过 joinClientPath。
func clientProjectDiff(r *http.Request, loc projectLocator, rel string) (ProjectDiff, error) {
	result := ProjectDiff{Path: rel, Diff: ""}
	rel = clientRelPath(rel)
	if _, ok := joinClientPath(loc.RootPath, rel); !ok {
		return result, errPathEscapes
	}

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

	others, err := clientGit(r, loc, "ls-files", "--others", "--exclude-standard", "-z", "--", rel)
	if err != nil {
		return result, err
	}
	if others.Code == 0 && strings.TrimSpace(others.Stdout) != "" {
		result.Untracked = true
		return result, nil
	}

	diff, err := clientGit(r, loc, "diff", "--no-ext-diff", "--unified="+gitDiffContextLines, "HEAD", "--", rel)
	if err != nil {
		return result, err
	}
	if diff.Code != 0 {
		// 空仓库：unborn HEAD，退回索引/工作区基线。
		diff, err = clientGit(r, loc, "diff", "--no-ext-diff", "--unified="+gitDiffContextLines, "--", rel)
		if err != nil {
			return result, err
		}
	}
	if diff.Code != 0 {
		return result, fmt.Errorf("%w: diff exit %d", errGitFailed, diff.Code)
	}
	if strings.Contains(diff.Stdout, "Binary files ") {
		result.Binary = true
	}
	result.Diff, result.Truncated = cappedDiff(diff.Stdout)
	return result, nil
}

// respondClientGitError 把设备侧失败统一成 502：不是用户错误，重试/重连设备即可。
func respondClientGitError(w http.ResponseWriter, err error) {
	JSONWithCode(w, http.StatusBadGateway, CodeRequestFailed,
		map[string]interface{}{"reason": err.Error()}, "device git failed")
}

// clientProjectLog 是 collectProjectLog 的设备版本（只读命令，参数仍由服务端构造）。
func clientProjectLog(r *http.Request, loc projectLocator, limit int) (ProjectLog, error) {
	result := ProjectLog{Commits: []ProjectCommit{}}
	if limit <= 0 || limit > maxProjectCommits {
		limit = 30
	}
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
	if br, err := clientGit(r, loc, "symbolic-ref", "--short", "-q", "HEAD"); err == nil && br.Code == 0 {
		result.Branch = strings.TrimSpace(br.Stdout)
	}
	if up, err := clientGit(r, loc, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"); err == nil && up.Code == 0 {
		result.Upstream = strings.TrimSpace(up.Stdout)
	}
	if result.Upstream != "" {
		if counts, err := clientGit(r, loc, "rev-list", "--left-right", "--count", "@{upstream}...HEAD"); err == nil && counts.Code == 0 {
			parts := strings.Fields(counts.Stdout)
			if len(parts) == 2 {
				result.Behind, _ = strconv.Atoi(parts[0])
				result.Ahead, _ = strconv.Atoi(parts[1])
			}
		}
	}
	logResp, err := clientGit(r, loc, "log", "--no-show-signature",
		fmt.Sprintf("-n%d", limit), "--format=%H%x00%h%x00%an%x00%aI%x00%s")
	if err != nil {
		return result, err
	}
	if logResp.Code == 0 {
		result.Commits = parseGitLog(logResp.Stdout)
	}
	return result, nil
}

// clientCommitDiff 是 collectCommitDiff 的设备版本。
func clientCommitDiff(r *http.Request, loc projectLocator, sha string) (ProjectDiff, error) {
	result := ProjectDiff{Diff: ""}
	if !validCommitSHA(sha) {
		return result, errPathNotRelative
	}
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
	diff, err := clientGit(r, loc, "show", "--no-ext-diff",
		"--unified="+gitDiffContextLines, "--format=", sha, "--")
	if err != nil {
		return result, err
	}
	if diff.Code != 0 {
		return result, fmt.Errorf("%w: show exit %d", errGitFailed, diff.Code)
	}
	if strings.Contains(diff.Stdout, "Binary files ") {
		result.Binary = true
	}
	result.Diff, result.Truncated = cappedDiff(diff.Stdout)
	return result, nil
}
