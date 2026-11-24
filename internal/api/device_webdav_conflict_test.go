package api

// H55：设备 WebDAV 一致性与截断语义回归。
//
// 三条验收：
//  1. 设备端列表被截断时，目录遍历必须失败（不能返回半份列表）；
//  2. 写入必须带 ExpectedHash，冲突返回 412 且不落盘；
//  3. 同一份内容的重试保持幂等（不重复写），冲突计数可观测。

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

type conflictFakeDevice struct {
	mu        sync.Mutex
	files     map[string]string
	dirs      map[string]bool
	truncated bool
	writeLog  []string
}

func contentHash(content string) string {
	sum := sha256.Sum256([]byte(content))
	return hex.EncodeToString(sum[:])
}

func (d *conflictFakeDevice) file(rel string) string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.files[rel]
}

func (d *conflictFakeDevice) setFile(rel, content string) {
	d.mu.Lock()
	d.files[rel] = content
	d.mu.Unlock()
}

func (d *conflictFakeDevice) writes() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]string, len(d.writeLog))
	copy(out, d.writeLog)
	return out
}

func (d *conflictFakeDevice) hashOf(rel string) string {
	d.mu.Lock()
	defer d.mu.Unlock()
	content, ok := d.files[rel]
	if !ok {
		return ""
	}
	return contentHash(content)
}

// startConflictFakeDevice 起一台"支持 hash op + expected_hash 校验"的 fake 设备。
func startConflictFakeDevice(t *testing.T, tenant, user, device, root string, st *conflictFakeDevice) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		up := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
		conn, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		dc := &deviceConn{
			tenant: tenant, user: user, device: device, ws: conn,
			pending: map[string]chan deviceOpResponse{}, closed: make(chan struct{}),
		}
		deviceHubSingleton.register(dc)
		t.Cleanup(func() { deviceHubSingleton.unregister(dc) })
		go func() {
			for {
				var resp deviceOpResponse
				if err := conn.ReadJSON(&resp); err != nil {
					return
				}
				dc.pendMu.Lock()
				ch := dc.pending[resp.ID]
				delete(dc.pending, resp.ID)
				dc.pendMu.Unlock()
				if ch != nil {
					ch <- resp
					close(ch)
				}
			}
		}()
	}))
	t.Cleanup(srv.Close)

	wsURL := "ws" + strings.TrimPrefix(srv.URL, "http")
	client, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial fake device: %v", err)
	}
	t.Cleanup(func() { client.Close() })

	rootNorm := strings.TrimRight(strings.ReplaceAll(root, `\`, "/"), "/")
	go func() {
		for {
			var req deviceOpRequest
			if err := client.ReadJSON(&req); err != nil {
				return
			}
			resp := deviceOpResponse{ID: req.ID}
			rel := strings.TrimPrefix(strings.ReplaceAll(req.Path, `\`, "/"), rootNorm+"/")
			if rel == rootNorm {
				rel = ""
			}
			st.mu.Lock()
			switch req.Op {
			case "stat":
				if st.dirs[rel] {
					resp.OK, resp.Stat = true, &deviceStat{IsDir: true, Mode: "dir"}
				} else if content, ok := st.files[rel]; ok {
					resp.OK, resp.Stat = true, &deviceStat{Size: int64(len(content)), Mode: "regular"}
				} else {
					resp.Error = "not found"
				}
			case "list":
				resp.OK = true
				names := map[string]deviceEntry{}
				prefix := rel
				if prefix != "" && !strings.HasSuffix(prefix, "/") {
					prefix += "/"
				}
				for name := range st.files {
					rest := strings.TrimPrefix(name, prefix)
					if strings.HasPrefix(name, prefix) && !strings.Contains(rest, "/") {
						names[rest] = deviceEntry{Name: rest, Size: int64(len(st.files[name]))}
					}
				}
				keys := make([]string, 0, len(names))
				for k := range names {
					keys = append(keys, k)
				}
				sort.Strings(keys)
				if st.truncated && len(keys) > 1 {
					// 模拟设备端超过列举上限：只回第一项并置 truncated。
					keys = keys[:1]
					resp.Truncated = true
				}
				for _, k := range keys {
					resp.Entries = append(resp.Entries, names[k])
				}
			case "read":
				content := st.files[rel]
				offset := int(req.Offset)
				if offset > len(content) {
					offset = len(content)
				}
				data := []byte(content)[offset:]
				if int64(len(data)) > req.MaxBytes {
					data = data[:req.MaxBytes]
				}
				resp.OK, resp.DataB64 = true, base64.StdEncoding.EncodeToString(data)
			case "hash":
				content, ok := st.files[rel]
				if !ok {
					resp.Error = "ENOENT: no such file"
					break
				}
				resp.OK, resp.Hash = true, contentHash(content)
			case "write":
				expected := strings.ToLower(strings.TrimSpace(req.ExpectedHash))
				current, exists := st.files[rel]
				currentHash := ""
				if exists {
					currentHash = contentHash(current)
				}
				if expected != "" && (currentHash == "" || currentHash != expected) {
					resp.Error = "conflict"
					break
				}
				data, _ := base64.StdEncoding.DecodeString(req.DataB64)
				st.files[rel] = string(data)
				st.writeLog = append(st.writeLog, expected)
				resp.OK, resp.Hash = true, contentHash(string(data))
			default:
				resp.Error = "unsupported op: " + req.Op
			}
			st.mu.Unlock()
			_ = client.WriteJSON(resp)
		}
	}()

	deadline := time.Now().Add(2 * time.Second)
	for !deviceHubSingleton.Online(tenant, user, device) && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if !deviceHubSingleton.Online(tenant, user, device) {
		t.Fatal("fake device 未注册")
	}
}

func deviceWebDAVPut(t *testing.T, fs *deviceWebDAVFS, name, body, ifMatch string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPut, "/v1/device-webdav/p1/"+name, strings.NewReader(body))
	req.Header.Set("Content-Type", "application/octet-stream")
	if ifMatch != "" {
		req.Header.Set("If-Match", ifMatch)
	}
	rec := httptest.NewRecorder()
	serveDeviceWebDAV(rec, req, "/v1/device-webdav/p1", fs)
	return rec
}

func TestDeviceWebDAVDirectoryTruncationFailsTraversal(t *testing.T) {
	st := &conflictFakeDevice{
		files:     map[string]string{"a.txt": "a", "b.txt": "b", "c.txt": "c"},
		dirs:      map[string]bool{"": true},
		truncated: true,
	}
	startConflictFakeDevice(t, "ten", "usr", "dev-trunc", `D:\proj`, st)
	fs := &deviceWebDAVFS{tenant: "ten", user: "usr", device: "dev-trunc", root: `D:\proj`}
	ctx := context.Background()

	// FS 层：Readdir 必须报错，不能把截断后的前 N 项当完整列表交出去。
	f, err := fs.OpenFile(ctx, "/", os.O_RDONLY, 0)
	if err != nil {
		t.Fatalf("open 目录: %v", err)
	}
	if entries, err := f.Readdir(0); !errors.Is(err, errDeviceListTruncated) {
		t.Fatalf("截断目录必须失败，得到 entries=%v err=%v", entries, err)
	}

	// WebDAV 层：PROPFIND Depth:1 不能返回 207（那意味着客户端把半份列表当真）。
	req := httptest.NewRequest("PROPFIND", "/v1/device-webdav/p1/",
		strings.NewReader(`<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:"><D:allprop/></D:propfind>`))
	req.Header.Set("Depth", "1")
	req.Header.Set("Content-Type", "application/xml")
	rec := httptest.NewRecorder()
	serveDeviceWebDAV(rec, req, "/v1/device-webdav/p1", fs)
	if rec.Code == http.StatusMultiStatus {
		t.Fatalf("截断目录不应成功返回 Multi-Status，body=%s", rec.Body.String())
	}
	if rec.Code < http.StatusInternalServerError {
		t.Fatalf("截断目录应显式失败（5xx），得到 %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "truncated") {
		t.Fatalf("失败响应应说明截断原因，body=%q", rec.Body.String())
	}
}

func TestDeviceWebDAVWriteConflictReturns412(t *testing.T) {
	st := &conflictFakeDevice{
		files: map[string]string{"a.txt": "v1"},
		dirs:  map[string]bool{"": true},
	}
	startConflictFakeDevice(t, "ten", "usr", "dev-conflict", `/srv/proj`, st)
	fs := &deviceWebDAVFS{tenant: "ten", user: "usr", device: "dev-conflict", root: `/srv/proj`}

	baseETag := `"` + st.hashOf("a.txt") + `"`
	before := deviceWebDAVWriteConflictCount()

	resp := deviceWebDAVPut(t, fs, "a.txt", "v2", baseETag)
	if resp.Code != http.StatusCreated {
		t.Fatalf("带正确 If-Match 的写入应成功，得到 %d: %s", resp.Code, resp.Body.String())
	}
	if got, want := resp.Header().Get("ETag"), `"`+contentHash("v2")+`"`; got != want {
		t.Fatalf("响应 ETag=%s，want %s（必须是内容 hash，客户端才会拿来做 If-Match）", got, want)
	}
	if st.file("a.txt") != "v2" {
		t.Fatalf("设备文件未更新: %q", st.file("a.txt"))
	}

	// 陈旧 ETag（v1）⇒ 设备内容已前进 ⇒ 412，且不落盘。
	resp = deviceWebDAVPut(t, fs, "a.txt", "v3", baseETag)
	if resp.Code != http.StatusPreconditionFailed {
		t.Fatalf("hash 冲突应返回 412，得到 %d: %s", resp.Code, resp.Body.String())
	}
	if reason := resp.Header().Get("X-Chiron-Device-Conflict"); reason == "" {
		t.Fatal("412 应带冲突原因头 X-Chiron-Device-Conflict")
	}
	if st.file("a.txt") != "v2" {
		t.Fatalf("冲突写入不得落盘，当前内容 %q", st.file("a.txt"))
	}
	if got := deviceWebDAVWriteConflictCount() - before; got < 1 {
		t.Fatalf("冲突次数应 +1，实际 +%d", got)
	}
}

func TestDeviceWebDAVWriteCarriesExpectedHashAndRetryIsIdempotent(t *testing.T) {
	st := &conflictFakeDevice{
		files: map[string]string{"a.txt": "v1"},
		dirs:  map[string]bool{"": true},
	}
	startConflictFakeDevice(t, "ten", "usr", "dev-expected", `D:\proj`, st)
	fs := &deviceWebDAVFS{tenant: "ten", user: "usr", device: "dev-expected", root: `D:\proj`}

	baseETag := `"` + st.hashOf("a.txt") + `"`
	resp := deviceWebDAVPut(t, fs, "a.txt", "v2", baseETag)
	if resp.Code != http.StatusCreated {
		t.Fatalf("首次写入应成功，得到 %d: %s", resp.Code, resp.Body.String())
	}
	writes := st.writes()
	if len(writes) != 1 {
		t.Fatalf("应发生 1 次设备写入，实际 %d", len(writes))
	}
	if writes[0] != contentHash("v1") {
		t.Fatalf("写入必须携带 ExpectedHash=%s，实际 %q", contentHash("v1"), writes[0])
	}

	// 同一份内容 + 同一个 If-Match 重试：命中"内容已一致"，幂等成功且不重复写。
	resp = deviceWebDAVPut(t, fs, "a.txt", "v2", baseETag)
	if resp.Code < 200 || resp.Code >= 300 {
		t.Fatalf("幂等重试应成功，得到 %d: %s", resp.Code, resp.Body.String())
	}
	if got := len(st.writes()); got != 1 {
		t.Fatalf("幂等重试不应重复写入，写入次数 %d", got)
	}
	if got := resp.Header().Get("X-Chiron-Device-Write"); got != "replayed" {
		t.Fatalf("幂等重试应标记 X-Chiron-Device-Write=replayed，得到 %q", got)
	}
	if st.file("a.txt") != "v2" {
		t.Fatalf("重试后内容被改动: %q", st.file("a.txt"))
	}
}

// 竞态路径：打开后设备内容被别的进程改写，Close 时必须由设备端拒绝。
func TestDeviceWebDAVWriteDetectsConcurrentChange(t *testing.T) {
	st := &conflictFakeDevice{
		files: map[string]string{"a.txt": "v1"},
		dirs:  map[string]bool{"": true},
	}
	startConflictFakeDevice(t, "ten", "usr", "dev-race", `/srv/race`, st)
	fs := &deviceWebDAVFS{tenant: "ten", user: "usr", device: "dev-race", root: `/srv/race`}
	ctx := context.Background()

	f, err := fs.OpenFile(ctx, "/a.txt", os.O_RDWR|os.O_CREATE|os.O_TRUNC, 0o644)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	st.setFile("a.txt", "edited-on-device")
	if _, err := f.Write([]byte("mine")); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := f.Close(); !errors.Is(err, errDeviceWriteConflict) {
		t.Fatalf("并发改写必须报冲突，得到 %v", err)
	}
	if st.file("a.txt") != "edited-on-device" {
		t.Fatalf("冲突时不得覆盖设备内容: %q", st.file("a.txt"))
	}
}
