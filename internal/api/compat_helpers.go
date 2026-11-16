package api

// compat_helpers.go —— 租户层退场后的等价实现。
//
// 这些函数原先分别住在 tenant_perms.go / tenant_features.go /
// ent_entitlement_gate.go / ent_costcenter_handler.go 里，随 SaaS 域一并删除。
// 调用点（登录发 token、模型下拉过滤、平台级端点门禁、注册唯一冲突判定）
// 是本地形态仍然需要的，所以在此给出**不依赖已删表**的精简版本。

import (
	"context"
	"errors"
	"net/http"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/jackc/pgx/v5/pgconn"
)

// resolveUserPermsDefault 返回该用户的权限集合。
//
// 原实现优先读 tenant_members ⋈ ent_roles，查不到再回退角色默认值；两张表都已删除，
// 因此这里直接使用 `auth.RolePermissions` —— 本地单机形态没有"每用户自定义角色"，
// 角色即权限来源。
//
// 返回值**永不为 nil**：auth.HasPermission 用 `Perms != nil` 区分"显式授予（含空集）"
// 与"未设置（回退 RolePermissions）"，返回 nil 会让调用方的回退逻辑意外生效、放大权限。
func resolveUserPermsDefault(_ context.Context, _ /* userID */, fallbackRole string) []string {
	return fallbackPerms(fallbackRole)
}

// fallbackPerms 取角色默认权限，并复制一份避免调用方改到 map 内部切片。
// 未知角色返回空切片而不是 nil：让"未知角色 = 无权限"成为确定结论。
func fallbackPerms(role string) []string {
	perms, ok := auth.RolePermissions[role]
	if !ok || perms == nil {
		return []string{}
	}
	out := make([]string, len(perms))
	copy(out, perms)
	return out
}

// isUniqueViolation 判定 SQLSTATE 23505（唯一约束冲突）。
// 注册路径用它把"邮箱重复"映射成模糊提示，避免账号枚举。
func isUniqueViolation(err error) bool {
	var pe *pgconn.PgError
	return errors.As(err, &pe) && pe.Code == "23505"
}

// isPlatformOwner 判定是否为平台级 owner。
//
// 租户管理层已退场，"平台级"与"实例唯一管理员"在本形态下同义，
// 因此口径就是 `claims.Role == "owner"`。
func isPlatformOwner(claims *auth.Claims) bool {
	return claims != nil && claims.Role == "owner"
}

// EffectiveModelSet 返回该请求的模型准入白名单。
//
// 第二个返回值为 false 表示**未施加任何限制** —— 调用方据此原样返回模型列表。
// 本地单机形态没有租户套餐与模型策略（ent_ 系列表已删），因此恒为"不限制"：
// 可用模型只由「已配置的 provider key + llm_models 缓存」决定。
func EffectiveModelSet(_ context.Context, _ /* tenantID */ int16, _ /* userID */ string) (map[string]struct{}, bool) {
	return nil, false
}

// handleModelRoutesSync 响应 Python 引擎启动时的 GET /v1/internal/model-routes。
//
// 引擎用这份数据做"按租户覆盖 provider/fallback"；本地单机形态没有租户模型路由
// （ent_model_routes 表已随 SaaS 域删除），因此恒返回空列表 —— 引擎侧的
// sync_routes 拿到 0 条即走默认路径，provider 只由已配置的 key 决定。
func handleModelRoutesSync(w http.ResponseWriter, r *http.Request) {
	OK(w, map[string]any{"routes": []any{}, "total": 0})
}
