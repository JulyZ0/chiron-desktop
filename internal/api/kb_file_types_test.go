package api

import (
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// kb_file_types_test.go —— 知识库类型白名单的**跨语言**契约。
//
// Go 侧的 kb_file_types.go 与 Python 侧的 app/knowledge/remote.py 各自维护一份
// 「哪些后缀能进知识库」。两份清单服务的是同一个产品规则，但代码路径完全不相交：
//
//   - Python 侧：POST /v1/kb/{id}/documents（base64 直传，只在 SaaS/接口调用时走）
//   - Go 侧：分片上传 Init/Complete（桌面端实际走的那条）
//
// 只改一边不会报错，只会让两个入口对"支持什么"给出不同答案。这里直接读 Python
// 源码做比对，任何一侧漂移都会立刻变红。

// pythonKBSuffixes 从 python-engine 源码里解析出某组后缀常量。
func pythonKBSuffixes(t *testing.T, constName string) []string {
	t.Helper()
	// 测试运行目录是 internal/api，仓库根在其上两级
	path := filepath.Join("..", "..", "python-engine", "app", "knowledge", "remote.py")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读不到 Python 侧白名单 %s: %v", path, err)
	}
	// 形如：_LOCAL_SUFFIXES = {".pdf"}
	re := regexp.MustCompile(`(?m)^` + regexp.QuoteMeta(constName) + `\s*=\s*\{([^}]*)\}`)
	m := re.FindSubmatch(raw)
	if m == nil {
		t.Fatalf("在 %s 里找不到常量 %s", path, constName)
	}
	var out []string
	for _, lit := range regexp.MustCompile(`"([^"]*)"`).FindAllStringSubmatch(string(m[1]), -1) {
		out = append(out, strings.ToLower(lit[1]))
	}
	sort.Strings(out)
	if len(out) == 0 {
		t.Fatalf("%s 解析出空列表，解析规则可能过期了", constName)
	}
	return out
}

func sortedCopy(in []string) []string {
	out := append([]string(nil), in...)
	sort.Strings(out)
	return out
}

func eqStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// TestKBSuffixesMatchPython 两侧的白名单必须逐字一致。
func TestKBSuffixesMatchPython(t *testing.T) {
	cases := []struct {
		provider  string
		goList    []string
		pythonCst string
	}{
		{"selfhosted", kbSelfHostedSuffixes, "_LOCAL_SUFFIXES"},
		{"cloud", kbCloudSuffixes, "_CLOUD_SUFFIXES"},
	}
	for _, c := range cases {
		want := pythonKBSuffixes(t, c.pythonCst)
		got := sortedCopy(c.goList)
		if !eqStrings(got, want) {
			t.Errorf("provider %s 的白名单与 Python %s 不一致：\n  Go     = %v\n  Python = %v\n"+
				"两侧必须同步修改，否则桌面端（走 Go）与接口（走 Python）对'支持什么'给出不同答案",
				c.provider, c.pythonCst, got, want)
		}
	}
}

// TestKBSuffixAllowed 类型判定本身的行为。
func TestKBSuffixAllowed(t *testing.T) {
	cases := []struct {
		provider string
		name     string
		want     bool
	}{
		// 自托管只吃 PDF
		{"selfhosted", "paper.pdf", true},
		{"selfhosted", "paper.PDF", true}, // 大小写不敏感
		{"selfhosted", "resume.docx", false},
		{"selfhosted", "notes.md", false},
		// cloud 支持 Office
		{"cloud", "resume.docx", true},
		{"cloud", "deck.pptx", true},
		{"cloud", "legacy.doc", true},
		{"cloud", "paper.pdf", true},
		{"cloud", "notes.md", false},
		{"cloud", "data.csv", false},
		// 无扩展名一律拒绝：旧实现把 ext=="" 当 txt 落库，等于给任意文件开后门
		{"cloud", "README", false},
		{"selfhosted", "noext", false},
		// 未知 provider 按并集放行（见 kbUnionSuffixes 的说明）
		{"", "paper.pdf", true},
		{"brand-new-provider", "resume.docx", true},
		{"brand-new-provider", "notes.md", false},
	}
	for _, c := range cases {
		if got := kbSuffixAllowed(c.provider, c.name); got != c.want {
			t.Errorf("kbSuffixAllowed(%q, %q) = %v，期望 %v", c.provider, c.name, got, c.want)
		}
	}
}

// TestKBUnionSuffixesIsSortedUnique 并集不能有重复，且必须有序（会拼进提示文案）。
func TestKBUnionSuffixesIsSortedUnique(t *testing.T) {
	union := kbUnionSuffixes()
	if !sort.StringsAreSorted(union) {
		t.Errorf("并集未排序：%v", union)
	}
	seen := map[string]bool{}
	for _, ext := range union {
		if seen[ext] {
			t.Errorf("并集出现重复项 %q：%v", ext, union)
		}
		seen[ext] = true
	}
	// 并集必须覆盖两侧全部类型，否则会莫名其妙拒掉某些文件
	for _, ext := range append(sortedCopy(kbSelfHostedSuffixes), kbCloudSuffixes...) {
		if !seen[ext] {
			t.Errorf("并集漏掉了 %q：%v", ext, union)
		}
	}
}

// TestKBTypeRejectionMentionsSupported 拒绝文案必须说清楚"现在支持什么"。
//
// 只报"不支持该类型"的话，用户会换个后缀再试，来回几次才意识到问题不在单个文件。
func TestKBTypeRejectionMentionsSupported(t *testing.T) {
	msg := kbTypeRejection("selfhosted", "resume.docx")
	if !strings.Contains(msg, ".docx") {
		t.Errorf("文案里应包含被拒的后缀，实际：%s", msg)
	}
	if !strings.Contains(msg, ".pdf") {
		t.Errorf("文案里应列出当前支持的类型 .pdf，实际：%s", msg)
	}

	// 无扩展名的文件也要给出可读说明，而不是露出空字符串
	noext := kbTypeRejection("cloud", "README")
	if !strings.Contains(noext, "无扩展名") {
		t.Errorf("无扩展名时文案应说明「无扩展名」，实际：%s", noext)
	}
}

// TestKBUnsupportedTypeErrorIsDistinguishable 类型错误必须能与存储故障区分开。
//
// Complete 靠 errors.As 把前者映射成 400、后者映射成 500。若类型错误只是普通
// fmt.Errorf，用户看到的就是"服务器错误"——换文件也没用，因为问题不在文件。
func TestKBUnsupportedTypeErrorIsDistinguishable(t *testing.T) {
	var err error = kbUnsupportedTypeError{msg: "nope"}
	var target kbUnsupportedTypeError
	if !errors.As(err, &target) {
		t.Fatal("kbUnsupportedTypeError 无法通过 errors.As 取出，Complete 会把它误报成 500")
	}
	if target.Error() != "nope" {
		t.Errorf("Error() = %q，期望透传 msg", target.Error())
	}
}
