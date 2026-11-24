package api

// ── 子 Agent 重跑（P0-2）──
//
//	POST /v1/subagent/runs/{run_id}/rerun
//
// 语义：把一个**已结束**的子 Agent run 按它**原来那条任务描述**重新派发一次，
// 立即返回新 run 的 id（新 run 在后台跑，事件经 `subagent.*` 汇入会话事件流）。
//
// 为什么由网关转发而不是网关自己写库：真正的派发在引擎里（asyncio 任务、工具面、
// 预算、落库都在那边）。网关只做三件事 —— **权限与会话归属校验**、**状态口径**
// （非终态直接 409，不必打一趟引擎）、**错误码如实透传**（404/409/503）。
//
// 安全口径与既有只读端点一致：别人的 run 一律按"不存在"处理（404），不泄露存在性。
// 重跑**不是提权通道**：写权限取原 run 的 `read_only`，由引擎沿用（见
// python-engine/app/subagent/rerun.py）。

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
)

// subagentRerunPath 是引擎侧内网端点的前缀（与 app/api/internal_subagent.py 逐字对应）。
const subagentRerunPath = "/v1/internal/subagent/runs/"

// rerunTarget 是网关侧判定"这条 run 能不能重跑"所需的最小信息。
type rerunTarget struct {
	Status    string
	SessionID string
}

// subagentRerunGuard 判定某状态能否重跑；返回空串表示可重跑，否则是不可重跑的原因码。
//
// 抽成纯函数是为了让它能被单测：这里是"会不会派出第二份并发作业"的唯一闸门，
// 而 running 的 run 已经有 owner（引擎侧的 asyncio 任务），再派一份就是两份并发作业。
// 与引擎侧 `RERUNNABLE`（app/subagent/rerun.py）同一集合，终态白名单复用
// `terminalRunStatuses`（它已与引擎状态机对齐，见 subagent_cancel.go 的说明）。
func subagentRerunGuard(status string) string {
	s := strings.TrimSpace(strings.ToLower(status))
	if s == "" {
		return "run_status_unknown"
	}
	if terminalRunStatuses[s] {
		return ""
	}
	return "run_not_terminal"
}

// rerunTargetFor 读取 run 的状态与会话（**同时**校验租户与用户归属）。
//
// 三条判据与引擎侧 `RUN_ACCESS_CLAUSE` 同口径（tenant + user + session 都必须对得上），
// 只是这里先做一次"能不能碰"的判定，避免把明显越权的请求再打一趟引擎。
func (h *SubagentHandler) rerunTargetFor(ctx context.Context, tenant, userID, runID string) (rerunTarget, bool) {
	var out rerunTarget
	if tenant == "" || runID == "" || h.store == nil {
		return out, false
	}
	row, err := h.store.FetchOne(ctx, `
SELECT status, COALESCE(root_session_id, '') AS root_session_id
  FROM subagent_runs
 WHERE id = $1 AND tenant_id = $2
   AND (user_id = $3 OR user_id IS NULL)`, runID, tenant, userID)
	if err != nil || row == nil {
		return out, false
	}
	out.Status, _ = row["status"].(string)
	out.SessionID, _ = row["root_session_id"].(string)
	return out, true
}

// tenantInt 把 claims 的租户规约到整数域（引擎侧的 tenant_id 是 smallint 且必填）。
//
// 与 `subagentTenant` 的分工：那个回字符串（Redis key / DB 文本比较用），
// 这个只在"要发给引擎的 body"里用 —— 传字符串会让引擎侧的 SQL 参数类型出错。
func tenantInt(claims *auth.Claims) int {
	if claims == nil {
		return 0
	}
	if claims.TenantID != 0 {
		return int(claims.TenantID)
	}
	n, err := strconv.Atoi(strings.TrimSpace(claims.UserID))
	if err != nil {
		return 0
	}
	return n
}

// RerunRun 按原任务重跑一个已结束的子 Agent run。
func (h *SubagentHandler) RerunRun(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	runID := strings.TrimSpace(r.PathValue("run_id"))
	if runID == "" {
		BadRequest(w, "run_id is required")
		return
	}
	tenantID := tenantInt(claims)
	if tenantID == 0 {
		// 没有租户上下文的会话没有"归属"可言：按不可见处理（与只读端点同一口径，
		// 不泄露存在性，也不给引擎送一个必然被拒的身份）。
		NotFound(w, "subagent run not found")
		return
	}
	target, visible := h.rerunTargetFor(r.Context(), subagentTenant(claims), claims.UserID, runID)
	if !visible {
		NotFound(w, "subagent run not found")
		return
	}
	// 会话归属：不能凭一条不属于自己的会话去重跑（与 CancelSessionRuns 同一套判定）
	if target.SessionID != "" && !h.sessionOwnedByCaller(r.Context(), target.SessionID, claims.UserID, claims.TenantID) {
		NotFound(w, "subagent run not found")
		return
	}
	if reason := subagentRerunGuard(target.Status); reason != "" {
		// 非终态：不派发（引擎侧同样会拒，这里先挡掉，省一趟往返也给出明确原因）
		JSON(w, http.StatusConflict, APIResponse{
			Success: false,
			Error:   "subagent run is " + target.Status + "; only finished runs can be rerun",
			Code:    reason,
		})
		return
	}
	if h.engine == nil {
		JSON(w, http.StatusServiceUnavailable, APIResponse{
			Success: false, Error: "rerun unavailable", Code: CodeServiceUnavailable,
		})
		return
	}

	body, err := json.Marshal(map[string]any{
		"tenant_id":  tenantID,
		"user_id":    claims.UserID,
		"session_id": target.SessionID,
	})
	if err != nil {
		InternalError(w, "rerun unavailable")
		return
	}
	resp, err := h.engine.PostResponse(
		r.Context(), subagentRerunPath+url.PathEscape(runID)+"/rerun", body,
	)
	if err != nil {
		slog.Warn("subagent rerun forward failed", "run_id", runID, "error", err)
		JSON(w, http.StatusServiceUnavailable, APIResponse{
			Success: false, Error: "rerun unavailable", Code: CodeServiceUnavailable,
		})
		return
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<10))

	// 引擎的状态码是**权威**（它会再校验一次状态与任务描述）：如实透传，不粉饰。
	switch {
	case resp.StatusCode == http.StatusNotFound:
		NotFound(w, "subagent run not found")
		return
	case resp.StatusCode == http.StatusConflict:
		JSON(w, http.StatusConflict, APIResponse{
			Success: false, Error: engineDetailError(raw, "subagent run cannot be rerun"),
			Code: engineDetailCode(raw, "run_not_terminal"),
		})
		return
	case resp.StatusCode < 200 || resp.StatusCode >= 300:
		slog.Warn("subagent rerun rejected by engine", "run_id", runID,
			"status", resp.StatusCode, "body", string(raw))
		JSON(w, http.StatusServiceUnavailable, APIResponse{
			Success: false, Error: engineDetailError(raw, "rerun unavailable"),
			Code: CodeServiceUnavailable,
		})
		return
	}

	var parsed struct {
		OK      bool   `json:"ok"`
		RunID   string `json:"run_id"`
		RerunOf string `json:"rerun_of"`
		Status  string `json:"status"`
		Depth   int    `json:"depth"`
	}
	if err := json.Unmarshal(raw, &parsed); err != nil || !parsed.OK || parsed.RunID == "" {
		// 引擎 2xx 但没给出新 run id = 没有真的派发出去。绝不返回一个假的 run_id。
		slog.Error("subagent rerun returned no run id", "run_id", runID, "body", string(raw))
		JSON(w, http.StatusBadGateway, APIResponse{
			Success: false, Error: "rerun did not start a new run", Code: CodeRequestFailed,
		})
		return
	}

	slog.Info("subagent rerun accepted", "from", runID, "to", parsed.RunID,
		"user_id", claims.UserID, "session_id", target.SessionID)
	OK(w, map[string]any{
		"status":        "accepted",
		"run_id":        parsed.RunID,
		"rerun_of":      runID,
		"parent_run_id": runID,
		"depth":         parsed.Depth,
	})
}

// engineDetailError 从引擎的 `{"detail": {"error": "..."}}` 里取出可读原因。
func engineDetailError(raw []byte, fallback string) string {
	var body struct {
		Detail struct {
			Error string `json:"error"`
		} `json:"detail"`
		Error string `json:"error"`
	}
	if json.Unmarshal(raw, &body) == nil {
		if body.Detail.Error != "" {
			return body.Detail.Error
		}
		if body.Error != "" {
			return body.Error
		}
	}
	return fallback
}

// engineDetailCode 取引擎给出的稳定错误码（`task_missing` / `run_not_terminal` …），
// 前端据此区分"回退到输入框"与"稍后再试"。
func engineDetailCode(raw []byte, fallback string) string {
	var body struct {
		Detail struct {
			Code string `json:"code"`
		} `json:"detail"`
	}
	if json.Unmarshal(raw, &body) == nil && body.Detail.Code != "" {
		return body.Detail.Code
	}
	return fallback
}
