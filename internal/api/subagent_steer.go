package api

// 父 → 子 steer 的网关入口（功能 F1）。
//
// 为什么不在客户端直接打引擎：子 Agent 的 run 注册表是**进程内**的，必须先按
// `subagent:run:{run_id}` 找到持有它的实例；同时归属校验（tenant+user+run）在网关侧
// 才有 DB 权威行。缺任一层，别人就能往你的子 Agent 里塞指令（内容注入），或把请求
// 随机打到不持有该 run 的副本上得到"假失败"。

import (
	"encoding/json"
	"io"
	"net/url"
	"log/slog"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

const subagentSteerPath = "/v1/internal/subagent/runs/%s/steer"

// SteerRun 给一个**运行中**的子 Agent 追加指令（不打断当前 run）。
func (h *SubagentHandler) SteerRun(w http.ResponseWriter, r *http.Request) {
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
	var body struct {
		Text string `json:"text"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	body.Text = strings.TrimSpace(body.Text)
	if body.Text == "" {
		BadRequest(w, "text is required")
		return
	}

	// 归属：别人的 run 一律 404（不泄露存在性），与取消/重跑同一套判定。
	status, _, visible := h.runAccessForCaller(
		r.Context(), subagentTenant(claims), claims.UserID, runID,
	)
	if !visible {
		NotFound(w, "subagent run not found")
		return
	}
	if terminalRunStatuses[status] {
		JSON(w, http.StatusConflict, APIResponse{
			Success: false,
			Error:   "subagent run is " + status + "; instruction was not delivered",
			Code:    "run_finished",
		})
		return
	}
	if h.engine == nil {
		JSON(w, http.StatusServiceUnavailable, APIResponse{
			Success: false, Error: "steer unavailable", Code: CodeServiceUnavailable,
		})
		return
	}

	ownerURL, owner, ok := engine.SubagentRunOwnerURL(r.Context(), runID)
	if !ok || ownerURL == "" {
		// 没有归属 = 没有实例持有它（已退出/TTL 过期/Redis 抖动）。不是"已受理"。
		JSON(w, http.StatusConflict, APIResponse{
			Success: false,
			Error:   "subagent run is not active on any instance",
			Code:    "run_not_active",
		})
		return
	}

	payload, err := json.Marshal(map[string]any{
		"text":    body.Text,
		"sender":  claims.UserID,
		"user_id": claims.UserID,
	})
	if err != nil {
		InternalError(w, "steer unavailable")
		return
	}
	resp, err := h.engine.PostResponseTo(
		r.Context(), ownerURL, fmtSteerPath(runID), payload,
	)
	if err != nil {
		slog.Warn("subagent steer forward failed", "run_id", runID,
			"owner", owner.InstanceID, "error", err)
		JSON(w, http.StatusServiceUnavailable, APIResponse{
			Success: false, Error: "steer unavailable", Code: CodeServiceUnavailable,
		})
		return
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<10))

	// 引擎状态码是权威：如实透传，不把"没送到"粉饰成成功。
	switch resp.StatusCode {
	case http.StatusNotFound:
		JSON(w, http.StatusConflict, APIResponse{
			Success: false, Error: "subagent run is not active on its owner",
			Code: "run_not_active",
		})
		return
	case http.StatusConflict:
		JSON(w, http.StatusConflict, APIResponse{
			Success: false, Error: engineDetailError(raw, "subagent run has finished"),
			Code:    engineDetailCode(raw, "run_finished"),
		})
		return
	case http.StatusTooManyRequests:
		JSON(w, http.StatusTooManyRequests, APIResponse{
			Success: false, Error: engineDetailError(raw, "steer queue is full"),
			Code:    "steer_queue_full",
		})
		return
	case http.StatusBadRequest:
		JSON(w, http.StatusBadRequest, APIResponse{
			Success: false, Error: engineDetailError(raw, "invalid instruction"),
			Code:    engineDetailCode(raw, "invalid_message"),
		})
		return
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		slog.Warn("subagent steer rejected by engine", "run_id", runID,
			"status", resp.StatusCode, "body", string(raw))
		JSON(w, http.StatusServiceUnavailable, APIResponse{
			Success: false, Error: "steer unavailable", Code: CodeServiceUnavailable,
		})
		return
	}

	var parsed struct {
		OK       bool   `json:"ok"`
		Delivery string `json:"delivery"`
	}
	if err := json.Unmarshal(raw, &parsed); err != nil || !parsed.OK {
		JSON(w, http.StatusServiceUnavailable, APIResponse{
			Success: false, Error: "steer unavailable", Code: CodeServiceUnavailable,
		})
		return
	}
	OK(w, map[string]any{
		"ok":       true,
		"run_id":   runID,
		"delivery": parsed.Delivery,
	})
}

// fmtSteerPath 只做一次转义，避免调用点各自拼字符串时漏掉。
func fmtSteerPath(runID string) string {
	return "/v1/internal/subagent/runs/" + url.PathEscape(runID) + "/steer"
}
