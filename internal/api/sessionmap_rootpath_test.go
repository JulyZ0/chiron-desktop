package api

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// validateRootPath 是项目绑定的**唯一安全边界**。
//
// `PUT /workspaces/{id}` 已经把 root_path 锁死成"沿用库里的值"，`PATCH /projects/{id}`
// 是唯一能改路径的入口，而它跑的就是这个函数。也就是说：这里漏一条判断，
// 项目就能指向 `C:\Windows`，等 P3 把 root_path 接成执行 cwd，agent 就拿到了
// 对系统目录的可写视图。所以每条拒绝理由都要有对应用例。
func TestValidateRootPath(t *testing.T) {
	// 临时目录一定是存在的目录，用它作为"合法路径"的基准。
	valid := t.TempDir()

	cases := []struct {
		name    string
		input   string
		wantErr string // 空串 = 期望通过
	}{
		{name: "合法绝对路径通过", input: valid},

		// 空与超长
		{name: "空串拒绝", input: "", wantErr: "root_path is required"},
		{name: "纯空白拒绝", input: "   ", wantErr: "root_path is required"},
		{
			name:    "超长拒绝",
			input:   filepath.Join(valid, strings.Repeat("a", 1024)),
			wantErr: "root_path is too long",
		},

		// 相对路径
		{name: "相对路径拒绝", input: "some/relative/dir", wantErr: "root_path must be an absolute path"},
		{name: "单点相对路径拒绝", input: ".", wantErr: "root_path must be an absolute path"},

		// 存在性
		{
			name:    "不存在的路径拒绝",
			input:   filepath.Join(valid, "definitely-not-here-9f3a"),
			wantErr: "root_path does not exist or is not accessible",
		},

		// 必须是目录
		{
			name:    "指向文件拒绝",
			input:   writeTempFile(t, valid),
			wantErr: "root_path is not a directory",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, errMsg := validateRootPath(tc.input)
			if tc.wantErr == "" {
				if errMsg != "" {
					t.Fatalf("期望通过，却报错 %q", errMsg)
				}
				if got == "" {
					t.Fatal("通过时必须返回规范化后的路径")
				}
				return
			}
			if errMsg != tc.wantErr {
				t.Fatalf("错误信息不符：期望 %q，实际 %q", tc.wantErr, errMsg)
			}
			if got != "" {
				t.Fatalf("被拒绝时必须返回空路径，实际 %q", got)
			}
		})
	}
}

// TestValidateRootPathRejectsSystemDirs 单独覆盖系统目录黑名单。
//
// 与上一条分开是因为判据不同：上面几条看的是"路径能否当项目根"（存在/是目录），
// 这条看的是**安全策略**（即使是合法目录也不许当项目根）。
// 混在一起会让安全策略的失效被其它用例的通过掩盖。
func TestValidateRootPathRejectsSystemDirs(t *testing.T) {
	// 直接测黑名单判定，不依赖这些目录在本机是否真实存在 ——
	// CI 与环境差异不该让安全策略的测试失效。
	for _, p := range []string{
		`C:\Windows`,
		`c:\windows`,
		`C:\Windows\System32`,
		`C:\Program Files`,
		`C:\Program Files (x86)`,
		`C:\ProgramData\Microsoft`,
	} {
		t.Run(p, func(t *testing.T) {
			_, errMsg := validateRootPath(p)
			if errMsg == "" {
				t.Fatalf("%s 必须被拒绝，否则 agent 执行 cwd 会落在系统目录", p)
			}
		})
	}
}

// TestValidateRootPathRejectsDriveRoot 钉住"整个盘不能当项目"。
//
// 驱动器根是黑名单挡不住的：`C:\` 不在 `C:\Windows` 之下，前缀匹配不会命中。
// 它又恰好是最容易被误选的路径（文件夹对话框里双击盘符就进去了），
// 而一旦成为项目根，后续执行 cwd 就没有任何边界。
func TestValidateRootPathRejectsDriveRoot(t *testing.T) {
	for _, p := range []string{`C:\`, `D:\`, `c:\`} {
		t.Run(p, func(t *testing.T) {
			_, errMsg := validateRootPath(p)
			if errMsg != "root_path cannot be a drive root" {
				t.Fatalf("驱动器根必须被拒绝，实际 %q", errMsg)
			}
		})
	}
}

// TestValidateRootPathNormalizes 确认返回值是 Clean 过的路径。
//
// 后端存的是 `root_path` 原值，而它会被拿去与黑名单、与 cwd 做比较。
// 若不 Clean，`C:\work\proj\..\proj` 这类路径能绕过 `HasPrefix` 比较。
func TestValidateRootPathNormalizes(t *testing.T) {
	base := t.TempDir()
	// 用一个"绕一圈回到原地"的路径：Clean 后应与 base 相等。
	winding := filepath.Join(base, "sub", "..")
	got, errMsg := validateRootPath(winding)
	if errMsg != "" {
		t.Fatalf("Clean 后存在的路径应当通过，实际报错 %q", errMsg)
	}
	if got != filepath.Clean(winding) {
		t.Fatalf("返回值必须是 Clean 后的路径：期望 %q，实际 %q", filepath.Clean(winding), got)
	}
	if got != base {
		t.Fatalf("Clean 结果应等于基准目录 %q，实际 %q", base, got)
	}
}

// writeTempFile 在 dir 下建一个文件，返回其路径。
func writeTempFile(t *testing.T, dir string) string {
	t.Helper()
	p := filepath.Join(dir, "not-a-dir.txt")
	if err := os.WriteFile(p, []byte("x"), 0o600); err != nil {
		t.Fatalf("write temp file: %v", err)
	}
	return p
}
