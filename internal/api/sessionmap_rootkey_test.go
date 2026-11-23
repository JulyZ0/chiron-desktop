package api

// 项目身份键（root_key）的回归测试。
//
// 这些用例钉的是"判重对象是 locator，不是字符串"：
// 同一设备上的大小写/`..`/junction 写法必须归一；不同设备/不同 provider 不参与比较。
// 数据库唯一索引与 23505→409 的并发路径由集成环境覆盖，这里只测纯函数。

import (
	"strings"
	"testing"
)

func TestLexicalRootKeyWindowsShape(t *testing.T) {
	cases := []struct{ a, b string }{
		{`D:\Repo`, `d:\repo`},
		{`D:\Repo\`, `d:/repo`},
		{`C:\a\..\a`, `c:\a`},
		{`\\Server\Share\Proj`, `//server/share/proj`},
	}
	for _, tc := range cases {
		if got, want := lexicalRootKey(tc.a), lexicalRootKey(tc.b); got != want {
			t.Fatalf("lexicalRootKey(%q)=%q, lexicalRootKey(%q)=%q; want equal", tc.a, got, tc.b, want)
		}
	}
}

func TestLexicalRootKeyCleansPosix(t *testing.T) {
	if got, want := lexicalRootKey("/srv/app/../app/"), "/srv/app"; got != want {
		t.Fatalf("lexicalRootKey = %q, want %q", got, want)
	}
	if strings.Contains(lexicalRootKey("/srv/app"), `\`) {
		t.Fatalf("root_key 不应包含反斜杠: %q", lexicalRootKey("/srv/app"))
	}
}

func TestLexicalRootKeyPreservesUNC(t *testing.T) {
	unc := lexicalRootKey(`\\Server\Share\Proj`)
	if !strings.HasPrefix(unc, "//") {
		t.Fatalf("UNC 键必须以 // 开头，得到 %q", unc)
	}
	if unc == lexicalRootKey("/server/share/proj") {
		t.Fatal("UNC 不能与 POSIX 绝对路径撞成同一个键")
	}
}

func TestStripWindowsVerbatim(t *testing.T) {
	cases := map[string]string{
		`\\?\C:\Repo`:            `C:\Repo`,
		`\\.\C:\Repo`:            `C:\Repo`,
		`\\?\UNC\srv\share\repo`: `\\srv\share\repo`,
		`D:\Repo`:                `D:\Repo`,
	}
	for in, want := range cases {
		if got := stripWindowsVerbatim(in); got != want {
			t.Fatalf("stripWindowsVerbatim(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestServerRootKeyFallsBackWhenMissing(t *testing.T) {
	// 路径不存在时不能返回空：目录被移动/删除不应让判重失效。
	p := `D:\definitely\missing\project-xyz`
	if got := serverRootKey(p); got == "" {
		t.Fatalf("serverRootKey(%q) 为空", p)
	}
}

func TestProjectRootKeyOfLegacyRows(t *testing.T) {
	// 迁移前的老行：root_key 为空、provider 为空 → 按 server 现算
	legacy := projectRootKeyOf("", "", "", `D:\Work\App`)
	if legacy == "" {
		t.Fatal("历史行应能现算出 root_key")
	}
	// client 行缺 root_key 时不能由服务端猜（路径语义属于客户端）
	if got := projectRootKeyOf(smProviderClient, "dev-1", "", `C:\x`); got != "" {
		t.Fatalf("client 缺 root_key 时应返回空，得到 %q", got)
	}
	// 已有 root_key 原样返回
	if got := projectRootKeyOf(smProviderServer, "", "already", `D:\x`); got != "already" {
		t.Fatalf("已有 root_key 被改写: %q", got)
	}
}

func TestTruncateRootKey(t *testing.T) {
	long := strings.Repeat("a", smMaxRootKeyLen+10)
	if got := truncateRootKey(long); len(got) != smMaxRootKeyLen {
		t.Fatalf("truncateRootKey 长度 = %d, want %d", len(got), smMaxRootKeyLen)
	}
	if got := truncateRootKey("short"); got != "short" {
		t.Fatalf("短键不应被改动: %q", got)
	}
}

func TestRootKeyHashStable(t *testing.T) {
	key := lexicalRootKey(`D:\Repo`)
	h1, h2 := rootKeyHash(key), rootKeyHash(key)
	if h1 != h2 {
		t.Fatal("同一 root_key 的哈希必须稳定")
	}
	if len(h1) != 64 {
		t.Fatalf("sha256 hex 长度 = %d, want 64", len(h1))
	}
	if rootKeyHash(key) == rootKeyHash(lexicalRootKey(`D:\Other`)) {
		t.Fatal("不同路径不应得到相同哈希")
	}
	if rootKeyHash("") != "" {
		t.Fatal("空 key 的哈希必须是空串（唯一索引 WHERE 依赖它）")
	}
}
