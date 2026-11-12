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
