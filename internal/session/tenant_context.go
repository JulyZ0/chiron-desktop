package session

import (
	"context"

	"github.com/athenavi/chiron/internal/auth"
)

// tenantIDFromContext 取**隐式建会话**该用的租户：调用方 claims 里的 tenant，缺省默认租户。
//
// 背景（A3 多租户口径统一）：显式建会话（`CreateSession`）早就要调用方传 tenantID，
// 但"消息先落库、会话后建"的隐式路径（SaveUserMessage / SaveMessages / CreateTurn /
// SaveMessage）仍硬编码 `DefaultTenantID` —— 邀请码/多租户用户经这条路径产生的会话会落进
// 默认租户：本人列表看不见，项目侧按 claims tenant 过滤也找不到（"归入项目 404"的同类根因）。
//
// 为什么从 ctx 读而不是加参数：这些函数的调用点遍布提交链、续跑链与后台收尾，
// 加参数会把 tenant 透传十几层；而 claims 在请求入口就已放进 ctx，落库用的
// `context.WithoutCancel(ctx)` 也会保留它（WithoutCancel 只剥离取消，不剥离值）。
// 内部任务/无请求上下文的调用没有 claims，退回默认租户（与 seed 一致）。
func tenantIDFromContext(ctx context.Context) int16 {
	if claims := auth.GetClaims(ctx); claims != nil && claims.TenantID != 0 {
		return claims.TenantID
	}
	return DefaultTenantID
}
