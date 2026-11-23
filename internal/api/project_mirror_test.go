package api

// S2 项目镜像的纯逻辑与端到端回归：相对路径安全、清单读写、设备↔镜像同步。

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestSafeMirrorRel(t *testing.T) {
	ok := []string{"a.go", "src/main.go", `win\dir\file.txt`}
	for _, rel := range ok {
		if got, valid := safeMirrorRel(rel); !valid || got == "" {
			t.Fatalf("safeMirrorRel(%q) 应通过", rel)
		}
	}
	bad := []string{"", "..", "../etc/passwd", "a/../../b", "./a", "a//b"}
	for _, rel := range bad {
		if _, valid := safeMirrorRel(rel); valid {
			t.Fatalf("safeMirrorRel(%q) 应拒绝", rel)
		}
	}
}

func TestMirrorManifestRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "m.json")
	want := map[string]string{"a.go": "abc", "src/b.go": "def"}
	if err := saveMirrorManifest(path, want); err != nil {
		t.Fatalf("save: %v", err)
	}
	got, err := loadMirrorManifest(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if len(got) != len(want) || got["a.go"] != "abc" || got["src/b.go"] != "def" {
		t.Fatalf("manifest roundtrip mismatch: %+v", got)
	}
	if err := os.WriteFile(path, []byte("{bad"), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err = loadMirrorManifest(path)
	if err != nil || len(got) != 0 {
		t.Fatalf("坏清单应返回空 map,nil；得到 %+v,%v", got, err)
	}
}

// fakeProjectDevice 启动一个最小设备：walk/read/write 三个 op，内容存内存。
func fakeProjectDevice(t *testing.T, tenant, user, device string, files map[string]string) {
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
		// 服务端读回包并分发给 pending（与 DeviceFSHandler 同构）
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
	// 客户端扮演设备：读 op、执行、回包
	go func() {
		for {
			var req deviceOpRequest
			if err := client.ReadJSON(&req); err != nil {
				return
			}
			resp := deviceOpResponse{ID: req.ID}
			switch req.Op {
			case "walk":
				resp.OK = true
				for name := range files {
					resp.Entries = append(resp.Entries, deviceEntry{Name: name})
				}
			case "read":
				rel := strings.TrimPrefix(strings.ReplaceAll(req.Path, `\`, "/"), "D:/proj/")
				if content, ok := files[rel]; ok {
					resp.OK = true
					resp.DataB64 = base64.StdEncoding.EncodeToString([]byte(content))
				} else {
					resp.Error = "not found"
				}
			case "write":
				rel := strings.TrimPrefix(strings.ReplaceAll(req.Path, `\`, "/"), "D:/proj/")
				data, _ := base64.StdEncoding.DecodeString(req.DataB64)
				if req.ExpectedHash != "" {
					sum := sha256.Sum256([]byte(files[rel]))
					if hex.EncodeToString(sum[:]) != req.ExpectedHash {
						resp.Error = "conflict"
						break
					}
				}
				files[rel] = string(data)
				sum := sha256.Sum256(data)
				resp.OK = true
				resp.Hash = hex.EncodeToString(sum[:])
			case "hash":
				rel := strings.TrimPrefix(strings.ReplaceAll(req.Path, `\`, "/"), "D:/proj/")
				content, ok := files[rel]
				if !ok {
					resp.Error = "not found"
					break
				}
				sum := sha256.Sum256([]byte(content))
				resp.OK, resp.Hash = true, hex.EncodeToString(sum[:])
			case "remove":
				rel := strings.TrimPrefix(strings.ReplaceAll(req.Path, `\`, "/"), "D:/proj/")
				delete(files, rel)
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

func TestProjectMirrorEnsureAndSyncBack(t *testing.T) {
	t.Setenv("CHIRON_MIRROR_ROOT", t.TempDir())
	files := map[string]string{"a.txt": "hello"}
	fakeProjectDevice(t, "ten", "usr", "dev-mirror", files)

	loc := projectLocator{
		ID: "proj-mirror-test", Provider: smProviderClient, DeviceID: "dev-mirror",
		RootPath: `D:\proj`,
	}
	root, err := ensureProjectMirror(context.Background(), "ten", "usr", loc)
	if err != nil {
		t.Fatalf("ensure mirror: %v", err)
	}
	if got, _ := os.ReadFile(filepath.Join(root, "a.txt")); string(got) != "hello" {
		t.Fatalf("镜像内容 = %q", got)
	}

	// agent 在镜像里改文件 → 回写设备
	if err := os.WriteFile(filepath.Join(root, "a.txt"), []byte("changed"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := syncProjectMirrorBack(context.Background(), "ten", "usr", loc); err != nil {
		t.Fatalf("sync back: %v", err)
	}
	if files["a.txt"] != "changed" {
		t.Fatalf("设备文件未回写: %q", files["a.txt"])
	}

	// 设备侧被并发修改 → 再回写必须冲突（不覆盖）
	files["a.txt"] = "device-edit"
	if err := os.WriteFile(filepath.Join(root, "a.txt"), []byte("agent-edit"), 0o600); err != nil {
		t.Fatal(err)
	}
	err = syncProjectMirrorBack(context.Background(), "ten", "usr", loc)
	if err == nil || !strings.Contains(err.Error(), "conflict") {
		t.Fatalf("应报 conflict，得到 %v", err)
	}
	if files["a.txt"] != "device-edit" {
		t.Fatalf("冲突时不得覆盖设备文件，得到 %q", files["a.txt"])
	}

	// 设备侧改回基线内容后，删除镜像文件 → 设备也应删除
	files["a.txt"] = "changed"
	if err := os.Remove(filepath.Join(root, "a.txt")); err != nil {
		t.Fatal(err)
	}
	if err := syncProjectMirrorBack(context.Background(), "ten", "usr", loc); err != nil {
		t.Fatalf("delete sync: %v", err)
	}
	if _, ok := files["a.txt"]; ok {
		t.Fatal("镜像删除后设备文件应被删除")
	}
}
