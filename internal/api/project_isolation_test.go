package api

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

func withProjectIsolationRoot(t *testing.T, root string) {
	t.Helper()
	prev := projectIsolationRoot
	SetProjectIsolationRoot(root)
	t.Cleanup(func() { SetProjectIsolationRoot(prev) })
}

func TestProjectRootAllowedInsideUserDirectory(t *testing.T) {
	root := t.TempDir()
	withProjectIsolationRoot(t, root)

	base := filepath.Join(root, "1", "u1")
	if err := os.MkdirAll(filepath.Join(base, "repo"), 0o700); err != nil {
		t.Fatal(err)
	}
	if !projectRootAllowed("1", "u1", filepath.Join(base, "repo")) {
		t.Fatal("用户自己的目录应允许")
	}
	if projectRootAllowed("1", "u2", filepath.Join(base, "repo")) {
		t.Fatal("不能指向别的用户目录")
	}
	if projectRootAllowed("1", "u1", filepath.Join(root, "2", "u9")) {
		t.Fatal("不能指向别的租户目录")
	}
}

func TestProjectRootAllowedRejectsDotDotEscape(t *testing.T) {
	root := t.TempDir()
	withProjectIsolationRoot(t, root)
	base := filepath.Join(root, "1", "u1")
	if err := os.MkdirAll(base, 0o700); err != nil {
		t.Fatal(err)
	}
	if projectRootAllowed("1", "u1", filepath.Join(base, "..", "..")) {
		t.Fatal(".. 逃逸必须拒绝")
	}
}

func TestEnsureUserProjectDirCreatesAndRejects(t *testing.T) {
	root := t.TempDir()
	withProjectIsolationRoot(t, root)
	claims := &auth.Claims{UserID: "u1", TenantID: 1}

	target := filepath.Join(root, "1", "u1", "newrepo")
	if err := ensureUserProjectDir(claims, target); err != nil {
		t.Fatalf("应创建用户目录: %v", err)
	}
	if st, err := os.Stat(target); err != nil || !st.IsDir() {
		t.Fatalf("目录未创建: %v", err)
	}

	outside := filepath.Join(root, "1", "u2", "x")
	if err := ensureUserProjectDir(claims, outside); err == nil {
		t.Fatal("别的用户目录必须拒绝")
	}
}

func TestProjectIsolationDisabledAllowsAnyPath(t *testing.T) {
	withProjectIsolationRoot(t, "")
	if !projectRootAllowed("1", "u1", filepath.Join(t.TempDir(), "anything")) {
		t.Fatal("桌面模式（未配 PROJECTS_ROOT）不应限制")
	}
	if err := ensureUserProjectDir(&auth.Claims{UserID: "u1", TenantID: 1}, "/tmp/x"); err != nil {
		t.Fatalf("桌面模式不应创建/限制: %v", err)
	}
}

// 桌面本地模式以 owner 身份运行：PROJECTS_ROOT 只约束团队里的普通成员，
// 否则本机用户选任意文件夹都会被 403（真实回归：开启"服务模式"后无法新建项目）。
func TestEnsureUserProjectDirOwnerBypassesIsolation(t *testing.T) {
	root := t.TempDir()
	withProjectIsolationRoot(t, root)
	owner := &auth.Claims{UserID: "u1", TenantID: 1, Role: "owner"}
	outside := filepath.Join(t.TempDir(), "my-code")
	if err := ensureUserProjectDir(owner, outside); err != nil {
		t.Fatalf("owner 不应被 PROJECTS_ROOT 限制: %v", err)
	}

	member := &auth.Claims{UserID: "u1", TenantID: 1, Role: "user"}
	if err := ensureUserProjectDir(member, outside); err == nil {
		t.Fatal("普通成员仍必须限制在自有目录内")
	}
}
