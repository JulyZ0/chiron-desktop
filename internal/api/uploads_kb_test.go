package api

import "testing"

// `kbStorageKey` 必须与 Python 侧 `app/knowledge/blob.py::build_key` 算出**同一个** key。
//
// **为什么钉死**：网关写原文、pageindex-worker 读原文，两侧各拼一次路径。key 一旦
// 漂移，上传照样成功、库里照样有行，只有索引阶段读不到字节 —— 表象是"知识库建好了
// 但永远 ready 不了"，而两边的代码单看都没有错。
//
// 期望值按 Python 实现逐字推算：
//
//	_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
//	safe = _UNSAFE_NAME_RE.sub("_", filename or "file").strip("._") or "file"
//	return f"{tenant_id}/{kb_id}/{doc_id}/{safe[:180]}"
func TestKBStorageKeyMatchesPython(t *testing.T) {
	cases := []struct {
		name string
		want string
	}{
		// 普通 ASCII 原样保留
		{"report.pdf", "1/kb-1/doc-1/report.pdf"},
		// 连续的中文/空格被折叠成单个 "_"，strip 再把前导 "_" 与 "." 一起去掉
		{"年度 报告.pdf", "1/kb-1/doc-1/pdf"},
		// 首尾的点会被 strip 掉：`.` 与 `..` 这类名字不能变成隐藏文件或上级目录
		{"..hidden..", "1/kb-1/doc-1/hidden"},
		// 整个名字都被替换/剥空时回落 "file"，绝不产出空文件名
		{"中文", "1/kb-1/doc-1/file"},
		{"...", "1/kb-1/doc-1/file"},
		// 分隔符被替换：不能借文件名穿出 doc 目录
		{"../../etc/passwd", "1/kb-1/doc-1/etc_passwd"},
	}
	for _, c := range cases {
		if got := kbStorageKey(1, "kb-1", "doc-1", c.name); got != c.want {
			t.Errorf("kbStorageKey(%q) = %q，期望 %q（须与 Python build_key 一致）",
				c.name, got, c.want)
		}
	}
}

// TestKBStorageKeyTruncates 超长名字必须截到 180 字节以内（Python 侧同款上限）。
func TestKBStorageKeyTruncates(t *testing.T) {
	long := make([]byte, 400)
	for i := range long {
		long[i] = 'a'
	}
	got := kbStorageKey(1, "kb", "doc", string(long))
	// "1" + "/" + "kb" + "/" + "doc" + "/" + 180
	want := 1 + 1 + 2 + 1 + 3 + 1 + 180
	if len(got) != want {
		t.Fatalf("长度 %d，期望 %d（文件名须截断到 180）", len(got), want)
	}
}
