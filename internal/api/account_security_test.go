package api

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"golang.org/x/crypto/bcrypt"
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

func TestCurrentPasswordPolicyUsesAuthoritativePasswordSet(t *testing.T) {
	hash, err := bcrypt.GenerateFromPassword([]byte("OldPassword123!"), bcrypt.MinCost)
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name        string
		passwordSet bool
		oldPassword string
		newPassword string
		want        string
	}{
		{name: "本地首次设置免旧密码", oldPassword: "", newPassword: "NewPassword123!", want: ""},
		{name: "团队账号空旧密码", passwordSet: true, oldPassword: "", newPassword: "NewPassword123!", want: "current password is required"},
		{name: "团队账号错误旧密码", passwordSet: true, oldPassword: "wrong", newPassword: "NewPassword123!", want: "current password is incorrect"},
		{name: "团队账号禁止相同密码", passwordSet: true, oldPassword: "OldPassword123!", newPassword: "OldPassword123!", want: "new password must be different from the current password"},
		{name: "团队账号正确旧密码", passwordSet: true, oldPassword: "OldPassword123!", newPassword: "NewPassword123!", want: ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := currentPasswordError(string(hash), tc.oldPassword, tc.newPassword, tc.passwordSet); got != tc.want {
				t.Fatalf("currentPasswordError() = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestUserResponseAlwaysIncludesPasswordState(t *testing.T) {
	for _, tc := range []struct {
		passwordSet bool
		mustChange  bool
	}{
		{},
		{passwordSet: true, mustChange: true},
	} {
		body, err := json.Marshal(UserResponse{
			ID: "u1", Name: "User", Role: "user",
			PasswordSet: tc.passwordSet, MustChangePassword: tc.mustChange,
		})
		if err != nil {
			t.Fatal(err)
		}
		for _, field := range []string{
			fmt.Sprintf(`"password_set":%t`, tc.passwordSet),
			fmt.Sprintf(`"must_change_password":%t`, tc.mustChange),
		} {
			if !strings.Contains(string(body), field) {
				t.Fatalf("认证状态字段不得省略：%s", body)
			}
		}
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
