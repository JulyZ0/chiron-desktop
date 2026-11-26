package session

import (
	"context"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

// A3：隐式建会话的租户必须来自调用方 claims，缺省才退回默认租户。
//
// 这条判据在运行时是"不见报错、只是数据落错桶"：多租户用户经消息路径创建的会话
// 被记进默认租户后，本人列表看不见、项目侧按 claims tenant 过滤也查不到。
func TestTenantIDFromContextPrefersClaims(t *testing.T) {
	ctx := auth.WithClaims(context.Background(), &auth.Claims{UserID: "u1", TenantID: 7})
	if got := tenantIDFromContext(ctx); got != 7 {
		t.Fatalf("有 claims 时必须用 claims 的租户，得到 %d", got)
	}

	// claims 存在但 tenant 未设置（单租户/老 token）⇒ 默认租户，而不是 0。
	zero := auth.WithClaims(context.Background(), &auth.Claims{UserID: "u1"})
	if got := tenantIDFromContext(zero); got != DefaultTenantID {
		t.Fatalf("claims.TenantID=0 时应回退默认租户，得到 %d", got)
	}

	// 无 claims（内部任务 / 后台收尾）⇒ 默认租户。
	if got := tenantIDFromContext(context.Background()); got != DefaultTenantID {
		t.Fatalf("无 claims 时应回退默认租户，得到 %d", got)
	}

	// nil ctx 不能 panic（后台任务里偶发）。
	if got := tenantIDFromContext(nil); got != DefaultTenantID {
		t.Fatalf("nil ctx 应回退默认租户，得到 %d", got)
	}
}
