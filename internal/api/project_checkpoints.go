package api

// ── 检查点时间线（只读）──
//
// 这个端点只回答一件事：**这个项目里，过去哪些时刻是可以回滚回去的**。
// 它不建检查点、不执行回滚 —— 建点由引擎在回合起止处做（`checkpoint_hooks`），
// 回滚是另一条端点的事。
//
// **为什么单独一个文件**
//
// `project_git.go` 是「读用户仓库的现状」，这里读的是「Chiron 自己记的历史」，
// 两者数据源与失败模式都不同（前者可能没装 git，后者是 PG 里的一张表）。
// 放一起会让 `repo: false` 这类语义混进来 —— 而"没有检查点"与"没有 git"
// 是完全不同的两件事，前端要给不同的提示。
//
// **`COALESCE` 必须带别名**
//
// `FetchAll` 用**列名**建 map。`COALESCE(error_code,'')` 不带别名时列名就是
// `coalesce`，`row["error_code"]` 恒取不到 —— 项目里已因同一个坑出过
// 「无法加载项目文件列表」。

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// ProjectCheckpoint 是时间线上的一个节点，字段与 `project_checkpoints` 对齐。
type ProjectCheckpoint struct {
	ID           string `json:"id"`
	Seq          int64  `json:"seq"`
	Kind         string `json:"kind"`
	State        string `json:"state"`
	Label        string `json:"label"`
	CommitSHA    string `json:"commit_sha"`
	RefName      string `json:"ref_name"`
	SessionID    string `json:"session_id,omitempty"`
	FileCount    int    `json:"file_count"`
	SkippedCount int    `json:"skipped_count"`
	ByteSize     int64  `json:"byte_size"`
	ErrorCode    string `json:"error_code,omitempty"`
	// AppliedAt 非空表示这个检查点曾被回滚到过；前端据此标记"回滚过"。
	AppliedAt string `json:"applied_at,omitempty"`
	CreatedAt string `json:"created_at"`
}

// ListProjectCheckpoints 返回项目的检查点时间线（按 seq 倒序，最新在前）。
//
// 可选 query 参数：
//   - `session_id`：只看某个会话的检查点（会话内回滚用的视图）。
//   - `limit`：条数上限，默认 100，硬上限 500 —— 时间线是给人看的，
//     一次拉几千条既没有用，也会让前端首屏变慢。
//
// **不做 state 过滤**：失败的检查点（`state='failed'`）也要显示。
// 用户需要知道"这一刻没能保护成功"，静默丢弃会让他以为回滚点是完整的。
func (h *SessionMapHandler) ListProjectCheckpoints(w http.ResponseWriter, r *http.Request) {
	// requireProject 会校验项目存在、绑定目录、且属于当前用户。
	if _, ok := h.requireProject(w, r); !ok {
		return
	}
	if db.GlobalDBManager == nil {
		logAndRespond(w, nil, http.StatusServiceUnavailable, "database unavailable")
		return
	}
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	wsID := strings.TrimSpace(r.PathValue("id"))

	limit := 100
	if raw := strings.TrimSpace(r.URL.Query().Get("limit")); raw != "" {
		if n, err := strconv.Atoi(raw); err == nil && n > 0 && n <= 500 {
			limit = n
		}
	}
	sessionID := strings.TrimSpace(r.URL.Query().Get("session_id"))

	// 参数顺序固定为 workspace / tenant / user / [session] / limit，
	// 与下面的 SQL 占位符一一对应。
	args := []interface{}{wsID, smTenant(claims), claims.UserID}
	where := `workspace_id = $1 AND tenant_id = $2 AND user_id = $3`
	if sessionID != "" {
		args = append(args, sessionID)
		where += ` AND session_id = $4`
	}
	args = append(args, limit)
	limitPlaceholder := "$" + strconv.Itoa(len(args))

	rows, err := db.GlobalDBManager.FetchAll(r.Context(),
		`SELECT id, seq, kind, state, label, commit_sha, ref_name,
		        COALESCE(session_id,'') AS session_id,
		        COALESCE(file_count,0) AS file_count,
		        COALESCE(skipped_count,0) AS skipped_count,
		        COALESCE(byte_size,0) AS byte_size,
		        COALESCE(error_code,'') AS error_code,
		        applied_at, created_at
		   FROM project_checkpoints
		  WHERE `+where+`
		  ORDER BY seq DESC
		  LIMIT `+limitPlaceholder, args...)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "could not read checkpoints")
		return
	}

	out := make([]ProjectCheckpoint, 0, len(rows))
	for _, row := range rows {
		out = append(out, ProjectCheckpoint{
			ID:           stringOf(row["id"]),
			Seq:          int64(intOf(row["seq"])),
			Kind:         stringOf(row["kind"]),
			State:        stringOf(row["state"]),
			Label:        stringOf(row["label"]),
			CommitSHA:    stringOf(row["commit_sha"]),
			RefName:      stringOf(row["ref_name"]),
			SessionID:    stringOf(row["session_id"]),
			FileCount:    intOf(row["file_count"]),
			SkippedCount: intOf(row["skipped_count"]),
			ByteSize:     int64(intOf(row["byte_size"])),
			ErrorCode:    stringOf(row["error_code"]),
			AppliedAt:    smTimeOf(row["applied_at"]),
			CreatedAt:    smTimeOf(row["created_at"]),
		})
	}
	OK(w, map[string]interface{}{"checkpoints": out})
}

// ── 检查点上报（写）──
//
// 这条端点补的是整条链路的缺口：`project_checkpoints` 表由迁移 0004 建好、时间线
// 只读端点也写好了，但**从来没有代码往里写一行** —— 于是列表恒为空，前端也就
// 无从渲染回滚点。引擎侧早就把快照做出来了（`checkpoint_hooks` 写 git ref、
// 未跟踪文件进 CAS），缺的只是"把它送过来"这一步。
//
// **为什么走内部令牌而不是用户 JWT**
//
// 上报者是引擎，它手里只有会话身份，没有用户的浏览器凭据。因此归属**不由请求体
// 声明**：网关按 `session_id` 反查 `session_map_nodes` → `session_map_workspaces`
// 得到 workspace/tenant/user。这样即使 internal_token 泄露，也只能往"真实存在的
// 会话所属项目"里写，而不是任意伪造归属。
//
// **幂等**
//
// 主键由 `(session_id, seq, kind)` 派生（sha256 前 32 位），重复上报 `ON CONFLICT
// DO NOTHING`。引擎在回合结束后可能重试，而同一个检查点被记两次会让时间线出现
// 两个"同一时刻"的节点，用户回滚时会分不清。
//
// **不做 state 过滤**：失败的检查点同样入表（`state='failed'`），前端要能显示
// "这一刻没保护成功"。静默丢弃会让用户以为回滚点是完整的。

const ingestUntrackedMax = 5000

type ingestUntrackedEntry struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

type ingestCheckpointBody struct {
	SessionID    string                 `json:"session_id"`
	TurnID       string                 `json:"turn_id"`
	Seq          int64                  `json:"seq"`
	Kind         string                 `json:"kind"`
	CommitSHA    string                 `json:"commit_sha"`
	TreeSHA      string                 `json:"tree_sha"`
	ParentSHA    string                 `json:"parent_sha"`
	HeadSHA      string                 `json:"head_sha"`
	RefName      string                 `json:"ref_name"`
	FileCount    int                    `json:"file_count"`
	SkippedCount int                    `json:"skipped_count"`
	ByteSize     int64                  `json:"byte_size"`
	Untracked    []ingestUntrackedEntry `json:"untracked"`
}

// checkpointID 由会话内的位置派生，保证同一检查点重复上报得到同一个主键。
func checkpointID(sessionID string, seq int64, kind string) string {
	sum := sha256.Sum256([]byte(fmt.Sprintf("%s|%d|%s", sessionID, seq, kind)))
	return hex.EncodeToString(sum[:])[:32]
}

// IngestProjectCheckpoint 接收引擎上报的检查点（POST /v1/internal/checkpoints）。
func IngestProjectCheckpoint(w http.ResponseWriter, r *http.Request) {
	var body ingestCheckpointBody
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}
	body.SessionID = strings.TrimSpace(body.SessionID)
	body.Kind = strings.TrimSpace(body.Kind)
	body.CommitSHA = strings.TrimSpace(body.CommitSHA)
	body.TreeSHA = strings.TrimSpace(body.TreeSHA)
	body.RefName = strings.TrimSpace(body.RefName)
	if body.SessionID == "" || body.CommitSHA == "" || body.TreeSHA == "" || body.RefName == "" || body.Kind == "" {
		BadRequest(w, "session_id, kind, commit_sha, tree_sha and ref_name are required")
		return
	}
	if body.Seq <= 0 {
		BadRequest(w, "seq must be positive")
		return
	}
	if len(body.Untracked) > ingestUntrackedMax {
		BadRequest(w, fmt.Sprintf("too many untracked entries (max %d)", ingestUntrackedMax))
		return
	}
	if db.GlobalDBManager == nil {
		JSON(w, http.StatusServiceUnavailable, APIResponse{Success: false, Error: "database unavailable"})
		return
	}

	// 归属只信数据库里的会话-项目映射，不信请求体。查不到就等于"这个会话没绑项目",
	// 引擎那边本来也不该建点（`project_root()` 为空时直接跳过），这里如实返回未入库。
	ws, err := db.GlobalDBManager.FetchOne(r.Context(),
		`SELECT w.id AS workspace_id, w.tenant_id AS tenant_id, COALESCE(w.user_id,'') AS user_id
		   FROM session_map_nodes n
		   JOIN session_map_workspaces w ON w.id = n.workspace_id
		  WHERE n.session_id = $1 AND n.edge_kind = 'project'
		  LIMIT 1`, body.SessionID)
	if err != nil {
		slog.Error("checkpoint ingest: session project lookup failed", "error", err, "session", body.SessionID)
		InternalError(w, "lookup failed")
		return
	}
	if ws == nil {
		OK(w, map[string]interface{}{"stored": false, "reason": "session has no bound project"})
		return
	}

	wsID := stringOf(ws["workspace_id"])
	tenantID := intOf(ws["tenant_id"])
	userID := stringOf(ws["user_id"])
	cpID := checkpointID(body.SessionID, body.Seq, body.Kind)
	duplicate := false

	tag, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO project_checkpoints
		   (id, tenant_id, user_id, workspace_id, session_id, turn_id, seq,
		    commit_sha, tree_sha, parent_sha, head_sha, ref_name, kind, state, label,
		    file_count, skipped_count, byte_size, created_at)
		 VALUES ($1,$2,NULLIF($3,''),$4,$5,NULLIF($6,''),$7,$8,$9,NULLIF($10,''),NULLIF($11,''),
		         $12,$13,'ok','',$14,$15,$16,NOW())
		 ON CONFLICT (id) DO NOTHING`,
		cpID, tenantID, userID, wsID, body.SessionID, body.TurnID, body.Seq,
		body.CommitSHA, body.TreeSHA, body.ParentSHA, body.HeadSHA, body.RefName, body.Kind,
		body.FileCount, body.SkippedCount, body.ByteSize)
	if err != nil {
		slog.Error("checkpoint ingest: insert failed", "error", err, "id", cpID)
		InternalError(w, "store failed")
		return
	}
	if tag.RowsAffected() == 0 {
		// 同一检查点重复上报（引擎重试、或清单分批发送的后续批）。主行已在，
		// 但**不能就此返回** —— 清单可能是上一次没写成功的，重试的意义正是补它。
		// 下面的插入是 `ON CONFLICT DO NOTHING`，重复补写无害。
		duplicate = true
	}

	// 清单分块插入。块大小取 500：Postgres 单条语句的参数上限是 65535，
	// 每行 4 个参数时理论上限远大于此，但过大的语句会让错误信息难以定位。
	if len(body.Untracked) > 0 {
		const chunk = 500
		for start := 0; start < len(body.Untracked); start += chunk {
			end := start + chunk
			if end > len(body.Untracked) {
				end = len(body.Untracked)
			}
			args := make([]interface{}, 0, (end-start)*4)
			var sb strings.Builder
			sb.WriteString(`INSERT INTO checkpoint_untracked (checkpoint_id, path, sha256, size) VALUES `)
			for i, e := range body.Untracked[start:end] {
				if i > 0 {
					sb.WriteString(",")
				}
				base := len(args)
				sb.WriteString(fmt.Sprintf("($%d,$%d,$%d,$%d)", base+1, base+2, base+3, base+4))
				args = append(args, cpID, e.Path, e.SHA256, e.Size)
			}
			sb.WriteString(" ON CONFLICT DO NOTHING")
			if _, err := db.GlobalDBManager.Exec(r.Context(), sb.String(), args...); err != nil {
				// 主行已经落库，清单失败不该让整个上报看起来成功 —— 但也无法回滚
				// （已提交）。如实记录并告知调用方"部分失败"，让前端能标出这个点
				// 的回滚可能不完整。
				slog.Error("checkpoint ingest: untracked insert failed", "error", err, "id", cpID)
				JSON(w, http.StatusInternalServerError, APIResponse{
					Success: false, Error: "checkpoint stored but untracked manifest failed",
					Code: CodeInternal,
				})
				return
			}
		}
	}

	out := map[string]interface{}{"stored": true, "id": cpID}
	if duplicate {
		out["duplicate"] = true
	}
	OK(w, out)
}
