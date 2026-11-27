package api

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// gitTestRepo 建一个临时仓库并配置提交身份。
func gitTestRepo(t *testing.T) string {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	root := t.TempDir()
	ctx := context.Background()
	if _, err := gitWrite(ctx, root, "init"); err != nil {
		t.Fatalf("git init: %v", err)
	}
	for _, kv := range [][2]string{
		{"user.email", "ci@example.com"},
		{"user.name", "CI"},
	} {
		if _, err := gitWrite(ctx, root, "config", kv[0], kv[1]); err != nil {
			t.Fatalf("git config %s: %v", kv[0], err)
		}
	}
	return root
}

func writeFile(t *testing.T, root, name, content string) {
	t.Helper()
	full := filepath.Join(root, filepath.FromSlash(name))
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// 仓库里的 pre-commit 必须**不执行**：否则绑定一个下载来的项目，点一次提交
// 就可能运行它指定的脚本。
func TestGitWriteDisablesRepoHooks(t *testing.T) {
	root := gitTestRepo(t)
	hooks := filepath.Join(root, ".git", "hooks")
	if err := os.MkdirAll(hooks, 0o755); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(root, "hook-ran.txt")
	hook := "#!/bin/sh\nprintf x > " + marker + "\nexit 1\n"
	if err := os.WriteFile(filepath.Join(hooks, "pre-commit"), []byte(hook), 0o755); err != nil {
		t.Fatal(err)
	}

	ctx := context.Background()
	writeFile(t, root, "a.txt", "hello")
	if _, err := gitWrite(ctx, root, append([]string{"add"}, gitPathsArgs([]string{"a.txt"})...)...); err != nil {
		t.Fatalf("add: %v", err)
	}
	if _, err := gitWrite(ctx, root, "commit", "-m", "test"); err != nil {
		t.Fatalf("commit should ignore hooks: %v", err)
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatalf("repo hook executed (marker exists)")
	}
}

// clean filter 是一个真实的命令执行面：`.gitattributes` + `filter.x.clean` 组合
// 可以在 `git add` 时运行任意程序。写前缀必须把它覆写成 cat。
func TestGitWriteNeutralizesCleanFilter(t *testing.T) {
	root := gitTestRepo(t)
	ctx := context.Background()
	writeFile(t, root, ".gitattributes", "*.secret filter=evil\n")
	if _, err := gitWrite(ctx, root, "config", "filter.evil.clean", "definitely-not-a-real-program-xyz"); err != nil {
		t.Fatal(err)
	}
	writeFile(t, root, "a.secret", "raw-content")
	// 没有覆写时 git 会因找不到 clean 程序而失败
	if _, err := gitWrite(ctx, root, append([]string{"add"}, gitPathsArgs([]string{"a.secret"})...)...); err != nil {
		t.Fatalf("add with broken filter should succeed under override: %v", err)
	}
	staged, err := gitWrite(ctx, root, "show", ":a.secret")
	if err != nil {
		t.Fatalf("show staged: %v", err)
	}
	if staged != "raw-content" {
		t.Fatalf("filter should not rewrite content, got %q", staged)
	}
}

// process filter 走的是另一条执行路径（long-running filter protocol），
// 也要被清空；否则 broken process 会让 add 直接失败。
func TestGitWriteNeutralizesProcessFilter(t *testing.T) {
	root := gitTestRepo(t)
	ctx := context.Background()
	writeFile(t, root, ".gitattributes", "*.bin filter=evilproc\n")
	if _, err := gitWrite(ctx, root, "config", "filter.evilproc.process", "definitely-not-a-real-program-xyz"); err != nil {
		t.Fatal(err)
	}
	writeFile(t, root, "a.bin", "payload")
	if _, err := gitWrite(ctx, root, append([]string{"add"}, gitPathsArgs([]string{"a.bin"})...)...); err != nil {
		t.Fatalf("add with broken process filter should succeed: %v", err)
	}
}

func TestGitWriteOverridesCoverSecurityFaces(t *testing.T) {
	root := gitTestRepo(t)
	ctx := context.Background()
	if _, err := gitWrite(ctx, root, "config", "filter.evil.clean", "nope"); err != nil {
		t.Fatal(err)
	}
	flags, err := gitWriteOverrides(ctx, root)
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(flags, " ")
	for _, want := range []string{
		"core.hooksPath=",
		"commit.gpgsign=false",
		"tag.gpgsign=false",
		"core.fsmonitor=false",
		"filter.evil.clean=cat",
		"filter.evil.smudge=cat",
		"filter.evil.process=",
		"filter.evil.required=false",
	} {
		if !strings.Contains(joined, want) {
			t.Fatalf("write overrides missing %q: %v", want, flags)
		}
	}
}

func TestValidateGitPath(t *testing.T) {
	good := []string{"a.txt", "src/a.ts", "dir/sub/file name.md"}
	for _, value := range good {
		if got, err := validateGitPath(value); err != nil || got != value {
			t.Fatalf("validateGitPath(%q) = %q, %v", value, got, err)
		}
	}
	bad := []string{
		"", " ", "../x", "a/../../x", "/abs", "C:/x", `dir\file`, "-x", "a\x00b",
	}
	for _, value := range bad {
		if _, err := validateGitPath(value); err == nil {
			t.Fatalf("validateGitPath(%q) should fail", value)
		}
	}
}

func TestAllowedGitPathsOnlyCurrentChanges(t *testing.T) {
	changes := ProjectChanges{Changes: []ProjectChange{
		{Path: "src/a.ts"},
		{Path: "new.txt"},
	}}
	got, err := allowedGitPaths([]string{"src/a.ts", "src/a.ts"}, changes)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0] != "src/a.ts" {
		t.Fatalf("unexpected: %v", got)
	}
	for _, requested := range [][]string{
		{"src/other.ts"},
		{"../x"},
		{"--force"},
		nil,
	} {
		if _, err := allowedGitPaths(requested, changes); err == nil {
			t.Fatalf("paths %v should be rejected", requested)
		}
	}
}
