package api

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/db"
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

// ── N5 P1：stage / unstage ──────────────────────────────────────────────

func TestStageAndUnstagePaths(t *testing.T) {
	root := gitTestRepo(t)
	ctx := context.Background()
	writeFile(t, root, "a.txt", "one")
	writeFile(t, root, "dir/b.txt", "two")

	before, err := collectProjectChanges(ctx, root)
	if err != nil {
		t.Fatal(err)
	}
	for _, change := range before.Changes {
		if change.Staged {
			t.Fatalf("%s should not be staged before", change.Path)
		}
	}

	if err := stageGitPaths(ctx, root, []string{"a.txt", "dir/b.txt"}); err != nil {
		t.Fatalf("stage: %v", err)
	}
	staged, err := collectProjectChanges(ctx, root)
	if err != nil {
		t.Fatal(err)
	}
	for _, change := range staged.Changes {
		if !change.Staged {
			t.Fatalf("%s should be staged after add", change.Path)
		}
	}

	if err := unstageGitPaths(ctx, root, []string{"a.txt"}); err != nil {
		t.Fatalf("unstage: %v", err)
	}
	after, err := collectProjectChanges(ctx, root)
	if err != nil {
		t.Fatal(err)
	}
	for _, change := range after.Changes {
		switch change.Path {
		case "a.txt":
			if change.Staged {
				t.Fatal("a.txt should be unstaged")
			}
		case "dir/b.txt":
			if !change.Staged {
				t.Fatal("dir/b.txt should stay staged")
			}
		}
	}
}

// 新仓库（尚无提交）里第一次暂存后撤回：`git reset --` 必须可用。
func TestUnstageOnUnbornBranch(t *testing.T) {
	root := gitTestRepo(t)
	ctx := context.Background()
	writeFile(t, root, "first.txt", "x")
	if err := stageGitPaths(ctx, root, []string{"first.txt"}); err != nil {
		t.Fatalf("stage: %v", err)
	}
	if err := unstageGitPaths(ctx, root, []string{"first.txt"}); err != nil {
		t.Fatalf("unstage on unborn branch: %v", err)
	}
	changes, err := collectProjectChanges(ctx, root)
	if err != nil {
		t.Fatal(err)
	}
	for _, change := range changes.Changes {
		if change.Path == "first.txt" && change.Staged {
			t.Fatal("first.txt should be unstaged")
		}
	}
}

func TestGitIndexPathsSelection(t *testing.T) {
	changes := ProjectChanges{Changes: []ProjectChange{
		{Path: "a.txt", Staged: true},
		{Path: "b.txt", Staged: false},
		{Path: "c.txt", Staged: true},
	}}
	// stage all：包含未暂存
	got, err := gitIndexPaths(gitPathsRequest{All: true}, changes, true)
	if err != nil || len(got) != 3 {
		t.Fatalf("stage all = %v, %v", got, err)
	}
	// unstage all：只包含已暂存
	got, err = gitIndexPaths(gitPathsRequest{All: true}, changes, false)
	if err != nil || len(got) != 2 || got[0] != "a.txt" || got[1] != "c.txt" {
		t.Fatalf("unstage all = %v, %v", got, err)
	}
	// 显式 unstage 未暂存路径必须被拒
	if _, err := gitIndexPaths(gitPathsRequest{Paths: []string{"b.txt"}}, changes, false); err == nil {
		t.Fatal("unstage of unstaged path should fail")
	}
	// 显式 stage 未暂存路径可以
	if got, err := gitIndexPaths(gitPathsRequest{Paths: []string{"b.txt"}}, changes, true); err != nil || len(got) != 1 {
		t.Fatalf("stage b.txt = %v, %v", got, err)
	}
}

func TestAcquireProjectGitLockIsExclusive(t *testing.T) {
	if db.Redis != nil {
		t.Skip("redis configured; covered by integration environment")
	}
	ctx := context.Background()
	release, err := acquireProjectGitLock(ctx, "p-lock")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := acquireProjectGitLock(ctx, "p-lock"); !errors.Is(err, errGitWriteBusy) {
		t.Fatalf("second acquire should be busy, got %v", err)
	}
	release()
	release2, err := acquireProjectGitLock(ctx, "p-lock")
	if err != nil {
		t.Fatalf("acquire after release: %v", err)
	}
	release2()
}

// ── N5 P2：commit ──────────────────────────────────────────────────────

func TestCommitStagedPathsOnlyCommitsIndex(t *testing.T) {
	root := gitTestRepo(t)
	ctx := context.Background()
	writeFile(t, root, "staged.txt", "s")
	writeFile(t, root, "other.txt", "o")
	if err := stageGitPaths(ctx, root, []string{"staged.txt"}); err != nil {
		t.Fatal(err)
	}

	sha, err := commitStagedPaths(ctx, root, "feat: staged only")
	if err != nil {
		t.Fatalf("commit: %v", err)
	}
	if sha == "" {
		t.Fatal("commit sha should not be empty")
	}
	changes, err := collectProjectChanges(ctx, root)
	if err != nil {
		t.Fatal(err)
	}
	for _, change := range changes.Changes {
		if change.Path == "staged.txt" && (change.Staged || change.Unstaged) {
			t.Fatalf("staged.txt should be committed and clean, got %+v", change)
		}
		if change.Path == "other.txt" && change.Staged {
			t.Fatal("other.txt was not staged and must not be committed")
		}
	}
	if _, err := gitWrite(ctx, root, "log", "--oneline", "-1"); err != nil {
		t.Fatalf("log: %v", err)
	}
}

func TestCommitRequiresMessageAndStagedContent(t *testing.T) {
	root := gitTestRepo(t)
	ctx := context.Background()
	if _, err := commitStagedPaths(ctx, root, "   "); err == nil {
		t.Fatal("empty message should fail")
	}
	writeFile(t, root, "a.txt", "x")
	if _, err := commitStagedPaths(ctx, root, "nothing staged"); err == nil {
		t.Fatal("commit with nothing staged should fail")
	}
}
