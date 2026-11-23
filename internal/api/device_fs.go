package api

// ── 设备文件桥（S1，只读）──
//
// 远程/团队模式下，项目文件在**用户设备**上；引擎与网关却跑在服务端。这里提供一条
// 由桌面端主动外连的 WebSocket 隧道：
//
//	远端 ProjectFS ──► deviceHub ──► /ws/device-fs ──► Electron 主进程 ──► 本机磁盘
//
// 设计约束（与 docs/prd-remote-execution-architecture.md §4 对齐）：
//   - 只读：v1 只有 canonicalize/stat/list/read；写与 git 在 S2。
//   - 身份：复用登录 token（与 /ws/rpa 同一口径），device_id 由客户端上报并只在该
//     用户/租户内生效；跨用户不可见。
//   - 服务端不信任客户端路径：所有 op 都带 root，由**客户端**做包含校验（路径语义
//     属于客户端 OS），服务端只负责把结果转给 ProjectFS。
//   - 单实例：连接表在进程内。多副本路由在 S3 引擎池时改 Redis pub/sub（TODO）。

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/gorilla/websocket"
)

var errDeviceOffline = errors.New("device_offline")

const (
	deviceFSProtocolVersion = 1
	deviceOpTimeout         = 15 * time.Second
	deviceReadMaxBytes      = 8 << 20 // 8MiB：WebDAV 整文件读取的上限
	deviceReadDeadline      = 90 * time.Second
	deviceWriteTimeout      = 10 * time.Second
)

type deviceStat struct {
	Size  int64  `json:"size"`
	IsDir bool   `json:"is_dir"`
	Mode  string `json:"mode,omitempty"`
}

type deviceEntry struct {
	Name  string `json:"name"`
	IsDir bool   `json:"is_dir"`
	Size  int64  `json:"size"`
}

type deviceOpRequest struct {
	ID           string `json:"id"`
	Op           string `json:"op"` // canonicalize | stat | list | read | walk | hash | write
	Root         string `json:"root,omitempty"`
	Path         string `json:"path,omitempty"`
	MaxBytes     int64  `json:"max_bytes,omitempty"`
	Offset       int64  `json:"offset,omitempty"`
	To           string `json:"to,omitempty"`
	DataB64      string `json:"data_b64,omitempty"`
	ExpectedHash string `json:"expected_hash,omitempty"`
}

type deviceOpResponse struct {
	ID        string        `json:"id"`
	OK        bool          `json:"ok"`
	Error     string        `json:"error,omitempty"`
	Protocol  int           `json:"protocol,omitempty"`
	RootKey   string        `json:"root_key,omitempty"`
	CleanPath string        `json:"clean_path,omitempty"`
	Exists    bool          `json:"exists,omitempty"`
	IsDir     bool          `json:"is_dir,omitempty"`
	Stat      *deviceStat   `json:"stat,omitempty"`
	DataB64   string        `json:"data_b64,omitempty"`
	Truncated bool          `json:"truncated,omitempty"`
	Entries   []deviceEntry `json:"entries,omitempty"`
	Hash      string        `json:"hash,omitempty"`
}

type deviceConn struct {
	tenant string
	user   string
	device string
	ws     *websocket.Conn

	writeMu sync.Mutex
	pendMu  sync.Mutex
	pending map[string]chan deviceOpResponse
	closed  chan struct{}
	once    sync.Once
}

func (c *deviceConn) close() {
	c.once.Do(func() {
		close(c.closed)
		_ = c.ws.Close()
		c.pendMu.Lock()
		for id, ch := range c.pending {
			ch <- deviceOpResponse{ID: id, OK: false, Error: "device disconnected"}
			close(ch)
			delete(c.pending, id)
		}
		c.pendMu.Unlock()
	})
}

type deviceHub struct {
	mu    sync.Mutex
	conns map[string]*deviceConn
}

func newDeviceHub() *deviceHub {
	return &deviceHub{conns: map[string]*deviceConn{}}
}

// deviceHubSingleton 进程内共享：项目创建/@引用与 WS 处理必须看到同一张连接表。
var deviceHubSingleton = newDeviceHub()

func deviceKey(tenant, user, device string) string {
	return tenant + "\x00" + user + "\x00" + device
}

func (h *deviceHub) register(conn *deviceConn) {
	key := deviceKey(conn.tenant, conn.user, conn.device)
	h.mu.Lock()
	old := h.conns[key]
	h.conns[key] = conn
	h.mu.Unlock()
	if old != nil && old != conn {
		// 同一设备重连：旧连接必须让位，否则请求会随机落在死连接上。
		slog.Info("device fs: replacing existing connection", "user", conn.user, "device", conn.device)
		old.close()
	}
}

func (h *deviceHub) unregister(conn *deviceConn) {
	key := deviceKey(conn.tenant, conn.user, conn.device)
	h.mu.Lock()
	if h.conns[key] == conn {
		delete(h.conns, key)
	}
	h.mu.Unlock()
	conn.close()
}

func (h *deviceHub) lookup(tenant, user, device string) (*deviceConn, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	c, ok := h.conns[deviceKey(tenant, user, device)]
	return c, ok
}

// Online 给 UI/创建项目做"设备是否在线"判断。
func (h *deviceHub) Online(tenant, user, device string) bool {
	_, ok := h.lookup(tenant, user, device)
	return ok
}

func newDeviceOpID() string {
	var b [12]byte
	if _, err := rand.Read(b[:]); err != nil {
		return time.Now().Format("150405.000000")
	}
	return hex.EncodeToString(b[:])
}

// Call 向指定设备发送一次只读操作，等待响应或超时。
func (h *deviceHub) Call(ctx context.Context, tenant, user, device string, req deviceOpRequest) (deviceOpResponse, error) {
	conn, ok := h.lookup(tenant, user, device)
	if !ok {
		return deviceOpResponse{}, errDeviceOffline
	}
	if req.ID == "" {
		req.ID = newDeviceOpID()
	}
	if req.MaxBytes <= 0 || req.MaxBytes > deviceReadMaxBytes {
		req.MaxBytes = deviceReadMaxBytes
	}
	ch := make(chan deviceOpResponse, 1)
	conn.pendMu.Lock()
	conn.pending[req.ID] = ch
	conn.pendMu.Unlock()
	defer func() {
		conn.pendMu.Lock()
		delete(conn.pending, req.ID)
		conn.pendMu.Unlock()
	}()

	conn.writeMu.Lock()
	_ = conn.ws.SetWriteDeadline(time.Now().Add(deviceWriteTimeout))
	err := conn.ws.WriteJSON(req)
	conn.writeMu.Unlock()
	if err != nil {
		conn.close()
		return deviceOpResponse{}, errDeviceOffline
	}

	timer := time.NewTimer(deviceOpTimeout)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return deviceOpResponse{}, ctx.Err()
	case <-timer.C:
		return deviceOpResponse{}, errors.New("device_fs_timeout")
	case <-conn.closed:
		return deviceOpResponse{}, errDeviceOffline
	case resp := <-ch:
		if !resp.OK {
			if resp.Error == "" {
				resp.Error = "device_fs_error"
			}
			return resp, errors.New(resp.Error)
		}
		return resp, nil
	}
}

var deviceFSUpgrader = websocket.Upgrader{
	ReadBufferSize:  8192,
	WriteBufferSize: 8192,
	CheckOrigin:     checkWebSocketOrigin,
}

// DeviceFSHandler 是 /ws/device-fs 的入口（桌面端主动外连）。
func DeviceFSHandler(hub *deviceHub, authenticator *auth.Authenticator) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		token := r.URL.Query().Get("token")
		deviceID := r.URL.Query().Get("device_id")
		if deviceID == "" {
			http.Error(w, "device_id required", http.StatusBadRequest)
			return
		}
		claims, err := authenticator.ValidateToken(token)
		if err != nil || claims == nil {
			http.Error(w, "invalid or missing token", http.StatusUnauthorized)
			return
		}
		conn, err := deviceFSUpgrader.Upgrade(w, r, nil)
		if err != nil {
			slog.Warn("device fs upgrade failed", "error", err)
			return
		}
		dc := &deviceConn{
			tenant:  smTenant(claims),
			user:    claims.UserID,
			device:  deviceID,
			ws:      conn,
			pending: map[string]chan deviceOpResponse{},
			closed:  make(chan struct{}),
		}
		hub.register(dc)
		defer hub.unregister(dc)

		conn.SetReadDeadline(time.Now().Add(deviceReadDeadline))
		conn.SetPongHandler(func(string) error {
			return conn.SetReadDeadline(time.Now().Add(deviceReadDeadline))
		})
		hello := deviceOpResponse{
			ID:       "hello",
			OK:       true,
			Protocol: deviceFSProtocolVersion,
		}
		dc.writeMu.Lock()
		_ = conn.SetWriteDeadline(time.Now().Add(deviceWriteTimeout))
		_ = conn.WriteJSON(hello)
		dc.writeMu.Unlock()

		for {
			_, data, err := conn.ReadMessage()
			if err != nil {
				return
			}
			var resp deviceOpResponse
			if json.Unmarshal(data, &resp) != nil || resp.ID == "" {
				continue
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
	}
}
