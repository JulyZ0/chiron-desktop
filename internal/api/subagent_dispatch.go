package api

// 从界面直接发起子 Agent 委派（F3）。
//
// 与"父模型自己调 subagent 工具"走同一条引擎后台路径；网关只做两件事：
// 证明会话属于调用者、把身份原样转给引擎（引擎按 body 身份重新校验）。

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
)

const subagentDispatchPath = "/v1/internal/subagent/dispatch"

// DispatchSubagent 在当前会话下启动一个后台子 Agent（不阻塞任何父 turn）。
func (h *SubagentHandler) DispatchSubagent(w http.ResponseWriter, r *http.Request) {
	claims := authClaims(r)
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	sessionID := strings.TrimSpace(r.PathValue("session_id"))
	if sessionID == "" {
		BadRequest(w, "session_id is required")
		return
	}
	var body struct {
		Task       string `json:"task"`
		Profile    string `json:"profile"`
		AllowWrite bool   `json:"allow_write"`
		MaxTurns   int    `json:"max_turns"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	body.Task = strings.TrimSpace(body.Task)
	if body.Task == "" {
		BadRequest(w, "task is required")
		return
	}
	if !h.sessionOwnedByCaller(r.Context(), sessionID, claims.UserID, claims.TenantID) {
		// 会话不可见与不存在同一口径：不泄露别人的会话是否存在。
		NotFound(w, "session not found")
		return
	}
	if h.engine == nil {
		JSON(w, http.StatusServiceUnavailable, APIResponse{
			Success: false, Error: "dispatch unavailable", Code: CodeServiceUnavailable,
		})
		return
	}
	payload, err := json.Marshal(map[string]any{
		"task":        body.Task,
		"session_id":  sessionID,
		"tenant_id":   claims.TenantID,
		"user_id":     claims.UserID,
		"profile":     strings.TrimSpace(body.Profile),
		"allow_write": body.AllowWrite,
		"max_turns":   body.MaxTurns,
	})
	if err != nil {
		InternalError(w, "dispatch unavailable")
		return
	}
	resp, err := h.engine.PostResponse(r.Context(), subagentDispatchPath, payload)
	if err != nil {
		slog.Warn("subagent dispatch forward failed", "session", sessionID, "error", err)
		JSON(w, http.StatusServiceUnavailable, APIResponse{
			Success: false, Error: "dispatch unavailable", Code: CodeServiceUnavailable,
		})
		return
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<10))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		slog.Warn("subagent dispatch rejected by engine", "session", sessionID,
			"status", resp.StatusCode, "body", string(raw))
		code := engineDetailCode(raw, "dispatch_refused")
		status := http.StatusConflict
		if resp.StatusCode == http.StatusServiceUnavailable {
			status = http.StatusServiceUnavailable
			code = CodeServiceUnavailable
		}
		JSON(w, status, APIResponse{
			Success: false, Error: engineDetailError(raw, "subagent dispatch refused"), Code: code,
		})
		return
	}
	var parsed struct {
		OK    bool   `json:"ok"`
		RunID string `json:"run_id"`
	}
	if err := json.Unmarshal(raw, &parsed); err != nil || !parsed.OK || parsed.RunID == "" {
		JSON(w, http.StatusServiceUnavailable, APIResponse{
			Success: false, Error: "dispatch unavailable", Code: CodeServiceUnavailable,
		})
		return
	}
	OK(w, map[string]any{"run_id": parsed.RunID, "status": "running"})
}

// authClaims 避免在多个 handler 里重复 auth.GetClaims 的 nil 处理。
func authClaims(r *http.Request) *auth.Claims { return auth.GetClaims(r.Context()) }

