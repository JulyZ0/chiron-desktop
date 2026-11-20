package config

// resources.go —— 随包资产（migrations / web / uv）的**唯一**路径口径。
//
// ## 要解决什么
//
// 打包态的进程 CWD 与安装目录**没有关系**：Electron 从开始菜单启动时，子进程的
// cwd 可能是 `C:\Windows\System32`、也可能是某个用户目录。任何 `filepath.Join(
// "migrations", "versions")` 这样的裸相对路径都只能靠"恰好从仓库根启动"生效，
// 实测表现是诊断页报 `read migrations dir migrations\versions: cannot find the path`，
// 而迁移其实**就在** `C:\Program Files\Chiron\resources\migrations\versions`。
//
// 反过来，开发态（`go run ./cmd/chiron`、`go test`）的 exe 在临时目录里，
// 资产却在仓库根。两种形态必须由同一处判断，否则 migrations / web / uv 会各自
// 漂移出不同的相对路径口径（这正是本文件诞生前的现场）。
//
// ## 优先级（按顺序取第一个**存在**的）
//
//  1. `CHIRON_RESOURCES_DIR`：桌面壳注入的资产根（打包态 = `<安装根>\resources`）；
//  2. `<exe 所在目录>/resources`：安装包里再套一层 resources 的布局；
//  3. `<exe 所在目录>`：当前 electron-builder 的形态（exe 与 migrations/web/uv 同级）；
//  4. 开发态仓库根：从 cwd 与 exe 目录向上找含 `migrations/versions` 的目录。
//
// 显式 env 只决定**先看哪里**，不是硬绑定：env 指错（少一层、指到 data 目录）时
// 会继续看后面的候选，而不是把整块能力判死 —— 配错路径应该降级，不该让网关起不来。
//
// 所有返回值都是**绝对路径**；找不到时返回空串，由调用方决定"报 unknown"还是"报错"。

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

const (
	// ResourcesDirEnv 是资产根的显式覆盖（桌面壳 frontend-vue/electron/local-stack.cjs 注入）。
	ResourcesDirEnv = "CHIRON_RESOURCES_DIR"
	// MigrationsDirEnv 直接指定迁移目录，优先级高于任何资源根探测（运维逃生口）。
	MigrationsDirEnv = "MIGRATIONS_DIR"

	// migrationsRelPath 是 alembic 迁移脚本在资产根下的位置。
	migrationsRelPath = "migrations/versions"
)

// ResourceRoots 返回按优先级排列的候选资源根（绝对路径、已去重）。
//
// 导出是为了让启动日志/诊断能说明"到底看了哪些地方"：排障时"没找到"必须能回答
// "找了哪"，否则用户只能猜。
func ResourceRoots() []string {
	return resourceRootsFor(exeDir(), cwd())
}

// ResourcesRoot 返回第一个**存在**的候选资源根；全都不存在时返回空串。
func ResourcesRoot() string {
	return resourcesRootIn(ResourceRoots())
}

// resourcesRootIn 是 ResourcesRoot 的可测试内核。
func resourcesRootIn(roots []string) string {
	for _, root := range roots {
		if isDir(root) {
			return root
		}
	}
	return ""
}

// ResourcePath 返回资源根下 rel 的绝对路径；所有候选根都没有它时返回空串。
//
// rel 用 `/` 分隔（内部用 filepath.Join 归一，Windows 上得到 `\`）。
// 绝对路径原样返回：调用方（如 MIGRATIONS_DIR）已经给定完整位置，不再猜。
func ResourcePath(rel string) string {
	rel = strings.TrimSpace(rel)
	if rel == "" {
		return ""
	}
	if filepath.IsAbs(rel) {
		return filepath.Clean(rel)
	}
	// 拒绝 `..`：这个入口只用于"资源根下的固定资产"，任何上跳都会越过资源根
	// （将来若把请求参数拼进来，就是一条目录穿越）。
	for _, seg := range strings.Split(filepath.ToSlash(rel), "/") {
		if seg == ".." {
			return ""
		}
	}
	return resolveIn(ResourceRoots(), rel)
}

// WebRoot 返回前端构建产物目录（含 index.html）；找不到返回空串（网关不托管 SPA）。
//
// 两种布局：
//   - 打包态：`<资源根>/web`（electron-builder.yml 的 extraResources 把 dist 复制到这里，
//     因为 Go 进程打不开 asar）；
//   - 开发态：`<仓库根>/frontend-vue/dist`。
func WebRoot() string {
	return webRootIn(ResourceRoots())
}

// webRootIn 是 WebRoot 的可测试内核。
func webRootIn(roots []string) string {
	for _, root := range roots {
		for _, rel := range []string{"web", filepath.Join("frontend-vue", "dist")} {
			dir := filepath.Join(root, rel)
			if isFile(filepath.Join(dir, "index.html")) {
				return dir
			}
		}
	}
	return ""
}

// UVBin 返回随包 uv 可执行文件的绝对路径；找不到返回空串。
//
// 布局与 `desktop-local/scripts/prepare_uv.py` / `python-engine/app/api/deps.py`
// 同一约定：`<资源根>/uv/<platform>-<arch>/uv(.exe)`；平台键算错时再枚举 `uv/*/`
// （只读目录列表，不执行任何东西）。开发态额外看 `frontend-vue/assets/uv/...`
// —— 打包前资产就落在那里。
func UVBin() string {
	return uvBinIn(ResourceRoots())
}

// uvBinIn 是 UVBin 的可测试内核。
func uvBinIn(roots []string) string {
	exe := uvExecutableName()
	key := uvPlatformKey()
	rels := []string{
		filepath.Join("uv", key, exe),
		filepath.Join("uv", exe),
		filepath.Join("frontend-vue", "assets", "uv", key, exe),
	}
	for _, root := range roots {
		for _, rel := range rels {
			if p := filepath.Join(root, rel); isFile(p) {
				return p
			}
		}
		entries, err := os.ReadDir(filepath.Join(root, "uv"))
		if err != nil {
			continue
		}
		for _, entry := range entries {
			if !entry.IsDir() {
				continue
			}
			if p := filepath.Join(root, "uv", entry.Name(), exe); isFile(p) {
				return p
			}
		}
	}
	return ""
}

// ── 内部实现 ─────────────────────────────────────────────────────────

// resourceRootsFor 是 ResourceRoots 的可测试内核：exe/wd 由调用方给，
// 单测才能用临时目录模拟"打包态"/"开发态"两种布局。
func resourceRootsFor(exe, wd string) []string {
	var roots []string
	seen := map[string]bool{}
	add := func(p string) {
		p = strings.TrimSpace(p)
		if p == "" {
			return
		}
		abs, err := filepath.Abs(p)
		if err != nil {
			return
		}
		abs = filepath.Clean(abs)
		// Windows 路径大小写不敏感：去重要按 Fold，否则同一目录会被看两遍。
		key := strings.ToLower(abs)
		if seen[key] {
			return
		}
		seen[key] = true
		roots = append(roots, abs)
	}

	add(os.Getenv(ResourcesDirEnv))
	if exe != "" {
		add(filepath.Join(exe, "resources"))
		add(exe)
	}
	if root := findRepoRoot(wd, exe); root != "" {
		add(root)
	}
	return roots
}

// resolveIn 在 roots 里找 rel 的第一个存在项（文件或目录都算）。
func resolveIn(roots []string, rel string) string {
	for _, root := range roots {
		candidate := filepath.Join(root, rel)
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
	}
	return ""
}

// findRepoRoot 从 start 一路向上找含 `migrations/versions` 的目录。
//
// 判据刻意用**迁移目录**而不是 `go.mod`：迁移目录是"这套资产能不能被找到"的
// 直接证据；go.mod 在同级子模块里也可能出现，用它判断会误认仓库根。
func findRepoRoot(starts ...string) string {
	for _, start := range starts {
		start = strings.TrimSpace(start)
		if start == "" {
			continue
		}
		dir, err := filepath.Abs(start)
		if err != nil {
			continue
		}
		for {
			if isDir(filepath.Join(dir, migrationsRelPath)) {
				return dir
			}
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			dir = parent
		}
	}
	return ""
}

// exeDir 返回当前可执行文件所在目录（解析符号链接：macOS/Linux 上 /usr/local/bin
// 的软链不解析就会指回安装前缀而不是真实位置）。失败返回空串。
func exeDir() string {
	exe, err := os.Executable()
	if err != nil {
		return ""
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		exe = resolved
	}
	return filepath.Dir(exe)
}

func cwd() string {
	wd, err := os.Getwd()
	if err != nil {
		return ""
	}
	return wd
}

func isDir(path string) bool {
	st, err := os.Stat(path)
	return err == nil && st.IsDir()
}

func isFile(path string) bool {
	st, err := os.Stat(path)
	return err == nil && !st.IsDir()
}

// uvExecutableName 与 `python-engine/app/api/deps.py::uv_executable_name` 同口径。
func uvExecutableName() string {
	if runtime.GOOS == "windows" {
		return "uv.exe"
	}
	return "uv"
}

// uvPlatformKey 与 `prepare_uv.py` / `runtime-install.cjs` / 引擎的
// `uv_platform_key()` 同一约定：`win32-x64` / `darwin-arm64` / `linux-x64`。
func uvPlatformKey() string {
	var platform string
	switch runtime.GOOS {
	case "windows":
		platform = "win32"
	case "darwin":
		platform = "darwin"
	default:
		platform = "linux"
	}
	var arch string
	switch runtime.GOARCH {
	case "amd64":
		arch = "x64"
	case "arm64":
		arch = "arm64"
	case "386":
		arch = "x86"
	default:
		arch = runtime.GOARCH
	}
	return platform + "-" + arch
}
