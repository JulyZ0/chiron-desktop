package api

// client 项目 git 的设备桥回路：参数前缀、非零退出码语义与 porcelain 解析。

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/gorilla/websocket"
)

func TestClientGitViaDevice(t *testing.T) {
	hub := deviceHubSingleton
	var sawStaticFlags atomic.Bool

	respond := func(req deviceOpRequest, conn *websocket.Conn) {
		if req.Op != "git" {
			_ = conn.WriteJSON(deviceOpResponse{ID: req.ID, OK: false, Error: "unexpected op"})
			return
		}
		for _, a := range req.Args {
			if a == "--no-optional-locks" {
				sawStaticFlags.Store(true)
			}
		}
		joined := strings.Join(req.Args, " ")
		switch {
		case strings.Contains(joined, "status --porcelain=v1 -z"):
			_ = conn.WriteJSON(deviceOpResponse{ID: req.ID, OK: true, Code: 0,
				Stdout: " M a.go\x00?? new.txt\x00R  old.txt\x00old2.txt\x00"})
		case strings.Contains(joined, "symbolic-ref"):
			_ = conn.WriteJSON(deviceOpResponse{ID: req.ID, OK: true, Code: 0, Stdout: "main\n"})
		case strings.Contains(joined, "rev-parse --is-inside-work-tree"):
			_ = conn.WriteJSON(deviceOpResponse{ID: req.ID, OK: true, Code: 0, Stdout: "true\n"})
		case strings.Contains(joined, "rev-parse --show-toplevel"):
			_ = conn.WriteJSON(deviceOpResponse{ID: req.ID, OK: true, Code: 0, Stdout: "/repo\n"})
		case strings.Contains(joined, "rev-parse --short HEAD"):
			_ = conn.WriteJSON(deviceOpResponse{ID: req.ID, OK: true, Code: 0, Stdout: "abc123\n"})
		default:
			_ = conn.WriteJSON(deviceOpResponse{ID: req.ID, OK: true, Code: 1, Stderr: "unsupported"})
		}
	}

	server := httptest.NewServer(deviceServerHandler(t, hub, "1", "u", "dgit"))
	defer server.Close()

	wsURL := "ws" + strings.TrimPrefix(server.URL, "http")
	client, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer client.Close()
	go func() {
		for {
			var req deviceOpRequest
			if err := client.ReadJSON(&req); err != nil {
				return
			}
			respond(req, client)
		}
	}()

	waitOnline(t, hub, "1", "u", "dgit")

	req := httptest.NewRequest("GET", "/", nil)
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u", TenantID: 1}))
	loc := projectLocator{ID: "p1", Provider: smProviderClient, DeviceID: "dgit", RootPath: "/repo"}

	vcs, err := clientProjectVCS(req, loc)
	if err != nil {
		t.Fatalf("vcs: %v", err)
	}
	if !vcs.Repo || !vcs.OwnRoot || vcs.RepoRoot != "/repo" {
		t.Fatalf("unexpected vcs: %+v", vcs)
	}

	changes, err := clientProjectChanges(req, loc)
	if err != nil {
		t.Fatalf("changes: %v", err)
	}
	if !changes.Repo || changes.Branch != "main" || changes.Head != "abc123" {
		t.Fatalf("unexpected changes header: %+v", changes)
	}
	if len(changes.Changes) != 3 {
		t.Fatalf("expected 3 changes, got %+v", changes.Changes)
	}
	if changes.Changes[0].Status != "M" || changes.Changes[1].Status != "?" {
		t.Fatalf("unexpected statuses: %+v", changes.Changes)
	}
	if changes.Changes[2].OldPath != "old2.txt" || changes.Changes[2].Path != "old.txt" {
		t.Fatalf("rename paths parsed wrong: %+v", changes.Changes[2])
	}
	if !sawStaticFlags.Load() {
		t.Fatal("git 调用必须带 staticGitFlags 前缀")
	}
}

// deviceServerHandler 模拟 DeviceFSHandler 的连接侧：注册设备并分发设备回包。
func deviceServerHandler(t *testing.T, hub *deviceHub, tenant, user, device string) http.Handler {
	t.Helper()
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		up := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
		conn, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		dc := &deviceConn{
			tenant: tenant, user: user, device: device, ws: conn,
			pending: map[string]chan deviceOpResponse{}, closed: make(chan struct{}),
		}
		hub.register(dc)
		t.Cleanup(func() { hub.unregister(dc) })
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
	})
}

func waitOnline(t *testing.T, hub *deviceHub, tenant, user, device string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for !hub.Online(tenant, user, device) && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if !hub.Online(tenant, user, device) {
		t.Fatal("device did not register")
	}
}
