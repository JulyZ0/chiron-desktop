package api

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// porcelain -z 的解析是本文件里最容易错的地方：NUL 分隔、重命名多一段、
// 两个状态字符各有含义。这里逐条钉住。

func TestParseGitStatusZBasic(t *testing.T) {
	// ` M` 工作区改了没暂存；`M ` 暂存了；`MM` 两边都有
	out := " M src/a.ts\x00M  src/b.ts\x00MM src/c.ts\x00"

	got := parseGitStatusZ(out)
	if len(got) != 3 {
		t.Fatalf("条目数应为 3，得到 %d", len(got))
	}

	if got[0].Path != "src/a.ts" || got[0].Status != "M" || got[0].Staged || !got[0].Unstaged {
		t.Errorf("工作区改动解析错: %+v", got[0])
	}
	if got[1].Path != "src/b.ts" || !got[1].Staged || got[1].Unstaged {
		t.Errorf("已暂存改动解析错: %+v", got[1])
	}
	if !got[2].Staged || !got[2].Unstaged {
		t.Errorf("两边都有改动应同时置位: %+v", got[2])
	}
}

func TestParseGitStatusZUntracked(t *testing.T) {
	got := parseGitStatusZ("?? notes.md\x00")
	if len(got) != 1 {
		t.Fatalf("条目数应为 1，得到 %d", len(got))
	}
	// 未跟踪文件不在索引里 —— 说它"已暂存"会让界面显示错误的勾选状态
	if got[0].Status != "?" || got[0].Staged {
		t.Errorf("未跟踪文件解析错: %+v", got[0])
	}
}

func TestParseGitStatusZRenameKeepsOldPath(t *testing.T) {
	// 重命名条目：先新路径，紧跟一段原路径。漏读第二段会让后续所有条目错位。
	out := "R  src/new.ts\x00src/old.ts\x00 M src/after.ts\x00"

	got := parseGitStatusZ(out)
	if len(got) != 2 {
		t.Fatalf("条目数应为 2（重命名的原路径不是独立条目），得到 %d: %+v", len(got), got)
	}
	if got[0].Path != "src/new.ts" || got[0].OldPath != "src/old.ts" || got[0].Status != "R" {
		t.Errorf("重命名解析错: %+v", got[0])
	}
	if got[1].Path != "src/after.ts" {
		t.Errorf("重命名后的条目错位: %+v", got[1])
	}
}

func TestParseGitStatusZPathsWithSpaces(t *testing.T) {
	// -z 模式下路径原样输出，空格不是分隔符
	got := parseGitStatusZ(" M src/a b/c d.ts\x00")
	if len(got) != 1 || got[0].Path != "src/a b/c d.ts" {
		t.Errorf("带空格路径解析错: %+v", got)
	}
}

func TestParseGitStatusZTruncatesAtLimit(t *testing.T) {
	var b strings.Builder
	for i := 0; i < maxProjectChanges+50; i++ {
		b.WriteString(" M f")
		b.WriteString(strings.Repeat("x", 4))
		b.WriteString("\x00")
	}
	got := parseGitStatusZ(b.String())
	if len(got) != maxProjectChanges {
		t.Errorf("应在 %d 条处截断，得到 %d", maxProjectChanges, len(got))
	}
}

func TestParseGitStatusZEmpty(t *testing.T) {
	if got := parseGitStatusZ(""); len(got) != 0 {
		t.Errorf("空输入应得空切片，得到 %d", len(got))
	}
	// 空输出也必须是非 nil 切片：JSON 里 null 会让前端多写一个分支
	if got := parseGitStatusZ(""); got == nil {
		t.Error("空输入应返回非 nil 切片")
	}
}

func TestChangeLetterPriority(t *testing.T) {
	cases := []struct {
		x, y byte
		want string
	}{
		{'?', '?', "?"},
		{'R', ' ', "R"},
		{' ', 'R', "R"},
		{'C', ' ', "C"},
		{'M', ' ', "M"},
		{' ', 'M', "M"},
		{'A', 'D', "D"}, // 工作区状态优先：磁盘上已经删了
	}
	for _, c := range cases {
		if got := changeLetter(c.x, c.y); got != c.want {
			t.Errorf("changeLetter(%q,%q) = %q，期望 %q", c.x, c.y, got, c.want)
		}
	}
}

func TestAddNumstatAccumulates(t *testing.T) {
	// MM 文件：暂存改 +3/-1，工作区又 +2/-4 ⇒ 相对 HEAD 是 +5/-5
	changes := []ProjectChange{{Path: "a.ts", Status: "M", Staged: true, Unstaged: true}}

	addNumstat(changes, "2\t4\ta.ts\n")
	addNumstat(changes, "3\t1\ta.ts\n")

	if changes[0].Insertions == nil || *changes[0].Insertions != 5 {
		t.Errorf("插入行应累加为 5，得到 %v", changes[0].Insertions)
	}
	if changes[0].Deletions == nil || *changes[0].Deletions != 5 {
		t.Errorf("删除行应累加为 5，得到 %v", changes[0].Deletions)
	}
}

func TestAddNumstatSkipsBinary(t *testing.T) {
	// 二进制文件 git 给 `-\t-`。当成 0 行会让界面打出一个 +0 -0 的假统计。
	changes := []ProjectChange{{Path: "logo.png", Status: "M"}}
	addNumstat(changes, "-\t-\tlogo.png\n")

	if changes[0].Insertions != nil || changes[0].Deletions != nil {
		t.Errorf("二进制文件不应有行数统计，得到 %+v", changes[0])
	}
}

func TestAddNumstatIgnoresUnknownPaths(t *testing.T) {
	// 简写重命名（`dir/{a => b}/c`）匹配不上，应当被跳过而不是猜
	changes := []ProjectChange{{Path: "dir/b/c.ts", Status: "R"}}
	addNumstat(changes, "1\t1\tdir/{a => b}/c.ts\n")

	if changes[0].Insertions != nil {
		t.Error("匹配不上的条目不应被写入统计")
	}
}

func TestAddNumstatEmptyInput(t *testing.T) {
	// 不能 panic，也不能给空切片写值
	addNumstat(nil, "1\t1\ta.ts\n")
	changes := []ProjectChange{{Path: "a.ts"}}
	addNumstat(changes, "")
	if changes[0].Insertions != nil {
		t.Error("空 numstat 不应产生统计")
	}
}

func TestGitEnvDropsRepoHijackVars(t *testing.T) {
	// 这些变量能把 git 指向另一个仓库/索引，继承下来就是劫持
	t.Setenv("GIT_DIR", "/somewhere/else")
	t.Setenv("GIT_INDEX_FILE", "/tmp/other.index")
	t.Setenv("GIT_WORK_TREE", "/tmp/other-tree")

	env := gitEnv()
	joined := strings.Join(env, "\n")

	for _, banned := range []string{"GIT_DIR=", "GIT_INDEX_FILE=", "GIT_WORK_TREE="} {
		if strings.Contains(joined, banned) {
			t.Errorf("环境里不应保留 %s", banned)
		}
	}
	// 自身必须设置的保护项
	for _, want := range []string{"GIT_OPTIONAL_LOCKS=0", "GIT_TERMINAL_PROMPT=0"} {
		if !strings.Contains(joined, want) {
			t.Errorf("环境里缺少 %s", want)
		}
	}
}

func TestGitReadPrefixIsReadOnly(t *testing.T) {
	// 固定前缀：`--no-optional-locks` 防 index 写回，
	// `core.quotepath=false` 防中文路径被转义
	var captured []string
	orig := gitRun
	gitRun = func(_ context.Context, _ string, args ...string) (string, error) {
		captured = args
		return "", nil
	}
	defer func() { gitRun = orig }()

	_, _ = gitRead(context.Background(), "/tmp", "status", "--porcelain=v1", "-z")

	if len(captured) < 3 {
		t.Fatalf("参数太少: %v", captured)
	}
	if captured[0] != "--no-optional-locks" {
		t.Errorf("首个参数应为 --no-optional-locks，得到 %q", captured[0])
	}
	if captured[1] != "-c" || captured[2] != "core.quotepath=false" {
		t.Errorf("缺少 quotepath 配置: %v", captured[:3])
	}
}

// 仓库自带的 `.git/config` 里可以指定**可执行程序**，git 会照做。
// `core.fsmonitor` 是最直接的一个：`git status` 会启动它。
//
// 这条用例真的建一个仓库、真的把 fsmonitor 指到一个会留下痕迹的脚本，然后
// 对比"裸跑"与"走 gitRead"的结果。仅断言参数里有没有 `-c core.fsmonitor=false`
// 是不够的 —— 那只证明我们写了这个字符串，不证明它挡住了执行。
func TestGitReadDoesNotRunRepoConfiguredPrograms(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("未安装 git")
	}
	root := t.TempDir()
	marker := filepath.Join(root, "pwned.txt")
	script := filepath.Join(root, "hook.sh")
	body := "#!/bin/sh\ntouch \"" + filepath.ToSlash(marker) + "\"\nexit 0\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatalf("写脚本失败: %v", err)
	}

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
	git("config", "core.fsmonitor", script)
	if err := os.WriteFile(filepath.Join(root, "a.txt"), []byte("x"), 0o644); err != nil {
		t.Fatalf("写文件失败: %v", err)
	}

	// 先确认这个环境**真的**会执行它。不能跳过这一步：有些环境（文件系统挂载为
	// noexec、Windows 上权限位不生效）本来就跑不了 hook，那时"标记文件不存在"
	// 什么也证明不了，测试会假通过。
	if _, err := gitRun(context.Background(), root, "status", "--porcelain=v1"); err != nil {
		t.Fatalf("裸跑 git status 失败: %v", err)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Skip("当前环境未执行 core.fsmonitor（脚本不可执行），本用例无从验证")
	}
	if err := os.Remove(marker); err != nil {
		t.Fatalf("清理痕迹失败: %v", err)
	}

	// 换受保护的前缀，同样的仓库同样的命令 —— 程序不该再被执行
	if _, err := gitRead(context.Background(), root, "status", "--porcelain=v1"); err != nil {
		t.Fatalf("gitRead 失败: %v", err)
	}
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("gitRead 执行了仓库配置里的程序（core.fsmonitor）")
	}
}

func TestCollectProjectChangesNotARepo(t *testing.T) {
	// 普通文件夹：repo=false 且不是错误
	orig := gitRun
	gitRun = func(context.Context, string, ...string) (string, error) {
		return "", errGitFailed
	}
	defer func() { gitRun = orig }()

	got, err := collectProjectChanges(context.Background(), t.TempDir())
	if err != nil {
		t.Fatalf("非 git 目录不应报错，得到 %v", err)
	}
	if got.Repo {
		t.Error("非 git 目录 repo 应为 false")
	}
	if got.Changes == nil {
		t.Error("Changes 应为非 nil 空切片")
	}
}

func TestCollectProjectChangesGitMissing(t *testing.T) {
	orig := gitRun
	gitRun = func(context.Context, string, ...string) (string, error) {
		return "", errGitMissing
	}
	defer func() { gitRun = orig }()

	got, err := collectProjectChanges(context.Background(), t.TempDir())
	if err != nil {
		t.Fatalf("缺 git 不应报错，得到 %v", err)
	}
	if !got.GitMissing {
		t.Error("应标记 GitMissing —— 否则界面会说成\"没有改动\"")
	}
}

// filterDriverNames 要能从 `git config --get-regexp '^filter\.'` 的输出里取出
// **驱动名**。驱动名可以含点（`filter.foo.bar.clean`），所以不能按第一个点切。
//
// 输入格式是 `git config --local --list` 的 `key=value`。为什么用 `--list` 而不是
// `--get-regexp`：后者在没有匹配项时以 exit 1 结束，与"仓库读不了"无法区分，
// 会把"探测失败"误当成"没有 filter"（fail-open）。见 repoFilterOverrides。
func TestFilterDriverNames(t *testing.T) {
	out := strings.Join([]string{
		"filter.lfs.clean=git-lfs clean -- %f",
		"filter.lfs.smudge=git-lfs smudge -- %f",
		"filter.lfs.process=git-lfs filter-process",
		"filter.lfs.required=true",
		// 值里含 `=` 也必须正确切分：只按第一个 `=` 分 key/value
		"filter.evil.clean=C:/tmp/a=b/evil.sh",
		"filter.foo.bar.clean=/bin/sh -c evil",
		"core.fsmonitor=true",
		"",
	}, "\n")

	got := filterDriverNames(out)
	want := []string{"lfs", "evil", "foo.bar"}
	if len(got) != len(want) {
		t.Fatalf("期望 %v，得到 %v", want, got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("第 %d 项应为 %q，得到 %q（全部：%v）", i, want[i], got[i], got)
		}
	}
}

// 干净仓库（只有 core.* 键）不应产生任何覆盖参数。
func TestFilterDriverNamesIgnoresNonFilterKeys(t *testing.T) {
	out := strings.Join([]string{
		"core.repositoryformatversion=0",
		"core.filemode=false",
		"core.bare=false",
		"remote.origin.url=git@example.com:x/y.git",
		"branch.main.remote=origin",
		"",
	}, "\n")
	if got := filterDriverNames(out); len(got) != 0 {
		t.Fatalf("非 filter 键不应被当成驱动名，得到 %v", got)
	}
}

// 仓库可以通过 `.gitattributes` + `.git/config` 让 git **执行任意程序**，入口不是
// 某个固定配置键，而是"attributes 里起名、config 里绑定"两半拼起来的：
//
//	git config filter.evil.clean "$PWD/evil.sh"   # 脚本里 touch PWNED
//	echo '*.txt filter=evil' > .gitattributes
//	git status --porcelain=v1                     # → PWNED 出现
//
// 固定的 `-c` 前缀挡不住它（名字是仓库随便起的），必须按仓库实际存在的驱动名动态
// 生成覆盖。这条用例真的建仓库、真的触发一次，再验证走 gitRead 时不再执行 —— 只
// 断言参数里有没有 `-c filter.…` 只能证明我们写了字符串，不能证明它挡住了执行。
func TestGitReadDoesNotRunRepoFilterDriver(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("未安装 git")
	}
	root := t.TempDir()
	marker := filepath.Join(root, "pwned-filter.txt")
	script := filepath.Join(root, "evil.sh")
	body := "#!/bin/sh\ntouch \"" + filepath.ToSlash(marker) + "\"\ncat\n"
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatalf("写脚本失败: %v", err)
	}

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
	// 用 -c 提交，否则 fillter 在 add/commit 阶段就会跑起来，痕迹提前出现
	git("-c", "user.email=a@b", "-c", "user.name=a", "config", "filter.evil.clean", script)
	git("config", "filter.evil.smudge", "cat")
	if err := os.WriteFile(filepath.Join(root, ".gitattributes"), []byte("*.txt filter=evil\n"), 0o644); err != nil {
		t.Fatalf("写 .gitattributes 失败: %v", err)
	}
	if err := os.WriteFile(filepath.Join(root, "a.txt"), []byte("hello\n"), 0o644); err != nil {
		t.Fatalf("写文件失败: %v", err)
	}

	// 先确认这个环境**真的**会执行 filter。不确认就跳过整条用例：
	// 有些环境（noexec 挂载、Windows 上权限位不生效）根本跑不起来，那时
	// "标记文件不存在"什么也证明不了，测试会假通过。
	if _, err := gitRun(context.Background(), root, "status", "--porcelain=v1"); err != nil {
		t.Fatalf("裸跑 git status 失败: %v", err)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Skip("当前环境未执行 filter 驱动（脚本不可执行），本用例无从验证")
	}
	if err := os.Remove(marker); err != nil {
		t.Fatalf("清理痕迹失败: %v", err)
	}

	// 同一个仓库、同一条命令，走受保护的前缀 —— 程序不该再被执行
	if _, err := gitRead(context.Background(), root, "status", "--porcelain=v1"); err != nil {
		t.Fatalf("gitRead 失败: %v", err)
	}
	if _, err := os.Stat(marker); err == nil {
		t.Fatal("gitRead 执行了仓库 .gitattributes 指定的 filter 驱动")
	}
}
