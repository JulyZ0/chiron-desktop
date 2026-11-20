package config

// resources_test.go —— 资源解析的优先级与两种布局。
//
// 这些用例刻意不碰真实安装目录：用 t.TempDir() 摆出
//   - 打包态（exe 与 migrations/web/uv 同级，或再套一层 resources/）；
//   - 开发态（cwd 在仓库深处的子目录，向上才看得到 migrations/versions）；
// 两种形态，再断言"显式 env 指错时能继续往下看"。
//
// 为什么值得单测：这条路径错了，症状是诊断页一句 `cannot find the path`，
// 而进程本身照常启动 —— 静默降级只有测试能拦住。

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writeFile 造一个文件（含父目录）。
func writeFile(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte("# fixture\n"), 0o644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

// mkMigrations 在 dir 下造一个可用于解析的迁移目录。
func mkMigrations(t *testing.T, dir string) string {
	t.Helper()
	versions := filepath.Join(dir, "migrations", "versions")
	writeFile(t, filepath.Join(versions, "0001_baseline.py"))
	return versions
}

// clearResourceEnv 隔离测试环境：宿主真的设了这些变量时不能污染断言。
func clearResourceEnv(t *testing.T) {
	t.Helper()
	t.Setenv(ResourcesDirEnv, "")
	t.Setenv(MigrationsDirEnv, "")
}

func TestResourceRootsPriorityOrder(t *testing.T) {
	clearResourceEnv(t)
	base := t.TempDir()
	exe := filepath.Join(base, "Program Files", "Chiron", "resources")
	env := filepath.Join(base, "pinned", "resources")
	wd := filepath.Join(base, "empty-cwd")
	if err := os.MkdirAll(wd, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv(ResourcesDirEnv, env)

	roots := resourceRootsFor(exe, wd)
	if len(roots) < 3 {
		t.Fatalf("候选根至少应含 env/exe-resources/exe，得到 %v", roots)
	}
	if roots[0] != filepath.Clean(env) {
		t.Errorf("第 1 位必须是 %s（显式 env），得到 %s", env, roots[0])
	}
	if roots[1] != filepath.Clean(filepath.Join(exe, "resources")) {
		t.Errorf("第 2 位应是 <exe>/resources，得到 %s", roots[1])
	}
	if roots[2] != filepath.Clean(exe) {
		t.Errorf("第 3 位应是 <exe>，得到 %s", roots[2])
	}
}

func TestResourcePathPackagedLayoutExeDir(t *testing.T) {
	clearResourceEnv(t)
	base := t.TempDir()
	exe := filepath.Join(base, "Chiron", "resources")
	want := mkMigrations(t, exe)

	roots := resourceRootsFor(exe, filepath.Join(base, "cwd"))
	if got := resolveIn(roots, "migrations/versions"); got != want {
		t.Fatalf("打包态应解析到 %s，得到 %q（roots=%v）", want, got, roots)
	}
}

func TestResourcePathPrefersNestedResourcesDir(t *testing.T) {
	clearResourceEnv(t)
	base := t.TempDir()
	exe := filepath.Join(base, "install")
	nested := mkMigrations(t, filepath.Join(exe, "resources"))
	mkMigrations(t, exe) // 同级也有一份：<exe>/resources 应优先

	roots := resourceRootsFor(exe, filepath.Join(base, "cwd"))
	if got := resolveIn(roots, "migrations/versions"); got != nested {
		t.Fatalf("应优先 <exe>/resources，得到 %q（roots=%v）", got, roots)
	}
}

func TestResourcePathEnvOverrideWins(t *testing.T) {
	clearResourceEnv(t)
	base := t.TempDir()
	exe := filepath.Join(base, "install")
	mkMigrations(t, exe)
	envRoot := filepath.Join(base, "pinned")
	envMigrations := mkMigrations(t, envRoot)
	t.Setenv(ResourcesDirEnv, envRoot)

	if got := resolveIn(resourceRootsFor(exe, filepath.Join(base, "cwd")), "migrations/versions"); got != envMigrations {
		t.Fatalf("CHIRON_RESOURCES_DIR 应压过 exe 旁，得到 %q", got)
	}
	// 走公开入口也要一致（ResourcePath → ResourceRoots → 真实 exe/cwd；env 仍排第一）。
	if got := ResourcePath("migrations/versions"); got != envMigrations {
		t.Fatalf("ResourcePath 应返回 env 下的迁移目录，得到 %q", got)
	}
}

func TestResourcePathBrokenEnvFallsThrough(t *testing.T) {
	clearResourceEnv(t)
	base := t.TempDir()
	exe := filepath.Join(base, "install", "resources")
	want := mkMigrations(t, exe)
	// env 指到一个空目录（少一层/指到 data 目录的典型错配）
	emptyEnv := filepath.Join(base, "wrong")
	if err := os.MkdirAll(emptyEnv, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv(ResourcesDirEnv, emptyEnv)

	if got := resolveIn(resourceRootsFor(exe, filepath.Join(base, "cwd")), "migrations/versions"); got != want {
		t.Fatalf("env 指错时应继续看 exe 旁，得到 %q", got)
	}
}

func TestResourcePathDevRepoRoot(t *testing.T) {
	clearResourceEnv(t)
	base := t.TempDir()
	repo := filepath.Join(base, "chiron-desktop")
	want := mkMigrations(t, repo)
	exe := filepath.Join(base, "go-build-temp", "b001") // 测试二进制常在临时目录
	if err := os.MkdirAll(exe, 0o755); err != nil {
		t.Fatal(err)
	}
	deepCwd := filepath.Join(repo, "internal", "db")

	roots := resourceRootsFor(exe, deepCwd)
	if got := resolveIn(roots, "migrations/versions"); got != want {
		t.Fatalf("开发态应从 cwd 向上找到仓库根 %s，得到 %q（roots=%v）", want, got, roots)
	}
}

func TestResourcePathMissingReturnsEmpty(t *testing.T) {
	clearResourceEnv(t)
	base := t.TempDir()
	exe := filepath.Join(base, "install")
	if err := os.MkdirAll(exe, 0o755); err != nil {
		t.Fatal(err)
	}
	roots := resourceRootsFor(exe, filepath.Join(base, "cwd"))
	if got := resolveIn(roots, "migrations/versions"); got != "" {
		t.Fatalf("不存在的资产必须返回空串（调用方据此报 unknown），得到 %q", got)
	}
	// exe 目录存在但没有任何资产：ResourcesRoot 指向它（布局的最后一档），
	// 而资产解析仍然为空 —— 两者语义不同，不能混成一句"没装"。
	if root := resourcesRootIn(roots); root != filepath.Clean(exe) {
		t.Fatalf("exe 目录存在时 ResourcesRoot 应是它，得到 %q", root)
	}
	if root := resourcesRootIn([]string{filepath.Join(base, "nope1"), filepath.Join(base, "nope2")}); root != "" {
		t.Fatalf("候选根全不存在时应为空串，得到 %q", root)
	}
}

func TestResourcePathRejectsParentTraversal(t *testing.T) {
	clearResourceEnv(t)
	t.Setenv(ResourcesDirEnv, t.TempDir())
	for _, rel := range []string{"../etc/passwd", "a/../../b", `..\windows\system32`} {
		if got := ResourcePath(rel); got != "" {
			t.Errorf("含 .. 的相对路径必须拒绝，%q → %q", rel, got)
		}
	}
	if got := ResourcePath(""); got != "" {
		t.Errorf("空路径应返回空串，得到 %q", got)
	}
}

func TestResourcesRootReturnsFirstExisting(t *testing.T) {
	clearResourceEnv(t)
	base := t.TempDir()
	envRoot := filepath.Join(base, "pinned")
	exe := filepath.Join(base, "install")
	mkMigrations(t, exe)
	if err := os.MkdirAll(envRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv(ResourcesDirEnv, envRoot)

	got := resourcesRootIn(resourceRootsFor(exe, filepath.Join(base, "cwd")))
	if got != filepath.Clean(envRoot) {
		t.Fatalf("第一个存在的候选根应是 env，得到 %q", got)
	}
}

func TestWebRootPackagedAndDev(t *testing.T) {
	clearResourceEnv(t)
	base := t.TempDir()
	exe := filepath.Join(base, "install", "resources")
	packaged := filepath.Join(exe, "web")
	writeFile(t, filepath.Join(packaged, "index.html"))
	roots := resourceRootsFor(exe, filepath.Join(base, "cwd"))
	if got := webRootIn(roots); got != packaged {
		t.Fatalf("打包态 WebRoot 应是 <exe>/web，得到 %q", got)
	}

	// 开发态：仓库根下的 frontend-vue/dist
	devRepo := filepath.Join(base, "repo")
	mkMigrations(t, devRepo)
	devDist := filepath.Join(devRepo, "frontend-vue", "dist")
	writeFile(t, filepath.Join(devDist, "index.html"))
	devRoots := resourceRootsFor(filepath.Join(base, "go-tmp"), filepath.Join(devRepo, "internal"))
	if got := webRootIn(devRoots); got != devDist {
		t.Fatalf("开发态 WebRoot 应是 frontend-vue/dist，得到 %q（roots=%v）", got, devRoots)
	}
}

func TestUVBinPackagedLayout(t *testing.T) {
	clearResourceEnv(t)
	base := t.TempDir()
	exe := filepath.Join(base, "install", "resources")
	want := filepath.Join(exe, "uv", uvPlatformKey(), uvExecutableName())
	writeFile(t, want)

	if got := uvBinIn(resourceRootsFor(exe, filepath.Join(base, "cwd"))); got != want {
		t.Fatalf("UVBin 应解析到随包路径 %s，得到 %q", want, got)
	}

	// 平台键算错（别的键）时仍能从 uv/*/ 枚举到。
	otherExe := filepath.Join(base, "install2")
	other := filepath.Join(otherExe, "uv", "some-other-key", uvExecutableName())
	writeFile(t, other)
	if got := uvBinIn(resourceRootsFor(otherExe, filepath.Join(base, "cwd2"))); got != other {
		t.Fatalf("平台键不匹配时应枚举 uv/*/，得到 %q", got)
	}

	// 没有 uv 时返回空串（开发机正常形态，不该伪造路径）。
	emptyExe := filepath.Join(base, "install3")
	if err := os.MkdirAll(emptyExe, 0o755); err != nil {
		t.Fatal(err)
	}
	if got := uvBinIn(resourceRootsFor(emptyExe, filepath.Join(base, "cwd3"))); got != "" {
		t.Fatalf("无 uv 时应为空串，得到 %q", got)
	}
}

func TestUVPlatformKeyShape(t *testing.T) {
	key := uvPlatformKey()
	if !strings.Contains(key, "-") {
		t.Fatalf("平台键应形如 win32-x64，得到 %q", key)
	}
	for _, prefix := range []string{"win32", "darwin", "linux"} {
		if strings.HasPrefix(key, prefix+"-") {
			return
		}
	}
	t.Fatalf("平台键前缀不认识：%q", key)
}
