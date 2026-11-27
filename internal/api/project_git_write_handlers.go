// project_git_write_handlers.go —— Git 写操作的 HTTP 层（N5 P1）。
//
// 本阶段只开放 **stage / unstage**：它们是"提交前可撤销"的一步，失败代价最低，
// 也足以把权限、锁、审计、路径校验这条链路跑通。commit/discard/分支在 P2/P3。
//
// 每个写请求的顺序是固定的：
//
//  1. 项目归属与根可用（`requireProject`，含隔离根校验）
//  2. client 项目直接拒绝（设备桥没有 git 写实现）
//  3. 必须是 git 工作树
//  4. 项目下没有活跃 run（否则会把"写了一半的回合"冻进索引）
//  5. 抢项目级写锁（多副本用 Redis；单机回落到进程内互斥）
//  6. 路径只取当前 status 条目（`allowedGitPaths`）
//  7. 执行 → 重新读改动 → 审计
package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/redis/go-redis/v9"
)

// errGitWriteBusy 表示同一项目的另一个写操作正在进行。
var errGitWriteBusy = errors.New("another git write is in progress for this project")

// gitWriteLockTTL 覆盖一次写命令的最长耗时；进程崩溃时锁会自动过期。
const gitWriteLockTTL = 45 * time.Second

// localGitLocks 是 Redis 不可用时的进程内兜底（桌面单机就是这种形态）。
var localGitLocks sync.Map // projectID -> *sync.Mutex

const releaseGitLockLua = `if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
end
return 0`

// acquireProjectGitLock 抢项目级写锁，返回释放函数。
//
// 多副本必须用 Redis：否则 A 副本在 commit、B 副本在 discard，同一个 index 会被
// 两条命令交叉改写。单机（Redis 未配置）用进程内 Mutex，语义一样但只对一个进程有效。
func acquireProjectGitLock(ctx context.Context, projectID string) (func(), error) {
	if db.Redis == nil {
		value, _ := localGitLocks.LoadOrStore(projectID, &sync.Mutex{})
		mu := value.(*sync.Mutex)
		if !mu.TryLock() {
			return nil, errGitWriteBusy
		}
		return mu.Unlock, nil
	}
	key := db.RedisKey("git:write:lock:" + projectID)
	token := newUUID()
	// RedisClient 没有 SetNX：用 SET NX PX 走 Do。NX 失败时 Redis 返回 nil，
	// go-redis 表现为 redis.Nil —— 那是"锁已被占"，不是故障。
	res := db.Redis.Do(ctx, "SET", key, token, "NX", "PX",
		int64(gitWriteLockTTL/time.Millisecond))
	if err := res.Err(); err != nil {
		if errors.Is(err, redis.Nil) {
			return nil, errGitWriteBusy
		}
		return nil, err
	}
	return func() {
		// 释放用独立的短超时：请求 ctx 可能已经被取消，而锁必须还回去。
		releaseCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = db.Redis.Eval(releaseCtx, releaseGitLockLua, []string{key}, token).Err()
	}, nil
}

// projectHasActiveRuns 判断项目下是否有未终态的 run。
//
// 用 DB 而不是网关内存里的 InFlight：后者只看得见**本副本**受理的 run，多副本部署时
// 另一台的 run 会让这里误判为"空闲"。agent_runs 是引擎写入的权威状态（终态必落库）。
func projectHasActiveRuns(ctx context.Context, projectID string) (bool, error) {
	if db.GlobalDBManager == nil {
		return false, nil
	}
	row, err := db.GlobalDBManager.FetchOne(ctx,
		`SELECT 1 AS active
		   FROM agent_runs r
		   JOIN session_map_nodes n ON n.session_id = r.session_id
		  WHERE n.workspace_id = $1 AND n.edge_kind = 'project'
		    AND r.status IN ('running', 'checkpointed', 'resuming')
		  LIMIT 1`, projectID)
	if err != nil {
		return false, err
	}
	return row != nil, nil
}

type gitPathsRequest struct {
	Paths []string `json:"paths"`
	// All 表示"当前改动里的全部条目"，省去前端把路径全传一遍。
	All bool `json:"all"`
}

// StageProjectGit POST /v1/session-map/projects/{id}/git/stage
func (h *SessionMapHandler) StageProjectGit(w http.ResponseWriter, r *http.Request) {
	h.projectGitIndexAction(w, r, true)
}

// UnstageProjectGit POST /v1/session-map/projects/{id}/git/unstage
func (h *SessionMapHandler) UnstageProjectGit(w http.ResponseWriter, r *http.Request) {
	h.projectGitIndexAction(w, r, false)
}

// beginProjectGitWrite 跑完所有写操作共用的前置检查，返回项目与释放函数。
//
// 顺序与文件头一致：归属 → client → git 仓库 → 活跃 run → 项目锁。
// 失败时已经写好响应，调用方只需 return。
func (h *SessionMapHandler) beginProjectGitWrite(w http.ResponseWriter, r *http.Request) (projectLocator, func(), bool) {
	loc, ok := h.requireProject(w, r)
	if !ok {
		return projectLocator{}, nil, false
	}
	if loc.Provider == smProviderClient {
		JSONWithCode(w, http.StatusConflict, CodeGitClientUnsupported, nil,
			"git write operations are not supported for device projects")
		return projectLocator{}, nil, false
	}
	if !projectVCS(r.Context(), loc.RootPath).Repo {
		JSONWithCode(w, http.StatusConflict, CodeGitNotRepo, nil,
			"project is not a git work tree")
		return projectLocator{}, nil, false
	}
	active, err := projectHasActiveRuns(r.Context(), loc.ID)
	if err != nil {
		logAndRespond(w, err, http.StatusServiceUnavailable, ErrDBUnavailable)
		return projectLocator{}, nil, false
	}
	if active {
		// 等这一轮跑完就能写：明确告诉客户端"可重试"。
		JSONWithRetryable(w, http.StatusConflict, CodeGitBusy, nil,
			"an agent run is still active for this project", true)
		return projectLocator{}, nil, false
	}
	release, err := acquireProjectGitLock(r.Context(), loc.ID)
	if err != nil {
		if errors.Is(err, errGitWriteBusy) {
			JSONWithRetryable(w, http.StatusConflict, CodeGitBusy, nil,
				"another git write is in progress for this project", true)
			return projectLocator{}, nil, false
		}
		logAndRespond(w, err, http.StatusServiceUnavailable, ErrDBUnavailable)
		return projectLocator{}, nil, false
	}
	return loc, release, true
}

// projectGitIndexAction 是 stage / unstage 的共用实现。`stage=false` 表示反向操作。
func (h *SessionMapHandler) projectGitIndexAction(w http.ResponseWriter, r *http.Request, stage bool) {
	loc, release, ok := h.beginProjectGitWrite(w, r)
	if !ok {
		return
	}
	defer release()

	var body gitPathsRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64*1024)).Decode(&body); err != nil {
		BadRequest(w, "invalid request body")
		return
	}
	if !body.All && len(body.Paths) == 0 {
		JSONWithCode(w, http.StatusBadRequest, CodeInvalidRequest, nil,
			"paths or all is required")
		return
	}

	before, err := collectProjectChanges(r.Context(), loc.RootPath)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "could not read git changes")
		return
	}

	paths, err := gitIndexPaths(body, before, stage)
	if err != nil {
		JSONWithCode(w, http.StatusBadRequest, CodeInvalidRequest, nil, err.Error())
		return
	}

	if stage {
		err = stageGitPaths(r.Context(), loc.RootPath, paths)
	} else {
		err = unstageGitPaths(r.Context(), loc.RootPath, paths)
	}
	if err != nil {
		switch {
		case errors.Is(err, errGitMissing):
			JSONWithCode(w, http.StatusConflict, CodeServiceUnavailable, nil, "git is not installed")
		case errors.Is(err, errGitFailed):
			// git 的 stderr 常带绝对路径：只进日志。
			logAndRespond(w, err, http.StatusInternalServerError, "git index operation failed")
		default:
			logAndRespond(w, err, http.StatusInternalServerError, "git index operation failed")
		}
		return
	}

	after, err := collectProjectChanges(r.Context(), loc.RootPath)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "could not read git changes")
		return
	}

	if claims := auth.GetClaims(r.Context()); claims != nil {
		action := "project.git.stage"
		if !stage {
			action = "project.git.unstage"
		}
		db.AuditLog(r.Context(), claims.UserID, smTenantID(claims), action,
			"session_map_workspaces:"+loc.ID,
			fmt.Sprintf("files=%d", len(paths)), r.RemoteAddr,
			map[string]interface{}{"project_id": loc.ID, "file_count": len(paths)})
	}
	OK(w, after)
}

// gitIndexPaths 决定本次要操作的路径。
//
// `all` 取当前 status 的全部条目（unstage 只取已暂存的）；显式路径必须落在允许集合里。
func gitIndexPaths(body gitPathsRequest, changes ProjectChanges, stage bool) ([]string, error) {
	if body.All {
		paths := make([]string, 0, len(changes.Changes))
		for _, change := range changes.Changes {
			if !stage && !change.Staged {
				continue
			}
			if change.Path != "" {
				paths = append(paths, change.Path)
			}
		}
		if len(paths) == 0 {
			return nil, errors.New("nothing to stage")
		}
		return paths, nil
	}
	if stage {
		return allowedGitPaths(body.Paths, changes)
	}
	// unstage：只允许"当前确实已暂存"的路径，避免把未暂存路径传给 git reset 造成误解。
	staged := ProjectChanges{Changes: make([]ProjectChange, 0, len(changes.Changes))}
	for _, change := range changes.Changes {
		if change.Staged {
			staged.Changes = append(staged.Changes, change)
		}
	}
	return allowedGitPaths(body.Paths, staged)
}

// stageGitPaths 把路径加入索引。
//
// `-A` 必须带：删除的文件在工作区已不存在，裸 `git add -- <path>` 会报
// "pathspec did not match"，用户看到的会是"删掉的文件永远无法暂存"。
func stageGitPaths(ctx context.Context, root string, paths []string) error {
	args := append([]string{"add", "-A"}, gitPathsArgs(paths)...)
	_, err := gitWrite(ctx, root, args...)
	return err
}

// unstageGitPaths 把路径移出索引（工作区不动）。
//
// 用 `git reset --` 而不是 `git restore --staged`：后者在**尚无提交**的新仓库里
// 会失败（没有 HEAD 可比较），而"刚 init 的项目第一次暂存后想撤回"是真实场景。
func unstageGitPaths(ctx context.Context, root string, paths []string) error {
	args := append([]string{"reset", "-q"}, gitPathsArgs(paths)...)
	_, err := gitWrite(ctx, root, args...)
	return err
}

// ── P2：commit ──────────────────────────────────────────────────────────

type gitCommitRequest struct {
	Message string `json:"message"`
}

// GitCommitResult 是提交成功的响应：前端要 sha 来提示"提交到哪一个"，
// 并直接用 `changes` 刷新列表（避免再发一次 GET 引起闪烁）。
type GitCommitResult struct {
	CommitSHA string         `json:"commit_sha"`
	Changes   ProjectChanges `json:"changes"`
}

// CommitProjectGit POST /v1/session-map/projects/{id}/git/commit
func (h *SessionMapHandler) CommitProjectGit(w http.ResponseWriter, r *http.Request) {
	loc, release, ok := h.beginProjectGitWrite(w, r)
	if !ok {
		return
	}
	defer release()

	var body gitCommitRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 32*1024)).Decode(&body); err != nil {
		BadRequest(w, "invalid request body")
		return
	}
	message := strings.TrimSpace(body.Message)
	if message == "" {
		JSONWithCode(w, http.StatusBadRequest, CodeInvalidRequest, nil,
			"commit message is required")
		return
	}
	if len(message) > 5000 {
		JSONWithCode(w, http.StatusBadRequest, CodeInvalidRequest, nil,
			"commit message is too long")
		return
	}

	before, err := collectProjectChanges(r.Context(), loc.RootPath)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "could not read git changes")
		return
	}
	staged := 0
	for _, change := range before.Changes {
		if change.Staged {
			staged++
		}
	}
	if staged == 0 {
		// **只提交已暂存**：不做隐式 `add -A`。用户没勾的东西不该被写进历史。
		JSONWithCode(w, http.StatusBadRequest, CodeInvalidRequest, nil,
			"nothing is staged; stage files before committing")
		return
	}

	sha, err := commitStagedPaths(r.Context(), loc.RootPath, message)
	if err != nil {
		switch {
		case errors.Is(err, errGitMissing):
			JSONWithCode(w, http.StatusConflict, CodeServiceUnavailable, nil, "git is not installed")
		case errors.Is(err, errGitFailed):
			logAndRespond(w, err, http.StatusInternalServerError, "git commit failed")
		default:
			logAndRespond(w, err, http.StatusInternalServerError, "git commit failed")
		}
		return
	}

	after, err := collectProjectChanges(r.Context(), loc.RootPath)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "could not read git changes")
		return
	}
	if claims := auth.GetClaims(r.Context()); claims != nil {
		db.AuditLog(r.Context(), claims.UserID, smTenantID(claims), "project.git.commit",
			"session_map_workspaces:"+loc.ID,
			fmt.Sprintf("sha=%s files=%d", sha, staged), r.RemoteAddr,
			map[string]interface{}{"project_id": loc.ID, "file_count": staged, "commit_sha": sha})
	}
	OK(w, GitCommitResult{CommitSHA: sha, Changes: after})
}

// commitStagedPaths 提交索引里已有的内容。**不带 `-a`**：工作区里未暂存的改动
// 不参与这次提交；也不支持 amend/签名（历史改写与凭据交互不在 N5 范围）。
func commitStagedPaths(ctx context.Context, root, message string) (string, error) {
	if strings.TrimSpace(message) == "" {
		return "", errors.New("commit message is required")
	}
	if _, err := gitWrite(ctx, root, "commit", "-m", message); err != nil {
		return "", err
	}
	sha, err := gitRead(ctx, root, "rev-parse", "--short", "HEAD")
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(sha), nil
}

// ── P3：discard / 分支 ──────────────────────────────────────────────────

type gitDiscardRequest struct {
	Paths []string `json:"paths"`
}

// DiscardProjectGit POST /v1/session-map/projects/{id}/git/discard
//
// 只丢弃**未暂存的已跟踪改动**：先 `stash push --keep-index` 留一份可找回的副本，
// 再把工作区回退到 index。未跟踪文件不在范围内 —— 它们没有基线可回退，删掉就是
// 真的删掉，不能靠 stash 恢复。
func (h *SessionMapHandler) DiscardProjectGit(w http.ResponseWriter, r *http.Request) {
	loc, release, ok := h.beginProjectGitWrite(w, r)
	if !ok {
		return
	}
	defer release()

	var body gitDiscardRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64*1024)).Decode(&body); err != nil {
		BadRequest(w, "invalid request body")
		return
	}
	if len(body.Paths) == 0 {
		JSONWithCode(w, http.StatusBadRequest, CodeInvalidRequest, nil, "paths is required")
		return
	}

	before, err := collectProjectChanges(r.Context(), loc.RootPath)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "could not read git changes")
		return
	}
	// 只允许"有未暂存改动的已跟踪文件"。
	discardable := ProjectChanges{}
	for _, change := range before.Changes {
		if change.Unstaged && change.Status != "?" {
			discardable.Changes = append(discardable.Changes, change)
		}
	}
	paths, err := allowedGitPaths(body.Paths, discardable)
	if err != nil {
		JSONWithCode(w, http.StatusBadRequest, CodeInvalidRequest, nil,
			"only unstaged tracked files can be discarded")
		return
	}

	if err := discardUnstagedPaths(r.Context(), loc.RootPath, paths); err != nil {
		switch {
		case errors.Is(err, errGitMissing):
			JSONWithCode(w, http.StatusConflict, CodeServiceUnavailable, nil, "git is not installed")
		default:
			logAndRespond(w, err, http.StatusInternalServerError, "git discard failed")
		}
		return
	}

	after, err := collectProjectChanges(r.Context(), loc.RootPath)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "could not read git changes")
		return
	}
	if claims := auth.GetClaims(r.Context()); claims != nil {
		db.AuditLog(r.Context(), claims.UserID, smTenantID(claims), "project.git.discard",
			"session_map_workspaces:"+loc.ID,
			fmt.Sprintf("files=%d", len(paths)), r.RemoteAddr,
			map[string]interface{}{"project_id": loc.ID, "file_count": len(paths)})
	}
	OK(w, after)
}

// discardUnstagedPaths 把未暂存改动存入 stash（可恢复），并回退工作区到 index。
func discardUnstagedPaths(ctx context.Context, root string, paths []string) error {
	args := []string{
		"stash", "push", "--keep-index",
		"-m", fmt.Sprintf("chiron-discard:%d", time.Now().Unix()),
	}
	args = append(args, gitPathsArgs(paths)...)
	_, err := gitWrite(ctx, root, args...)
	return err
}

// GitBranches 是分支列表响应。
type GitBranches struct {
	Branches []string `json:"branches"`
	Current  string   `json:"current"`
}

// ListProjectGitBranches GET /v1/session-map/projects/{id}/git/branches
func (h *SessionMapHandler) ListProjectGitBranches(w http.ResponseWriter, r *http.Request) {
	loc, ok := h.requireProject(w, r)
	if !ok {
		return
	}
	if loc.Provider == smProviderClient {
		JSONWithCode(w, http.StatusConflict, CodeGitClientUnsupported, nil,
			"git branches are not supported for device projects")
		return
	}
	if !projectVCS(r.Context(), loc.RootPath).Repo {
		JSONWithCode(w, http.StatusConflict, CodeGitNotRepo, nil, "project is not a git work tree")
		return
	}
	branches, err := listGitBranches(r.Context(), loc.RootPath)
	if err != nil {
		if errors.Is(err, errGitMissing) {
			JSONWithCode(w, http.StatusConflict, CodeServiceUnavailable, nil, "git is not installed")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "could not list branches")
		return
	}
	OK(w, branches)
}

func listGitBranches(ctx context.Context, root string) (GitBranches, error) {
	out, err := gitRead(ctx, root, "for-each-ref", "--format=%(refname:short)", "refs/heads")
	if err != nil {
		return GitBranches{}, err
	}
	branches := make([]string, 0, 8)
	for _, line := range strings.Split(out, "\n") {
		if name := strings.TrimSpace(line); name != "" {
			branches = append(branches, name)
		}
	}
	// detached HEAD 时 symbolic-ref 退出非 0：那是正常状态（current 为空）。
	current, _ := gitRead(ctx, root, "symbolic-ref", "--short", "-q", "HEAD")
	return GitBranches{Branches: branches, Current: strings.TrimSpace(current)}, nil
}

type gitBranchRequest struct {
	// Action: create | switch
	Action string `json:"action"`
	Name   string `json:"name"`
	From   string `json:"from"`
}

// BranchProjectGit POST /v1/session-map/projects/{id}/git/branch
func (h *SessionMapHandler) BranchProjectGit(w http.ResponseWriter, r *http.Request) {
	loc, release, ok := h.beginProjectGitWrite(w, r)
	if !ok {
		return
	}
	defer release()

	var body gitBranchRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 16*1024)).Decode(&body); err != nil {
		BadRequest(w, "invalid request body")
		return
	}
	action := strings.TrimSpace(body.Action)
	name := strings.TrimSpace(body.Name)
	if name == "" || (action != "create" && action != "switch") {
		JSONWithCode(w, http.StatusBadRequest, CodeInvalidRequest, nil,
			"action (create|switch) and name are required")
		return
	}
	// 交给 git 自己判定引用名合法性：`..`、空格、`~^:?*[` 等规则由它维护，
	// 在 Go 里抄一份迟早漂移。
	if _, err := gitRead(r.Context(), loc.RootPath, "check-ref-format", "--branch", name); err != nil {
		JSONWithCode(w, http.StatusBadRequest, CodeInvalidRequest, nil, "invalid branch name")
		return
	}

	if action == "switch" {
		changes, err := collectProjectChanges(r.Context(), loc.RootPath)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "could not read git changes")
			return
		}
		if len(changes.Changes) > 0 {
			JSONWithCode(w, http.StatusConflict, CodeGitDirty, nil,
				"working tree is not clean; commit or discard changes before switching branches")
			return
		}
	}

	var err error
	if action == "switch" {
		_, err = gitWrite(r.Context(), loc.RootPath, "switch", name)
	} else {
		args := []string{"branch", name}
		if from := strings.TrimSpace(body.From); from != "" {
			args = append(args, from)
		}
		_, err = gitWrite(r.Context(), loc.RootPath, args...)
	}
	if err != nil {
		if errors.Is(err, errGitMissing) {
			JSONWithCode(w, http.StatusConflict, CodeServiceUnavailable, nil, "git is not installed")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "git branch operation failed")
		return
	}

	branches, err := listGitBranches(r.Context(), loc.RootPath)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "could not list branches")
		return
	}
	if claims := auth.GetClaims(r.Context()); claims != nil {
		db.AuditLog(r.Context(), claims.UserID, smTenantID(claims), "project.git.branch",
			"session_map_workspaces:"+loc.ID,
			action+"="+name, r.RemoteAddr,
			map[string]interface{}{"project_id": loc.ID, "action": action, "branch": name})
	}
	OK(w, branches)
}
