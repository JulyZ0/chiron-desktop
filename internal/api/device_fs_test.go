package api

// S1 设备文件桥的纯逻辑回归：路径拼接/越界拒绝/离线语义。
// WebSocket 连接与客户端真实读写由 electron/__tests__/device-fs.test.cjs 覆盖。

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestDeviceHubRoundTrip(t *testing.T) {
	hub := newDeviceHub()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		up := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
		conn, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		dc := &deviceConn{
			tenant: "t", user: "u", device: "d", ws: conn,
			pending: map[string]chan deviceOpResponse{}, closed: make(chan struct{}),
		}
		hub.register(dc)
		go func() {
			// 模拟 DeviceFSHandler 的读循环：把设备的回包分发给 pending。
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
	defer srv.Close()

	wsURL := "ws" + strings.TrimPrefix(srv.URL, "http")
	client, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer client.Close()

	// 模拟桌面设备：收到 op 就回一条固定 stat。
	go func() {
		for {
			var req deviceOpRequest
			if err := client.ReadJSON(&req); err != nil {
				return
			}
			_ = client.WriteJSON(deviceOpResponse{
				ID:   req.ID,
				OK:   true,
				Stat: &deviceStat{Size: 3, IsDir: false, Mode: "regular"},
			})
		}
	}()

	deadline := time.Now().Add(2 * time.Second)
	for !hub.Online("t", "u", "d") && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if !hub.Online("t", "u", "d") {
		t.Fatal("设备未注册到 hub")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	resp, err := hub.Call(ctx, "t", "u", "d", deviceOpRequest{Op: "stat", Path: "/x"})
	if err != nil {
		t.Fatalf("call: %v", err)
	}
	if resp.Stat == nil || resp.Stat.Size != 3 || resp.Stat.Mode != "regular" {
		t.Fatalf("unexpected response: %+v", resp)
	}
}

func TestJoinClientPath(t *testing.T) {
	cases := []struct {
		root, rel, want string
		ok              bool
	}{
		{`D:\Work\App`, `src\main.go`, `D:\Work\App\src\main.go`, true},
		{`D:\Work\App`, `src/main.go`, `D:\Work\App\src\main.go`, true},
		{`/srv/app`, `src/main.go`, `/srv/app/src/main.go`, true},
		{`/srv/app`, `../etc/passwd`, "", false},
		{`/srv/app`, `a/../../etc`, "", false},
		{`/srv/app`, `/etc/passwd`, `/srv/app/etc/passwd`, true},
	}
	for _, tc := range cases {
		got, ok := joinClientPath(tc.root, tc.rel)
		if ok != tc.ok || got != tc.want {
			t.Fatalf("joinClientPath(%q,%q) = (%q,%v), want (%q,%v)", tc.root, tc.rel, got, ok, tc.want, tc.ok)
		}
	}
}

func TestDeviceHubOffline(t *testing.T) {
	_, err := deviceHubSingleton.Call(context.Background(), "tenant", "user", "no-such-device", deviceOpRequest{Op: "stat"})
	if !errors.Is(err, errDeviceOffline) {
		t.Fatalf("离线设备应返回 errDeviceOffline，得到 %v", err)
	}
}
