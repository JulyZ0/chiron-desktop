package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
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
	"github.com/redis/go-redis/v9"
)

type wsAuthTestRedis struct {
	db.RedisClient
	mu         sync.Mutex
	values     map[string]string
	getErr     error
	onPublish  func(string, string)
	pubsubCore *redis.Client
}

func newWSAuthTestRedis(t *testing.T) *wsAuthTestRedis {
	t.Helper()
	core := redis.NewClient(&redis.Options{
		Addr: "127.0.0.1:0",
		Dialer: func(context.Context, string, string) (net.Conn, error) {
			return nil, errors.New("test pubsub disabled")
		},
		MaxRetries:   -1,
		DialTimeout:  time.Millisecond,
		ReadTimeout:  time.Millisecond,
		WriteTimeout: time.Millisecond,
	})
	t.Cleanup(func() { _ = core.Close() })
	return &wsAuthTestRedis{values: make(map[string]string), pubsubCore: core}
}

func (r *wsAuthTestRedis) Get(_ context.Context, key string) *redis.StringCmd {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.getErr != nil {
		return redis.NewStringResult("", r.getErr)
	}
	value, ok := r.values[key]
	if !ok {
		return redis.NewStringResult("", redis.Nil)
	}
	return redis.NewStringResult(value, nil)
}

func (r *wsAuthTestRedis) Set(_ context.Context, key string, value interface{}, _ time.Duration) *redis.StatusCmd {
	r.mu.Lock()
	r.values[key] = fmt.Sprint(value)
	r.mu.Unlock()
	return redis.NewStatusResult("OK", nil)
}

func (r *wsAuthTestRedis) Exists(_ context.Context, keys ...string) *redis.IntCmd {
	r.mu.Lock()
	defer r.mu.Unlock()
	var count int64
	for _, key := range keys {
		if _, ok := r.values[key]; ok {
			count++
		}
	}
	return redis.NewIntResult(count, nil)
}

func (r *wsAuthTestRedis) Publish(_ context.Context, channel string, message interface{}) *redis.IntCmd {
	if r.onPublish != nil {
		r.onPublish(channel, message.(string))
	}
	return redis.NewIntResult(1, nil)
}

func (r *wsAuthTestRedis) Subscribe(ctx context.Context, channels ...string) *redis.PubSub {
	ps := r.pubsubCore.Subscribe(ctx, channels...)
	_ = ps.Close()
	return ps
}

type wsAuthTestEnv struct {
	auth      *auth.Authenticator
	token     string
	claims    *auth.Claims
	rpaHub    *RPAHub
	deviceHub *deviceHub
	tickets   *deviceTicketStore
	// lastKeyID 是最近一次换票用过的公钥指纹：WS 握手签票时必须一致，
	// 否则票据存储会把它判成"同一 device_id 换了公钥"从而拒绝签发。
	lastKeyID string
	server    *httptest.Server
}

func newWSAuthTestEnv(t *testing.T) *wsAuthTestEnv {
	t.Helper()
	a := auth.NewAuthenticator("ws-auth-test-secret", time.Hour)
	env := &wsAuthTestEnv{
		auth:      a,
		rpaHub:    NewRPAHub(nil),
		deviceHub: newDeviceHub(),
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /ws/rpa", RPAWebSocketHandler(env.rpaHub, a))
	// H46：设备桥只认一次性票据（query token 已删除），测试也要按新流程走。
	env.tickets = newDeviceTicketStore()
	mux.HandleFunc("GET /ws/device-fs", DeviceFSHandler(env.deviceHub, env.tickets))
	// H46：票据端点（覆盖 authMW 的撤销判定）
	mux.Handle("POST /v1/device-fs/ticket", AuthMiddleware(a)(http.HandlerFunc(
		DeviceFSTicketHandler(env.tickets, a))))
	protected := AuthMiddleware(a)(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	mux.Handle("GET /v1/business", protected)
	env.server = httptest.NewServer(mux)
	t.Cleanup(env.server.Close)
	return env
}

func (e *wsAuthTestEnv) issueToken(t *testing.T, mustChange bool) {
	t.Helper()
	e.issueTokenForUser(t, "ws-user", mustChange)
}

func (e *wsAuthTestEnv) issueTokenForUser(t *testing.T, userID string, mustChange bool) {
	t.Helper()
	var err error
	e.token, err = e.auth.GenerateTokenWithState(userID, "ws@example.com", "user", 1, nil, mustChange)
	if err != nil {
		t.Fatalf("签发 token: %v", err)
	}
	e.claims, err = e.auth.ValidateToken(e.token)
	if err != nil {
		t.Fatalf("解析 token: %v", err)
	}
}

func (e *wsAuthTestEnv) wsURL(path string) string {
	query := url.Values{}
	if path == "/ws/rpa" {
		// RPA 桥仍是 query token（H46 只改设备桥）。
		query.Set("token", e.token)
		query.Set("client_id", "ws-client")
	} else {
		// 设备桥：一次性票据；device_id 已钉在票据里，不再走 query。
		query.Set("ticket", e.deviceTicket("ws-device"))
	}
	return "ws" + strings.TrimPrefix(e.server.URL, "http") + path + "?" + query.Encode()
}

// ticketKeyID 返回本次测试使用的设备公钥指纹（换票时记录，未换票则用固定值）。
func (e *wsAuthTestEnv) ticketKeyID() string {
	if e.lastKeyID != "" {
		return e.lastKeyID
	}
	return "ws-auth-test-key"
}

// deviceTicket 直接向票据存储签发一张（跳过 HTTP 换票：这里测的是 WS 侧）。
func (e *wsAuthTestEnv) deviceTicket(deviceID string) string {
	ticket, _ := e.tickets.issue(deviceTicket{
		tenant: tenantStr(e.claims.TenantID),
		user:   e.claims.UserID,
		device: deviceID,
		keyID:  e.ticketKeyID(),
	}, deviceTicketTTL)
	return ticket
}

func (e *wsAuthTestEnv) httpStatus(t *testing.T) int {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/v1/business", nil)
	req.Header.Set("Authorization", "Bearer "+e.token)
	rec := httptest.NewRecorder()
	AuthMiddleware(e.auth)(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})).ServeHTTP(rec, req)
	return rec.Code
}

func assertWSHandshakeRejected(t *testing.T, env *wsAuthTestEnv, want int) {
	t.Helper()
	// H46：设备桥的凭据是**一次性票据**，与登录 token 的撤销语义不同（票据 ≤60s 自然过期，
	// 服务端要踢人是关连接）。所以这里只对仍用 token 的 /ws/rpa 断言握手被拒；
	// 设备桥的"拒绝无效/过期票据"由 device_fs_ticket_test.go 覆盖。
	for _, path := range []string{"/ws/rpa"} {
		conn, resp, err := websocket.DefaultDialer.Dial(env.wsURL(path), nil)
		if err == nil {
			_ = conn.Close()
			t.Fatalf("%s 撤销后仍建立连接", path)
		}
		if resp == nil || resp.StatusCode != want {
			t.Fatalf("%s 握手状态=%v，want %d（err=%v）", path, resp, want, err)
		}
		_ = resp.Body.Close()
	}
}

func dialWSAuth(t *testing.T, env *wsAuthTestEnv, path string) *websocket.Conn {
	t.Helper()
	conn, resp, err := websocket.DefaultDialer.Dial(env.wsURL(path), nil)
	if err != nil {
		t.Fatalf("建立 %s 连接: %v", path, err)
	}
	_ = resp.Body.Close()
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

func readWSHello(t *testing.T, conn *websocket.Conn, path string) {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	if path == "/ws/rpa" {
		var msg RPAMessage
		if err := conn.ReadJSON(&msg); err != nil || msg.Type != RPAMsgAck {
			t.Fatalf("RPA 初始化消息异常: msg=%+v err=%v", msg, err)
		}
		return
	}
	var hello deviceOpResponse
	if err := conn.ReadJSON(&hello); err != nil || !hello.OK || hello.ID != "hello" {
		t.Fatalf("device-fs 初始化消息异常: hello=%+v err=%v", hello, err)
	}
}

func assertWSClosed(t *testing.T, conn *websocket.Conn) {
	t.Helper()
	assertWSClosedBefore(t, conn, 2*time.Second)
}

func assertWSClosedBefore(t *testing.T, conn *websocket.Conn, timeout time.Duration) {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(timeout))
	for {
		_, _, err := conn.ReadMessage()
		if err == nil {
			continue
		}
		var closeErr *websocket.CloseError
		if !errors.As(err, &closeErr) {
			t.Fatalf("连接未通过 WebSocket close 断开: %v", err)
		}
		if closeErr.Code != websocket.ClosePolicyViolation && closeErr.Code != websocket.CloseInternalServerErr {
			t.Fatalf("非认证撤销关闭码: %v", closeErr)
		}
		return
	}
}

func waitWSAuthHubsEmpty(t *testing.T, env *wsAuthTestEnv) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		_, rpaOnline := env.rpaHub.GetClient("ws-client")
		if !rpaOnline && !env.deviceHub.Online("1", "ws-user", "ws-device") {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("连接撤销后仍注册在 hub")
}

func TestWebSocketHandshakeUsesHTTPRevocationSemantics(t *testing.T) {
	rdb := newWSAuthTestRedis(t)
	previous := db.Redis
	db.Redis = rdb
	t.Cleanup(func() { db.Redis = previous })

	actions := []string{"logout", "password_change", "disabled", "revoked", "force_logout"}
	envs := make(map[string]*wsAuthTestEnv, len(actions))
	var latestIssuedAt int64
	for _, action := range actions {
		env := newWSAuthTestEnv(t)
		env.issueTokenForUser(t, "ws-user-"+action, false)
		envs[action] = env
		if env.claims.IssuedAt.Unix() > latestIssuedAt {
			latestIssuedAt = env.claims.IssuedAt.Unix()
		}
	}
	if time.Now().Unix() <= latestIssuedAt {
		time.Sleep(time.Until(time.Unix(latestIssuedAt+1, 0)))
	}

	for _, action := range actions {
		t.Run(action, func(t *testing.T) {
			env := envs[action]
			if action == "logout" {
				rdb.Set(context.Background(), db.RedisKey("jwt:blacklist:")+env.claims.ID, "1", time.Hour)
				markJWTBlacklisted(env.claims.ID)
			} else if err := revokeUserSessions(context.Background(), env.claims.UserID); err != nil {
				t.Fatalf("写入会话纪元: %v", err)
			}
			if got := env.httpStatus(t); got != http.StatusUnauthorized {
				t.Fatalf("HTTP 状态=%d，want 401", got)
			}
			assertWSHandshakeRejected(t, env, http.StatusUnauthorized)
		})
	}
}

func TestWebSocketHandshakeMatchesMustChangePasswordPolicy(t *testing.T) {
	rdb := newWSAuthTestRedis(t)
	previous := db.Redis
	db.Redis = rdb
	t.Cleanup(func() { db.Redis = previous })

	env := newWSAuthTestEnv(t)
	env.issueToken(t, true)
	if got := env.httpStatus(t); got != http.StatusForbidden {
		t.Fatalf("HTTP 强制改密状态=%d，want 403", got)
	}
	assertWSHandshakeRejected(t, env, http.StatusForbidden)
}

func TestWebSocketHandshakeFailsClosedWithoutRevocationStore(t *testing.T) {
	previous := db.Redis
	db.Redis = nil
	t.Cleanup(func() { db.Redis = previous })

	env := newWSAuthTestEnv(t)
	env.issueToken(t, false)
	if got := env.httpStatus(t); got != http.StatusUnauthorized {
		t.Fatalf("HTTP 撤销存储不可用状态=%d，want 401", got)
	}
	assertWSHandshakeRejected(t, env, http.StatusUnauthorized)
}

func TestWebSocketHandshakeFailsClosedOnRedisError(t *testing.T) {
	rdb := newWSAuthTestRedis(t)
	previous := db.Redis
	db.Redis = rdb
	t.Cleanup(func() { db.Redis = previous })
	rdb.mu.Lock()
	rdb.getErr = errors.New("redis unavailable")
	rdb.mu.Unlock()

	env := newWSAuthTestEnv(t)
	env.issueToken(t, false)
	assertWSHandshakeRejected(t, env, http.StatusUnauthorized)
}

// 纪元精度是秒，而"改密成功 → 响应里签发新 token"就发生在同一秒内。
// 判定口径必须是 epoch > iat（严格大于）：否则改密后的新 token 会被立刻判死。
// WS 曾经用 epoch >= iat，导致同一条链路 HTTP 放行、WS 拒绝 —— 这里锁住对齐后的行为。
func TestWebSocketHandshakeAcceptsSameSecondEpochLikeHTTP(t *testing.T) {
	rdb := newWSAuthTestRedis(t)
	previous := db.Redis
	db.Redis = rdb
	t.Cleanup(func() { db.Redis = previous })

	env := newWSAuthTestEnv(t)
	env.issueToken(t, false)
	epoch := env.claims.IssuedAt.Unix()
	rdb.Set(context.Background(), db.RedisKey("jwt:epoch:")+env.claims.UserID, epoch, time.Hour)
	if got := env.httpStatus(t); got != http.StatusNoContent {
		t.Fatalf("HTTP 同秒纪元状态=%d，want 204", got)
	}
	conn := dialWSAuth(t, env, "/ws/rpa")
	readWSHello(t, conn, "/ws/rpa")
	_ = conn.Close()
}

func TestWebSocketHandshakeRejectsTokenOlderThanEpoch(t *testing.T) {
	rdb := newWSAuthTestRedis(t)
	previous := db.Redis
	db.Redis = rdb
	t.Cleanup(func() { db.Redis = previous })

	env := newWSAuthTestEnv(t)
	env.issueToken(t, false)
	rdb.Set(context.Background(), db.RedisKey("jwt:epoch:")+env.claims.UserID,
		env.claims.IssuedAt.Unix()+1, time.Hour)
	assertWSHandshakeRejected(t, env, http.StatusUnauthorized)
}

func TestWebSocketActiveConnectionsDisconnectOnEpochEvent(t *testing.T) {
	rdb := newWSAuthTestRedis(t)
	previousRedis, previousInterval := db.Redis, wsAuthRevalidateInterval
	db.Redis, wsAuthRevalidateInterval = rdb, time.Hour
	t.Cleanup(func() { db.Redis, wsAuthRevalidateInterval = previousRedis, previousInterval })
	rdb.onPublish = func(channel, payload string) {
		if channel == db.RedisKey("jwt:blacklist:sync") {
			handleWebSocketBlacklistEvent(payload)
		}
	}

	env := newWSAuthTestEnv(t)
	env.issueToken(t, false)
	// H46：jti 黑名单只对**持有 token 的桥**有意义；设备桥的凭据是一次性票据
	// （没有 jti，≤60s 自然过期），它的撤销语义由用户级代次负责（见下一个用例）。
	conns := make(map[string]*websocket.Conn, 1)
	for _, path := range []string{"/ws/rpa"} {
		conn := dialWSAuth(t, env, path)
		readWSHello(t, conn, path)
		conns[path] = conn
	}
	epoch := env.claims.IssuedAt.Unix()
	rdb.Set(context.Background(), db.RedisKey("jwt:epoch:")+env.claims.UserID, epoch, time.Hour)
	payload, err := json.Marshal(wsAuthEpochEvent{
		Type: wsAuthEpochEventType, UserID: env.claims.UserID, Epoch: epoch,
	})
	if err != nil {
		t.Fatalf("编码 epoch 事件: %v", err)
	}
	if err := rdb.Publish(context.Background(), db.RedisKey("jwt:blacklist:sync"), string(payload)).Err(); err != nil {
		t.Fatalf("发布 epoch 事件: %v", err)
	}
	for _, conn := range conns {
		assertWSClosedBefore(t, conn, 500*time.Millisecond)
	}
	waitWSAuthHubsEmpty(t, env)
}

func TestWebSocketActiveConnectionsDisconnectOnEpochRevalidation(t *testing.T) {
	rdb := newWSAuthTestRedis(t)
	previousRedis, previousInterval := db.Redis, wsAuthRevalidateInterval
	db.Redis, wsAuthRevalidateInterval = rdb, 10*time.Millisecond
	t.Cleanup(func() { db.Redis, wsAuthRevalidateInterval = previousRedis, previousInterval })

	env := newWSAuthTestEnv(t)
	env.issueToken(t, false)
	conns := map[string]*websocket.Conn{}
	for _, path := range []string{"/ws/rpa", "/ws/device-fs"} {
		conn := dialWSAuth(t, env, path)
		readWSHello(t, conn, path)
		conns[path] = conn
	}

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Unix() <= env.claims.IssuedAt.Unix() && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if err := revokeUserSessions(context.Background(), env.claims.UserID); err != nil {
		t.Fatalf("撤销会话: %v", err)
	}
	for _, conn := range conns {
		assertWSClosed(t, conn)
	}
	waitWSAuthHubsEmpty(t, env)
	assertWSHandshakeRejected(t, env, http.StatusUnauthorized)
}

func TestWebSocketActiveConnectionsDisconnectOnBlacklistEvent(t *testing.T) {
	rdb := newWSAuthTestRedis(t)
	previousRedis, previousInterval := db.Redis, wsAuthRevalidateInterval
	db.Redis, wsAuthRevalidateInterval = rdb, time.Hour
	t.Cleanup(func() { db.Redis, wsAuthRevalidateInterval = previousRedis, previousInterval })
	rdb.onPublish = func(channel, payload string) {
		if channel == db.RedisKey("jwt:blacklist:sync") {
			handleWebSocketBlacklistEvent(payload)
		}
	}

	env := newWSAuthTestEnv(t)
	env.issueToken(t, false)
	// H46：jti 黑名单只对**持有 token 的桥**有意义。设备桥的凭据是一次性票据
	// （没有 jti，≤60s 自然过期），它的撤销语义由用户级代次负责（见会话撤销用例）。
	conns := make(map[string]*websocket.Conn, 1)
	for _, path := range []string{"/ws/rpa"} {
		conn := dialWSAuth(t, env, path)
		readWSHello(t, conn, path)
		conns[path] = conn
	}
	rdb.Set(context.Background(), db.RedisKey("jwt:blacklist:")+env.claims.ID, "1", time.Hour)
	broadcastBlacklistSync(env.claims.ID)
	for _, conn := range conns {
		assertWSClosed(t, conn)
	}
	assertWSHandshakeRejected(t, env, http.StatusUnauthorized)
}

func TestWebSocketActiveConnectionsSurviveRevalidation(t *testing.T) {
	rdb := newWSAuthTestRedis(t)
	previousRedis, previousInterval := db.Redis, wsAuthRevalidateInterval
	db.Redis, wsAuthRevalidateInterval = rdb, 10*time.Millisecond
	t.Cleanup(func() { db.Redis, wsAuthRevalidateInterval = previousRedis, previousInterval })

	env := newWSAuthTestEnv(t)
	env.issueToken(t, false)
	for _, path := range []string{"/ws/rpa", "/ws/device-fs"} {
		conn := dialWSAuth(t, env, path)
		readWSHello(t, conn, path)
		go func(path string, conn *websocket.Conn) {
			for {
				if path == "/ws/rpa" {
					var msg RPAMessage
					if err := conn.ReadJSON(&msg); err != nil {
						return
					}
					if msg.Type == RPAMsgCommand {
						_ = conn.WriteJSON(RPAMessage{Type: RPAMsgResult, ID: msg.ID, Result: map[string]interface{}{"ok": true}})
					}
					continue
				}
				var req deviceOpRequest
				if err := conn.ReadJSON(&req); err != nil {
					return
				}
				_ = conn.WriteJSON(deviceOpResponse{ID: req.ID, OK: true, Stat: &deviceStat{Size: 9}})
			}
		}(path, conn)
	}
	time.Sleep(60 * time.Millisecond)

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	result, err := env.rpaHub.SendCommand(ctx, "ws-client", &RPACommand{Method: "ping"}, "ws-user")
	if err != nil || result == nil || result.Error != nil {
		t.Fatalf("RPA 重验后不可用: result=%+v err=%v", result, err)
	}
	resp, err := env.deviceHub.Call(ctx, "1", "ws-user", "ws-device", deviceOpRequest{Op: "stat"})
	if err != nil || resp.Stat == nil || resp.Stat.Size != 9 {
		t.Fatalf("device-fs 重验后不可用: resp=%+v err=%v", resp, err)
	}
}

func TestWebSocketRevalidationFailsClosedOnRedisError(t *testing.T) {
	rdb := newWSAuthTestRedis(t)
	previousRedis, previousInterval := db.Redis, wsAuthRevalidateInterval
	db.Redis, wsAuthRevalidateInterval = rdb, 10*time.Millisecond
	t.Cleanup(func() { db.Redis, wsAuthRevalidateInterval = previousRedis, previousInterval })

	env := newWSAuthTestEnv(t)
	env.issueToken(t, false)
	conns := make([]*websocket.Conn, 0, 2)
	for _, path := range []string{"/ws/rpa", "/ws/device-fs"} {
		conn := dialWSAuth(t, env, path)
		readWSHello(t, conn, path)
		conns = append(conns, conn)
	}
	rdb.mu.Lock()
	rdb.getErr = errors.New("redis unavailable")
	rdb.mu.Unlock()
	for _, conn := range conns {
		assertWSClosed(t, conn)
	}
}
