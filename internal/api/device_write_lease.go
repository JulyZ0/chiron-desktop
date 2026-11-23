package api

// ── 设备写租约（R5）──
//
// client 项目的镜像回写会修改用户本机文件。默认**只读**：用户在项目侧栏明确
// 授予写权限（项目级租约）之前，回合照常执行、但差异不回写设备，并提示本轮修改
// 不会保存。租约存 Redis（无 TTL）：Redis 丢失即回到只读，安全侧失败。

import (
	"context"
	"errors"
	"net/http"
	"os"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// deviceWriteTenantEnabled 是租户级总闸（部署层）：0/false/off 时任何项目都不能写回。
func deviceWriteTenantEnabled() bool {
	v := strings.ToLower(strings.TrimSpace(os.Getenv("CHIRON_DEVICE_WRITE_ENABLED")))
	return v != "0" && v != "false" && v != "off"
}

func deviceWriteLeaseKey(projectID string) string {
	return db.RedisKey("device:write:lease:") + strings.TrimSpace(projectID)
}

func deviceWriteAllowed(ctx context.Context, projectID string) bool {
	if !deviceWriteTenantEnabled() {
		return false
	}
	if db.Redis == nil || strings.TrimSpace(projectID) == "" {
		return false
	}
	n, err := db.Redis.Exists(ctx, deviceWriteLeaseKey(projectID)).Result()
	return err == nil && n > 0
}

func setDeviceWriteLease(ctx context.Context, projectID string, allow bool) error {
	if db.Redis == nil {
		return errRedisLeaseUnavailable
	}
	key := deviceWriteLeaseKey(projectID)
	if allow {
		return db.Redis.Set(ctx, key, "1", 0).Err()
	}
	return db.Redis.Del(ctx, key).Err()
}

var errRedisLeaseUnavailable = errors.New("redis unavailable")

// tenantActiveWriteLeases 统计本租户当前有多少 client 项目持有写租约。
//
// 租约键本身只带 project_id（不重复存 tenant），所以先按租户列出 client 项目，
// 再用一次多键 EXISTS 统计。数量级是"每租户项目数"，不需要扫描全库。
func tenantActiveWriteLeases(ctx context.Context, tenantID int16) (int, error) {
	if db.GlobalDBManager == nil || db.Redis == nil {
		return 0, errRedisLeaseUnavailable
	}
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT id FROM session_map_workspaces
		  WHERE tenant_id = $1 AND COALESCE(provider,'server') = $2`,
		tenantID, smProviderClient)
	if err != nil {
		return 0, err
	}
	defer rows.Close()
	keys := make([]string, 0, 16)
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			continue
		}
		keys = append(keys, deviceWriteLeaseKey(id))
	}
	if err := rows.Err(); err != nil {
		return 0, err
	}
	if len(keys) == 0 {
		return 0, nil
	}
	n, err := db.Redis.Exists(ctx, keys...).Result()
	if err != nil {
		return 0, err
	}
	return int(n), nil
}

func deviceWriteProjectLimit(ctx context.Context) int {
	return loadDailyCostLimits(ctx).DeviceWriteMaxProjects
}

// writableClientProject 校验项目属于当前用户且 provider=client。
func writableClientProject(r *http.Request, claims *auth.Claims) (string, bool) {
	if db.GlobalDBManager == nil || claims == nil {
		return "", false
	}
	id := strings.TrimSpace(r.PathValue("id"))
	if id == "" {
		return "", false
	}
	row, err := db.GlobalDBManager.FetchOne(r.Context(),
		`SELECT COALESCE(provider,'server') AS provider
		   FROM session_map_workspaces
		  WHERE id = $1 AND tenant_id = $2 AND user_id = $3`,
		id, smTenant(claims), claims.UserID)
	if err != nil || row == nil || stringOf(row["provider"]) != smProviderClient {
		return "", false
	}
	return id, true
}

func (h *SessionMapHandler) GetDeviceWriteLease(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	id, ok := writableClientProject(r, claims)
	if !ok {
		JSONWithCode(w, http.StatusNotFound, CodeNotFound, nil, "project not found")
		return
	}
	OK(w, map[string]interface{}{"allowed": deviceWriteAllowed(r.Context(), id)})
}

func (h *SessionMapHandler) SetDeviceWriteLease(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	// R5 企业权限：授予/收回用户磁盘写权限只允许 owner/admin 操作。
	if claims.Role != "owner" && claims.Role != "admin" {
		JSONWithCode(w, http.StatusForbidden, "device_write_role_required", nil,
			"only owner or admin can change device write access")
		return
	}
	id, ok := writableClientProject(r, claims)
	if !ok {
		JSONWithCode(w, http.StatusNotFound, CodeNotFound, nil, "project not found")
		return
	}
	var body struct {
		Allow *bool `json:"allow"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || body.Allow == nil {
		BadRequest(w, "allow is required")
		return
	}
	if !deviceWriteTenantEnabled() {
		JSONWithCode(w, http.StatusForbidden, "device_write_disabled", nil,
			"device write-back is disabled for this deployment")
		return
	}
	if *body.Allow && !deviceWriteAllowed(r.Context(), id) {
		limit := deviceWriteProjectLimit(r.Context())
		if limit > 0 {
			used, err := tenantActiveWriteLeases(r.Context(), claims.TenantID)
			if err != nil {
				JSONWithCode(w, http.StatusServiceUnavailable, CodeServiceUnavailable, nil, err.Error())
				return
			}
			if used >= limit {
				JSONWithCode(w, http.StatusConflict, "device_write_limit_reached",
					map[string]interface{}{"used": used, "limit": limit},
					"tenant device-write project limit reached; revoke another project first")
				return
			}
		}
	}
	if err := setDeviceWriteLease(r.Context(), id, *body.Allow); err != nil {
		JSONWithCode(w, http.StatusServiceUnavailable, CodeServiceUnavailable, nil, err.Error())
		return
	}
	action := "device_write_revoked"
	detail := "allowed=false"
	if *body.Allow {
		action = "device_write_granted"
		detail = "allowed=true"
	}
	// R5 读写审计：授予/收回是"能不能改用户磁盘"的开关，必须留痕。
	db.AuditLog(r.Context(), claims.UserID, claims.TenantID, action,
		"/v1/session-map/projects/"+id+"/device-write", detail, r.RemoteAddr, nil)
	OK(w, map[string]interface{}{"allowed": *body.Allow})
}
