package api

// project_files.go —— 项目文件浏览与读取（只读）。
//
// **为什么是只读**
//
// 这一层存在的目的是让"用户看到的东西"和"agent 改的东西"是同一份文件。写入仍然只
// 由 agent 的文件工具（python-engine/app/tools）经沙箱完成，前端不提供第二个写入口：
// 两个写入口意味着两套权限判断、两套并发保护，早晚会有一条绕过审计。等编辑器真正
// 需要落盘时再单独评估（那时要先解决"agent 正在改这个文件"的冲突）。
//
// **为什么不复用曾经的 /api/editor/\***
//
// 那组端点（全局单例，绑 `cfg.StorageRoot`）已退役：它返回到深度 5 的整棵子树，
// 前端无法懒加载，大仓库会直接卡住；而且它挂的是 per-user 存储目录，不是用户绑定的
// 项目目录，语义完全不同。这里的路径来自项目（`session_map_workspaces.root_path`），
// 一次只列一层，并且带符号链接检查 —— 两者不是同一件事，不能合并。
//
// **安全边界只有一条**
//
// 所有路径都必须落在 `root_path` 之内。能被绑成项目的目录，在创建/改绑时已经过
// `validateRootPath` 那道关（拒绝系统目录、驱动器根），因此这里不再重复那套判断 ——
// 只需回答一个问题：**这个相对路径能不能从项目根走出去**。词法上（`..`、绝对路径）
// 与物理上（符号链接）都要拦：只做 `filepath.Join` + 前缀比较的话，项目内一个指向
// `C:\Windows` 的链接就能把整个系统盘读出来。
//
// 分工：本文件下半部分是纯函数（只碰文件系统，不碰 HTTP、不碰数据库），上面是
// handler 外壳。这样路径边界可以脱离数据库直接单测 —— 见 project_files_test.go。

import (
	"bufio"
	"context"
	"errors"
	"io"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

const (
	// maxProjectListEntries 是单层目录一次返回的条目上限。
	//
	// 只读一层（不递归）已经避开了最大的那个坑，但 `node_modules` 单层就有上万个
	// 条目。截断必须**说出来**（响应带 truncated），否则前端会把"只看到前 N 个"
	// 误认成"这里只有 N 个"。
	maxProjectListEntries = 2000
	// dirBatchEntries 是列目录时每次从内核取的条目数。
	//
	// 为什么要分批：`maxProjectListEntries` 只截断**输出**，而 `os.ReadDir` 会先
	// 把整个目录读进内存。打开 `node_modules` 或带 hidden 打开 `.git` 时，为了
	// 返回 2000 条要先分配几万个 DirEntry —— 这个端点点开工作台就会调。
	// 256 是个折中：足够摊薄系统调用，又让峰值内存与目录大小脱钩。
	dirBatchEntries = 256
	// maxProjectFileBytes 曾是"文本预览一次装得下的上限"，超过就整个拒绝。
	// 现在读取走按行窗口（见 readProjectTextWindow），这个常量只保留给
	// `respondFileError` 的错误载荷（`limit` 字段）—— 前端据此说明"为什么被拒"。
	maxProjectFileBytes = 2 << 20
	// maxProjectRawBytes 是原始字节（图片 / PDF）上限。比文本宽：它们不参与模型
	// 上下文，只是给人看的。
	maxProjectRawBytes = 32 << 20
	// maxProjectWindowLines / maxProjectWindowBytes 是**超大文本文件**的分屏预算。
	//
	// 为什么需要另开一条路径：`maxProjectFileBytes` 是"一次响应装得下"的上限，超了
	// 就直接 413 —— 对"打开一个 5MB 的日志"这个动作是死路，用户连第一行都看不到。
	// 而把上限抬高到几十 MB 也不是办法：那会把整份内容塞进一个 JSON 响应和 DOM。
	// 折中是只给一屏（2000 行 ≈ 前端 PREVIEW_LINE_LIMIT），并明确告知"还有更多"。
	//
	// 字节上限与行数上限**必须同时有**：只限行数时，一行 minified 的 5MB JS 照样
	// 会一次灌满；只限字节时，一行一个字符的空文件也要读几百万行才停。
	maxProjectWindowLines = 2000
	maxProjectWindowBytes = 1 << 20
	// binarySniffBytes 是判定"是不是文本"时读的头部长度。
	//
	// 判据是 NUL 字节而不是扩展名：扩展名会骗人（`.log` 里可以是二进制），
	// 而文本文件里不会出现 NUL。这与 git 的启发式一致。
	binarySniffBytes = 8000
	// maxSearchEntries 是搜索时最多访问的条目数（预算）。
	//
	// 搜索必须递归，而递归在大仓库上可能要走几十万条目。预算是硬停止线：用完就
	// 返回已有结果并标记 truncated —— 用户宁可看到"部分结果"也不要等十秒。
	maxSearchEntries = 20000
	// maxSearchDepth 是搜索深度上限。
	maxSearchDepth = 12
	// maxSearchResults 是搜索返回条数上限。与 Reasonix 的 ref 搜索同量级。
	maxSearchResults = 50
)

// searchSkipDirs 是搜索时整目录跳过的名字。
//
// 判据是"里面不可能有用户想 @ 的源文件，而且条目数极多"。注意这里**不**跳过所有
// 点开头目录 —— `.github`、`.claude` 里的文件用户确实会引用；只跳过下面这些
// 确定无用的。逐条列白名单而不是规则匹配：规则匹配容易在某天误伤用户的目录。
var searchSkipDirs = map[string]bool{
	".git":          true,
	".hg":           true,
	".svn":          true,
	"node_modules":  true,
	".venv":         true,
	"venv":          true,
	"__pycache__":   true,
	".mypy_cache":   true,
	".pytest_cache": true,
	"dist":          true,
	"build":         true,
	".next":         true,
	".nuxt":         true,
	"target":        true,
	".cache":        true,
	".idea":         true,
}

// ── 失败的身份 ──
//
// 用 sentinel 而不是错误句子：handler 要按**类型**决定状态码与稳定错误码，
// 而文案会随界面语言变。判据读结构，不读措辞。

var (
	errPathNotRelative   = errors.New("path must be relative to the project root")
	errPathEscapes       = errors.New("path escapes the project root")
	errPathEscapesLink   = errors.New("path escapes the project root by a symbolic link")
	errRootUnavailable   = errors.New("project root is no longer available")
	errNotADirectory     = errors.New("not a directory")
	errIsADirectory      = errors.New("path is a directory")
	errFileNotFound      = errors.New("file not found")
	errFileTooLarge      = errors.New("file is too large to preview")
	errFileTooLargeRaw   = errors.New("file is too large to open")
	errPathCannotResolve = errors.New("path cannot be resolved")
	// errNotRegularFile：命名管道、套接字、设备节点。它们不是"内容太大"也不是
	// "找不到"，而是"打开这个动作本身有副作用"——见 openProjectFile 的说明。
	errNotRegularFile = errors.New("path is not a regular file")
)

// ProjectFileEntry 是目录列表里的一项。
type ProjectFileEntry struct {
	Name string `json:"name"`
	// Path 是相对项目根的 POSIX 路径 —— 与 `@` 引用、URL query 用的同一套写法。
	// 统一用 `/` 而不是本平台分隔符：这个值会出现在聊天记录与分享链接里，
	// `src\main.ts` 在别的平台上没有意义。
	Path string `json:"path"`
	Type string `json:"type"` // "file" | "dir"
	// Size 只对文件有意义。目录不给：拿它要 stat 每个子项，列表本身不该有这代价。
	Size int64 `json:"size,omitempty"`
}

// ProjectListing 是一层目录的读取结果。
type ProjectListing struct {
	// Path 是相对项目根的 POSIX 路径（根目录为空串）。
	Path    string             `json:"path"`
	Entries []ProjectFileEntry `json:"entries"`
	// Truncated 表示条目数触到上限、列表被截断。
	Truncated bool `json:"truncated"`
}

// ProjectFile 是一个文件的文本读取结果。
type ProjectFile struct {
	Path string `json:"path"`
	// Binary 为真时 Content 一定为空串：无法当文本读的文件不是错误，只是不能这样读。
	Binary  bool   `json:"binary"`
	Size    int64  `json:"size"`
	Content string `json:"content,omitempty"`
	// TotalLines 只对文本有意义（末尾无换行时仍算一行）。
	TotalLines int `json:"total_lines,omitempty"`
	// Offset 是本次内容窗口的起始行（0-based）。只在按行窗口读取时出现，
	// 前端据此把行号接上，而不是让每段窗口都从 1 开始数。
	Offset int `json:"offset,omitempty"`
	// Truncated 为真表示窗口之外还有内容。界面**必须**说出来并提供继续加载，
	// 否则用户会以为文件只有这么长 —— 并据此判断"这个函数不存在"。
	Truncated bool `json:"truncated,omitempty"`
}

// ── 纯函数：路径边界 ──

// foldPath 把路径归一成可比较的形式。
//
// Windows 上路径大小写不敏感（NTFS 默认），`C:\Work\proj` 与 `c:\work\proj` 指的是
// 同一个目录。前缀比较在这里会给出两个方向的错：把合法的判成逃逸，或把
// `C:\Work\proj-evil` 判成在 `C:\Work\proj` 之内（少了分隔符那一段）。先按平台折叠，
// 再交给 filepath.Rel 做逐段比较，两个问题一起消掉。
func foldPath(p string) string {
	if runtime.GOOS == "windows" {
		return strings.ToLower(p)
	}
	return p
}

// pathInsideRoot 判断 target 是否落在 root 之内（含 root 自身）。
func pathInsideRoot(root, target string) bool {
	rel, err := filepath.Rel(foldPath(root), foldPath(target))
	if err != nil {
		return false
	}
	if rel == "." {
		return true
	}
	return !filepath.IsAbs(rel) && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// resolveProjectPath 把相对路径解析成项目内的绝对路径。
//
// 返回 (绝对路径, 错误)。相对路径为空表示项目根自身。
//
// **两道关**：先按词法回答"这个字符串能不能走出去"（`..`、绝对路径、驱动器相对
// 路径），再把真实路径（EvalSymlinks 之后）比一次 —— 项目里放一个指向外部的符号
// 链接是完全正常的事（比如 `dist -> ../shared/dist`），只有物理比较能发现它通向哪里。
func resolveProjectPath(root, rel string) (string, error) {
	if strings.ContainsRune(rel, 0) {
		return "", errPathEscapes
	}
	if rel == "" || rel == "." {
		return filepath.Clean(root), nil
	}
	// 前端传来的永远是 POSIX 写法；转成本平台写法后，Windows 上 `src\main.ts`
	// 与 `src/main.ts` 才会指向同一个文件。
	slashed := filepath.FromSlash(rel)
	// 绝对路径必须单独拦：Go 的 `filepath.Join(root, abs)` **不会**丢弃 root
	// （那是 Python `os.path.join` 的行为），它会把两者拼起来 —— 结果虽然仍在
	// root 内，但用户写绝对路径的意图是"读这个绝对位置"，静默改写它比拒绝更糟。
	if filepath.IsAbs(slashed) || filepath.VolumeName(slashed) != "" {
		return "", errPathNotRelative
	}
	// 根相对路径（Windows 上的 `\etc`、POSIX 上的 `/etc`）。POSIX 已由 IsAbs 拦住，
	// Windows 上 IsAbs 对 `\etc` 返回 **false** —— 它相对的是当前驱动器根，不是
	// 项目根。漏掉这一条，`\` 开头的写法和普通相对路径就没有区别了。
	if strings.HasPrefix(slashed, string(filepath.Separator)) {
		return "", errPathNotRelative
	}
	joined := filepath.Clean(filepath.Join(root, slashed))
	if !pathInsideRoot(root, joined) {
		return "", errPathEscapes
	}

	realRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		return "", errRootUnavailable
	}
	realTarget, err := filepath.EvalSymlinks(joined)
	if err != nil {
		// 文件不存在是调用方要区分的事（读不到 vs 越界），这里如实透出。
		if errors.Is(err, os.ErrNotExist) {
			return joined, nil
		}
		return "", errPathCannotResolve
	}
	if !pathInsideRoot(realRoot, realTarget) {
		return "", errPathEscapesLink
	}
	return joined, nil
}

// projectRelPath 把绝对路径转回给前端的相对 POSIX 路径。
func projectRelPath(root, abs string) string {
	rel, err := filepath.Rel(root, abs)
	if err != nil {
		return filepath.ToSlash(abs)
	}
	if rel == "." {
		return ""
	}
	return filepath.ToSlash(rel)
}

// ── 纯函数：列目录 / 读文件 ──

// listProjectDir 列出 root 内 rel 这一层目录。
//
// 不递归：一次只读一层，前端按需展开。递归在大仓库上是秒级开销，而用户可能只是
// 想看看根目录里有没有那个文件。
func listProjectDir(root, rel string, showHidden bool) (*ProjectListing, error) {
	abs, err := resolveProjectPath(root, rel)
	if err != nil {
		return nil, err
	}
	info, statErr := os.Stat(abs)
	if statErr != nil {
		if errors.Is(statErr, os.ErrNotExist) {
			return nil, errFileNotFound
		}
		return nil, statErr
	}
	if !info.IsDir() {
		return nil, errNotADirectory
	}

	// 分批读取，而不是 os.ReadDir 一次性读完。
	//
	// 差别只在**峰值内存**：`os.ReadDir` 会为整个目录分配 DirEntry 切片，而这里
	// 的 2000 条上限只截断输出。带 hidden 点开 `.git`（几万个对象）或 `node_modules`
	// 时，原实现在返回 2000 条之前要先分配全部条目。
	dir, openErr := os.Open(abs)
	if openErr != nil {
		return nil, openErr
	}
	defer dir.Close()

	listing := &ProjectListing{
		Path:    projectRelPath(root, abs),
		Entries: make([]ProjectFileEntry, 0, min(maxProjectListEntries, dirBatchEntries)),
	}
	// 用带标签的 break 跳出两层循环：外层是"取下一批"，内层是"处理这一批"。
	// truncated 只在**真的要输出第 N+1 条**时才置位 —— 目录恰好 2000 条可见项
	// 不算截断，这也是原实现的语义。
collect:
	for {
		batch, readErr := dir.ReadDir(dirBatchEntries)
		for _, e := range batch {
			name := e.Name()
			// 默认不列点开头的条目：`.git` 里有几万个对象，列出来对"找我要的文件"
			// 没有帮助。但必须是**可选**的 —— `.github`/`.env` 用户确实会要看。
			if !showHidden && strings.HasPrefix(name, ".") {
				continue
			}
			if len(listing.Entries) >= maxProjectListEntries {
				listing.Truncated = true
				break collect
			}
			full := filepath.Join(abs, name)
			relPath := projectRelPath(root, full)

			// 符号链接：按目标类型报告，让用户看到的是"点开它会怎样"，而不是链接
			// 这个实现细节。断开的链接直接跳过 —— 列出来也点不开。
			//
			// 通向项目外的链接同样跳过，判据与打开时**同一套**（resolveProjectPath）：
			// 它在打开时必定拒绝，列出来就只剩"看得见、点不开"，还顺带把外部目录的
			// 文件名泄进了项目列表。这里复用而不是另写一份前缀比较 —— 两处判据一旦
			// 分开写，迟早会在某个边界（大小写、8.3 短名、UNC）上分叉。
			if e.Type()&os.ModeSymlink != 0 {
				if _, linkErr := resolveProjectPath(root, relPath); linkErr != nil {
					continue
				}
				target, statErr := os.Stat(full)
				if statErr != nil {
					continue
				}
				if target.IsDir() {
					listing.Entries = append(listing.Entries, ProjectFileEntry{Name: name, Path: relPath, Type: "dir"})
				} else {
					listing.Entries = append(listing.Entries, ProjectFileEntry{Name: name, Path: relPath, Type: "file", Size: target.Size()})
				}
				continue
			}
			if e.IsDir() {
				listing.Entries = append(listing.Entries, ProjectFileEntry{Name: name, Path: relPath, Type: "dir"})
				continue
			}
			var size int64
			if fi, statErr := e.Info(); statErr == nil {
				size = fi.Size()
			}
			listing.Entries = append(listing.Entries, ProjectFileEntry{Name: name, Path: relPath, Type: "file", Size: size})
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			return nil, readErr
		}
	}

	// 目录优先，然后按名字（大小写不敏感 —— 与资源管理器一致，Windows 用户不会
	// 预期 `Zebra` 排在 `apple` 前面）。
	sort.SliceStable(listing.Entries, func(i, j int) bool {
		a, b := listing.Entries[i], listing.Entries[j]
		if a.Type != b.Type {
			return a.Type == "dir"
		}
		la, lb := strings.ToLower(a.Name), strings.ToLower(b.Name)
		if la != lb {
			return la < lb
		}
		return a.Name < b.Name
	})
	return listing, nil
}

// searchProjectFiles 在项目内按名字搜索文件（`@` 引用用的就是它）。
//
// **为什么必须能跨目录搜**：`@` 的语义是"我知道有个文件叫这个名字，但不想手打路径"。
// 如果只能列当前目录，用户得先点进 3 层目录才能找到它 —— 那不如直接粘贴路径。
//
// 匹配规则：对**相对路径**做大小写不敏感的子串匹配，并给"文件名命中"更高的排序权重。
// 只给文件名匹配是不够的：用户常记得 `api/v1` 这段目录名，而不是文件名。
//
// 预算是硬停止线（maxSearchEntries）：递归在大仓库上可能要走几十万条目，宁可返回
// 部分结果并标记 truncated，也不要让敲 `@` 卡住两秒。
//
// `ctx` 用于**提前退出**：`@` 输入框每敲一个字符就会重新搜一次，前端会主动取消
// 上一发（见 ChatInput 的 fileSearchAbort）。不在这里检查就没有意义 —— 请求被取消
// 了，服务端仍会把整棵树走完，白烧 CPU 与磁盘。取消时返回已完成的部分结果、
// 不报错：那是调用方主动放弃，不是失败。
func searchProjectFiles(ctx context.Context, root, query string, limit int) ([]ProjectFileEntry, bool, error) {
	q := strings.ToLower(strings.TrimSpace(query))
	if limit <= 0 || limit > maxSearchResults {
		limit = maxSearchResults
	}
	if realRoot, err := filepath.EvalSymlinks(root); err == nil {
		root = realRoot
	} else {
		return nil, false, errRootUnavailable
	}
	// 空查询列出根目录下的文件：让用户先看到一个起点，而不是一片空白。
	if q == "" {
		listing, err := listProjectDir(root, "", false)
		if err != nil {
			return nil, false, err
		}
		out := make([]ProjectFileEntry, 0, len(listing.Entries))
		for _, e := range listing.Entries {
			if e.Type != "file" {
				continue
			}
			if len(out) >= limit {
				return out, true, nil
			}
			out = append(out, e)
		}
		return out, false, nil
	}

	type hit struct {
		entry ProjectFileEntry
		// score 越小越靠前：0 = 文件名前缀，1 = 文件名包含，2 = 仅路径包含。
		score int
	}
	var hits []hit
	visited := 0
	truncated := false

	// 手写显式栈而不是递归：深度上限与预算都要在**每一层**检查，递归写法里
	// 这两件事容易散落在多个 return 分支上。
	type frame struct {
		rel   string
		depth int
	}
	// 目录遍历走 os.Root，而不是对每个目录调一次 resolveProjectPath。
	//
	// 后者每次做两次 EvalSymlinks —— 那是逐路径段的系统调用，而 `@` 搜索是
	// "一敲一个字"的高频路径，走完上千个目录就是几千次多余 stat。
	//
	// 换成 os.Root 不是单纯图快：它把"不许越界"交给内核在打开时判定，比
	// "先解析、再比较前缀"更强 —— 解析与真正打开之间本来有个时间窗。
	// 也就是说这次改动同时更省、更严。
	//
	// 前置条件（两条都必须成立）：
	//   1. root 在函数入口已 EvalSymlinks 成真实路径；
	//   2. 下面的循环**不进入任何符号链接目录**（见 `e.Type()&os.ModeSymlink`）。
	// 于是栈里的 rel 只由真实目录名拼成。
	rootFS, rootErr := os.OpenRoot(root)
	if rootErr != nil {
		return nil, false, errRootUnavailable
	}
	defer rootFS.Close()

	stack := []frame{{rel: "", depth: 0}}
	for len(stack) > 0 {
		// 每个目录看一次是否已被取消。放在出栈处而不是在最内层循环：最内层的
		// 成本是 `ReadDir` 的返回值，而这里一次判断就覆盖了整个目录 subtree。
		if ctx.Err() != nil {
			break
		}
		f := stack[len(stack)-1]
		stack = stack[:len(stack)-1]

		dirRel := f.rel
		if dirRel == "" {
			dirRel = "."
		}
		dir, openErr := rootFS.Open(filepath.FromSlash(dirRel))
		if openErr != nil {
			// 单个目录打不开（被删、权限不足、越界的链接）不该让整次搜索失败。
			continue
		}
		dirents, readErr := dir.ReadDir(-1)
		dir.Close()
		if readErr != nil {
			continue
		}
		for _, e := range dirents {
			visited++
			if visited > maxSearchEntries {
				truncated = true
				break
			}
			name := e.Name()
			childRel := name
			if f.rel != "" {
				childRel = f.rel + "/" + name
			}

			if e.IsDir() || e.Type()&os.ModeSymlink != 0 {
				// 符号链接一律不进入：可能成环，也可能指向项目外。不进入是
				// os.Root 能安全遍历的前提之一 —— 栈里的路径只由真实目录名拼成。
				if e.Type()&os.ModeSymlink != 0 {
					continue
				}
				if searchSkipDirs[name] {
					continue
				}
				if f.depth+1 > maxSearchDepth {
					continue
				}
				stack = append(stack, frame{rel: childRel, depth: f.depth + 1})
				continue
			}

			lowerName := strings.ToLower(name)
			lowerRel := strings.ToLower(childRel)
			var score int
			switch {
			case strings.HasPrefix(lowerName, q):
				score = 0
			case strings.Contains(lowerName, q):
				score = 1
			case strings.Contains(lowerRel, q):
				score = 2
			default:
				continue
			}
			var size int64
			if fi, err := e.Info(); err == nil {
				size = fi.Size()
			}
			hits = append(hits, hit{
				entry: ProjectFileEntry{Name: name, Path: childRel, Type: "file", Size: size},
				score: score,
			})
		}
		if truncated {
			break
		}
	}

	// 先按权重，再按路径长度（越短越可能是用户想要的那个），最后按字典序。
	// 三者一起定序，结果才是稳定的 —— 只按权重排会让同一批命中每次顺序不同。
	sort.SliceStable(hits, func(i, j int) bool {
		if hits[i].score != hits[j].score {
			return hits[i].score < hits[j].score
		}
		if len(hits[i].entry.Path) != len(hits[j].entry.Path) {
			return len(hits[i].entry.Path) < len(hits[j].entry.Path)
		}
		return hits[i].entry.Path < hits[j].entry.Path
	})

	out := make([]ProjectFileEntry, 0, limit)
	for _, h := range hits {
		if len(out) >= limit {
			truncated = true
			break
		}
		out = append(out, h.entry)
	}
	return out, truncated, nil
}

// openProjectFile 打开 root 内 rel 指定的普通文件，并校验大小上限。
//
// 调用方负责 Close。把"打开 + 限额"放在一起，是为了让两个读端点（文本 / 原始字节）
// 不会各自漏掉其中一步。
func openProjectFile(root, rel string, limit int64, tooLarge error) (*os.File, os.FileInfo, error) {
	abs, err := resolveProjectPath(root, rel)
	if err != nil {
		return nil, nil, err
	}
	info, statErr := os.Stat(abs)
	if statErr != nil {
		if errors.Is(statErr, os.ErrNotExist) {
			return nil, nil, errFileNotFound
		}
		return nil, nil, statErr
	}
	if info.IsDir() {
		return nil, nil, errIsADirectory
	}
	// 命名管道、套接字、设备节点：**必须在 open 之前拦住**。
	//
	// 在 Unix 上 `open(2)` 一个 FIFO 会阻塞到有写端打开它 —— 不是返回错误，
	// 是永久挂住。这个请求的 goroutine 就再也不返回，连超时都无从触发，因为
	// 我们根本没走到可以设 deadline 的地方。而 `@某个 fifo` 只需要用户在消息里
	// 打一个路径，触发成本近乎为零。
	//
	// `stat` 落在 `open` 之前正是关键：它对 FIFO 立即返回（不打开、不阻塞），
	// 所以判据必须放在这里，而不能等拿到 *os.File 之后再 f.Stat()。
	if !info.Mode().IsRegular() {
		return nil, nil, errNotRegularFile
	}
	if info.Size() > limit {
		return nil, nil, tooLarge
	}
	f, openErr := openRefFile(root, rel)
	if openErr != nil {
		return nil, nil, openErr
	}
	return f, info, nil
}

// readProjectText 读取文本文件内容；二进制文件返回 Binary=true 且内容为空。
//
// 二进制**不是错误**：报成失败会让前端只能显示红字，而这个文件本来好端端的 ——
// 只是没法当文本读。
func readProjectText(root, rel string) (*ProjectFile, error) {
	return readProjectTextWindow(root, rel, 0, 0)
}

// readProjectTextWindow 从第 offset 行（0-based）开始读一屏，最多 limit 行。
//
// 为什么另开窗口路径而不是把 `maxProjectFileBytes` 调大：调大只是把"一次拒绝"
// 换成"一次卡死" —— 整份内容仍然要进一个 JSON 响应和一棵 DOM 树。窗口把动作
// 变成可执行的：先看一屏，需要再按 offset 接着读。
func readProjectTextWindow(root, rel string, offset, limit int) (*ProjectFile, error) {
	if offset < 0 {
		offset = 0
	}
	if limit <= 0 {
		limit = maxProjectWindowLines
	}
	// 硬上限仍然是 maxProjectRawBytes：再大的文件连"翻页"都没意义，而且逐行
	// 扫几十 MB 也只会拖住这个请求。
	f, info, err := openProjectFile(root, rel, maxProjectRawBytes, errFileTooLargeRaw)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	out := &ProjectFile{Path: projectRelPath(root, rel), Size: info.Size()}

	// 先嗅探头部再读全文：不必为一张图片构造出 2MB 的 string。
	head := make([]byte, binarySniffBytes)
	n, _ := io.ReadFull(f, head)
	head = head[:n]
	if looksBinary(head) {
		out.Binary = true
		return out, nil
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return nil, err
	}

	// 单遍顺序扫描：一边收集窗口，一边把总行数数完。
	//
	// 为什么不先数行数再 Seek 定位：那要扫两遍，而磁盘 IO 是这里最贵的一环。
	// 也不按字节偏移 Seek —— 偏移会落在行中间，还得往回找换行，边界情况更多。
	//
	// 读取本身也要封顶，不能只信 stat：`openProjectFile` 的大小检查只是**一个
	// 时刻的快照**，stat 与 read 之间文件可能被追加（最典型的是正在写的日志）。
	reader := bufio.NewReaderSize(f, 64<<10)
	var window strings.Builder
	lineNo := 0    // 已扫过的行数，等于"下一行的 0-based 下标"
	collected := 0 // 窗口内已收集的行数
	windowBytes := 0
	invalidUTF8 := false

	for {
		line, readErr := reader.ReadString('\n')
		if line != "" {
			if !utf8.ValidString(line) {
				invalidUTF8 = true
			}
			// 两个上限都要守：只限行数时，一行 minified 的几 MB 照样一次灌满；
			// 只限字节时，一行一个字符的文件要读几百万行才停。
			// `collected == 0` 是刻意的兜底：单行就超预算时至少给出一行，
			// 否则用户拿到的是"有内容但窗口是空的"。
			if lineNo >= offset && collected < limit &&
				(collected == 0 || windowBytes+len(line) <= maxProjectWindowBytes) {
				window.WriteString(line)
				windowBytes += len(line)
				collected++
			}
			lineNo++
		}
		if readErr != nil {
			if errors.Is(readErr, io.EOF) {
				break
			}
			return nil, readErr
		}
	}

	// 非法 UTF-8 也按二进制处理：硬转成文本只会得到一串替换字符，用户看不出原件
	// 是什么编码。这时"用系统程序打开"才是对的动作。
	if invalidUTF8 {
		out.Binary = true
		return out, nil
	}

	// 行数 = 换行符个数，末尾**没有**换行时再补一行。
	// 逐行读取天然满足这条：`line1\nline2\n` 读到 2 行，而不是 3 行。
	out.TotalLines = lineNo
	out.Offset = offset
	out.Content = window.String()
	// 窗口之外还有行，就必须说出来 —— 只给一屏却不告知，等于让用户以为
	// 文件只有这么长，并据此判断"这个函数不存在"。
	out.Truncated = (lineNo - offset) > collected
	return out, nil
}

// looksBinary 判断文件头部像不像二进制。
//
// 判据只看 NUL：UTF-8 / GBK / Latin-1 的文本都不含 0x00，而几乎所有二进制格式都会
// 在头部出现它。这不是完美判据（一个纯 ASCII 的 .class 文件可能混过去），但它的
// **误判方向是安全的**：把二进制当文本读最多显示乱码，反过来把源码当二进制拒就会
// 让人读不到自己的文件。
func looksBinary(head []byte) bool {
	for _, b := range head {
		if b == 0 {
			return true
		}
	}
	return false
}

// ── handler 外壳 ──

// requireProject 处理三个文件端点共同的鉴权与取根。
//
// ok 为 false 时响应已经写出，调用方直接 return。把这段重复三次的流程收在一处，
// 是为了让"谁能读这个项目"只有一个答案 —— 分散后早晚有一个分支漏掉归属校验。
func (h *SessionMapHandler) requireProject(w http.ResponseWriter, r *http.Request) (root string, ok bool) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return "", false
	}
	wsID := strings.TrimSpace(r.PathValue("id"))
	if wsID == "" {
		BadRequest(w, "project id is required")
		return "", false
	}
	if !smOwnsWorkspace(r, wsID, claims) {
		JSONWithCode(w, http.StatusNotFound, CodeNotFound, nil, "project not found")
		return "", false
	}
	root, why := projectRootOf(r, wsID, claims)
	if why != nil {
		code := CodeNotFound
		status := http.StatusNotFound
		if errors.Is(why, errRootUnavailable) {
			code, status = CodeProjectRootUnavailable, http.StatusConflict
		}
		JSONWithCode(w, status, code, nil, why.Error())
		return "", false
	}
	return root, true
}

// requireProject 的取值来源：项目根此刻是否仍然可用。
//
// 项目创建时校验过路径，但目录可能之后被删、改名、或换了挂载点。这种情况必须明确
// 报错：返回空列表会让用户以为"项目是空的"，然后去找 agent 为什么看不到文件。
func projectRootOf(r *http.Request, wsID string, claims *auth.Claims) (string, error) {
	if db.GlobalDBManager == nil {
		return "", errors.New("database unavailable")
	}
	row, err := db.GlobalDBManager.FetchOne(r.Context(),
		// **必须写 AS root_path**：FetchOne 用**列名**建 map，而 `COALESCE(x,'')`
		// 不带别名时 Postgres 给的列名就是 `coalesce`。少了别名，下面
		// `row["root_path"]` 永远取不到值 → 每个项目都被判成"没有绑定文件夹" →
		// 工作台一直显示「无法加载项目文件列表」。
		`SELECT COALESCE(root_path,'') AS root_path FROM session_map_workspaces
		  WHERE id = $1 AND tenant_id = $2 AND user_id = $3`,
		wsID, smTenant(claims), claims.UserID)
	if err != nil || row == nil {
		return "", errors.New("project not found")
	}
	root := stringOf(row["root_path"])
	if root == "" {
		// 画布不是项目：它没有绑定本地文件夹，自然没有文件可列。
		return "", errors.New("project has no root folder")
	}
	info, statErr := os.Stat(root)
	if statErr != nil || !info.IsDir() {
		return "", errRootUnavailable
	}
	return root, nil
}

// respondFileError 把纯函数的失败映射成状态码 + 稳定错误码。
//
// 映射表就是"前端能做什么"的清单：路径越界与 404 的区别在于**用户该改请求还是改
// 环境**，文案因此不能合并成一句"失败"。
func respondFileError(w http.ResponseWriter, err error, rel string) {
	switch {
	case errors.Is(err, errPathNotRelative), errors.Is(err, errPathEscapes), errors.Is(err, errPathEscapesLink):
		JSONWithCode(w, http.StatusBadRequest, CodeInvalidRequest,
			map[string]interface{}{"path": rel, "reason": err.Error()}, err.Error())
	case errors.Is(err, errPathCannotResolve):
		JSONWithCode(w, http.StatusBadRequest, CodeInvalidRequest,
			map[string]interface{}{"path": rel, "reason": err.Error()}, err.Error())
	case errors.Is(err, errNotADirectory), errors.Is(err, errIsADirectory):
		JSONWithCode(w, http.StatusBadRequest, CodeInvalidRequest,
			map[string]interface{}{"path": rel, "reason": err.Error()}, err.Error())
	// 非常规文件（FIFO / 套接字 / 设备节点）是**明确的客户端错误**，不是 500：
	// 路径合法、存在，但这种东西没有"内容"可读。回 400 + 稳定错误码，让前端
	// 能给出"这个文件不能预览"而不是"服务异常"。
	case errors.Is(err, errNotRegularFile):
		JSONWithCode(w, http.StatusBadRequest, CodeInvalidRequest,
			map[string]interface{}{"path": rel, "reason": err.Error()}, err.Error())
	case errors.Is(err, errFileNotFound):
		JSONWithCode(w, http.StatusNotFound, CodeNotFound, map[string]interface{}{"path": rel}, "file not found")
	case errors.Is(err, errFileTooLarge):
		JSONWithCode(w, http.StatusRequestEntityTooLarge, CodeFileTooLarge,
			map[string]interface{}{"limit": maxProjectFileBytes}, "file is too large to preview")
	case errors.Is(err, errFileTooLargeRaw):
		JSONWithCode(w, http.StatusRequestEntityTooLarge, CodeFileTooLarge,
			map[string]interface{}{"limit": maxProjectRawBytes}, "file is too large to open")
	case errors.Is(err, errRootUnavailable):
		JSONWithCode(w, http.StatusConflict, CodeProjectRootUnavailable, nil, err.Error())
	default:
		logAndRespond(w, err, http.StatusInternalServerError, "project file operation failed")
	}
}

// ListProjectFiles 列出项目内**一层**目录。
//
// query: path（相对路径，缺省为根）、hidden（"1"/"true" 时包含点开头的条目）
func (h *SessionMapHandler) ListProjectFiles(w http.ResponseWriter, r *http.Request) {
	root, ok := h.requireProject(w, r)
	if !ok {
		return
	}
	rel := r.URL.Query().Get("path")
	hidden := r.URL.Query().Get("hidden")
	showHidden := hidden == "1" || strings.EqualFold(hidden, "true")

	listing, err := listProjectDir(root, rel, showHidden)
	if err != nil {
		respondFileError(w, err, rel)
		return
	}
	OK(w, map[string]interface{}{
		// root 是给界面上那句"来自你授权的文件夹"用的：用户要能确认自己看的是哪个目录。
		"root":      root,
		"path":      listing.Path,
		"entries":   listing.Entries,
		"truncated": listing.Truncated,
	})
}

// SearchProjectFiles 在项目内按名字搜索文件，供 `@` 引用使用。
//
// query: q（关键词，可为空）、limit（条数上限，默认/封顶 maxSearchResults）
func (h *SessionMapHandler) SearchProjectFiles(w http.ResponseWriter, r *http.Request) {
	root, ok := h.requireProject(w, r)
	if !ok {
		return
	}
	q := r.URL.Query().Get("q")
	limit := 0
	if raw := r.URL.Query().Get("limit"); raw != "" {
		if n, err := strconv.Atoi(raw); err == nil {
			limit = n
		}
	}
	hits, truncated, err := searchProjectFiles(r.Context(), root, q, limit)
	if err != nil {
		respondFileError(w, err, "")
		return
	}
	OK(w, map[string]interface{}{
		"root":      root,
		"files":     hits,
		"truncated": truncated,
	})
}

// ReadProjectFile 读取项目内的一个文本文件。
//
// query: path（必填，相对路径）、offset（可选，起始行，0-based）、
// limit（可选，本屏最多多少行）。
//
// 为什么带 offset/limit：大文件不能靠"把上限抬高"来打开 —— 那只是把一次拒绝换成
// 一次卡死。窗口让用户先看到第一屏，再按 offset 接着读，代价与服务端内存都恒定。
func (h *SessionMapHandler) ReadProjectFile(w http.ResponseWriter, r *http.Request) {
	root, ok := h.requireProject(w, r)
	if !ok {
		return
	}
	rel := r.URL.Query().Get("path")
	if rel == "" {
		BadRequest(w, "path is required")
		return
	}
	offset := 0
	if raw := r.URL.Query().Get("offset"); raw != "" {
		// 非法数字按 0 处理而不是报错：offset 只影响"从哪开始看"，
		// 为一个笔误让整个预览失败没有道理。
		if n, convErr := strconv.Atoi(raw); convErr == nil && n > 0 {
			offset = n
		}
	}
	limit := 0
	if raw := r.URL.Query().Get("limit"); raw != "" {
		if n, convErr := strconv.Atoi(raw); convErr == nil && n > 0 {
			limit = n
		}
	}
	file, err := readProjectTextWindow(root, rel, offset, limit)
	if err != nil {
		respondFileError(w, err, rel)
		return
	}
	OK(w, file)
}

// RawProjectFile 按原始字节返回项目内的文件（图片、PDF、其它二进制）。
//
// 单独一个端点而不是给 ReadProjectFile 加 `?raw=1`：两者的响应体完全不同
// （JSON vs 字节流），合成一条路径会让 Content-Type 在两条分支里各写一遍，
// 而浏览器与 <img> 只认后者。
func (h *SessionMapHandler) RawProjectFile(w http.ResponseWriter, r *http.Request) {
	root, ok := h.requireProject(w, r)
	if !ok {
		return
	}
	rel := r.URL.Query().Get("path")
	if rel == "" {
		BadRequest(w, "path is required")
		return
	}
	f, info, err := openProjectFile(root, rel, maxProjectRawBytes, errFileTooLargeRaw)
	if err != nil {
		respondFileError(w, err, rel)
		return
	}
	defer f.Close()

	// 只按扩展名给 Content-Type，不嗅探内容：项目里的文件由用户和 agent 写入，
	// 内容嗅探会让一个扩展名为 .txt 的 HTML 文件被当成网页执行。
	ctype := mime.TypeByExtension(strings.ToLower(filepath.Ext(rel)))
	if ctype == "" {
		ctype = "application/octet-stream"
	}
	w.Header().Set("Content-Type", ctype)
	w.Header().Set("Content-Length", strconv.FormatInt(info.Size(), 10))
	// 不缓存：文件随时可能被 agent 改，缓存住会让预览停在旧内容上。
	w.Header().Set("Cache-Control", "no-store")
	// nosniff：浏览器不得把声明为 application/octet-stream 的文件当 HTML 渲染。
	w.Header().Set("X-Content-Type-Options", "nosniff")
	// 响应头已发出，这里出错只能记日志。
	_, _ = io.Copy(w, f)
}
