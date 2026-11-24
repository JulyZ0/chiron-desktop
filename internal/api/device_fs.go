package api

// ── 设备文件桥（S1，只读）──
//
// 远程/团队模式下，项目文件在**用户设备**上；引擎与网关却跑在服务端。这里提供一条
// 由桌面端主动外连的 WebSocket 隧道：
//
//	远端 ProjectFS ──► deviceHub ──► /ws/device-fs ──► Electron 主进程 ──► 本机磁盘
//
// 设计约束（与 docs/prd-remote-team.md §2/§4 对齐）：
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
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/gorilla/websocket"
)

var errDeviceOffline = errors.New("device_offline")

const (
	deviceFSProtocolVersion = 1
	deviceOpTimeout         = 15 * time.Second
	deviceReadMaxBytes      = 8 << 20 // 8MiB：WebDAV 整文件读取的上限
	deviceReadDeadline      = 90 * time.Second
	deviceHeartbeatInterval = 30 * time.Second
	deviceWriteTimeout      = 10 * time.Second
	// deviceDisconnectedMsg 是连接被替换/断开时回给在途请求的文案。
	// callConn 会把它映射回 errDeviceOffline，调用方据此走"设备离线"分支，
	// 而不是当成设备返回的业务错误。
	deviceDisconnectedMsg = "device disconnected"
)

// deviceFSTimings 把连接层的时间参数集中到一处。
//
// 生产默认值保持"心跳间隔是读超时的 1/3"：30s 的 ping 让空闲连接不被 NAT/代理掐断，
// 90s 读超时则保证真正掉线在三倍心跳内被发现。测试可以把 hub 的时间参数缩到毫秒级，
// 用来验证"超过读超时仍在线""大 op 期间不被误判离线"这类长时间行为，而不改生产默认。
type deviceFSTimings struct {
	readDeadline time.Duration
	heartbeat    time.Duration
	writeTimeout time.Duration
}

// withDefaults 给零值补生产默认值：测试只覆盖关心的那几项，其余仍是线上行为。
func (t deviceFSTimings) withDefaults() deviceFSTimings {
	if t.readDeadline <= 0 {
		t.readDeadline = deviceReadDeadline
	}
	if t.heartbeat <= 0 {
		t.heartbeat = deviceHeartbeatInterval
	}
	if t.writeTimeout <= 0 {
		t.writeTimeout = deviceWriteTimeout
	}
	return t
}

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
	Op           string `json:"op"` // canonicalize | stat | list | read | walk | hash | write | mkdir | remove | rename | git
	Root         string `json:"root,omitempty"`
	Path         string `json:"path,omitempty"`
	To           string `json:"to,omitempty"`
	MaxBytes     int64  `json:"max_bytes,omitempty"`
	Offset       int64  `json:"offset,omitempty"`
	DataB64      string `json:"data_b64,omitempty"`
	ExpectedHash string `json:"expected_hash,omitempty"`
	// Args 只用于 git op：由服务端构造，设备端不解释其语义，只负责在项目根内执行。
	Args []string `json:"args,omitempty"`
	// TimeoutMS 是 git op 的执行上限（毫秒）；缺省/越界时设备端用自己的默认值。
	TimeoutMS int `json:"timeout_ms,omitempty"`
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
	// git op 的结果：非零 Code 不是传输错误（"不是仓库"等语义由调用方解释）。
	Code   int    `json:"code,omitempty"`
	Stdout string `json:"stdout,omitempty"`
	Stderr string `json:"stderr,omitempty"`
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
			// 通道容量是 1，但读循环可能已经塞过一条（先删除再发送，理论上不会同时出现）；
			// 用 select 兜一层，保证 close 不会因为没人收而阻塞住整个 hub。
			select {
			case ch <- deviceOpResponse{ID: id, OK: false, Error: deviceDisconnectedMsg}:
			default:
			}
			close(ch)
			delete(c.pending, id)
		}
		c.pendMu.Unlock()
	})
}

// pendingCount 只给测试/诊断用：连接断开后这里必须回到 0。
func (c *deviceConn) pendingCount() int {
	c.pendMu.Lock()
	defer c.pendMu.Unlock()
	return len(c.pending)
}

type deviceHub struct {
	mu    sync.Mutex
	conns map[string]*deviceConn
	// timings 是连接层时间参数；零值走生产默认（见 deviceFSTimings.withDefaults）。
	timings deviceFSTimings

	// 多副本路由（R4）：连接在哪个网关实例上，请求就经 Redis 转给那个实例。
	rdb        db.RedisClient
	instanceID string
	routeOnce  sync.Once
	pendMu     sync.Mutex
	pending    map[string]chan routedResponse
}

type routedRequest struct {
	ID     string          `json:"id"`
	From   string          `json:"from"`
	Tenant string          `json:"tenant"`
	User   string          `json:"user"`
	Device string          `json:"device"`
	Req    deviceOpRequest `json:"req"`
}

type routedResponse struct {
	ID    string           `json:"id"`
	Resp  deviceOpResponse `json:"resp"`
	Error string           `json:"error,omitempty"`
}

func newDeviceHub() *deviceHub {
	return &deviceHub{
		conns:   map[string]*deviceConn{},
		pending: map[string]chan routedResponse{},
		timings: deviceFSTimings{}.withDefaults(),
	}
}

// timing 返回补全过默认值的时间参数（hub 由测试直接构造时也能安全使用）。
func (h *deviceHub) timing() deviceFSTimings {
	return h.timings.withDefaults()
}

// deviceHubSingleton 进程内共享：项目创建/@引用与 WS 处理必须看到同一张连接表。
var deviceHubSingleton = newDeviceHub()

const deviceOwnerTTL = 5 * time.Minute

func deviceOwnerKey(tenant, user, device string) string {
	return db.RedisKey("device:fs:owner:") + deviceKey(tenant, user, device)
}

func deviceInstanceID() string {
	host, _ := os.Hostname()
	return fmt.Sprintf("%s-%d", strings.NewReplacer(":", "-", " ", "-").Replace(host), os.Getpid())
}

// StartDeviceFSRouting 启用跨网关副本的设备路由（R4）。
//
// 连接只在持有它的实例上；其它实例发请求时用 Redis：
//
//	owner 键记录连接归属；req:<owner> 转发；resp:<from> 回包。
//
// 没有 Redis 时保持单实例行为。
func StartDeviceFSRouting(ctx context.Context, rdb db.RedisClient) {
	h := deviceHubSingleton
	if rdb == nil {
		return
	}
	h.rdb = rdb
	h.instanceID = deviceInstanceID()
	h.routeOnce.Do(func() {
		go h.subscribeRequests(ctx)
		go h.subscribeResponses(ctx)
	})
}

func (h *deviceHub) subscribeRequests(ctx context.Context) {
	ch := db.RedisKey("device:fs:req:") + h.instanceID
	ps := h.rdb.Subscribe(ctx, ch)
	defer ps.Close()
	for {
		select {
		case <-ctx.Done():
			return
		case msg, ok := <-ps.Channel():
			if !ok {
				return
			}
			h.handleRoutedRequest(ctx, msg.Payload)
		}
	}
}

func (h *deviceHub) subscribeResponses(ctx context.Context) {
	ch := db.RedisKey("device:fs:resp:") + h.instanceID
	ps := h.rdb.Subscribe(ctx, ch)
	defer ps.Close()
	for {
		select {
		case <-ctx.Done():
			return
		case msg, ok := <-ps.Channel():
			if !ok {
				return
			}
			var resp routedResponse
			if json.Unmarshal([]byte(msg.Payload), &resp) != nil {
				continue
			}
			h.pendMu.Lock()
			pending := h.pending[resp.ID]
			delete(h.pending, resp.ID)
			h.pendMu.Unlock()
			if pending != nil {
				pending <- resp
				close(pending)
			}
		}
	}
}

func (h *deviceHub) handleRoutedRequest(ctx context.Context, payload string) {
	var req routedRequest
	if json.Unmarshal([]byte(payload), &req) != nil || req.From == "" || req.ID == "" {
		return
	}
	conn, ok := h.lookup(req.Tenant, req.User, req.Device)
	var resp routedResponse
	resp.ID = req.ID
	if !ok {
		resp.Error = errDeviceOffline.Error()
	} else {
		rctx, cancel := context.WithTimeout(ctx, deviceCallTimeout(req.Req))
		defer cancel()
		out, err := h.callConn(rctx, conn, req.Req)
		resp.Resp = out
		if err != nil {
			resp.Error = err.Error()
		}
	}
	raw, _ := json.Marshal(resp)
	_ = h.rdb.Publish(ctx, db.RedisKey("device:fs:resp:")+req.From, raw)
}

func (h *deviceHub) claimOwner(conn *deviceConn) {
	if h.rdb == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := h.rdb.Set(ctx, deviceOwnerKey(conn.tenant, conn.user, conn.device), h.instanceID, deviceOwnerTTL).Err(); err != nil {
		slog.Warn("device fs owner claim failed", "error", err)
	}
}

func (h *deviceHub) refreshOwner(conn *deviceConn) {
	ticker := time.NewTicker(60 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-conn.closed:
			return
		case <-ticker.C:
			h.claimOwner(conn)
		}
	}
}

func (h *deviceHub) releaseOwner(conn *deviceConn) {
	if h.rdb == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	key := deviceOwnerKey(conn.tenant, conn.user, conn.device)
	if owner, err := h.rdb.Get(ctx, key).Result(); err == nil && owner == h.instanceID {
		_ = h.rdb.Del(ctx, key).Err()
	}
}

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
	h.claimOwner(conn)
	go h.refreshOwner(conn)
}

func (h *deviceHub) unregister(conn *deviceConn) {
	key := deviceKey(conn.tenant, conn.user, conn.device)
	h.mu.Lock()
	current := h.conns[key] == conn
	if current {
		delete(h.conns, key)
	}
	h.mu.Unlock()
	conn.close()
	// 只释放自己拥有的 owner 键：重连时新连接可能已经抢先写入，
	// 老连接的收尾不能把新归属删掉。
	if current {
		h.releaseOwner(conn)
	}
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

// deviceCallTimeout 是单次 op 的等待上限。
//
// 缺省 15s 够文件读写用；git 可能跑得更久（大仓库 status），请求里带 TimeoutMS 时
// 在它之上留 2s 余量，否则设备还在跑、网关已经放弃，回包会被当成超时丢弃。
func deviceCallTimeout(req deviceOpRequest) time.Duration {
	d := deviceOpTimeout
	if req.TimeoutMS > 0 {
		if want := time.Duration(req.TimeoutMS)*time.Millisecond + 2*time.Second; want > d {
			d = want
		}
	}
	return d
}

// Call 向指定设备发送一次操作；本实例没有连接时经 Redis 转给持有连接的副本。
func (h *deviceHub) Call(ctx context.Context, tenant, user, device string, req deviceOpRequest) (deviceOpResponse, error) {
	if conn, ok := h.lookup(tenant, user, device); ok {
		return h.callConn(ctx, conn, req)
	}
	return h.callRemote(ctx, tenant, user, device, req)
}

// 浏览器 WebSocket 不会主动发送协议 ping，服务端必须主动探活。
//
// 保活链路：这里发 ping → 客户端协议栈自动回 pong → PongHandler 续期读超时。
// 因此 deadline 只在"真的没有任何入站帧（含 pong）"时才会到期，空闲连接和
// 正在跑大 walk/read 的连接都不会被误判离线。
func heartbeatDeviceConn(conn *deviceConn, interval, writeTimeout time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-conn.closed:
			return
		case <-ticker.C:
			if err := conn.ws.WriteControl(websocket.PingMessage, nil, time.Now().Add(writeTimeout)); err != nil {
				slog.Info("device fs: heartbeat ping failed", "device", conn.device, "error", err)
				conn.close()
				return
			}
		}
	}
}

func (h *deviceHub) callConn(ctx context.Context, conn *deviceConn, req deviceOpRequest) (deviceOpResponse, error) {
	timing := h.timing()
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
	_ = conn.ws.SetWriteDeadline(time.Now().Add(timing.writeTimeout))
	err := conn.ws.WriteJSON(req)
	conn.writeMu.Unlock()
	if err != nil {
		conn.close()
		return deviceOpResponse{}, errDeviceOffline
	}
	// 请求发出时也续期一次：设备正在跑大 op（walk/git）时，这段时间里只有心跳 pong
	// 能刷新 deadline，提前续期可以让"请求刚发出就被判掉线"的边界窗口消失。
	_ = conn.ws.SetReadDeadline(time.Now().Add(timing.readDeadline))

	timer := time.NewTimer(deviceCallTimeout(req))
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
			// 连接被关闭/替换时排空的 pending 也走离线语义，调用方才能正确降级。
			if resp.Error == deviceDisconnectedMsg || resp.Error == errDeviceOffline.Error() {
				return resp, errDeviceOffline
			}
			return resp, errors.New(resp.Error)
		}
		return resp, nil
	}
}

// callRemote 把 op 发给持有设备连接的网关副本，并等待它的回包。
//
// 连接归属由 `device:fs:owner:*` 记录；请求走 `req:<owner>`，回包走 `resp:<本实例>`。
// Redis 不可用/归属缺失时按离线处理 —— 不猜、不重试到别的实例。
func (h *deviceHub) callRemote(ctx context.Context, tenant, user, device string, req deviceOpRequest) (deviceOpResponse, error) {
	if h.rdb == nil || h.instanceID == "" {
		return deviceOpResponse{}, errDeviceOffline
	}
	owner, err := h.rdb.Get(ctx, deviceOwnerKey(tenant, user, device)).Result()
	if err != nil || owner == "" || owner == h.instanceID {
		return deviceOpResponse{}, errDeviceOffline
	}
	if req.ID == "" {
		req.ID = newDeviceOpID()
	}
	ch := make(chan routedResponse, 1)
	h.pendMu.Lock()
	h.pending[req.ID] = ch
	h.pendMu.Unlock()
	defer func() {
		h.pendMu.Lock()
		delete(h.pending, req.ID)
		h.pendMu.Unlock()
	}()

	payload, _ := json.Marshal(routedRequest{
		ID: req.ID, From: h.instanceID, Tenant: tenant, User: user, Device: device, Req: req,
	})
	if err := h.rdb.Publish(ctx, db.RedisKey("device:fs:req:")+owner, payload).Err(); err != nil {
		return deviceOpResponse{}, errDeviceOffline
	}
	timer := time.NewTimer(deviceCallTimeout(req))
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return deviceOpResponse{}, ctx.Err()
	case <-timer.C:
		return deviceOpResponse{}, errors.New("device_fs_timeout")
	case resp := <-ch:
		if resp.Error != "" {
			return resp.Resp, errors.New(resp.Error)
		}
		return resp.Resp, nil
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

		timing := hub.timing()
		conn.SetReadDeadline(time.Now().Add(timing.readDeadline))
		conn.SetPongHandler(func(string) error {
			return conn.SetReadDeadline(time.Now().Add(timing.readDeadline))
		})
		go heartbeatDeviceConn(dc, timing.heartbeat, timing.writeTimeout)
		hello := deviceOpResponse{
			ID:       "hello",
			OK:       true,
			Protocol: deviceFSProtocolVersion,
		}
		dc.writeMu.Lock()
		_ = conn.SetWriteDeadline(time.Now().Add(timing.writeTimeout))
		_ = conn.WriteJSON(hello)
		dc.writeMu.Unlock()

		for {
			_, data, err := conn.ReadMessage()
			if err != nil {
				// 读到错误（对端关闭 / 读超时 / hub 主动替换连接）即认为连接已死：
				// unregister 会排空 pending，把在途请求全部落成"设备离线"。
				return
			}
			// 任何入站消息都算活跃：大 walk/read 的连续回包、设备主动的其它帧
			// 都会续期，只有真正静默（连 pong 都没有）才会走到上面的超时分支。
			_ = conn.SetReadDeadline(time.Now().Add(timing.readDeadline))
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
