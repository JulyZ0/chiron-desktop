// project_vcs_test.go —— 版本控制状态检测与初始化的契约。
//
// 这几个函数直接决定"还原点能不能建"，而它们的判据（是不是仓库根、能不能
// 初始化）只有跑真实 git 才靠得住：`rev-parse` 的输出在各种边界（子目录、
// worktree、`GIT_DIR` 环境）差异很大，用替身回放的字符串只能证明我们写了
// 那个字符串。

package api

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// requireGit 在没装 git 的机器上跳过（GitMissing 分支另有替身用例覆盖）。
func requireGit(t *testing.T) {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("未安装 git")
	}
}

// rawGit 直接用系统 git 准备/检查夹具，不经过被测的受保护前缀。
func rawGit(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	cmd.Env = gitEnv()
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v 失败: %v\n%s", args, err, out)
	}
	return string(out)
}

func TestProjectVCSPlainDirectory(t *testing.T) {
	requireGit(t)

	dir := t.TempDir()
	got := projectVCS(context.Background(), dir)

	if got.Repo {
		t.Fatalf("普通文件夹不该被当成工作树: %+v", got)
	}
	// 目录里的文件不该被读：初始化之前这里连 .git 都没有。
	if got.GitMissing {
		t.Fatalf("环境里有 git 却报缺失: %+v", got)
	}
	if got.OwnRoot || got.RepoRoot != "" {
		t.Fatalf("非仓库不该有 repo_root / own_root: %+v", got)
	}
}

func TestProjectVCSRepoRoot(t *testing.T) {
	requireGit(t)

	dir := t.TempDir()
	rawGit(t, dir, "init", "-q", ".")

	got := projectVCS(context.Background(), dir)
	if !got.Repo {
		t.Fatalf("仓库根应判定为工作树: %+v", got)
	}
	// `OwnRoot` 的判据是"仓库根 == 项目根"。t.TempDir() 在 Windows 上可能给出
	// 短名（8.3）或大小写不同的路径，`sameRootPath` 的规范化正是为这种情形存在，
	// 所以这里断言 true 就是在验证那条规范化。
	if !got.OwnRoot {
		t.Fatalf("仓库根应判定为自身根: got=%+v", got)
	}
	if !sameRootPath(got.RepoRoot, dir) {
		t.Fatalf("repo_root 应指向项目根: got=%q want=%q", got.RepoRoot, dir)
	}
}

func TestProjectVCSSubdirectoryOfRepo(t *testing.T) {
	requireGit(t)

	root := t.TempDir()
	rawGit(t, root, "init", "-q", ".")
	sub := filepath.Join(root, "pkg", "inner")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatalf("建子目录失败: %v", err)
	}

	got := projectVCS(context.Background(), sub)
	// 位于仓库子目录时检查点**可用**（git -C 会找到祖先仓库）。
	if !got.Repo {
		t.Fatalf("子目录也应判定为工作树内: %+v", got)
	}
	// 但**不是**自身根 —— 这是禁止在子目录里再 git init 的唯一依据。
	if got.OwnRoot {
		t.Fatalf("子目录不应判定为自身根: %+v", got)
	}
	if !sameRootPath(got.RepoRoot, root) {
		t.Fatalf("repo_root 应指向祖先仓库根: got=%q want=%q", got.RepoRoot, root)
	}
}

func TestInitProjectRepoCreatesRepoWithoutCommitting(t *testing.T) {
	requireGit(t)

	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "a.txt"), []byte("x\n"), 0o644); err != nil {
		t.Fatalf("写文件失败: %v", err)
	}

	got, err := initProjectRepo(context.Background(), dir)
	if err != nil {
		t.Fatalf("初始化失败: %v", err)
	}
	if !got.Repo || !got.OwnRoot {
		t.Fatalf("初始化后应判定为自身仓库根: %+v", got)
	}
	if _, err := os.Stat(filepath.Join(dir, ".git")); err != nil {
		t.Fatalf(".git 未创建: %v", err)
	}

	// **不能有提交**：用户同意的是"启用还原点"，不是"把我的项目提交一遍"。
	// HEAD 指向一个不存在的分支，`rev-parse --verify HEAD` 必然失败。
	cmd := exec.Command("git", "rev-parse", "--verify", "--quiet", "HEAD")
	cmd.Dir = dir
	cmd.Env = gitEnv()
	if out, err := cmd.CombinedOutput(); err == nil {
		t.Fatalf("初始化后不该存在任何提交，HEAD 却可解析: %s", out)
	}

	// 工作区文件仍在且**未被暂存**：`git status --porcelain` 必须把它列为未跟踪。
	// 顺手 add 一次也会让用户以为自己的代码已经被"保存"了。
	status := rawGit(t, dir, "status", "--porcelain=v1")
	if !strings.Contains(status, "?? a.txt") {
		t.Fatalf("初始化不该把文件加入索引，status=%q", status)
	}
}

func TestInitProjectRepoIsIdempotentInsideRepo(t *testing.T) {
	requireGit(t)

	root := t.TempDir()
	rawGit(t, root, "init", "-q", ".")
	rawGit(t, root, "config", "chiron.test-marker", "1")

	// 子目录也走同一条路径：已经在仓库里就必须**什么都不做**。
	sub := filepath.Join(root, "sub")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatalf("建子目录失败: %v", err)
	}

	got, err := initProjectRepo(context.Background(), sub)
	if err != nil {
		t.Fatalf("已在仓库内不该报错: %v", err)
	}
	if !got.Repo || got.OwnRoot {
		t.Fatalf("子目录初始化应原样返回状态: %+v", got)
	}

	// 没有在子目录里再 init 出嵌套仓库 —— 那会把该目录从父仓库的工作树里"抠"出来。
	if _, err := os.Stat(filepath.Join(sub, ".git")); err == nil {
		t.Fatal("在仓库子目录里执行了 git init（嵌套仓库），会切断父仓库的工作树")
	}
	// 父仓库的本地配置未被重写（真重新 init 会重置它）。
	if cfg := rawGit(t, root, "config", "chiron.test-marker"); strings.TrimSpace(cfg) != "1" {
		t.Fatalf("父仓库配置被改动: %q", cfg)
	}
}

func TestProjectVCSReportsGitMissing(t *testing.T) {
	// 替身：模拟 `git` 不可执行。`errGitMissing` 与"不是仓库"必须分开 ——
	// 后者该给"启用"入口，前者该让用户去装 git。
	orig := gitRun
	gitRun = func(context.Context, string, ...string) (string, error) {
		return "", errGitMissing
	}
	defer func() { gitRun = orig }()

	got := projectVCS(context.Background(), t.TempDir())
	if !got.GitMissing {
		t.Fatalf("git 缺失应单独上报: %+v", got)
	}
	if got.Repo {
		t.Fatalf("git 缺失时不该报有仓库: %+v", got)
	}
}
