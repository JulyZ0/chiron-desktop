package api

// S1 设备文件桥的回归：路径拼接/越界拒绝/离线语义，以及 P1 长连接稳定性
// （心跳保活、重连替换、忙时不断线、断线排空 pending）。
// 客户端真实读写仍由 electron/__tests__/device-fs.test.cjs 覆盖。

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/gorilla/websocket"
)

// ── 测试脚手架：真实 WebSocket + 真实 DeviceFSHandler ──

type deviceFSTestEnv struct {
	hub     *deviceHub
	srv     *httptest.Server
	token   string
	tickets *deviceTicketStore
	tenant  string
}

// newDeviceFSTestEnv 用毫秒级时间参数起一个真实的 /ws/device-fs 端点。
// 时间参数只影响测试 hub，生产默认值（30s/90s/10s）不受影响。
func newDeviceFSTestEnv(t *testing.T, timings deviceFSTimings) *deviceFSTestEnv {
	t.Helper()
	previousRedis := db.Redis
	db.Redis = newWSAuthTestRedis(t)
	t.Cleanup(func() { db.Redis = previousRedis })
	hub := newDeviceHub()
	if timings != (deviceFSTimings{}) {
		hub.timings = timings
	}
	a := auth.NewAuthenticator("device-fs-test-secret", time.Hour)
	token, err := a.GenerateToken("u", "u@example.com", "user", 1, nil)
	if err != nil {
		t.Fatalf("签发测试 token: %v", err)
	}
	// H46：WS 只认票据（`?ticket=`），query token 已删除。
	tickets := newDeviceTicketStore()
	srv := httptest.NewServer(DeviceFSHandler(hub, tickets))
	t.Cleanup(srv.Close)
	return &deviceFSTestEnv{hub: hub, srv: srv, token: token, tickets: tickets, tenant: "1"}
}

// fakeDevice 是桌面端的替身：读请求、按 handler 回包（handler 为 nil 表示只收不回，
// 等价于设备卡住/连接已僵死）。
type fakeDevice struct {
	conn *websocket.Conn
	mu   sync.Mutex
}

func (d *fakeDevice) write(resp deviceOpResponse) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.conn.WriteJSON(resp)
}

func (e *deviceFSTestEnv) dial(t *testing.T, deviceID string, handler func(deviceOpRequest) *deviceOpResponse) *fakeDevice {
	t.Helper()
	ticket, _ := e.tickets.issue(deviceTicket{
		tenant: e.tenant,
		user:   "u",
		device: deviceID,
		keyID:  "device-fs-test-key",
	}, deviceTicketTTL)
	wsURL := "ws" + strings.TrimPrefix(e.srv.URL, "http") + "/?ticket=" + url.QueryEscape(ticket)
	conn, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	dev := &fakeDevice{conn: conn}
	t.Cleanup(func() { _ = conn.Close() })
	go func() {
		for {
			var req deviceOpRequest
			if err := conn.ReadJSON(&req); err != nil {
				return
			}
			if handler == nil {
				continue
			}
			go func(req deviceOpRequest) {
				resp := handler(req)
				if resp == nil {
					return
				}
				// 回包失败只可能是连接已经断了，测试断言由调用侧完成。
				_ = dev.write(*resp)
			}(req)
		}
	}()
	return dev
}

func (e *deviceFSTestEnv) connFor(t *testing.T, deviceID string) *deviceConn {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		e.hub.mu.Lock()
		for _, c := range e.hub.conns {
			if c.device == deviceID {
				e.hub.mu.Unlock()
				return c
			}
		}
		e.hub.mu.Unlock()
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("设备 %s 未注册到 hub", deviceID)
	return nil
}

func (e *deviceFSTestEnv) online(deviceID string) bool {
	e.hub.mu.Lock()
	defer e.hub.mu.Unlock()
	for _, c := range e.hub.conns {
		if c.device == deviceID {
			return true
		}
	}
	return false
}

func waitPending(t *testing.T, conn *deviceConn, want int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if conn.pendingCount() >= want {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("等待 pending=%d 超时（当前 %d）", want, conn.pendingCount())
}

// ── 1) 心跳保活：超过读超时仍然在线 ──

func TestDeviceFSHeartbeatKeepsIdleConnOnline(t *testing.T) {
	env := newDeviceFSTestEnv(t, deviceFSTimings{
		readDeadline: 150 * time.Millisecond,
		heartbeat:    30 * time.Millisecond,
		writeTimeout: time.Second,
	})
	env.dial(t, "dev-idle", func(req deviceOpRequest) *deviceOpResponse {
		return &deviceOpResponse{ID: req.ID, OK: true, Stat: &deviceStat{Size: 11, Mode: "regular"}}
	})
	conn := env.connFor(t, "dev-idle")

	// 静默 5 倍读超时。没有 ping/pong 保活时，读 deadline 早就到期了。
	time.Sleep(5 * 150 * time.Millisecond)

	if !env.online("dev-idle") {
		t.Fatal("空闲超过读超时后被误判离线（心跳未生效）")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	resp, err := env.hub.Call(ctx, conn.tenant, conn.user, conn.device, deviceOpRequest{Op: "stat", Path: "/x"})
	if err != nil {
		t.Fatalf("心跳保活后调用失败: %v", err)
	}
	if resp.Stat == nil || resp.Stat.Size != 11 {
		t.Fatalf("响应异常: %+v", resp)
	}
}

// ── 2) 同设备重连：旧连接被替换，旧在途请求立刻拿到离线 ──

func TestDeviceFSReconnectReplacesOldConn(t *testing.T) {
	env := newDeviceFSTestEnv(t, deviceFSTimings{
		readDeadline: 2 * time.Second,
		heartbeat:    100 * time.Millisecond,
		writeTimeout: time.Second,
	})
	// 旧连接只收不回：请求会一直挂在 pending 上，正好用来验证替换语义。
	env.dial(t, "dev-r", nil)
	oldConn := env.connFor(t, "dev-r")

	errCh := make(chan error, 1)
	go func() {
		_, err := env.hub.Call(context.Background(), oldConn.tenant, oldConn.user, oldConn.device,
			deviceOpRequest{Op: "walk", Path: "/big"})
		errCh <- err
	}()
	waitPending(t, oldConn, 1)

	// 同一设备（同 tenant/user/device）重连：旧连接必须让位。
	env.dial(t, "dev-r", func(req deviceOpRequest) *deviceOpResponse {
		return &deviceOpResponse{ID: req.ID, OK: true, Stat: &deviceStat{Size: 7, Mode: "regular"}}
	})

	select {
	case err := <-errCh:
		if !errors.Is(err, errDeviceOffline) {
			t.Fatalf("旧连接的在途请求应返回 errDeviceOffline，得到 %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("旧连接的在途请求挂死（未被替换机制中断）")
	}
	if n := oldConn.pendingCount(); n != 0 {
		t.Fatalf("旧连接 pending 未排空：%d", n)
	}
	if !env.online("dev-r") {
		t.Fatal("重连后设备应保持在线")
	}

	// 新连接接管请求。
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	resp, err := env.hub.Call(ctx, oldConn.tenant, oldConn.user, oldConn.device, deviceOpRequest{Op: "stat"})
	if err != nil {
		t.Fatalf("新连接调用失败: %v", err)
	}
	if resp.Stat == nil || resp.Stat.Size != 7 {
		t.Fatalf("重连后请求没有落到新连接: %+v", resp)
	}
}

// ── 3) 忙碌期间持续有回包：不会被误判离线（心跳故意拉到远超读超时）──

func TestDeviceFSBusyTrafficNotMarkedOffline(t *testing.T) {
	env := newDeviceFSTestEnv(t, deviceFSTimings{
		readDeadline: 200 * time.Millisecond,
		heartbeat:    10 * time.Second, // 关掉心跳：只能靠入站回包续期
		writeTimeout: time.Second,
	})
	bigRead := strings.Repeat("x", 1<<20) // 1MiB：模拟大 read
	env.dial(t, "dev-busy", func(req deviceOpRequest) *deviceOpResponse {
		time.Sleep(40 * time.Millisecond) // 模拟大目录 walk 的单次耗时
		if req.Op == "read" {
			return &deviceOpResponse{ID: req.ID, OK: true, DataB64: bigRead, Truncated: false}
		}
		return &deviceOpResponse{ID: req.ID, OK: true, Entries: []deviceEntry{{Name: "a.txt", Size: 1}}}
	})
	conn := env.connFor(t, "dev-busy")

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	// 连续 8 次 op，总时长（~320ms）已超过读超时的 2 倍。
	for i := 0; i < 8; i++ {
		if _, err := env.hub.Call(ctx, conn.tenant, conn.user, conn.device,
			deviceOpRequest{Op: "walk", Path: "/"}); err != nil {
			t.Fatalf("第 %d 次调用被误判离线: %v", i, err)
		}
	}
	// 大 read 也不能把连接打掉。
	if _, err := env.hub.Call(ctx, conn.tenant, conn.user, conn.device,
		deviceOpRequest{Op: "read", Path: "/big.bin", MaxBytes: 1 << 20}); err != nil {
		t.Fatalf("大 read 后连接异常: %v", err)
	}
	if !env.online("dev-busy") {
		t.Fatal("忙碌期间设备被判离线")
	}
}

// ── 4) 连接断开：所有 pending 请求都完成（不挂死、不泄漏）──

func TestDeviceFSDisconnectCompletesPending(t *testing.T) {
	env := newDeviceFSTestEnv(t, deviceFSTimings{
		readDeadline: 5 * time.Second,
		heartbeat:    100 * time.Millisecond,
		writeTimeout: time.Second,
	})
	dev := env.dial(t, "dev-d", nil)
	conn := env.connFor(t, "dev-d")

	const inflight = 3
	errCh := make(chan error, inflight)
	for i := 0; i < inflight; i++ {
		go func(i int) {
			_, err := env.hub.Call(context.Background(), conn.tenant, conn.user, conn.device,
				deviceOpRequest{Op: "walk", Path: "/", TimeoutMS: 30000})
			errCh <- err
		}(i)
	}
	waitPending(t, conn, inflight)

	// 设备侧断网（进程被杀/网络切换的最坏情况）。
	_ = dev.conn.Close()

	for i := 0; i < inflight; i++ {
		select {
		case err := <-errCh:
			if !errors.Is(err, errDeviceOffline) {
				t.Fatalf("断线后 pending 应返回 errDeviceOffline，得到 %v", err)
			}
		case <-time.After(5 * time.Second):
			t.Fatalf("断线后仍有 %d 个 pending 挂死", inflight-i)
		}
	}
	if n := conn.pendingCount(); n != 0 {
		t.Fatalf("断线后 pending 未清空：%d", n)
	}
	if env.online("dev-d") {
		t.Fatal("断线后设备应标记为离线")
	}
}

// ── 基础回归 ──

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

func TestDeviceConnHeartbeat(t *testing.T) {
	pong := make(chan struct{}, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		up := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
		conn, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		dc := &deviceConn{ws: conn, closed: make(chan struct{}), pending: map[string]chan deviceOpResponse{}}
		defer dc.close()
		_ = conn.SetReadDeadline(time.Now().Add(time.Second))
		conn.SetPongHandler(func(string) error {
			select {
			case pong <- struct{}{}:
			default:
			}
			return conn.SetReadDeadline(time.Now().Add(time.Second))
		})
		go heartbeatDeviceConn(dc, 10*time.Millisecond, time.Second)
		for {
			if _, _, err := conn.ReadMessage(); err != nil {
				return
			}
		}
	}))
	defer srv.Close()

	wsURL := "ws" + strings.TrimPrefix(srv.URL, "http")
	client, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer client.Close()
	go func() {
		for {
			if _, _, err := client.ReadMessage(); err != nil {
				return
			}
		}
	}()

	select {
	case <-pong:
	case <-time.After(500 * time.Millisecond):
		t.Fatal("设备连接未收到心跳 pong")
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

func TestDeviceFSTimingsDefaults(t *testing.T) {
	got := deviceFSTimings{}.withDefaults()
	if got.readDeadline != 90*time.Second || got.heartbeat != 30*time.Second || got.writeTimeout != 10*time.Second {
		t.Fatalf("生产默认值被改动: %+v", got)
	}
	if got.heartbeat*3 > got.readDeadline {
		t.Fatalf("心跳间隔应为读超时的 1/3 以内: %+v", got)
	}
	// hub 直接构造（零值）也必须拿到生产默认值。
	if h := (&deviceHub{}).timing(); h.readDeadline != 90*time.Second {
		t.Fatalf("零值 hub 未回落到默认时间参数: %+v", h)
	}
}
