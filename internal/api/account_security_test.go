package api

import (
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

// 系统生成的临时密码必须**一定能过**现有复杂度策略，否则管理员会拿到一个
// "系统说它合规、登录却说它不合规"的密码。
func TestGenerateTemporaryPasswordAlwaysPassesPolicy(t *testing.T) {
	for i := 0; i < 200; i++ {
		pwd, err := generateTemporaryPassword()
		if err != nil {
			t.Fatalf("生成失败: %v", err)
		}
		if len(pwd) != 18 {
			t.Fatalf("长度应为 18，得到 %d（%q）", len(pwd), pwd)
		}
		if ok, msg := auth.ValidatePasswordComplexity(pwd); !ok {
			t.Fatalf("生成的密码没过复杂度校验: %s（%q）", msg, pwd)
		}
	}
}

func TestUserStatusNormalizesEmptyToActive(t *testing.T) {
	for _, in := range []string{"", "  ", "ACTIVE", "Active"} {
		if got := userStatus(in); got != userStatusActive {
			t.Errorf("userStatus(%q) = %q, want active", in, got)
		}
	}
	if got := userStatus(" disabled "); got != userStatusDisabled {
		t.Errorf("userStatus(disabled) = %q", got)
	}
}

func TestAllowBeforePasswordChange(t *testing.T) {
	for _, p := range []string{
		"/v1/auth/password", "/v1/auth/logout", "/v1/auth/profile",
		"/v1/auth/session", "/v1/auth/refresh",
	} {
		if !allowBeforePasswordChange(p) {
			t.Errorf("%s 应放行", p)
		}
	}
	for _, p := range []string{"/v1/conversations", "/v1/admin/users", "/v1/kb"} {
		if allowBeforePasswordChange(p) {
			t.Errorf("%s 不应在改密前放行", p)
		}
	}
}
