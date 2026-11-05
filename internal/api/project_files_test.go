package api

// project_files_test.go —— 项目文件层的边界用例。
//
// 这里测的全是**纯函数**（不碰 HTTP、不碰数据库），因为这些函数决定了"一个路径
// 能不能读到项目外的东西"。handler 外壳再正确，只要 `resolveProjectPath` 漏一条
// 判断，项目里放一个 `..\..\Windows` 就能把系统盘读出来。所以每条拒绝理由都要有
// 对应用例 —— 与 sessionmap_rootpath_test.go 的口径一致。

import (
	"context"
	"errors"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// mkProject 造一个临时项目根，并在其中写入给定的相对路径。

func writeProject(t *testing.T, root string, rel string, content string) string {
	t.Helper()
	full := filepath.Join(root, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
	return full
}

// ── 路径边界 ──

func TestResolveProjectPathRejectsEscapes(t *testing.T) {
	root := t.TempDir()

	// 每种写法的共同点：都指向项目之外。`..` 与绝对路径是两条不同的逃逸路线，
	// 前者靠 Clean 之后的前缀比较拦，后者靠 IsAbs/VolumeName 先拦（否则 Join 会
	// 静默丢掉 root）。
	outside := filepath.Dir(root)
	cases := []struct {
		name string
		rel  string
		want error
	}{
		{name: "上跳一级", rel: "../secret.txt", want: errPathEscapes},
		{name: "上跳多级", rel: "../../../../../../etc/passwd", want: errPathEscapes},
		{name: "绝对路径", rel: filepath.Join(outside, "x.txt"), want: errPathNotRelative},
		{name: "根绝对路径", rel: string(filepath.Separator) + "etc", want: errPathNotRelative},
		{name: "NUL 字节", rel: "ok\x00.txt", want: errPathEscapes},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := resolveProjectPath(root, tc.rel); !errors.Is(err, tc.want) {
				t.Fatalf("rel=%q 期望 %v，得到 %v", tc.rel, tc.want, err)
			}
		})
	}
}

func TestResolveProjectPathWindowsSpecific(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("仅 Windows：驱动器相对路径与大小写不敏感")
	}
	root := t.TempDir()

	// `C:foo` 是"驱动器相对路径"——不带分隔符，实际指向当前目录下的 foo。
	// IsAbs 对它返回 false（因为它不是绝对路径），但 Join 会把它拼成别的东西，
	// 所以必须用 VolumeName 单独拦。这类写法最容易漏。
	if _, err := resolveProjectPath(root, `C:Windows\System32`); !errors.Is(err, errPathNotRelative) {
		t.Fatalf("驱动器相对路径应被拒绝，得到 %v", err)
	}
	// 反斜杠写法（用户从资源管理器复制来的路径）应当被接受并指向同一文件，
	// 而不是被当成一个带反斜杠的文件名。
	writeProject(t, root, "src/main.ts", "x")
	got, err := resolveProjectPath(root, `src\main.ts`)
	if err != nil {
		t.Fatalf("反斜杠相对路径应被接受：%v", err)
	}
	if !strings.HasSuffix(got, filepath.Join("src", "main.ts")) {
		t.Fatalf("解析结果不对：%q", got)
	}
}

func TestResolveProjectPathAcceptsInside(t *testing.T) {
	root := t.TempDir()
	writeProject(t, root, "src/deep/nested/file.ts", "hello")

	cases := []string{
		"",                // 根自身
		".",               // 根自身
		"src",             // 目录
		"src/deep/nested", // 深层目录
		"src/deep/nested/file.ts",
		"src/./deep/../deep/nested/file.ts", // 中间的上跳只要没走出去就合法
	}
	for _, rel := range cases {
		t.Run(rel, func(t *testing.T) {
			if _, err := resolveProjectPath(root, rel); err != nil {
				t.Fatalf("rel=%q 应被接受：%v", rel, err)
			}
		})
	}
}

func TestResolveProjectPathRejectsSymlinkEscape(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows 创建符号链接需要开发者模式或管理员权限，CI 上不可靠")
	}
	outside := t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "secret.txt"), []byte("s"), 0o644); err != nil {
		t.Fatalf("write outside: %v", err)
	}
	root := t.TempDir()
	// 词法上 `link` 完全在 root 之内；只有 EvalSymlinks 之后才发现它通向外面。
	if err := os.Symlink(outside, filepath.Join(root, "link")); err != nil {
		t.Skipf("无法创建符号链接：%v", err)
	}
	if _, err := resolveProjectPath(root, "link/secret.txt"); !errors.Is(err, errPathEscapesLink) {
		t.Fatalf("符号链接逃逸应被拒绝，得到 %v", err)
	}
}

func TestPathInsideRootSiblingPrefix(t *testing.T) {
	// `proj-evil` 与 `proj` 是兄弟目录，字符串前缀看起来"在 proj 里"。
	// 用逐段比较（filepath.Rel）才能分开，纯 strings.HasPrefix 会放行。
	root := filepath.Join(t.TempDir(), "proj")
	sibling := root + "-evil"
	if pathInsideRoot(root, sibling) {
		t.Fatalf("%q 不应被判为在 %q 内", sibling, root)
	}
	if !pathInsideRoot(root, filepath.Join(root, "a", "b")) {
		t.Fatal("子路径应被判为在内")
	}
	if !pathInsideRoot(root, root) {
		t.Fatal("根自身应被判为在内")
	}
}

// ── 列目录 ──

func TestListProjectDirSortsDirsFirst(t *testing.T) {
	root := t.TempDir()
	writeProject(t, root, "zeta.txt", "z")
	writeProject(t, root, "Alpha.txt", "a")
	if err := os.MkdirAll(filepath.Join(root, "beta-dir"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	listing, err := listProjectDir(root, "", false)
	if err != nil {
		t.Fatalf("列目录失败：%v", err)
	}
	if len(listing.Entries) != 3 {
		t.Fatalf("期望 3 项，得到 %d：%+v", len(listing.Entries), listing.Entries)
	}
	// 目录优先，然后大小写不敏感按名字：Alpha, zeta。
	if listing.Entries[0].Name != "beta-dir" || listing.Entries[0].Type != "dir" {
		t.Fatalf("第一项应是目录 beta-dir，得到 %+v", listing.Entries[0])
	}
	if listing.Entries[1].Name != "Alpha.txt" {
		t.Fatalf("第二项应是 Alpha.txt（大小写不敏感排序），得到 %+v", listing.Entries[1])
	}
	// Path 必须是 POSIX 分隔符：它会进聊天记录与分享链接，反斜杠在别的平台没意义。
	for _, e := range listing.Entries {
		if strings.Contains(e.Path, `\`) {
			t.Fatalf("Path 应使用 / 分隔：%q", e.Path)
		}
	}
}

func TestListProjectDirLazyDepth(t *testing.T) {
	root := t.TempDir()
	writeProject(t, root, "src/nested/deep.ts", "x")

	// 列根目录只应看到 src 这一项，不应穿透到 nested/deep.ts。
	// 这是"懒加载"的核心断言：一次只读一层。
	listing, err := listProjectDir(root, "", false)
	if err != nil {
		t.Fatalf("列目录失败：%v", err)
	}
	if len(listing.Entries) != 1 || listing.Entries[0].Name != "src" || listing.Entries[0].Type != "dir" {
		t.Fatalf("根目录应只有 src 一个目录：%+v", listing.Entries)
	}
	if listing.Entries[0].Size != 0 {
		t.Fatal("目录不应带 size")
	}

	// 展开一层后才看到 nested。
	sub, err := listProjectDir(root, "src", false)
	if err != nil {
		t.Fatalf("列 src 失败：%v", err)
	}
	if len(sub.Entries) != 1 || sub.Entries[0].Name != "nested" {
		t.Fatalf("src 下应只有 nested：%+v", sub.Entries)
	}
	if sub.Path != "src" {
		t.Fatalf("Path 应为 src，得到 %q", sub.Path)
	}
}

func TestListProjectDirHiddenToggle(t *testing.T) {
	root := t.TempDir()
	writeProject(t, root, ".env", "SECRET=1")
	writeProject(t, root, "visible.txt", "v")
	if err := os.MkdirAll(filepath.Join(root, ".github"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	shown, err := listProjectDir(root, "", false)
	if err != nil {
		t.Fatalf("列目录失败：%v", err)
	}
	if len(shown.Entries) != 1 || shown.Entries[0].Name != "visible.txt" {
		t.Fatalf("默认不应列出点开头的条目：%+v", shown.Entries)
	}

	withHidden, err := listProjectDir(root, "", true)
	if err != nil {
		t.Fatalf("列目录失败：%v", err)
	}
	names := map[string]bool{}
	for _, e := range withHidden.Entries {
		names[e.Name] = true
	}
	// 隐藏的**文件**和隐藏的**目录**都要能出现：`.env` 是前者，`.github` 是后者。
	for _, want := range []string{".env", ".github", "visible.txt"} {
		if !names[want] {
			t.Fatalf("开启 hidden 后应包含 %q：%+v", want, withHidden.Entries)
		}
	}
}

func TestListProjectDirRejectsFile(t *testing.T) {
	root := t.TempDir()
	writeProject(t, root, "a.txt", "x")
	// 对一个文件调"列目录"应是明确的类型错误，而不是"空列表"——
	// 后者会让调用方以为这个文件是个空目录。
	if _, err := listProjectDir(root, "a.txt", false); !errors.Is(err, errNotADirectory) {
		t.Fatalf("期望 errNotADirectory，得到 %v", err)
	}
}

func TestListProjectDirMissing(t *testing.T) {
	root := t.TempDir()
	if _, err := listProjectDir(root, "nope", false); !errors.Is(err, errFileNotFound) {
		t.Fatalf("期望 errFileNotFound，得到 %v", err)
	}
}

// 指向项目外的符号链接不能出现在列表里。
//
// 现状是矛盾的：断开的链接被跳过（"列出来也点不开"），但通向项目外的链接照样
// 列成 dir/file，而 `resolveProjectPath` 在打开时一定拒绝它。用户看到条目、
// 点开却报"超出项目根" —— 界面承诺了一件不会发生的事，正是幽灵行为。
// 顺带这里有信息泄露：外部目录的文件名被列进了项目列表。
func TestListProjectDirSkipsSymlinkEscape(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	writeProject(t, outside, "secret.txt", "should not be listed")
	writeProject(t, root, "inside.txt", "x")

	if err := os.Symlink(outside, filepath.Join(root, "escape")); err != nil {
		t.Skipf("当前环境不支持创建符号链接: %v", err)
	}
	// 指向项目内的链接是正常用法，必须留着 —— 修复不能顺手把这一类也砍掉。
	if err := os.Symlink(filepath.Join(root, "inside.txt"), filepath.Join(root, "alias.txt")); err != nil {
		t.Skipf("当前环境不支持创建符号链接: %v", err)
	}

	listing, err := listProjectDir(root, "", false)
	if err != nil {
		t.Fatalf("列目录失败: %v", err)
	}

	var names []string
	for _, e := range listing.Entries {
		names = append(names, e.Name)
	}
	for _, name := range names {
		if name == "escape" {
			t.Fatalf("指向项目外的符号链接出现在列表里: %v", names)
		}
	}
	found := false
	for _, name := range names {
		if name == "alias.txt" {
			found = true
		}
	}
	if !found {
		t.Fatalf("指向项目内的符号链接不应被跳过: %v", names)
	}
}

// ── 读文件 ──

func TestReadProjectText(t *testing.T) {
	root := t.TempDir()
	writeProject(t, root, "a.txt", "line1\nline2\n")

	got, err := readProjectText(root, "a.txt")
	if err != nil {
		t.Fatalf("读取失败：%v", err)
	}
	if got.Binary {
		t.Fatal("文本文件不应被判定为二进制")
	}
	if got.Content != "line1\nline2\n" {
		t.Fatalf("内容不对：%q", got.Content)
	}
	// 末尾有换行时，"行数" 是换行数（`line1\nline2\n` 是两行，不是三行）。
	if got.TotalLines != 2 {
		t.Fatalf("行数应为 2，得到 %d", got.TotalLines)
	}
	if got.Path != "a.txt" {
		t.Fatalf("Path 应为 a.txt，得到 %q", got.Path)
	}
}

func TestReadProjectTextEmptyFile(t *testing.T) {
	root := t.TempDir()
	writeProject(t, root, "empty.txt", "")
	got, err := readProjectText(root, "empty.txt")
	if err != nil {
		t.Fatalf("读取失败：%v", err)
	}
	// 空文件是 0 行。算成 1 行会让界面显示一个不存在的行号。
	if got.TotalLines != 0 {
		t.Fatalf("空文件行数应为 0，得到 %d", got.TotalLines)
	}
}

func TestReadProjectTextBinary(t *testing.T) {
	root := t.TempDir()
	// 带 NUL 的字节流：不是错误，只是不能当文本读。
	writeProject(t, root, "blob.bin", "abc\x00def")

	got, err := readProjectText(root, "blob.bin")
	if err != nil {
		t.Fatalf("二进制文件不应报错：%v", err)
	}
	if !got.Binary {
		t.Fatal("含 NUL 的文件应被判为二进制")
	}
	if got.Content != "" {
		t.Fatalf("二进制文件不应带内容：%q", got.Content)
	}
}

func TestReadProjectTextInvalidUTF8(t *testing.T) {
	root := t.TempDir()
	// 无 NUL 但非法 UTF-8（一个 GBK 编码的中文）。判成二进制，让界面改用系统程序打开，
	// 而不是显示一串替换字符让人以为文件坏了。
	writeProject(t, root, "gbk.txt", "\xd6\xd0\xce\xc4")

	got, err := readProjectText(root, "gbk.txt")
	if err != nil {
		t.Fatalf("非法 UTF-8 不应报错：%v", err)
	}
	if !got.Binary {
		t.Fatal("非法 UTF-8 应被判为二进制")
	}
}

func TestReadProjectTextRejectsDirectory(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "dir"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if _, err := readProjectText(root, "dir"); !errors.Is(err, errIsADirectory) {
		t.Fatalf("期望 errIsADirectory，得到 %v", err)
	}
}

func TestReadProjectTextTooLarge(t *testing.T) {
	root := t.TempDir()
	// 造一个超过上限的文本文件。内容全是 'a'，不含 NUL，所以判定为文本 ——
	// 测试要确认**大小**这一关先于读取生效，而不是靠内容判定挡住。
	big := strings.Repeat("a", maxProjectFileBytes+1)
	writeProject(t, root, "big.txt", big)

	_, err := readProjectText(root, "big.txt")
	if !errors.Is(err, errFileTooLarge) {
		t.Fatalf("期望 errFileTooLarge，得到 %v", err)
	}
}

func TestReadProjectTextRejectsEscape(t *testing.T) {
	root := t.TempDir()
	// 读端点同样要过边界：列目录能拦，读文件也必须拦。
	if _, err := readProjectText(root, "../outside.txt"); !errors.Is(err, errPathEscapes) {
		t.Fatalf("期望 errPathEscapes，得到 %v", err)
	}
}

// ── 搜索（`@` 引用的数据源）──

func TestSearchProjectFilesMatchesAcrossDirs(t *testing.T) {
	root := t.TempDir()
	writeProject(t, root, "src/api/handler.ts", "x")
	writeProject(t, root, "src/views/Home.vue", "x")
	writeProject(t, root, "README.md", "x")

	// 跨目录命中：用户记得文件名，不想先点进 src/api。
	hits, truncated, err := searchProjectFiles(context.Background(), root, "handler", 0)
	if err != nil {
		t.Fatalf("搜索失败：%v", err)
	}
	if truncated {
		t.Fatal("小项目不该截断")
	}
	if len(hits) != 1 || hits[0].Path != "src/api/handler.ts" {
		t.Fatalf("期望命中 src/api/handler.ts，得到 %+v", hits)
	}
	if hits[0].Type != "file" {
		t.Fatalf("搜索结果只应有文件：%+v", hits[0])
	}
}

func TestSearchProjectFilesMatchesDirSegment(t *testing.T) {
	root := t.TempDir()
	writeProject(t, root, "src/api/v1/users.ts", "x")
	writeProject(t, root, "src/ui/button.ts", "x")

	// 用户也可能记得的是**目录名**（`api/v1`）而不是文件名。
	hits, _, err := searchProjectFiles(context.Background(), root, "api/v1", 0)
	if err != nil {
		t.Fatalf("搜索失败：%v", err)
	}
	if len(hits) != 1 || hits[0].Path != "src/api/v1/users.ts" {
		t.Fatalf("期望按路径片段命中，得到 %+v", hits)
	}
}

func TestSearchProjectFilesRanksNameOverPath(t *testing.T) {
	root := t.TempDir()
	// `util.ts` 在名字上命中；`util/other.ts` 只在路径上命中。
	writeProject(t, root, "deep/util/other.ts", "x")
	writeProject(t, root, "util.ts", "x")

	hits, _, err := searchProjectFiles(context.Background(), root, "util", 0)
	if err != nil {
		t.Fatalf("搜索失败：%v", err)
	}
	if len(hits) < 2 {
		t.Fatalf("应命中两条，得到 %+v", hits)
	}
	// 名字命中排在路径命中之前：用户敲 `util` 时多半就是要那个叫 util 的。
	if hits[0].Path != "util.ts" {
		t.Fatalf("名字命中应排第一，得到 %+v", hits)
	}
}

func TestSearchProjectFilesSkipsNoiseDirs(t *testing.T) {
	root := t.TempDir()
	writeProject(t, root, "node_modules/pkg/index.js", "x")
	writeProject(t, root, ".git/config", "x")
	writeProject(t, root, "src/index.js", "x")

	hits, _, err := searchProjectFiles(context.Background(), root, "index", 0)
	if err != nil {
		t.Fatalf("搜索失败：%v", err)
	}
	// 只保留 src 下那个：node_modules 与 .git 里的同名文件是噪声，
	// 命中它们会把用户真正要的文件挤到后面。
	if len(hits) != 1 || hits[0].Path != "src/index.js" {
		t.Fatalf("应只命中 src/index.js，得到 %+v", hits)
	}
}

func TestSearchProjectFilesRespectsLimit(t *testing.T) {
	root := t.TempDir()
	for i := 0; i < 10; i++ {
		writeProject(t, root, "src/file"+string(rune('a'+i))+".ts", "x")
	}
	hits, truncated, err := searchProjectFiles(context.Background(), root, "file", 3)
	if err != nil {
		t.Fatalf("搜索失败：%v", err)
	}
	if len(hits) != 3 {
		t.Fatalf("应被 limit 截到 3 条，得到 %d", len(hits))
	}
	// 截断必须**说出来**，否则前端会把"只看到 3 个"当成"只有 3 个"。
	if !truncated {
		t.Fatal("超出 limit 时应标记 truncated")
	}
}

func TestSearchProjectFilesEmptyQueryListsRootFiles(t *testing.T) {
	root := t.TempDir()
	writeProject(t, root, "a.ts", "x")
	if err := os.MkdirAll(filepath.Join(root, "adir"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}

	// 空查询给一个起点，而不是一片空白；目录不出现（`@` 引用的是文件）。
	hits, _, err := searchProjectFiles(context.Background(), root, "", 0)
	if err != nil {
		t.Fatalf("搜索失败：%v", err)
	}
	if len(hits) != 1 || hits[0].Path != "a.ts" {
		t.Fatalf("空查询应只列出根目录文件，得到 %+v", hits)
	}
}

func TestSearchProjectFilesStaysInsideRoot(t *testing.T) {
	root := t.TempDir()
	parent := filepath.Dir(root)
	// 父目录里放一个同名文件：搜索绝不能把它带出来。
	if err := os.WriteFile(filepath.Join(parent, "escape-me.ts"), []byte("x"), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}

	hits, _, err := searchProjectFiles(context.Background(), root, "escape-me", 0)
	if err != nil {
		t.Fatalf("搜索失败：%v", err)
	}
	if len(hits) != 0 {
		t.Fatalf("不应搜到项目外的文件：%+v", hits)
	}
}

// ── 二进制嗅探 ──

func TestLooksBinary(t *testing.T) {
	cases := []struct {
		name string
		in   []byte
		want bool
	}{
		{name: "纯文本", in: []byte("hello world"), want: false},
		{name: "中文 UTF-8", in: []byte("中文注释"), want: false},
		{name: "含 NUL", in: []byte("PK\x03\x04\x00"), want: true},
		{name: "空头部", in: nil, want: false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := looksBinary(tc.in); got != tc.want {
				t.Fatalf("looksBinary(%q) = %v，期望 %v", tc.in, got, tc.want)
			}
		})
	}
}

// ── 错误 → 响应映射 ──

func TestRespondFileErrorDistinguishesCauses(t *testing.T) {
	// 映射表就是"用户该做什么"的清单。越界是 400（改请求），不存在是 404
	// （改环境），根不可用是 409（去修项目路径）—— 三者不能合并成一个状态码，
	// 否则界面只能显示同一句"失败"。
	cases := []struct {
		err        error
		wantStatus int
		wantCode   string
	}{
		{err: errPathEscapes, wantStatus: 400, wantCode: CodeInvalidRequest},
		{err: errPathEscapesLink, wantStatus: 400, wantCode: CodeInvalidRequest},
		{err: errFileNotFound, wantStatus: 404, wantCode: CodeNotFound},
		{err: errRootUnavailable, wantStatus: 409, wantCode: CodeProjectRootUnavailable},
		{err: errFileTooLarge, wantStatus: 413, wantCode: CodeFileTooLarge},
		{err: errFileTooLargeRaw, wantStatus: 413, wantCode: CodeFileTooLarge},
	}
	for _, tc := range cases {
		t.Run(tc.err.Error(), func(t *testing.T) {
			rec := httptest.NewRecorder()
			respondFileError(rec, tc.err, "x")
			if rec.Code != tc.wantStatus {
				t.Fatalf("状态码 = %d，期望 %d", rec.Code, tc.wantStatus)
			}
			if !strings.Contains(rec.Body.String(), `"code":"`+tc.wantCode+`"`) {
				t.Fatalf("响应应带 code=%s，实际 body=%s", tc.wantCode, rec.Body.String())
			}
		})
	}
}
