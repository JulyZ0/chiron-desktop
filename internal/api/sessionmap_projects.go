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
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"strings"
	"time"
	"unicode"

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
	// 项目身份键的最大长度，与迁移里 `root_key varchar(2048)` 对齐。
	smMaxRootKeyLen = 2048
	// provider：当前只有 server；client（设备文件桥）在后续切片接入。
	smProviderServer = "server"
	smProviderClient = "client"
)

// smForbiddenRoots 是拒绝绑定的系统目录（小写、分隔符统一为 `/` 后比较）。
//
// 这些目录里放项目没有正当理由，却能让 agent 在后续阶段（P3 接通执行 cwd）
// 获得对系统文件的可写视图。现在挡住比将来在沙箱层补救便宜。
//
// 用 `/` 而不是 `\`：网关可能跑在 Linux（团队模式），`filepath` 不把 `\`
// 当分隔符，统一后再做前缀匹配，判据才不随宿主 OS 漂移。
var smForbiddenRoots = []string{
	`c:/windows`,
	`c:/program files`,
	`c:/program files (x86)`,
	`c:/programdata/microsoft`,
}

// projectIsolationRoot 是团队模式下用户项目目录的根（见 config.ProjectsRoot）。
//
// 为空 = 桌面单机：用户本来就能选本机任意文件夹，不额外限制。
// 非空 = 多用户服务：项目 root_path 必须落在 `<root>/<tenant>/<user>/` 内。
var projectIsolationRoot string

// SetProjectIsolationRoot 由路由器在启动时注入（团队模式未配时回落 StorageRoot/projects）。
func SetProjectIsolationRoot(root string) {
	projectIsolationRoot = strings.TrimSpace(root)
}

// projectPathSegment 把租户/用户标识收敛成安全的单段目录名。
func projectPathSegment(v string) string {
	v = strings.TrimSpace(v)
	if v == "" {
		return "default"
	}
	var b strings.Builder
	for _, r := range v {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '.', r == '_', r == '-':
			b.WriteRune(r)
		default:
			b.WriteRune('_')
		}
	}
	return b.String()
}

func userProjectBase(tenant, userID string) string {
	if projectIsolationRoot == "" || userID == "" {
		return ""
	}
	return filepath.Join(projectIsolationRoot, projectPathSegment(tenant), projectPathSegment(userID))
}

// projectDirSlug 把项目名收敛成可安全用作单段目录名的形式：
// 保留字母/数字（含中文），空白与其它符号折成 '-'，去掉首尾 '-'/'.'，最长 40。
func projectDirSlug(name string) string {
	var b strings.Builder
	lastDash := false
	for _, r := range strings.TrimSpace(name) {
		switch {
		case unicode.IsLetter(r) || unicode.IsDigit(r):
			b.WriteRune(r)
			lastDash = false
		default:
			if !lastDash && b.Len() > 0 {
				b.WriteByte('-')
				lastDash = true
			}
		}
		if b.Len() >= 40 {
			break
		}
	}
	slug := strings.Trim(b.String(), "-.")
	if slug == "" {
		slug = "project"
	}
	return slug
}

// createManagedProjectDir 为团队 Web 用户创建 `<PROJECTS_ROOT>/<tenant>/<user>/<slug>`
// 目录。重名时追加序号，用户不需要理解服务器目录结构。
func createManagedProjectDir(claims *auth.Claims, name string) (string, error) {
	base := userProjectBase(smTenant(claims), claims.UserID)
	if base == "" {
		return "", errors.New("project workspace is unavailable")
	}
	slug := projectDirSlug(name)
	for i := 1; i <= 50; i++ {
		dirName := slug
		if i > 1 {
			dirName = fmt.Sprintf("%s-%d", slug, i)
		}
		candidate := filepath.Join(base, dirName)
		if _, err := os.Stat(candidate); err == nil {
			continue
		} else if !os.IsNotExist(err) {
			return "", err
		}
		if err := os.MkdirAll(candidate, 0o700); err != nil {
			return "", err
		}
		return candidate, nil
	}
	return "", errors.New("cannot allocate project directory")
}

// pathWithin 判断 target 是否位于 base 之内（含 base 本身）。
// Windows 路径大小写不敏感，比较前统一小写；拒绝 `..` 逃逸。
func pathWithin(base, target string) bool {
	b, err := filepath.Abs(base)
	if err != nil {
		return false
	}
	t, err := filepath.Abs(target)
	if err != nil {
		return false
	}
	// 符号链接可能让词法路径在目录内、真实指向目录外：能解析就按解析后的路径比较。
	if rb, err := filepath.EvalSymlinks(b); err == nil {
		b = rb
	}
	if rt, err := filepath.EvalSymlinks(t); err == nil {
		t = rt
	}
	if runtime.GOOS == "windows" {
		b = strings.ToLower(b)
		t = strings.ToLower(t)
	}
	rel, err := filepath.Rel(b, t)
	if err != nil {
		return false
	}
	if rel == "." {
		return true
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// projectRootAllowed 判断某个项目根是否允许当前用户使用。
func projectRootAllowed(tenant, userID, root string) bool {
	base := userProjectBase(tenant, userID)
	if base == "" {
		return true
	}
	return pathWithin(base, root)
}

// ensureUserProjectDir 在团队模式下把项目根**限制并创建**在用户自己的目录内。
//
// 桌面模式（未配 PROJECTS_ROOT）直接通过；团队模式下：
//   - 路径不在 `<root>/<tenant>/<user>/` 内 ⇒ 拒绝（防止指向别人的项目/系统目录）；
//   - 路径不存在 ⇒ 创建（Web 用户没法像桌面那样浏览服务器目录，给一个可写落点）。
//
// projectRootNotAllowedError 表示路径越出了该用户被允许的项目目录。
// 单独成类型是为了让 handler 返回可本地化的 code 与允许根，而不是一句通用 403。
type projectRootNotAllowedError struct {
	Base string
}

func (e *projectRootNotAllowedError) Error() string {
	return "root_path must be inside your own project directory: " + e.Base
}

// 桌面本地模式（Electron + 本机 owner）允许选择本机任意文件夹：
// PROJECTS_ROOT 的隔离语义只针对**多人服务**下的非 owner。owner 是部署负责人，
// 在团队 Web 里同样有平台级运维权限，限制其项目根只会把本地桌面用户挡在门外。
func ensureUserProjectDir(claims *auth.Claims, raw string) error {
	if projectIsolationRoot == "" {
		return nil
	}
	if claims == nil {
		return fmt.Errorf("not authenticated")
	}
	if claims.Role == "owner" {
		return nil
	}
	base := userProjectBase(smTenant(claims), claims.UserID)
	if base == "" {
		return nil
	}
	p := filepath.Clean(strings.TrimSpace(raw))
	if p == "" || !filepath.IsAbs(p) {
		return fmt.Errorf("root_path must be an absolute path")
	}
	if !pathWithin(base, p) {
		return &projectRootNotAllowedError{Base: base}
	}
	if err := os.MkdirAll(base, 0o700); err != nil {
		return fmt.Errorf("cannot create project directory: %w", err)
	}
	if _, err := os.Stat(p); os.IsNotExist(err) {
		if err := os.MkdirAll(p, 0o700); err != nil {
			return fmt.Errorf("cannot create project directory: %w", err)
		}
	}
	return nil
}

// isWindowsAbsPath 判断是否为 Windows 盘符绝对路径（与宿主 OS 无关）。
//
// `filepath.IsAbs` 只认宿主平台：Linux 上的网关会把 `D:\repo` 当成相对路径，
// 结果是"合法路径被拒、驱动器根报错文案不对"。root_path 由桌面客户端上报，
// 判据必须按路径本身的形状来。
func isWindowsAbsPath(p string) bool {
	if len(p) < 3 || p[1] != ':' {
		return false
	}
	c := p[0]
	if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z') {
		return false
	}
	return p[2] == '\\' || p[2] == '/'
}

// normalizePathSeparators 把 `\` 统一成 `/`，只用于**策略比较**，不用于文件系统访问。
func normalizePathSeparators(p string) string {
	return strings.ReplaceAll(p, `\`, "/")
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
	if !filepath.IsAbs(p) && !isWindowsAbsPath(p) {
		return "", "root_path must be an absolute path"
	}
	cleaned := filepath.Clean(p)
	// 策略检查放在 os.Stat 之前：驱动器根/系统目录即使在当前宿主上不存在，
	// 也必须给出"策略拒绝"的明确文案，而不是"路径不存在"。
	policy := strings.ToLower(normalizePathSeparators(cleaned))
	// 驱动器根目录本身（`C:\`）不允许：整个盘当项目会让后续执行 cwd 无边界。
	if len(policy) == 3 && policy[1] == ':' && policy[2] == '/' {
		return "", "root_path cannot be a drive root"
	}
	for _, bad := range smForbiddenRoots {
		if policy == bad || strings.HasPrefix(policy, bad+"/") {
			return "", "root_path points to a protected system directory"
		}
	}
	info, err := os.Stat(cleaned)
	if err != nil {
		// 不回显 err：它含底层路径信息，对前端排错没有增量价值。
		return "", "root_path does not exist or is not accessible"
	}
	if !info.IsDir() {
		return "", "root_path is not a directory"
	}
	return cleaned, ""
}

// lexicalRootKey 只做**词法**规范化：清洗分隔符/`.`/`..`，按路径形状决定大小写。
//
// 为什么不在这里 EvalSymlinks：这个函数要能被"路径已不存在"的历史行调用。
// 物理解析放在 serverRootKey 里，失败时回退到这里。
func lexicalRootKey(p string) string {
	p = strings.TrimSpace(p)
	if p == "" {
		return ""
	}
	win := isWindowsAbsPath(p) || strings.HasPrefix(p, `\\`) || strings.HasPrefix(p, "//")
	if win {
		q := path.Clean(strings.ReplaceAll(p, `\`, "/"))
		// path.Clean 会把开头的 `//` 折叠成 `/`；UNC 必须保留双斜杠，
		// 否则 `\\server\share\x` 会与 POSIX 的 `/server/share/x` 撞成同一个键。
		if !strings.HasPrefix(q, "//") && (strings.HasPrefix(p, `\\`) || strings.HasPrefix(p, "//")) {
			q = "/" + q
		}
		return strings.ToLower(q)
	}
	q := filepath.ToSlash(filepath.Clean(p))
	if runtime.GOOS == "windows" || runtime.GOOS == "darwin" {
		q = strings.ToLower(q)
	}
	return q
}

// stripWindowsVerbatim 去掉 Windows 扩展长度前缀（`\\?\`、`\\.\`、`\\?\UNC\`）。
//
// EvalSymlinks 在 Windows 上可能带回这些前缀；不剥掉的话同一个目录会算出
// `//?/c:/...` 与 `c:/...` 两个键，判重失效。
func stripWindowsVerbatim(p string) string {
	switch {
	case strings.HasPrefix(p, `\\?\UNC\`):
		return `\\` + p[len(`\\?\UNC\`):]
	case strings.HasPrefix(p, `\\?\`), strings.HasPrefix(p, `\\.\`):
		return p[4:]
	default:
		return p
	}
}

// serverRootKey 计算 server provider 的身份键：优先解析 symlink/junction，
// 解析失败（路径被移动/删除）时退回词法结果 —— 判重不能因为目录暂时不在就失效。
func serverRootKey(p string) string {
	raw := strings.TrimSpace(p)
	if raw == "" {
		return ""
	}
	if resolved, err := filepath.EvalSymlinks(raw); err == nil && resolved != "" {
		return lexicalRootKey(stripWindowsVerbatim(resolved))
	}
	return lexicalRootKey(raw)
}

// projectRootKeyOf 取一行的身份键；历史行 root_key 为空时按 provider 现算。
//
// provider 为空按 server 处理（迁移前的全部项目）。
func projectRootKeyOf(provider, deviceID, rootKey, rootPath string) string {
	if rootKey != "" {
		return rootKey
	}
	if strings.TrimSpace(rootPath) == "" {
		return ""
	}
	if strings.TrimSpace(provider) == "" {
		provider = smProviderServer
	}
	// client 的 root_key 必须由设备侧计算（路径语义属于客户端）；缺失时不能
	// 用服务端规则猜，返回空表示"暂不可判重"，由设备桥重新授权时补齐。
	if provider != smProviderServer {
		return ""
	}
	return serverRootKey(rootPath)
}

// truncateRootKey 防止超长路径把唯一索引写失败（列宽 2048）。
func truncateRootKey(key string) string {
	if len(key) <= smMaxRootKeyLen {
		return key
	}
	return key[:smMaxRootKeyLen]
}

// rootKeyHash 是唯一索引实际使用的列。
//
// 为什么不直接把 root_key 放进索引：Postgres btree 单行索引项约 2704 字节上限，
// 2048 字符的中文路径（UTF-8 每字 3 字节）会超限，建索引和插入都会失败。
// sha256 十六进制固定 64 字节，碰撞概率可忽略。
func rootKeyHash(key string) string {
	if key == "" {
		return ""
	}
	sum := sha256.Sum256([]byte(key))
	return hex.EncodeToString(sum[:])
}

// projectLocatorTaken 判断当前用户是否已有绑定同一 **locator** 的项目。
//
// 判据是 `(provider, device_id, root_key)`，不是 `root_path` 字符串：
// 两台设备上的 `C:\work\app` 是两个不同项目；同一设备上的 `D:\Repo` 与
// `d:\repo` 仍要判重。历史行 root_key 为空时按行内 root_path 现算，并顺手回填。
//
// excludeID 传自身（改名/改路径场景），使"路径不动"不会被自己挡住。
func projectLocatorTaken(ctx context.Context, tenant, userID, provider, deviceID, rootKey, excludeID string) (bool, error) {
	if db.GlobalDBManager == nil || rootKey == "" {
		return false, nil
	}
	if provider == "" {
		provider = smProviderServer
	}
	targetHash := rootKeyHash(rootKey)
	rows, err := db.GlobalDBManager.FetchAll(ctx,
		`SELECT id, COALESCE(provider,'server') AS provider,
		        COALESCE(device_id,'') AS device_id,
		        COALESCE(root_key,'') AS root_key,
		        COALESCE(root_key_hash,'') AS root_key_hash,
		        COALESCE(root_path,'') AS root_path
		   FROM session_map_workspaces
		  WHERE tenant_id = $1 AND user_id = $2 AND COALESCE(root_path,'') <> ''`,
		tenant, userID)
	if err != nil {
		return false, err
	}
	for _, row := range rows {
		id := stringOf(row["id"])
		rowProvider := stringOf(row["provider"])
		if rowProvider == "" {
			rowProvider = smProviderServer
		}
		rowKey := projectRootKeyOf(rowProvider, stringOf(row["device_id"]), stringOf(row["root_key"]), stringOf(row["root_path"]))
		rowHash := stringOf(row["root_key_hash"])
		// 惰性回填：历史行第一次参与判重时补上身份键与哈希，唯一索引随后才覆盖它们。
		if rowKey != "" && (rowHash == "" || stringOf(row["root_key"]) == "") {
			rowKey = truncateRootKey(rowKey)
			rowHash = rootKeyHash(rowKey)
			if _, err := db.GlobalDBManager.Exec(ctx,
				`UPDATE session_map_workspaces SET root_key = $1, root_key_hash = $2 WHERE id = $3`,
				rowKey, rowHash, id); err != nil {
				slog.Warn("backfill project root_key failed", "workspace_id", id, "error", err)
			}
		}
		if id == excludeID {
			continue
		}
		if rowProvider == provider && stringOf(row["device_id"]) == deviceID && rowHash != "" && rowHash == targetHash {
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
//
// `filepath.Clean` 只认宿主 OS 的分隔符，而服务端可能跑在 Linux（团队模式），
// 路径却来自 Windows 客户端。比较前先把 `\` 折叠成 `/`，判据才与宿主无关。
func sameRootPath(a, b string) bool {
	if a == "" || b == "" {
		return false
	}
	norm := func(p string) string {
		return filepath.Clean(normalizePathSeparators(strings.TrimSpace(p)))
	}
	return strings.EqualFold(norm(a), norm(b))
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
			        COALESCE(w.provider,'server') AS provider, COALESCE(w.device_id,'') AS device_id,
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
				id := stringOf(row["id"])
				provider := stringOf(row["provider"])
				if provider == "" {
					provider = smProviderServer
				}
				allowed := false
				if provider == smProviderClient {
					allowed = deviceWriteAllowed(r.Context(), id)
				}
				out = append(out, map[string]interface{}{
					"id":             id,
					"name":           stringOf(row["name"]),
					"root_path":      stringOf(row["root_path"]),
					"provider":       provider,
					"device_id":      stringOf(row["device_id"]),
					"write_allowed":  allowed,
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
		Provider string `json:"provider"`
		DeviceID string `json:"device_id"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}
	provider := strings.TrimSpace(strings.ToLower(body.Provider))
	if provider == "" {
		provider = smProviderServer
	}
	deviceID := strings.TrimSpace(body.DeviceID)
	if provider != smProviderServer && provider != smProviderClient {
		BadRequest(w, "unsupported project provider")
		return
	}
	// 团队 Web 没有桌面选目录能力：未给 root_path 时在用户自己的项目根下创建
	// 托管目录（PROJECTS_ROOT 已启用才允许；桌面单机仍要求显式选文件夹）。
	if provider == smProviderServer && strings.TrimSpace(body.RootPath) == "" {
		if projectIsolationRoot == "" {
			BadRequest(w, "root_path is required")
			return
		}
		managed, err := createManagedProjectDir(claims, body.Name)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "cannot create project directory")
			return
		}
		body.RootPath = managed
	}
	var rootPath, rootKey string
	if provider == smProviderClient {
		// client provider：路径在用户设备上，服务端**不能** os.Stat / 不能套
		// PROJECTS_ROOT。由设备桥做规范化与存在性检查，并返回客户端 OS 语义的 root_key。
		if deviceID == "" {
			BadRequest(w, "device_id is required for client projects")
			return
		}
		resp, err := deviceHubSingleton.Call(r.Context(), smTenant(claims), claims.UserID, deviceID, deviceOpRequest{
			Op:   "canonicalize",
			Path: strings.TrimSpace(body.RootPath),
		})
		if err != nil {
			if errors.Is(err, errDeviceOffline) {
				JSONWithCode(w, http.StatusConflict, "device_offline", nil,
					"the device running Chiron is offline; open the app on that computer and retry")
				return
			}
			JSON(w, http.StatusBadRequest, APIResponse{Success: false, Error: err.Error()})
			return
		}
		if !resp.Exists || !resp.IsDir {
			JSON(w, http.StatusBadRequest, APIResponse{Success: false, Error: "root_path does not exist or is not a directory on the device"})
			return
		}
		if resp.RootKey == "" {
			JSON(w, http.StatusBadRequest, APIResponse{Success: false, Error: "device did not return a project root key"})
			return
		}
		rootPath = strings.TrimSpace(resp.CleanPath)
		if rootPath == "" {
			rootPath = strings.TrimSpace(body.RootPath)
		}
		rootKey = truncateRootKey(resp.RootKey)
	} else {
		// 团队模式：先把项目根限制/创建在 `<PROJECTS_ROOT>/<tenant>/<user>/` 内，
		// 再做存在性等常规校验。
		if err := ensureUserProjectDir(claims, body.RootPath); err != nil {
			var denied *projectRootNotAllowedError
			if errors.As(err, &denied) {
				JSONWithCode(w, http.StatusForbidden, "project_root_forbidden",
					map[string]interface{}{"base": denied.Base},
					"root_path must be inside your own project directory")
				return
			}
			Forbidden(w, err.Error())
			return
		}
		var why string
		rootPath, why = validateRootPath(body.RootPath)
		if why != "" {
			JSON(w, http.StatusBadRequest, APIResponse{Success: false, Error: why})
			return
		}
		rootKey = truncateRootKey(serverRootKey(rootPath))
	}
	// 同一目录只允许建一个项目。判重对象是 locator：
	// `(provider, device_id, root_key)` —— 不同设备上的同路径互不冲突。
	taken, err := projectLocatorTaken(r.Context(), smTenant(claims), claims.UserID, provider, deviceID, rootKey, "")
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
		// 客户端路径可能是 Windows 写法；服务端在 Linux 上时 filepath.Base 不认反斜杠。
		name = path.Base(strings.ReplaceAll(rootPath, `\`, "/"))
	}
	wsID, err := id.UUID()
	if err != nil {
		JSON(w, http.StatusInternalServerError, APIResponse{Success: false, Error: "cannot allocate id"})
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO session_map_workspaces
		   (id, tenant_id, user_id, name, root_path, provider, device_id, root_key, root_key_hash, sort_index, viewport, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 0, '{}'::jsonb, NOW(), NOW())`,
		wsID, smTenant(claims), claims.UserID, name, rootPath, provider, deviceID, rootKey, rootKeyHash(rootKey)); err != nil {
		// 唯一索引是并发下的最后一道防线：两个请求同时通过上面的判重时，
		// 后到的 INSERT 会撞 23505，同样按"重复项目"返回，而不是 500。
		if isUniqueViolation(err) {
			JSON(w, http.StatusConflict, APIResponse{
				Success: false, Error: "a project already uses this folder",
			})
			return
		}
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
		"provider": provider, "device_id": deviceID,
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
		// 路径归属决定校验方：client 项目必须由设备桥校验，不能在服务端 os.Stat。
		var provider, deviceID string
		row, rowErr := db.GlobalDBManager.FetchOne(r.Context(),
			`SELECT COALESCE(provider,'server') AS provider, COALESCE(device_id,'') AS device_id
			   FROM session_map_workspaces
			  WHERE id = $1 AND tenant_id = $2 AND user_id = $3`,
			wsID, smTenant(claims), claims.UserID)
		if rowErr != nil || row == nil {
			JSON(w, http.StatusNotFound, APIResponse{Success: false, Error: "project not found"})
			return
		}
		provider = stringOf(row["provider"])
		if provider == "" {
			provider = smProviderServer
		}
		deviceID = stringOf(row["device_id"])

		var rp, rootKey string
		if provider == smProviderClient {
			if deviceID == "" {
				BadRequest(w, "client project is missing device_id")
				return
			}
			resp, err := deviceHubSingleton.Call(r.Context(), smTenant(claims), claims.UserID, deviceID, deviceOpRequest{
				Op:   "canonicalize",
				Path: strings.TrimSpace(*body.RootPath),
			})
			if err != nil {
				if errors.Is(err, errDeviceOffline) {
					JSON(w, http.StatusConflict, APIResponse{Success: false, Error: "device_offline"})
					return
				}
				JSON(w, http.StatusBadRequest, APIResponse{Success: false, Error: err.Error()})
				return
			}
			if !resp.Exists || !resp.IsDir || resp.RootKey == "" {
				JSON(w, http.StatusBadRequest, APIResponse{Success: false, Error: "root_path does not exist or is not a directory on the device"})
				return
			}
			rp = strings.TrimSpace(resp.CleanPath)
			if rp == "" {
				rp = strings.TrimSpace(*body.RootPath)
			}
			rootKey = truncateRootKey(resp.RootKey)
		} else {
			if err := ensureUserProjectDir(claims, *body.RootPath); err != nil {
				Forbidden(w, err.Error())
				return
			}
			var why string
			rp, why = validateRootPath(*body.RootPath)
			if why != "" {
				JSON(w, http.StatusBadRequest, APIResponse{Success: false, Error: why})
				return
			}
			rootKey = truncateRootKey(serverRootKey(rp))
		}
		// 与 CreateProject 同一条判据：把项目改指到另一个项目已在用的目录同样要拒。
		// excludeID 传自身，否则「只改名字、路径不动」会被自己挡住。
		taken, err := projectLocatorTaken(r.Context(), smTenant(claims), claims.UserID, provider, deviceID, rootKey, wsID)
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
		sets = append(sets, "root_key = "+arg(rootKey))
		sets = append(sets, "root_key_hash = "+arg(rootKeyHash(rootKey)))
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
		if isUniqueViolation(err) {
			JSON(w, http.StatusConflict, APIResponse{
				Success: false, Error: "a project already uses this folder",
			})
			return
		}
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
	// H78：绑定的**另一端**也要校验归属 —— 会话必须属于调用者。
	//
	// 旧实现只校验目标 workspace：于是 A 可以把自己项目的 `force=true` 用在本会话
	// 别人的 session 上，DELETE 会按 session_id 删掉 B 的归属（"移动"语义变成了
	// 跨用户抢绑定），同时把 B 的会话标题/状态暴露进 A 的项目列表。
	// 未知与越权统一 404（不泄露 session 是否存在）。
	if !smOwnsSession(r.Context(), sessionID, claims) {
		JSON(w, http.StatusNotFound, APIResponse{Success: false, Error: "session not found"})
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
		// 删除旧归属时再带一层 owner 收敛（H78）：`workspace_id <> $2` 只保证"不是本项目"，
		// 不保证"这条归属属于同一个人"。归属若在两次查询之间被并发改动，这一步就不会误删。
		if _, err := tx.Exec(r.Context(),
			`DELETE FROM session_map_nodes n
			  USING session_map_workspaces w
			  WHERE n.session_id = $1 AND n.edge_kind = 'project' AND n.workspace_id <> $2
			    AND w.id = n.workspace_id AND w.tenant_id = $3 AND w.user_id = $4`,
			sessionID, wsID, smTenant(claims), claims.UserID); err != nil {
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
// smOwnsSession 判定会话是否属于调用者（H78）。
//
// 与 `smOwnsWorkspace` 同一套判据（tenant + user），同样"查不到即否" ——
// 与 HTTP 层的 404 语义一致，避免"能区分存在与无权"的探针。
func smOwnsSession(ctx context.Context, sessionID string, claims *auth.Claims) bool {
	if db.GlobalDBManager == nil || sessionID == "" || claims == nil {
		return false
	}
	row, err := db.GlobalDBManager.FetchOne(ctx,
		`SELECT 1 AS ok FROM sessions
		  WHERE id = $1 AND user_id = $2 AND ($3 = '' OR tenant_id = $3)`,
		sessionID, claims.UserID, smTenant(claims))
	return err == nil && row != nil
}

func smOwnsWorkspace(r *http.Request, wsID string, claims *auth.Claims) bool {
	if db.GlobalDBManager == nil {
		return false
	}
	row, err := db.GlobalDBManager.FetchOne(r.Context(),
		`SELECT 1 AS ok FROM session_map_workspaces
		  WHERE id = $1 AND tenant_id = $2 AND user_id = $3`, wsID, smTenant(claims), claims.UserID)
	return err == nil && row != nil
}
