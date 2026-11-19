package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/market"
)

// 市场「知识库」条目 = 文档合集。
//
// 这组用例锁死三件事：
//
//  1. **清单在产生副作用之前校验**：写错的清单若等到建库之后才报错，会留下
//     "建好了但一份文档都没有"的空库 —— 用户既看不出原因，重试还会再建一个。
//  2. **文档字节不经网关**：导入走内部端点 `kb-import`，由引擎出网抓取。若哪天
//     有人改回"网关抓取 + base64 上传"，几十 MB 的 PDF 会多一遍编解码，而
//     "多大算太大"也会出现第二份判断。
//  3. **任何一份失败都回滚刚建的库**：半成品库不会写入安装记录，用户重试就会
//     再建一个，空库越积越多。

// ── 清单校验（纯函数） ──

func TestNormalizeCollectionDocuments_FillsExtensionFromFileType(t *testing.T) {
	out, err := normalizeCollectionDocuments([]knowledgeDocument{
		{Name: "guide", URL: "https://example.com/a", FileType: "PDF"},
	})
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if got := out[0].Name; got != "guide.pdf" {
		t.Errorf("name = %q, want guide.pdf（file_type 应补成扩展名）", got)
	}
	if got := out[0].FileType; got != "pdf" {
		t.Errorf("file_type = %q, want pdf（应规范化成小写）", got)
	}
}

// name 自带扩展名时以 name 为准：引擎按 name 的扩展判断 provider 是否支持。
func TestNormalizeCollectionDocuments_NameExtensionWins(t *testing.T) {
	out, err := normalizeCollectionDocuments([]knowledgeDocument{
		{Name: "paper.PDF", URL: "https://example.com/a", FileType: "docx"},
	})
	if err != nil {
		t.Fatalf("err = %v", err)
	}
	if out[0].Name != "paper.PDF" {
		t.Errorf("name = %q，应保持原样（不追加第二个扩展名）", out[0].Name)
	}
}

func TestNormalizeCollectionDocuments_RejectsBadEntries(t *testing.T) {
	cases := []struct {
		name string
		in   []knowledgeDocument
		want string
	}{
		{
			name: "缺 url",
			in:   []knowledgeDocument{{Name: "a.pdf"}},
			want: "url is required",
		},
		{
			name: "非 https",
			in:   []knowledgeDocument{{Name: "a.pdf", URL: "http://example.com/a.pdf"}},
			want: "https",
		},
		{
			name: "相对地址",
			in:   []knowledgeDocument{{Name: "a.pdf", URL: "/a.pdf"}},
			want: "https",
		},
		{
			name: "无扩展名且无 file_type",
			in:   []knowledgeDocument{{Name: "a", URL: "https://example.com/a"}},
			want: "file extension",
		},
		{
			name: "缺 name",
			in:   []knowledgeDocument{{URL: "https://example.com/a.pdf"}},
			want: "name is required",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := normalizeCollectionDocuments(tc.in)
			if err == nil {
				t.Fatalf("应当报错，却通过了")
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("err = %v，want 含 %q", err, tc.want)
			}
		})
	}
}

func TestNormalizeCollectionDocuments_CapsCount(t *testing.T) {
	in := make([]knowledgeDocument, maxCollectionDocuments+1)
	for i := range in {
		in[i] = knowledgeDocument{Name: "a.pdf", URL: "https://example.com/a.pdf"}
	}
	if _, err := normalizeCollectionDocuments(in); err == nil {
		t.Fatalf("超过 %d 份应报错", maxCollectionDocuments)
	}
}

// dotfile 不算带扩展名：合集里出现 `.env` 这种名字本身就是错的。
func TestHasExtension(t *testing.T) {
	for name, want := range map[string]bool{
		"a.pdf":   true,
		".env":    false,
		"noext":   false,
		"a.":      false,
		"a.b.txt": true,
	} {
		if got := hasExtension(name); got != want {
			t.Errorf("hasExtension(%q) = %v, want %v", name, got, want)
		}
	}
}

// ── 安装：导入走内部端点 ──

// collectionEngine 起一个假引擎：`POST /v1/kb` 回建库结果，`kb-import` 按
// importReply 回结果，其余路径（DELETE 回滚）记录 URI。
type collectionEngine struct {
	srv        *httptest.Server
	imports    []map[string]any
	rollbacks  []string
	importHits int
}

func newCollectionEngine(t *testing.T, importReply string) *collectionEngine {
	t.Helper()
	e := &collectionEngine{}
	e.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodDelete:
			e.rollbacks = append(e.rollbacks, r.RequestURI)
			_, _ = w.Write([]byte(`{"ok":true}`))
		case strings.HasSuffix(r.URL.Path, "/v1/internal/market/kb-import"):
			e.importHits++
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			e.imports = append(e.imports, body)
			_, _ = w.Write([]byte(importReply))
		default: // POST /v1/kb
			_, _ = w.Write([]byte(`{"id":"kb-1"}`))
		}
	}))
	t.Cleanup(e.srv.Close)
	return e
}

func collectionItem(payload string) market.Item {
	return market.Item{
		Kind:    market.KindKnowledge,
		ID:      "knowledge:collection",
		Name:    "合集",
		Payload: json.RawMessage(payload),
	}
}

func TestKnowledgeInstall_ImportsEachDocumentViaEngine(t *testing.T) {
	e := newCollectionEngine(t, `{"ok":true,"bytes":10}`)
	inst := knowledgeMarketInstaller{py: engine.NewPythonClient(e.srv.URL)}

	ref, err := inst.Install(claimsCtx(), collectionItem(`{
		"documents":[
			{"name":"a.pdf","url":"https://example.com/a.pdf"},
			{"name":"b","url":"https://example.com/b","file_type":"docx"}
		]}`), nil)
	if err != nil {
		t.Fatalf("安装失败：%v", err)
	}

	if e.importHits != 2 {
		t.Fatalf("kb-import 调用 %d 次，want 2", e.importHits)
	}
	// 网关只传地址与身份，**不传字节**。
	for i, body := range e.imports {
		if _, has := body["content"]; has {
			t.Errorf("第 %d 份带了 content 字段：字节不该经网关搬运", i)
		}
		if body["kb_id"] != "kb-1" {
			t.Errorf("第 %d 份 kb_id = %v, want kb-1", i, body["kb_id"])
		}
		if body["user_id"] != "u1" {
			t.Errorf("第 %d 份 user_id = %v, want u1（身份必须由网关填）", i, body["user_id"])
		}
		if body["url"] == "" {
			t.Errorf("第 %d 份缺 url", i)
		}
	}
	if got := e.imports[1]["name"]; got != "b.docx" {
		t.Errorf("第二份 name = %v, want b.docx（file_type 补扩展名）", got)
	}
	if got, _ := ref["documents"].(int); got != 2 {
		t.Errorf("ref[documents] = %v, want 2", ref["documents"])
	}
	if e.rollbacks != nil {
		t.Errorf("成功安装不该回滚：%v", e.rollbacks)
	}
}

// 空白合集是合法形态：只建库，不调导入。
func TestKnowledgeInstall_EmptyCollectionCreatesEmptyBase(t *testing.T) {
	e := newCollectionEngine(t, `{"ok":true}`)
	inst := knowledgeMarketInstaller{py: engine.NewPythonClient(e.srv.URL)}

	ref, err := inst.Install(claimsCtx(), collectionItem(`{"visibility":"tenant"}`), nil)
	if err != nil {
		t.Fatalf("安装失败：%v", err)
	}
	if e.importHits != 0 {
		t.Errorf("空合集不该调用导入 %d 次", e.importHits)
	}
	if got, _ := ref["documents"].(int); got != 0 {
		t.Errorf("ref[documents] = %v, want 0", ref["documents"])
	}
}

// 任何一份导入失败 → 回滚刚建的库，并把"哪一份、为什么"带出来。
func TestKnowledgeInstall_RollsBackOnImportFailure(t *testing.T) {
	e := newCollectionEngine(t, `{"ok":false,"error":"unsupported_document","detail":"provider 不支持 pdf"}`)
	inst := knowledgeMarketInstaller{py: engine.NewPythonClient(e.srv.URL)}

	_, err := inst.Install(claimsCtx(), collectionItem(`{
		"documents":[{"name":"a.pdf","url":"https://example.com/a.pdf"}]}`), nil)
	if err == nil {
		t.Fatal("导入失败时安装必须失败，不能留下半成品库")
	}
	if !errors.Is(err, errInstallUpstream) {
		t.Errorf("err = %v，want 归上游故障（502）", err)
	}
	// 用户要能看到是哪一份、为什么 —— 否则只能看到一句"安装失败"。
	for _, want := range []string{"a.pdf", "unsupported_document", "provider 不支持"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("err = %v，缺少 %q", err, want)
		}
	}
	if len(e.rollbacks) != 1 {
		t.Fatalf("回滚调用 %d 次，want 1：%v", len(e.rollbacks), e.rollbacks)
	}
	if !strings.HasPrefix(e.rollbacks[0], "/v1/kb/kb-1?") {
		t.Errorf("回滚 URI = %q，want /v1/kb/kb-1?…", e.rollbacks[0])
	}
}

// 清单非法 → 400，且**不产生任何副作用**（连建库请求都不该发出）。
func TestKnowledgeInstall_InvalidCollectionIs400WithoutSideEffects(t *testing.T) {
	e := newCollectionEngine(t, `{"ok":true}`)
	inst := knowledgeMarketInstaller{py: engine.NewPythonClient(e.srv.URL)}

	_, err := inst.Install(claimsCtx(), collectionItem(`{
		"documents":[{"name":"a.pdf","url":"http://example.com/a.pdf"}]}`), nil)
	if !errors.Is(err, market.ErrBadRequest) {
		t.Fatalf("err = %v，want ErrBadRequest（发布方的错，不是上游故障）", err)
	}
	if e.importHits != 0 || len(e.rollbacks) != 0 {
		t.Errorf("清单校验失败时不该有任何引擎副作用：imports=%d rollbacks=%v",
			e.importHits, e.rollbacks)
	}
}
