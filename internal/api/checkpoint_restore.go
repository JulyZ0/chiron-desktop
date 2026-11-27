package api

import (
	"context"
	"log/slog"
	"net/http"
	"strings"
	"time"

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
		//
		// `COALESCE(user_id,'')`：`project_checkpoints.user_id` 可空（写入侧是
		// `NULLIF($3,'')`），而 `user_id = ''` 永远不匹配 NULL。少这层包裹时，
		// 任何"创建时没有 user_id"的检查点都会被判成不存在 —— 用户看到的是
		// "还原点列得出来，但点了回滚报 not found"。
		owned, err := db.GlobalDBManager.FetchOne(r.Context(),
			`SELECT c.id, c.state, COALESCE(c.session_id,'') AS session_id,
			        COALESCE(c.workspace_id,'') AS workspace_id,
			        COALESCE(w.provider,'server') AS provider,
			        COALESCE(w.device_id,'') AS device_id,
			        COALESCE(w.root_path,'') AS root_path,
			        COALESCE(w.root_key,'') AS root_key
			   FROM project_checkpoints c
			   LEFT JOIN session_map_workspaces w ON w.id = c.workspace_id AND w.tenant_id = c.tenant_id
			  WHERE c.id = $1 AND c.tenant_id = $2 AND COALESCE(c.user_id,'') = $3`,
			cid, smTenantID(claims), claims.UserID)
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

		loc := checkpointLocatorFromRow(owned)
		if loc.Provider == smProviderClient && !deviceWriteAllowed(r.Context(), loc.ID) {
			JSONWithCode(w, http.StatusForbidden, "device_write_required", nil,
				"grant write access to this project before restoring a checkpoint")
			return
		}
		root, err := checkpointRootForLocator(r.Context(), claims, loc)
		if err != nil {
			JSONWithCode(w, http.StatusConflict, "checkpoint_root_unavailable", nil, err.Error())
			return
		}

		// 只转发白名单字段：调用方不该能用请求体改 checkpoint_id 以外的任何身份信息。
		// client 项目的 root 只能由网关按归属查询结果解析为服务端镜像。
		forward := map[string]interface{}{"checkpoint_id": cid}
		if loc.Provider == smProviderClient {
			forward["root"] = root
		}
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
		// client 项目：引擎回滚写的是服务端镜像；成功后按既有 ExpectedHash 语义
		// 立刻回写。冲突必须显式返回，不能把"镜像已恢复、设备未改"报成完整成功。
		if loc.Provider == smProviderClient {
			if ok, _ := result["ok"].(bool); ok {
				// 回写用**独立**上下文：用户点完回滚就切走页面时请求 ctx 会被取消，
				// 那会让镜像已经恢复、设备却停在半路（下一次回合还会以旧为基线）。
				sctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
				defer cancel()
				if err := syncProjectMirrorBack(sctx, smTenant(claims), claims.UserID, loc); err != nil {
					JSONWithCode(w, http.StatusConflict, "checkpoint_sync_failed", nil, err.Error())
					return
				}
			}
		}
		OK(w, result)
	}
}
