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
	if err := setDeviceWriteLease(r.Context(), id, *body.Allow); err != nil {
		JSONWithCode(w, http.StatusServiceUnavailable, CodeServiceUnavailable, nil, err.Error())
		return
	}
	OK(w, map[string]interface{}{"allowed": *body.Allow})
}
