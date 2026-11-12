package api

// ── 项目（Workspace 即项目）──
//
// 语义与不变式见 `docs/project-management-design.md`：
//   - 一个 workspace 要么是**项目**（root_path 非空，绑定本地文件夹），
//     要么是**画布**（root_path 为空，旧形态）。不设第三种。
//   - 会话归属 = `session_map_nodes` 里 `edge_kind='project'` 的行。
//
// **归属为什么单独一套端点，而不复用 PUT /workspaces/{id}**
//
// `PUT /workspaces/{id}` 的语义是「整体覆盖画布快照」，而归属是「增量增删」。
// 混在一条路径上，前端保存画布时必然要用一份不知道归属的 body 覆盖整张表，
// 结果就是把归属删光。分开之后，两条写入路径的语义各自成立。
//
// **归属记录为什么直接写 PG，不走 Redis 热层**
//
// 热层快照的结构是「viewport + nodes」，nodes 里放的是画布节点。把归属塞进去
// 会让热层与画布语义纠缠，而且 flusher 落库时会走 `smPersist` 的全量替换逻辑。
// 归属的读写频率低（用户手工归会话），直写 PG 的延迟完全可以接受。

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
	"github.com/jackc/pgx/v5"
)

const (
	// smProjectEdgeKind 标记「这一行是会话归属记录，不是画布节点」。
	smProjectEdgeKind = "project"
	// smMaxRootPathLen 与迁移里 `root_path varchar(1024)` 对齐。
	// 在 API 层拒绝超长，而不是让 DB 隐式截断 —— 截断会产生一个指向错误目录的项目。
	smMaxRootPathLen = 1024
)

// smForbiddenRoots 是拒绝绑定的系统目录（小写、Clean 后比较）。
//
// 这些目录里放项目没有正当理由，却能让 agent 在后续阶段（P3 接通执行 cwd）
// 获得对系统文件的可写视图。现在挡住比将来在沙箱层补救便宜。
var smForbiddenRoots = []string{
	`c:\windows`,
	`c:\program files`,
	`c:\program files (x86)`,
	`c:\programdata\microsoft`,
}

// validateRootPath 校验并规范化项目根目录。
//
// 返回 (规范化路径, 错误说明)。错误说明为空表示通过。
// **判据只在这里**，前端校验只为即时反馈 —— 与「隔离档位只有一处事实来源」同理。
func validateRootPath(raw string) (string, string) {
	p := strings.TrimSpace(raw)
	if p == "" {
		return "", "root_path is required"
	}
	if len(p) > smMaxRootPathLen {
		return "", "root_path is too long"
	}
	if !filepath.IsAbs(p) {
		return "", "root_path must be an absolute path"
	}
	cleaned := filepath.Clean(p)
	info, err := os.Stat(cleaned)
	if err != nil {
		// 不回显 err：它含底层路径信息，对前端排错没有增量价值。
		return "", "root_path does not exist or is not accessible"
	}
	if !info.IsDir() {
		return "", "root_path is not a directory"
	}
	lower := strings.ToLower(cleaned)
	// 驱动器根目录本身（`C:\`）不允许：整个盘当项目会让后续执行 cwd 无边界。
	if len(lower) == 3 && lower[1] == ':' && (lower[2] == '\\' || lower[2] == '/') {
		return "", "root_path cannot be a drive root"
	}
	for _, bad := range smForbiddenRoots {
		if lower == bad || strings.HasPrefix(lower, bad+`\`) {
			return "", "root_path points to a protected system directory"
		}
	}
	return cleaned, ""
}

// rootPathTaken 判断当前用户是否已有一个绑定同一目录的项目。
//
// **为什么必须由服务端拒绝**：前端的路径选择器只做即时提示，而 `/projects`
// 与 `/projects/{id}` 两个端点都能被直接调用。判据集中在这一处，两个入口都调它，
// 才不会出现"新建拦住了、改名漏了"。
//
// **为什么把比对放在 Go 侧而不是 SQL 的 `lower(root_path) = lower($3)`**：
// Postgres 的 `lower()` 依赖数据库 locale，对非 ASCII 字符的行为可能与文件系统
// 不一致；而 `sameRootPath` 是可单测的纯函数，判据只有一处、且能直接断言。
// 代价是把该用户的项目读出来（个数级个位），开销可忽略。
//
// excludeID 传自身的 id（改名场景），使"只改名字、路径不动"不会被自己挡住。
func rootPathTaken(ctx context.Context, tenant, userID, rootPath, excludeID string) (bool, error) {
	if db.GlobalDBManager == nil || rootPath == "" {
		return false, nil
	}
	rows, err := db.GlobalDBManager.FetchAll(ctx,
		`SELECT id, COALESCE(root_path,'') AS root_path
		   FROM session_map_workspaces
		  WHERE tenant_id = $1 AND user_id = $2 AND COALESCE(root_path,'') <> ''`,
		tenant, userID)
	if err != nil {
		return false, err
	}
	for _, row := range rows {
		if stringOf(row["id"]) == excludeID {
			continue
		}
		if sameRootPath(stringOf(row["root_path"]), rootPath) {
			return true, nil
		}
	}
	return false, nil
}

// sameRootPath 判断两个项目根是否指向同一目录。
//
// 两条规则，都来自文件系统的实际行为：
//   - **大小写不敏感**：Windows 与 macOS 默认如此。若按原样比对，用户把
//     `D:\Repo` 换成 `d:\repo` 就能建出第二个项目，而两个项目会对同一个 `.git`
//     各自建检查点，时间线互相污染。
//   - **规范化后再比**：`filepath.Clean` 吃掉尾分隔符、`.`、以及 `..`。
//     用户从系统对话框选出的路径与手输的路径常常在这些细节上不同。
func sameRootPath(a, b string) bool {
	if a == "" || b == "" {
		return false
	}
	return strings.EqualFold(filepath.Clean(a), filepath.Clean(b))
}

// smTimeOf 把驱动返回的时间值渲染成 RFC3339；nil 给空串。
//
// 不同驱动可能给 time.Time 或 string，统一处理避免各端点各写一套。
func smTimeOf(v interface{}) string {
	switch t := v.(type) {
	case nil:
		return ""
	case time.Time:
		return t.UTC().Format(time.RFC3339)
	case string:
		return t
	default:
		return fmt.Sprint(t)
	}
}

// ListProjects 列出当前用户的全部**项目**（root_path 非空的 workspace）。
//
// 不返回画布：侧边栏只呈现项目，画布是项目详情页里的一种视图，不该占据导航。
func (h *SessionMapHandler) ListProjects(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	out := []map[string]interface{}{}
	if db.GlobalDBManager != nil {
		rows, err := db.GlobalDBManager.FetchAll(r.Context(),
			// 每个 COALESCE 都带 AS 别名：FetchOne/FetchAll 用列名建 map，
			// 无别名时列名是 `coalesce`，多个同名列会互相覆盖（见 SessionProject 的注释）。
			`SELECT w.id, COALESCE(w.name,'') AS name, COALESCE(w.root_path,'') AS root_path,
			        COALESCE(w.sort_index,0) AS sort_index, w.updated_at,
			        (SELECT COUNT(DISTINCT n.session_id) FROM session_map_nodes n
			          WHERE n.workspace_id = w.id AND n.edge_kind = 'project'
			            AND n.session_id IS NOT NULL) AS session_count,
			        (SELECT MAX(s.updated_at) FROM session_map_nodes n
			           JOIN sessions s ON s.id = n.session_id
			          WHERE n.workspace_id = w.id AND n.edge_kind = 'project') AS last_active_at
			   FROM session_map_workspaces w
			  WHERE w.tenant_id = $1 AND w.user_id = $2 AND COALESCE(w.root_path,'') <> ''
			  ORDER BY COALESCE(w.sort_index,0), COALESCE(w.name,''), w.id`,
			smTenant(claims), claims.UserID)
		if err != nil {
			slog.Error("list projects failed", "error", err)
		} else {
			for _, row := range rows {
				out = append(out, map[string]interface{}{
					"id":             stringOf(row["id"]),
					"name":           stringOf(row["name"]),
					"root_path":      stringOf(row["root_path"]),
					"sort_index":     intOf(row["sort_index"]),
					"session_count":  intOf(row["session_count"]),
					"last_active_at": smTimeOf(row["last_active_at"]),
					"updated_at":     smTimeOf(row["updated_at"]),
				})
			}
		}
	}
	OK(w, map[string]interface{}{"projects": out})
}

// CreateProject 用指定文件夹建项目。name 缺省时取文件夹名 —— 让用户少填一个字段。
func (h *SessionMapHandler) CreateProject(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	if db.GlobalDBManager == nil {
		JSON(w, http.StatusServiceUnavailable, APIResponse{Success: false, Error: "database unavailable"})
		return
	}
	var body struct {
		Name     string `json:"name"`
		RootPath string `json:"root_path"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}
	rootPath, why := validateRootPath(body.RootPath)
	if why != "" {
		JSON(w, http.StatusBadRequest, APIResponse{Success: false, Error: why})
		return
	}
	// 同一目录只允许建一个项目。两个项目绑定同一 `.git` 会各自建检查点，
	// 时间线互相污染，回滚时看到的是对方的快照。
	//
	// 查重失败时**拒绝**而不是放行：放行会重新引入"重复项目"，
	// 而这正是要修的问题；代价只是用户重试一次。
	taken, err := rootPathTaken(r.Context(), smTenant(claims), claims.UserID, rootPath, "")
	if err != nil {
		slog.Error("check duplicate project root failed", "error", err)
		JSON(w, http.StatusInternalServerError, APIResponse{Success: false, Error: "create failed"})
		return
	}
	if taken {
		JSON(w, http.StatusConflict, APIResponse{
			Success: false, Error: "a project already uses this folder",
		})
		return
	}
	name := strings.TrimSpace(body.Name)
	if name == "" {
		name = filepath.Base(rootPath)
	}
	wsID, err := id.UUID()
	if err != nil {
		JSON(w, http.StatusInternalServerError, APIResponse{Success: false, Error: "cannot allocate id"})
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO session_map_workspaces
		   (id, tenant_id, user_id, name, root_path, sort_index, viewport, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, 0, '{}'::jsonb, NOW(), NOW())`,
		wsID, smTenant(claims), claims.UserID, name, rootPath); err != nil {
		slog.Error("create project failed", "error", err)
		JSON(w, http.StatusInternalServerError, APIResponse{Success: false, Error: "create failed"})
		return
	}
	// 热层同步一份空快照：随后进画布视图时直接命中，不必回源。
	smCachePut(r.Context(), &SessionMapWorkspace{
		ID: wsID, Name: name, RootPath: rootPath, Viewport: map[string]interface{}{},
		Nodes: []SessionMapNode{}, TenantID: smTenant(claims), UserID: claims.UserID,
	})
	JSON(w, http.StatusCreated, APIResponse{Success: true, Data: map[string]interface{}{
		"id": wsID, "name": name, "root_path": rootPath, "sort_index": 0,
	}})
}

// PatchProject 局部更新项目名 / 路径 / 排序。
//
// 用指针区分「未提交」与「提交了空值」：改名传 `{"name":""}` 应当被拒，
// 而只改路径时不传 name 不该把名字清空。
func (h *SessionMapHandler) PatchProject(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	wsID := strings.TrimSpace(r.PathValue("id"))
	if wsID == "" {
		BadRequest(w, "project id is required")
		return
	}
	if db.GlobalDBManager == nil {
		JSON(w, http.StatusServiceUnavailable, APIResponse{Success: false, Error: "database unavailable"})
		return
	}
	var body struct {
		Name      *string `json:"name"`
		RootPath  *string `json:"root_path"`
		SortIndex *int    `json:"sort_index"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}
	sets := []string{"updated_at = NOW()"}
	args := []interface{}{wsID, smTenant(claims), claims.UserID}
	arg := func(v interface{}) string {
		args = append(args, v)
		return fmt.Sprintf("$%d", len(args))
	}
	if body.Name != nil {
		n := strings.TrimSpace(*body.Name)
		if n == "" {
			BadRequest(w, "name cannot be empty")
			return
		}
		sets = append(sets, "name = "+arg(n))
	}
	if body.RootPath != nil {
		rp, why := validateRootPath(*body.RootPath)
		if why != "" {
			JSON(w, http.StatusBadRequest, APIResponse{Success: false, Error: why})
			return
		}
		// 与 CreateProject 同一条判据：把项目改指到另一个项目已在用的目录同样要拒。
		// excludeID 传自身，否则「只改名字、路径不动」会被自己挡住。
		taken, err := rootPathTaken(r.Context(), smTenant(claims), claims.UserID, rp, wsID)
		if err != nil {
			slog.Error("check duplicate project root failed", "error", err)
			JSON(w, http.StatusInternalServerError, APIResponse{Success: false, Error: "update failed"})
			return
		}
		if taken {
			JSON(w, http.StatusConflict, APIResponse{
				Success: false, Error: "a project already uses this folder",
			})
			return
		}
		sets = append(sets, "root_path = "+arg(rp))
	}
	if body.SortIndex != nil {
		sets = append(sets, "sort_index = "+arg(*body.SortIndex))
	}
	if len(sets) == 1 {
		BadRequest(w, "nothing to update")
		return
	}
	tag, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE session_map_workspaces SET `+strings.Join(sets, ", ")+
			` WHERE id = $1 AND tenant_id = $2 AND user_id = $3`, args...)
	if err != nil {
		slog.Error("patch project failed", "error", err)
		JSON(w, http.StatusInternalServerError, APIResponse{Success: false, Error: "update failed"})
		return
	}
	if tag.RowsAffected() == 0 {
		JSON(w, http.StatusNotFound, APIResponse{Success: false, Error: "project not found"})
		return
	}
	// 热层里的快照带着旧的 name/root_path，必须失效重建，否则下一次读会拿到旧值。
	if db.Redis != nil {
		db.Redis.Do(r.Context(), "DEL", smCacheKey(wsID))
	}
	OK(w, map[string]interface{}{"id": wsID, "updated": true})
}

// ListProjectSessions 列出某项目内的会话。
func (h *SessionMapHandler) ListProjectSessions(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	wsID := strings.TrimSpace(r.PathValue("id"))
	if wsID == "" || db.GlobalDBManager == nil {
		BadRequest(w, "project id is required")
		return
	}
	if !smOwnsWorkspace(r, wsID, claims) {
		JSON(w, http.StatusNotFound, APIResponse{Success: false, Error: "project not found"})
		return
	}
	out := []map[string]interface{}{}
	rows, err := db.GlobalDBManager.FetchAll(r.Context(),
		// COALESCE 全部带 AS 别名，理由见 SessionProject 的注释（重名列互相覆盖）。
		`SELECT s.id, COALESCE(NULLIF(s.title,''), '') AS title, COALESCE(s.status,'') AS status,
		        s.updated_at, COALESCE(s.pinned,false) AS pinned, n.created_at AS bound_at
		   FROM session_map_nodes n
		   JOIN sessions s ON s.id = n.session_id
		  WHERE n.workspace_id = $1 AND n.edge_kind = 'project'
		  ORDER BY COALESCE(s.pinned,false) DESC, s.updated_at DESC`, wsID)
	if err != nil {
		slog.Error("list project sessions failed", "error", err)
	}
	for _, row := range rows {
		out = append(out, map[string]interface{}{
			"id":         stringOf(row["id"]),
			"title":      stringOf(row["title"]),
			"status":     stringOf(row["status"]),
			"updated_at": smTimeOf(row["updated_at"]),
			"pinned":     boolOf(row["pinned"]),
			"bound_at":   smTimeOf(row["bound_at"]),
		})
	}
	OK(w, map[string]interface{}{"sessions": out})
}

// BindProjectSession 把会话归入项目。
//
// 幂等：已在本项目则直接成功。已被别的项目占用时返回 409，除非 `?force=true` ——
// 那是「移动」语义，在**同一事务**里删旧归属再插新归属，避免中间态出现两条归属
// （唯一索引 I1 也会拦住，但撞索引报错对用户没有解释力）。
func (h *SessionMapHandler) BindProjectSession(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	wsID := strings.TrimSpace(r.PathValue("id"))
	sessionID := strings.TrimSpace(r.PathValue("sid"))
	if wsID == "" || sessionID == "" || db.GlobalDBManager == nil {
		BadRequest(w, "project id and session id are required")
		return
	}
	if !smOwnsWorkspace(r, wsID, claims) {
		JSON(w, http.StatusNotFound, APIResponse{Success: false, Error: "project not found"})
		return
	}
	force := r.URL.Query().Get("force") == "true"

	var existingWS string
	if row, err := db.GlobalDBManager.FetchOne(r.Context(),
		`SELECT n.workspace_id FROM session_map_nodes n
		  WHERE n.session_id = $1 AND n.edge_kind = 'project' LIMIT 1`, sessionID); err == nil && row != nil {
		existingWS = stringOf(row["workspace_id"])
	}
	if existingWS == wsID {
		OK(w, map[string]interface{}{"bound": true, "already": true})
		return
	}
	if existingWS != "" && !force {
		// body 带 owner_project_id，前端据此提示「该会话已在「xxx」项目中」。
		JSON(w, http.StatusConflict, APIResponse{
			Success: false,
			Error:   "session_already_bound",
			Data:    map[string]interface{}{"owner_project_id": existingWS},
		})
		return
	}

	nodeID, err := id.UUID()
	if err != nil {
		JSON(w, http.StatusInternalServerError, APIResponse{Success: false, Error: "cannot allocate id"})
		return
	}
	err = db.GlobalDBManager.WithTransaction(r.Context(), func(tx pgx.Tx) error {
		// 先删旧归属（force 才可能非空），再插新的。
		// 顺序不能反：唯一索引 I1 在 INSERT 那一刻就要成立。
		if _, err := tx.Exec(r.Context(),
			`DELETE FROM session_map_nodes
			  WHERE session_id = $1 AND edge_kind = 'project' AND workspace_id <> $2`,
			sessionID, wsID); err != nil {
			return err
		}
		_, err := tx.Exec(r.Context(),
			`INSERT INTO session_map_nodes
			   (id, workspace_id, session_id, parent_node_id, edge_kind, x, y, title, color,
			    collapsed, hidden, pinned, branch_from_seq, created_at, updated_at)
			 VALUES ($1, $2, $3, NULL, 'project', 0, 0, NULL, NULL, false, true, false, NULL, NOW(), NOW())
			 ON CONFLICT (id) DO NOTHING`,
			nodeID, wsID, sessionID)
		return err
	})
	if err != nil {
		slog.Error("bind project session failed", "error", err, "project", wsID, "session", sessionID)
		JSON(w, http.StatusInternalServerError, APIResponse{Success: false, Error: "bind failed"})
		return
	}
	OK(w, map[string]interface{}{"bound": true, "moved_from": existingWS})
}

// UnbindProjectSession 把会话移出项目。会话本身不删。
func (h *SessionMapHandler) UnbindProjectSession(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	wsID := strings.TrimSpace(r.PathValue("id"))
	sessionID := strings.TrimSpace(r.PathValue("sid"))
	if wsID == "" || sessionID == "" || db.GlobalDBManager == nil {
		BadRequest(w, "project id and session id are required")
		return
	}
	if !smOwnsWorkspace(r, wsID, claims) {
		JSON(w, http.StatusNotFound, APIResponse{Success: false, Error: "project not found"})
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`DELETE FROM session_map_nodes
		  WHERE workspace_id = $1 AND session_id = $2 AND edge_kind = 'project'`,
		wsID, sessionID); err != nil {
		slog.Error("unbind project session failed", "error", err)
		JSON(w, http.StatusInternalServerError, APIResponse{Success: false, Error: "unbind failed"})
		return
	}
	OK(w, map[string]interface{}{"unbound": true})
}

// SessionProject 反查一个会话所属的项目（`@` 引用文件要知道搜哪个目录）。
//
// 为什么需要它：ChatView 手里只有 `activeSessionId`，而项目文件端点认的是 project id。
// 没有这条反查，前端只能遍历全部项目再逐个拉会话列表 —— 项目一多就是 N+1 次请求，
// 而这件事在 SQL 里是一行 join。
//
// 不属于任何项目时返回 `project: null` 而不是 404：那是**正常状态**（未归组的会话），
// 前端据此让文件那一源整体缺席，而不是弹一个"找不到"的错误。
func (h *SessionMapHandler) SessionProject(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	sessionID := strings.TrimSpace(r.PathValue("sid"))
	if sessionID == "" || db.GlobalDBManager == nil {
		BadRequest(w, "session id is required")
		return
	}
	// 归属条件写在**同一条查询**里（w.tenant_id / w.user_id），而不是先查再验：
	// 分开写就有"查出别人的项目再拒绝"的中间态，而这里连那一步都不该发生。
	row, err := db.GlobalDBManager.FetchOne(r.Context(),
		// **每个 COALESCE 都必须带 AS 别名**：FetchOne 用列名建 map，而
		// `COALESCE(x,'')` 不带别名时列名就是 `coalesce` —— 两个列都叫这个，
		// 后写的一个会把前一个覆盖掉，于是 row["name"] / row["root_path"]
		// 一律取不到值（都成空串）。这正是「会话明明绑定了项目、界面却说未归入」的根因。
		`SELECT w.id, COALESCE(w.name,'') AS name, COALESCE(w.root_path,'') AS root_path
		   FROM session_map_nodes n
		   JOIN session_map_workspaces w ON w.id = n.workspace_id
		  WHERE n.session_id = $1 AND n.edge_kind = 'project'
		    AND w.tenant_id = $2 AND w.user_id = $3
		  LIMIT 1`, sessionID, smTenant(claims), claims.UserID)
	if err != nil {
		slog.Error("session project lookup failed", "error", err)
	}
	if row == nil {
		OK(w, map[string]interface{}{"project": nil})
		return
	}
	OK(w, map[string]interface{}{
		"project": map[string]interface{}{
			"id":        stringOf(row["id"]),
			"name":      stringOf(row["name"]),
			"root_path": stringOf(row["root_path"]),
		},
	})
}

// ListUnassignedSessions 列出不属于任何项目的会话，作为侧边栏的兜底分组。
//
// 存量会话全都没有归属，没有这个分组它们在改造后就会从侧边栏消失。
func (h *SessionMapHandler) ListUnassignedSessions(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	out := []map[string]interface{}{}
	if db.GlobalDBManager != nil {
		rows, err := db.GlobalDBManager.FetchAll(r.Context(),
			// COALESCE 全部带 AS 别名，见 SessionProject 的注释。
			`SELECT s.id, COALESCE(NULLIF(s.title,''),'') AS title, COALESCE(s.status,'') AS status,
			        s.updated_at, COALESCE(s.pinned,false) AS pinned
			   FROM sessions s
			  WHERE s.tenant_id = $1 AND s.user_id = $2
			    AND NOT EXISTS (
			          SELECT 1 FROM session_map_nodes n
			           WHERE n.session_id = s.id AND n.edge_kind = 'project')
			  ORDER BY COALESCE(s.pinned,false) DESC, s.updated_at DESC
			  LIMIT 200`,
			smTenant(claims), claims.UserID)
		if err != nil {
			slog.Error("list unassigned sessions failed", "error", err)
		}
		for _, row := range rows {
			out = append(out, map[string]interface{}{
				"id":         stringOf(row["id"]),
				"title":      stringOf(row["title"]),
				"status":     stringOf(row["status"]),
				"updated_at": smTimeOf(row["updated_at"]),
				"pinned":     boolOf(row["pinned"]),
			})
		}
	}
	OK(w, map[string]interface{}{"sessions": out})
}

// smOwnsWorkspace 校验 workspace 属于当前用户/租户。
//
// 项目端点的每条路径都要过这一关：`id` 来自 URL，是用户可以任意构造的值。
func smOwnsWorkspace(r *http.Request, wsID string, claims *auth.Claims) bool {
	if db.GlobalDBManager == nil {
		return false
	}
	row, err := db.GlobalDBManager.FetchOne(r.Context(),
		`SELECT 1 AS ok FROM session_map_workspaces
		  WHERE id = $1 AND tenant_id = $2 AND user_id = $3`, wsID, smTenant(claims), claims.UserID)
	return err == nil && row != nil
}
