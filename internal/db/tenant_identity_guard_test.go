package db

import (
	"os"
	"strings"
	"testing"
)

// H33：租户、幂等与邮箱唯一性的三方口径必须一致。
//
// 这三条都只存在于 DDL 里（幂等键范围、邮箱唯一索引与登录谓词），没有 DB 无法
// 从行为上断言，所以用源码守卫钉住迁移文件的关键契约：任何一条被"顺手改回去"，
// 这里立刻红，而不是等到多租户/大小写变体在生产上互相踩。
func TestTenantIdentityMigrationInvariants(t *testing.T) {
	const path = "../../migrations/versions/0013_tenant_identity.py"
	src, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读取 %s 失败: %v", path, err)
	}
	text := string(src)

	checks := []struct{ name, want string }{
		{"幂等键主键必须含租户", "PRIMARY KEY (tenant_id, idempotency_key)"},
		{"幂等键按 key 单查仍需索引", "ix_task_idempotency_key"},
		{"邮箱唯一索引必须基于 lower(email)", "ON public.users (lower(email))"},
		{"必须保留冲突隔离表（冲突处理方案）", "identity_email_conflicts"},
		{"必须先把存量 email 规范化", "lower(btrim(email))"},
		{"迁移必须挂在 0012 之后", `down_revision: str | Sequence[str] | None = "0012_session_fold_lineage"`},
	}
	for _, c := range checks {
		if !strings.Contains(text, c.want) {
			t.Fatalf("%s：迁移里找不到 %q", c.name, c.want)
		}
	}

	// 唯一索引必须仍是唯一索引（改成非唯一会让大小写变体共存）。
	if strings.Contains(text, "CREATE INDEX IF NOT EXISTS ux_users_email_lower") {
		t.Fatal("ux_users_email_lower 必须是 UNIQUE 索引")
	}
}

// 登录/注册/服务层必须与 lower(email) 索引同口径。
func TestEmailNormalizationIsUsedOnWriteAndRead(t *testing.T) {
	cases := []struct{ file, want string }{
		{"../auth/local.go", "lower(COALESCE(email, '')) = $1"},
		{"../auth/local.go", "NormalizeEmail(email)"},
		{"../auth/local.go", "NormalizeEmail(user.Email)"},
		{"../api/auth.go", "req.Email = auth.NormalizeEmail(req.Email)"},
		{"../api/auth.go", "lower(COALESCE(email, '')) = $1 AND tenant_id = $2"},
	}
	for _, c := range cases {
		src, err := os.ReadFile(c.file)
		if err != nil {
			t.Fatalf("读取 %s 失败: %v", c.file, err)
		}
		if !strings.Contains(string(src), c.want) {
			t.Fatalf("%s：缺少 %q（邮箱规范化与唯一索引口径必须一致）", c.file, c.want)
		}
	}
}
