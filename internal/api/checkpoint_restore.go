package api

import (
	"log/slog"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
)

// 还原点的**回滚**入口（POST /v1/checkpoints/{cid}/restore）。
//
// 为什么不在网关里自己动手恢复文件：回滚要读引擎的 CAS blob、复用
// `git_checkpoint.restore()` 的两阶段落地逻辑。网关另写一份必然与它行为不一致，
// 而"两份实现里有一份是错的"在回滚这种写用户磁盘的操作上代价最高。
// 所以网关只做两件事：**校验归属**，然后转发。
//
// 为什么归属必须在这里查：引擎侧的回滚端点只认内部令牌，它拿到 `checkpoint_id`
// 就直接开工 —— 那个端点无法区分"这个 id 是调用者自己的"还是别人的。少了这道
// 校验，任何登录用户（或任何一个账号泄露）都能凭一个猜到的 id 覆写他人工作区。
// 所以这里先用 claims 把 `(id, tenant, user)` 一起查一遍，查不到就当不存在。
func CheckpointRestoreHandler(python *engine.PythonClient) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if python == nil {
			InternalError(w, "python engine not available")
			return
		}
		claims := auth.GetClaims(r.Context())
		if claims == nil {
			Unauthorized(w, "missing auth context")
			return
		}
		cid := strings.TrimSpace(r.PathValue("cid"))
		if cid == "" {
			BadRequest(w, "checkpoint id is required")
			return
		}
		var body map[string]interface{}
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, ErrInvalidReq)
			return
		}
		if db.GlobalDBManager == nil {
			JSON(w, http.StatusServiceUnavailable, APIResponse{Success: false, Error: "database unavailable"})
			return
		}

		// 归属校验与"存在性"合成一次查询：查不到与无权限返回同一个 404，
		// 不泄露"这个 id 存在但不是你的"。
		owned, err := db.GlobalDBManager.FetchOne(r.Context(),
			`SELECT id, state FROM project_checkpoints
			  WHERE id = $1 AND tenant_id = $2 AND user_id = $3`,
			cid, smTenant(claims), claims.UserID)
		if err != nil {
			slog.Error("checkpoint restore: ownership lookup failed", "error", err, "id", cid)
			InternalError(w, "lookup failed")
			return
		}
		if owned == nil {
			NotFound(w, "checkpoint not found")
			return
		}
		if st := stringOf(owned["state"]); st != "" && st != "ok" {
			// 状态不是 ok 的快照本身就没完整写出来。让引擎也拒一次是多余的，
			// 但在这里拦住能给出更清楚的原因。
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "checkpoint state is " + st + ", not ok",
				Code:    CodeRequestFailed,
			})
			return
		}

		// 只转发白名单字段：调用方不该能用请求体改 checkpoint_id 以外的任何身份信息。
		forward := map[string]interface{}{"checkpoint_id": cid}
		if v, ok := body["allow_delete"]; ok {
			forward["allow_delete"] = v
		}
		if v, ok := body["paths"]; ok {
			forward["paths"] = v
		}

		var result map[string]interface{}
		if err := python.PostJSON(r.Context(), "/v1/checkpoints/restore", forward, &result); err != nil {
			slog.Warn("checkpoint restore failed", "id", cid, "error", err)
			// 引擎的 4xx/5xx 被 PostJSON 压成 error 文本。回滚失败的原因
			// （CAS blob 缺失、文件被占用）对用户有意义，原样带出去。
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   err.Error(),
				Code:    CodeRequestFailed,
			})
			return
		}
		OK(w, result)
	}
}
