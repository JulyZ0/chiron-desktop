package api

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/storage"
)

// ── H29：对象键由上传会话推导，客户端 file_url 只用于比对 ──

const (
	testSessionAssetID = "11111111-2222-3333-4444-555555555555"
)

func withTenantClaims(r *http.Request, tenantID int16, userID string) *http.Request {
	ctx := auth.WithClaims(r.Context(), &auth.Claims{TenantID: tenantID, UserID: userID})
	return r.WithContext(ctx)
}

func postJSON(t *testing.T, h http.HandlerFunc, r *http.Request, payload map[string]any) *httptest.ResponseRecorder {
	t.Helper()
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal payload: %v", err)
	}
	r2 := httptest.NewRequest(http.MethodPost, "/v1/media/complete", bytes.NewReader(body))
	if c := auth.GetClaims(r.Context()); c != nil {
		r2 = r2.WithContext(auth.WithClaims(r2.Context(), c))
	}
	rec := httptest.NewRecorder()
	h(rec, r2)
	return rec
}

// 伪造 file_url 必须在**任何 DB 写入之前**被拒：这些用例都不需要数据库，
// 一旦回归（重新信任客户端 URL）就会变成 500/写入，测试随即失败。
func TestCompleteUploadRejectsForgedFileURL(t *testing.T) {
	h := newMediaTestHandler(t, t.TempDir())
	claimsReq := withTenantClaims(httptest.NewRequest(http.MethodPost, "/v1/media/complete", nil), 1, "u1")

	cases := []struct {
		name    string
		id      string
		fileURL string
	}{
		{"other tenant prefix", testSessionAssetID, "/media/2/" + testSessionAssetID + "/a.png"},
		{"other asset under same tenant", testSessionAssetID, "/media/1/99999999-2222-3333-4444-555555555555/a.png"},
		{"absolute external url", testSessionAssetID, "https://evil.example/media/1/" + testSessionAssetID + "/a.png"},
		{"protocol relative", testSessionAssetID, "//evil.example/media/1/" + testSessionAssetID + "/a.png"},
		{"path traversal", testSessionAssetID, "/media/1/" + testSessionAssetID + "/../../2/x/a.png"},
		{"foreign bucket object", testSessionAssetID, "https://minio.example/other-bucket/media/1/" + testSessionAssetID + "/a.png"},
		{"missing session id", "", "/media/1/" + testSessionAssetID + "/a.png"},
		{"non-uuid session id", "session_123", "/media/1/session_123/a.png"},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rec := postJSON(t, h.CompleteUpload, claimsReq, map[string]any{
				"id": c.id, "name": "a.png", "file_url": c.fileURL, "size": 10,
			})
			if rec.Code != http.StatusBadRequest {
				t.Fatalf("伪造 file_url 应 400，得到 %d：%s", rec.Code, rec.Body.String())
			}
			if !strings.Contains(rec.Body.String(), "upload session") {
				t.Fatalf("拒绝原因应指明与上传会话不匹配，得到：%s", rec.Body.String())
			}
		})
	}
}

// 服务端推导出的键必须与预签名阶段完全一致，否则合法上传会被误杀。
func TestResolveSessionObjectKeyAcceptsSessionDerivedKey(t *testing.T) {
	root := t.TempDir()
	store := storage.NewAtomicStore(storage.NewLocalStore(root))

	got, err := resolveSessionObjectKey(1, testSessionAssetID, "a.png", "/media/1/"+testSessionAssetID+"/a.png", store)
	if err != nil {
		t.Fatalf("合法键应通过，得到错误：%v", err)
	}
	want := "media/1/" + testSessionAssetID + "/a.png"
	if got != want {
		t.Fatalf("推导键 = %q，期望 %q", got, want)
	}

	// 文件名净化与否必须两边一致：带空白的名字在预签名阶段被净化，完成阶段
	// 推导的键也必须是净化后的形态（否则合法上传永远完不成）。
	got2, err := resolveSessionObjectKey(1, testSessionAssetID, " a.png ", "/media/1/"+testSessionAssetID+"/a.png", store)
	if err != nil || got2 != want {
		t.Fatalf("净化后应得到 %q，得到 %q / %v", want, got2, err)
	}
}

func TestResolveSessionObjectKeyRejectsTraversalName(t *testing.T) {
	store := storage.NewAtomicStore(storage.NewLocalStore(t.TempDir()))
	if _, err := resolveSessionObjectKey(1, testSessionAssetID, "../../etc/passwd", "/media/1/x", store); err == nil {
		t.Fatal("含路径分隔符的文件名应被拒绝")
	}
}

// ── H29：读取侧重新校验对象归属 ──

func TestMediaOwnedObjectKey(t *testing.T) {
	store := storage.NewAtomicStore(storage.NewLocalStore(t.TempDir()))
	asset := testSessionAssetID

	ok := []struct{ name, path, fileURL string }{
		{"new layout object key", "media/1/" + asset + "/a.png", ""},
		{"new layout relative path", "1/" + asset + "/a.png", ""},
		{"derived from file_url", "", "/media/1/" + asset + "/a.png"},
		{"legacy owner dir", "u_u1/" + asset[:8] + "_a.png", ""},
	}
	for _, c := range ok {
		if _, err := mediaOwnedObjectKey(1, asset, "u1", c.path, c.fileURL, store); err != nil {
			t.Fatalf("%s：应通过，得到错误 %v", c.name, err)
		}
	}

	bad := []struct{ name, path, fileURL string }{
		{"other tenant", "media/2/" + asset + "/a.png", ""},
		{"other asset", "media/1/99999999-2222-3333-4444-555555555555/a.png", ""},
		{"foreign absolute url", "", "https://evil.example/media/1/" + asset + "/a.png"},
		{"traversal", "media/1/" + asset + "/../../2/x", ""},
		{"empty", "", ""},
		{"other user legacy dir", "u_u2/" + asset[:8] + "_a.png", ""},
	}
	for _, c := range bad {
		if _, err := mediaOwnedObjectKey(1, asset, "u1", c.path, c.fileURL, store); err == nil {
			t.Fatalf("%s：应被拒绝", c.name)
		}
	}
}

// ── H29：可执行/HTML/SVG 默认 attachment + nosniff，私有媒体 private/no-store ──

func TestMediaDownloadHeadersForActiveContent(t *testing.T) {
	cases := []struct {
		name        string
		mime        string
		fileName    string
		wantType    string
		wantKind    string
		attachments bool
	}{
		{"html mime", "text/html; charset=utf-8", "note.html", "application/octet-stream", "attachment", true},
		{"svg mime", "image/svg+xml", "logo.svg", "application/octet-stream", "attachment", true},
		{"html by extension", "", "report.html", "application/octet-stream", "attachment", true},
		{"xhtml", "application/xhtml+xml", "page.xhtml", "application/octet-stream", "attachment", true},
		{"javascript", "application/javascript", "app.js", "application/octet-stream", "attachment", true},
		{"png inline", "image/png", "pic.png", "image/png", "inline", false},
		{"pdf inline", "application/pdf", "doc.pdf", "application/pdf", "inline", false},
		{"unknown type", "application/zip", "a.zip", "application/zip", "attachment", true},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			p := mediaDownloadHeaders(c.mime, c.fileName)
			if p.ContentType != c.wantType {
				t.Fatalf("Content-Type = %q，期望 %q", p.ContentType, c.wantType)
			}
			if !strings.HasPrefix(p.Disposition, c.wantKind) {
				t.Fatalf("Disposition = %q，期望以 %q 开头", p.Disposition, c.wantKind)
			}
			if p.Attachment != c.attachments {
				t.Fatalf("Attachment = %v，期望 %v", p.Attachment, c.attachments)
			}
			if p.CacheControl != mediaPrivateCacheControl {
				t.Fatalf("Cache-Control = %q，期望 %q", p.CacheControl, mediaPrivateCacheControl)
			}
		})
	}
}

func TestDispositionFilenameCannotInjectHeaders(t *testing.T) {
	p := mediaDownloadHeaders("image/png", "a\"; filename=evil.html\r\nX-Injected: 1.png")
	if strings.ContainsAny(p.Disposition, "\r\n") {
		t.Fatalf("Content-Disposition 不得含换行：%q", p.Disposition)
	}
	if strings.Contains(p.Disposition, "evil.html\"") {
		t.Fatalf("文件名中的引号必须被剔除，得到 %q", p.Disposition)
	}
}

// HTML/SVG 内容经签名下载必须 attachment + nosniff，且不得被公共缓存留存。
func TestServeMediaFileAttachesActiveContent(t *testing.T) {
	root := t.TempDir()
	h := NewMediaHandler(storage.NewAtomicStore(storage.NewLocalStore(root)), nil)
	h.SetMediaRoot(root)

	rel := "1/" + testSessionAssetID + "/evil.svg"
	full := filepath.Join(root, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(full, []byte(`<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>`), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}

	policy := mediaDownloadHeaders("image/svg+xml", "evil.svg")
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/media/s/x", nil)
	h.serveMediaFile(rec, req, rel, policy)

	if got := rec.Header().Get("Content-Disposition"); !strings.HasPrefix(got, "attachment") {
		t.Fatalf("SVG 必须 attachment，得到 %q", got)
	}
	if got := rec.Header().Get("X-Content-Type-Options"); got != "nosniff" {
		t.Fatalf("必须设置 nosniff，得到 %q", got)
	}
	if got := rec.Header().Get("Cache-Control"); got != "private, no-store" {
		t.Fatalf("私有媒体必须 private, no-store，得到 %q", got)
	}
	if got := rec.Header().Get("Content-Type"); got != "application/octet-stream" {
		t.Fatalf("活动内容不得以可渲染类型返回，得到 %q", got)
	}
	if rec.Code != http.StatusOK {
		t.Fatalf("附件下载应 200，得到 %d", rec.Code)
	}
}

// 文本创建路径同样不得把 HTML/SVG 落库（与直传策略统一）。
func TestCreateRejectsActiveContentNames(t *testing.T) {
	h := newMediaTestHandler(t, t.TempDir())
	req := withTenantClaims(httptest.NewRequest(http.MethodPost, "/v1/media", nil), 1, "u1")

	for _, name := range []string{"x.html", "x.svg", "x.xhtml", "x.xml", "x.mhtml"} {
		rec := httptest.NewRecorder()
		r2 := httptest.NewRequest(http.MethodPost, "/v1/media", bytes.NewReader(
			[]byte(`{"name":"`+name+`","type":"text","content":"<svg onload=alert(1)>"}`)))
		r2 = r2.WithContext(req.Context())
		h.Create(rec, r2)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("%s：应 400，得到 %d：%s", name, rec.Code, rec.Body.String())
		}
	}

	// 普通 markdown 仍然可创建（校验只针对活动内容）
	rec := httptest.NewRecorder()
	r2 := httptest.NewRequest(http.MethodPost, "/v1/media", bytes.NewReader(
		[]byte(`{"name":"note.md","type":"text","content":"# hi"}`)))
	r2 = r2.WithContext(req.Context())
	h.Create(rec, r2)
	if rec.Code == http.StatusBadRequest {
		t.Fatalf("普通文本不应被类型校验拒绝：%s", rec.Body.String())
	}
}
