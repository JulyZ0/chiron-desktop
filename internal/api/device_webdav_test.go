package api

// R2 spike：WebDAV facade 经设备桥的读写/列目录/改名/删除。
// 这里用最小 fake 设备（内存文件表）验证端到端语义，不依赖真实桌面。

import (
	"context"
	"encoding/base64"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/gorilla/websocket"
	"golang.org/x/net/webdav"
)

type fakeWebDAVDevice struct {
	files map[string]string
	dirs  map[string]bool
}

func normalizeFakePath(root, p string) string {
	q := strings.ReplaceAll(p, `\`, "/")
	rootNorm := strings.TrimRight(strings.ReplaceAll(root, `\`, "/"), "/")
	if q == rootNorm {
		return ""
	}
	return strings.TrimPrefix(q, rootNorm+"/")
}

func startFakeWebDAVDevice(t *testing.T, tenant, user, device, root string, st *fakeWebDAVDevice) {
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

	go func() {
		for {
			var req deviceOpRequest
			if err := client.ReadJSON(&req); err != nil {
				return
			}
			resp := deviceOpResponse{ID: req.ID}
			rel := normalizeFakePath(root, req.Path)
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
					if strings.HasPrefix(name, prefix) {
						rest := strings.TrimPrefix(name, prefix)
						if !strings.Contains(rest, "/") {
							names[rest] = deviceEntry{Name: rest, Size: int64(len(st.files[name]))}
						}
					}
				}
				for name := range st.dirs {
					if strings.HasPrefix(name, prefix) && name != rel {
						rest := strings.TrimPrefix(name, prefix)
						if !strings.Contains(rest, "/") {
							names[rest] = deviceEntry{Name: rest, IsDir: true}
						}
					}
				}
				keys := make([]string, 0, len(names))
				for k := range names {
					keys = append(keys, k)
				}
				sort.Strings(keys)
				for _, k := range keys {
					resp.Entries = append(resp.Entries, names[k])
				}
			case "walk":
				resp.OK = true
				for name := range st.files {
					if name != "" {
						resp.Entries = append(resp.Entries, deviceEntry{Name: name, Size: int64(len(st.files[name]))})
					}
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
			case "write":
				data, _ := base64.StdEncoding.DecodeString(req.DataB64)
				st.files[rel] = string(data)
				resp.OK = true
			case "mkdir":
				st.dirs[rel] = true
				resp.OK = true
			case "remove":
				delete(st.files, rel)
				delete(st.dirs, rel)
				resp.OK = true
			case "rename":
				to := normalizeFakePath(root, req.To)
				if content, ok := st.files[rel]; ok {
					st.files[to] = content
					delete(st.files, rel)
				}
				resp.OK = true
			default:
				resp.Error = "unsupported"
			}
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

func TestDeviceWebDAVReadWriteRenameDelete(t *testing.T) {
	st := &fakeWebDAVDevice{files: map[string]string{"a.txt": "hello"}, dirs: map[string]bool{}}
	startFakeWebDAVDevice(t, "ten", "usr", "dev-webdav", `D:\proj`, st)
	fs := &deviceWebDAVFS{tenant: "ten", user: "usr", device: "dev-webdav", root: `D:\proj`}
	ctx := context.Background()

	info, err := fs.Stat(ctx, "/a.txt")
	if err != nil || info.Size() != 5 || info.IsDir() {
		t.Fatalf("stat: %v %+v", err, info)
	}

	f, err := fs.OpenFile(ctx, "/a.txt", os.O_RDWR, 0o644)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	buf := make([]byte, 32)
	n, _ := f.Read(buf)
	if string(buf[:n]) != "hello" {
		t.Fatalf("read = %q", buf[:n])
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		t.Fatal(err)
	}
	if _, err := f.Write([]byte("changed")); err != nil {
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	if st.files["a.txt"] != "changed" {
		t.Fatalf("设备文件未写入: %q", st.files["a.txt"])
	}

	if err := fs.Rename(ctx, "/a.txt", "/b.txt"); err != nil {
		t.Fatalf("rename: %v", err)
	}
	if _, ok := st.files["b.txt"]; !ok {
		t.Fatal("rename 未生效")
	}
	if err := fs.RemoveAll(ctx, "/b.txt"); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if _, ok := st.files["b.txt"]; ok {
		t.Fatal("remove 未生效")
	}
}

func TestDeviceWebDAVPermissionAndProtocol(t *testing.T) {
	st := &fakeWebDAVDevice{files: map[string]string{"a.txt": "hello"}, dirs: map[string]bool{"": true, "sub": true}}
	startFakeWebDAVDevice(t, "ten", "usr", "dev-webdav2", `/srv/proj`, st)
	fs := &deviceWebDAVFS{tenant: "ten", user: "usr", device: "dev-webdav2", root: `/srv/proj`}

	if _, err := fs.Stat(context.Background(), "/../etc/passwd"); !os.IsPermission(err) {
		t.Fatalf("越界应返回 permission，得到 %v", err)
	}
	if rootInfo, err := fs.Stat(context.Background(), "/"); err != nil || !rootInfo.IsDir() {
		t.Fatalf("根目录 Stat 失败: %v %+v", err, rootInfo)
	}

	h := &webdav.Handler{Prefix: "/v1/device-webdav/p1", FileSystem: fs, LockSystem: webdav.NewMemLS()}
	srv := httptest.NewServer(h)
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/v1/device-webdav/p1/a.txt")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK || string(body) != "hello" {
		t.Fatalf("GET = %d %q", resp.StatusCode, body)
	}

	req, _ := http.NewRequest("PROPFIND", srv.URL+"/v1/device-webdav/p1/", strings.NewReader(`<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:"><D:allprop/></D:propfind>`))
	req.Header.Set("Depth", "1")
	req.Header.Set("Content-Type", "application/xml")
	resp2, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusMultiStatus {
		b, _ := io.ReadAll(resp2.Body)
		t.Fatalf("PROPFIND = %d，期望 207；body=%s", resp2.StatusCode, string(b))
	}
	if !strings.Contains(strings.ToLower(resp2.Status), "multi-status") {
		t.Logf("status=%s", resp2.Status)
	}
	_ = path.Base("/")
}

func TestClientProjectFileAPIs(t *testing.T) {
	st := &fakeWebDAVDevice{
		files: map[string]string{"a.txt": "line1\nline2\n", "sub/b.txt": "bee"},
		dirs:  map[string]bool{"": true, "sub": true},
	}
	startFakeWebDAVDevice(t, "1", "usr", "dev-files", `/srv/files`, st)
	loc := projectLocator{ID: "p-files", Provider: smProviderClient, DeviceID: "dev-files", RootPath: `/srv/files`}

	req := func() *http.Request {
		r := httptest.NewRequest(http.MethodGet, "/v1/session-map/projects/p-files/files", nil)
		return r.WithContext(auth.WithClaims(r.Context(), &auth.Claims{UserID: "usr", TenantID: 1}))
	}

	// list
	rec := httptest.NewRecorder()
	clientListProjectFiles(rec, req(), loc, "", false)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "a.txt") || !strings.Contains(rec.Body.String(), `"dir"`) {
		t.Fatalf("list = %d %s", rec.Code, rec.Body.String())
	}

	// read window
	rec = httptest.NewRecorder()
	clientReadProjectFile(rec, req(), loc, "a.txt", 0, 10)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "line1") {
		t.Fatalf("read = %d %s", rec.Code, rec.Body.String())
	}

	// search
	rec = httptest.NewRecorder()
	clientSearchProjectFiles(rec, req(), loc, "b.txt", 0)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "sub/b.txt") {
		t.Fatalf("search = %d %s", rec.Code, rec.Body.String())
	}

	// raw
	rec = httptest.NewRecorder()
	clientRawProjectFile(rec, req(), loc, "a.txt")
	if rec.Code != http.StatusOK || rec.Body.String() != "line1\nline2\n" {
		t.Fatalf("raw = %d %q", rec.Code, rec.Body.String())
	}
}
