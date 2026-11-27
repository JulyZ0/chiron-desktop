package api

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// 单文件 diff 的取数与边界。真仓库用例在没装 git 的环境跳过 —— 与同目录其余用例同口径。
func initDiffRepo(t *testing.T) string {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("未安装 git")
	}
	root := t.TempDir()
	git := func(args ...string) {
		t.Helper()
		cmd := exec.Command("git", args...)
		cmd.Dir = root
		cmd.Env = gitEnv()
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v 失败: %v\n%s", args, err, out)
		}
	}
	git("init", "-q", ".")
	git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "--allow-empty", "-qm", "init")
	return root
}

func TestCollectProjectDiffModified(t *testing.T) {
	root := initDiffRepo(t)
	file := filepath.Join(root, "a.txt")
	if err := os.WriteFile(file, []byte("one\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command("git", "add", "a.txt")
	cmd.Dir = root
	cmd.Env = gitEnv()
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git add: %v\n%s", err, out)
	}
	cmd = exec.Command("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "a")
	cmd.Dir = root
	cmd.Env = gitEnv()
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git commit: %v\n%s", err, out)
	}
	if err := os.WriteFile(file, []byte("two\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	got, err := collectProjectDiff(context.Background(), root, "a.txt")
	if err != nil {
		t.Fatalf("collectProjectDiff: %v", err)
	}
	if !got.Repo || got.Untracked || got.Binary {
		t.Fatalf("判定不对: %+v", got)
	}
	if !strings.Contains(got.Diff, "-one") || !strings.Contains(got.Diff, "+two") {
		t.Fatalf("diff 内容不对: %q", got.Diff)
	}
	// 只读守卫：diff 走同一套固定前缀，不允许仓库配置里的外部 diff。
	if !strings.Contains(got.Diff, "diff --git") {
		t.Fatalf("不是统一 diff: %q", got.Diff)
	}
}

func TestCollectProjectDiffUntrackedReturnsNoDiff(t *testing.T) {
	root := initDiffRepo(t)
	if err := os.WriteFile(filepath.Join(root, "new.txt"), []byte("hello\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := collectProjectDiff(context.Background(), root, "new.txt")
	if err != nil {
		t.Fatal(err)
	}
	if !got.Repo || !got.Untracked {
		t.Fatalf("新文件应标记 untracked: %+v", got)
	}
	if got.Diff != "" {
		t.Fatalf("未跟踪文件不应返回 diff: %q", got.Diff)
	}
}

func TestCollectProjectDiffRejectsPathEscape(t *testing.T) {
	root := initDiffRepo(t)
	for _, rel := range []string{"../outside.txt", "..\\outside.txt", "/etc/passwd", "C:\\Windows\\win.ini"} {
		if _, err := collectProjectDiff(context.Background(), root, rel); err == nil {
			t.Errorf("路径 %q 未被拒绝", rel)
		}
	}
}

func TestCappedDiffTruncatesOnLineBoundary(t *testing.T) {
	line := strings.Repeat("x", 1024) + "\n"
	big := strings.Repeat(line, (maxProjectDiffBytes/len(line))+10)
	out, truncated := cappedDiff(big)
	if !truncated {
		t.Fatal("超限 diff 未标记截断")
	}
	if len(out) > maxProjectDiffBytes {
		t.Fatalf("截断后仍超限: %d", len(out))
	}
	if !strings.HasSuffix(out, "\n") {
		t.Fatal("截断点没有回退到换行，前端会读到半行")
	}
	// 每行长度一致：能整除说明既没多也没少半行。
	for _, ln := range strings.Split(strings.TrimSuffix(out, "\n"), "\n") {
		if len(ln) != len(line)-1 {
			t.Fatalf("出现半行（%d 字节）", len(ln))
		}
	}
}

func TestCappedDiffKeepsSmallInput(t *testing.T) {
	const small = "diff --git a/x b/x\n"
	if out, truncated := cappedDiff(small); out != small || truncated {
		t.Fatalf("小 diff 被改动: %q truncated=%v", out, truncated)
	}
}

func TestCollectProjectLogAndCommitDiff(t *testing.T) {
	root := initDiffRepo(t)
	file := filepath.Join(root, "a.txt")
	if err := os.WriteFile(file, []byte("hello\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{
		{"add", "a.txt"},
		{"-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "add a.txt"},
	} {
		cmd := exec.Command("git", args...)
		cmd.Dir = root
		cmd.Env = gitEnv()
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}

	log, err := collectProjectLog(context.Background(), root, 10)
	if err != nil {
		t.Fatalf("collectProjectLog: %v", err)
	}
	if !log.Repo || log.Branch == "" {
		t.Fatalf("repo/branch 缺失: %+v", log)
	}
	if len(log.Commits) < 2 {
		t.Fatalf("提交数不足: %+v", log.Commits)
	}
	head := log.Commits[0]
	if head.Subject != "add a.txt" || head.Author != "a" || len(head.SHA) < 7 {
		t.Fatalf("最新提交解析错: %+v", head)
	}

	diff, err := collectCommitDiff(context.Background(), root, head.SHA)
	if err != nil {
		t.Fatalf("collectCommitDiff: %v", err)
	}
	if !diff.Repo || !strings.Contains(diff.Diff, "+hello") {
		t.Fatalf("提交 diff 内容不对: %+v", diff)
	}
}

func TestCommitSHARejectsOptionInjection(t *testing.T) {
	for _, sha := range []string{"--upload-pack=evil", "HEAD~1", "abc", strings.Repeat("a", 41)} {
		if validCommitSHA(sha) {
			t.Errorf("sha %q 应被拒绝", sha)
		}
	}
	if !validCommitSHA("0123456") {
		t.Error("合法短 sha 被拒绝")
	}
}

func TestShowProjectCommitRejectsBadSHA(t *testing.T) {
	root := initDiffRepo(t)
	if _, err := collectCommitDiff(context.Background(), root, "--output=/tmp/x"); err == nil {
		t.Fatal("非法 sha 未被拒绝")
	}
}
