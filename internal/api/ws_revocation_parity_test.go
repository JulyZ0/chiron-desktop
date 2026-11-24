package api

// H6：WebSocket 撤销一致性回归。
//
// 这里锁三件事：
//  1. HTTP 与 WS 的撤销判定**逐项一致**（登出/改密/禁用/撤销/强制下线/存储故障）；
//  2. 批量撤销会广播 epoch 事件，已有长连接立刻断开，而不是等 15s 重验；
//  3. 事件分流正确：epoch JSON 不能被当成 jti 写进黑名单。

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/gorilla/websocket"
)

// wsHandshakeStatus 返回 WS 握手结果：101 表示升级成功，其余是 HTTP 状态码。
func wsHandshakeStatus(t *testing.T, env *wsAuthTestEnv, path string) int {
	t.Helper()
	conn, resp, err := websocket.DefaultDialer.Dial(env.wsURL(path), nil)
	if err == nil {
		_ = conn.Close()
		return http.StatusSwitchingProtocols
	}
	if resp == nil {
		t.Fatalf("%s 握手无 HTTP 响应: %v", path, err)
	}
	defer resp.Body.Close()
	return resp.StatusCode
}

func TestSessionRevocationParityBetweenHTTPAndWebSocket(t *testing.T) {
	const wsUpgraded = http.StatusSwitchingProtocols
	cases := []struct {
		name       string
		mustChange bool
		setup      func(t *testing.T, env *wsAuthTestEnv, rdb *wsAuthTestRedis) func()
		httpWant   int
		wsWant     int
	}{
		{
			name:     "有效凭据",
			httpWant: http.StatusNoContent,
			wsWant:   wsUpgraded,
		},
		{
			// 改密/重置/禁用/撤销/强制下线都走这条：纪元严格大于 token.iat ⇒ 撤销。
			name: "纪元晚于签发（改密/禁用/撤销/强制下线）",
			setup: func(t *testing.T, env *wsAuthTestEnv, rdb *wsAuthTestRedis) func() {
				// 纪元取当前秒；先等过 token 签发的那一秒，撤销才会严格晚于 iat。
				time.Sleep(time.Until(time.Unix(env.claims.IssuedAt.Unix()+1, 0).Add(20 * time.Millisecond)))
				if err := revokeUserSessions(context.Background(), env.claims.UserID); err != nil {
					t.Fatalf("写入会话纪元: %v", err)
				}
				return nil
			},
			httpWant: http.StatusUnauthorized,
			wsWant:   http.StatusUnauthorized,
		},
		{
			// 同秒边界：改密响应里新签发的 token 与纪元同秒，必须能继续用。
			name: "纪元与签发同秒",
			setup: func(t *testing.T, env *wsAuthTestEnv, rdb *wsAuthTestRedis) func() {
				rdb.Set(context.Background(), db.RedisKey("jwt:epoch:")+env.claims.UserID,
					env.claims.IssuedAt.Unix(), time.Hour)
				return nil
			},
			httpWant: http.StatusNoContent,
			wsWant:   wsUpgraded,
		},
		{
			name: "登出（jti 黑名单）",
			setup: func(t *testing.T, env *wsAuthTestEnv, rdb *wsAuthTestRedis) func() {
				rdb.Set(context.Background(), db.RedisKey("jwt:blacklist:")+env.claims.ID, "1", time.Hour)
				markJWTBlacklisted(env.claims.ID)
				return func() { jwtBlacklistCache.Delete(env.claims.ID) }
			},
			httpWant: http.StatusUnauthorized,
			wsWant:   http.StatusUnauthorized,
		},
		{
			name:       "强制改密",
			mustChange: true,
			httpWant:   http.StatusForbidden,
			wsWant:     http.StatusForbidden,
		},
		{
			name: "撤销存储不可用",
			setup: func(t *testing.T, env *wsAuthTestEnv, rdb *wsAuthTestRedis) func() {
				previous := db.Redis
				db.Redis = nil
				return func() { db.Redis = previous }
			},
			httpWant: http.StatusUnauthorized,
			wsWant:   http.StatusUnauthorized,
		},
		{
			name: "撤销存储读失败",
			setup: func(t *testing.T, env *wsAuthTestEnv, rdb *wsAuthTestRedis) func() {
				rdb.mu.Lock()
				rdb.getErr = errors.New("redis unavailable")
				rdb.mu.Unlock()
				return func() {
					rdb.mu.Lock()
					rdb.getErr = nil
					rdb.mu.Unlock()
				}
			},
			httpWant: http.StatusUnauthorized,
			wsWant:   http.StatusUnauthorized,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rdb := newWSAuthTestRedis(t)
			previousRedis, previousInterval := db.Redis, wsAuthRevalidateInterval
			db.Redis, wsAuthRevalidateInterval = rdb, time.Hour
			t.Cleanup(func() { db.Redis, wsAuthRevalidateInterval = previousRedis, previousInterval })

			env := newWSAuthTestEnv(t)
			env.issueToken(t, tc.mustChange)
			if tc.setup != nil {
				if restore := tc.setup(t, env, rdb); restore != nil {
					t.Cleanup(restore)
				}
			}

			if got := env.httpStatus(t); got != tc.httpWant {
				t.Fatalf("HTTP 状态=%d，want %d", got, tc.httpWant)
			}
			if got := wsHandshakeStatus(t, env, "/ws/rpa"); got != tc.wsWant {
				t.Fatalf("WS 握手状态=%d，want %d（与 HTTP 必须同口径）", got, tc.wsWant)
			}
			if got := wsHandshakeStatus(t, env, "/ws/device-fs"); got != tc.wsWant {
				t.Fatalf("device-fs 握手状态=%d，want %d", got, tc.wsWant)
			}
		})
	}
}

// 批量撤销必须推事件：只有纪元键的话，已建立的连接要等下一次周期性重验（默认 15s）。
func TestRevokeUserSessionsBroadcastsEpochEventAndClosesConnections(t *testing.T) {
	rdb := newWSAuthTestRedis(t)
	previousRedis, previousInterval := db.Redis, wsAuthRevalidateInterval
	db.Redis, wsAuthRevalidateInterval = rdb, time.Hour
	t.Cleanup(func() { db.Redis, wsAuthRevalidateInterval = previousRedis, previousInterval })

	published := make([]wsAuthEpochEvent, 0, 1)
	rdb.onPublish = func(channel, payload string) {
		if channel != authRevocationChannel() {
			t.Errorf("撤销事件发到了错误 channel: %q", channel)
			return
		}
		var event wsAuthEpochEvent
		if err := json.Unmarshal([]byte(payload), &event); err != nil {
			t.Errorf("撤销事件不是 epoch JSON: %q (%v)", payload, err)
			return
		}
		published = append(published, event)
		// 等价于另一副本收到事件：分发器直接处理本地在连 WS。
		handleWebSocketBlacklistEvent(payload)
	}

	env := newWSAuthTestEnv(t)
	env.issueToken(t, false)
	conns := make([]*websocket.Conn, 0, 2)
	for _, path := range []string{"/ws/rpa", "/ws/device-fs"} {
		conn := dialWSAuth(t, env, path)
		readWSHello(t, conn, path)
		conns = append(conns, conn)
	}

	// 纪元是秒精度：等过签发那一秒再撤销，旧 token 才会被判定为"撤销之前签发"。
	time.Sleep(time.Until(time.Unix(env.claims.IssuedAt.Unix()+1, 0).Add(20 * time.Millisecond)))
	if err := revokeUserSessions(context.Background(), env.claims.UserID); err != nil {
		t.Fatalf("撤销会话: %v", err)
	}
	if len(published) != 1 {
		t.Fatalf("应广播 1 条撤销事件，实际 %d 条", len(published))
	}
	event := published[0]
	epoch, err := sessionEpoch(context.Background(), env.claims.UserID)
	if err != nil {
		t.Fatalf("读取纪元: %v", err)
	}
	if event.Type != wsAuthEpochEventType || event.UserID != env.claims.UserID || event.Epoch != epoch {
		t.Fatalf("撤销事件内容不正确: %+v（纪元=%d）", event, epoch)
	}
	if event.Epoch < env.claims.IssuedAt.Unix() {
		t.Fatalf("纪元 %d 早于 token 签发 %d，旧 token 不会被判定撤销",
			event.Epoch, env.claims.IssuedAt.Unix())
	}
	for _, conn := range conns {
		assertWSClosedBefore(t, conn, time.Second)
	}
	waitWSAuthHubsEmpty(t, env)
	assertWSHandshakeRejected(t, env, http.StatusUnauthorized)
}

// 事件分流：epoch JSON 是事件，不是 jti。若按 jti 处理，会污染黑名单缓存。
func TestEpochRevocationEventIsNotCachedAsJTI(t *testing.T) {
	payload, err := json.Marshal(wsAuthEpochEvent{Type: wsAuthEpochEventType, UserID: "parity-user", Epoch: 42})
	if err != nil {
		t.Fatal(err)
	}
	raw := string(payload)
	jwtBlacklistCache.Delete(raw)
	handleWebSocketBlacklistEvent(raw)
	if entry, ok := jwtBlacklistCache.Load(raw); ok {
		t.Fatalf("epoch 事件被当成 jti 写入黑名单缓存: %+v", entry)
	}
}
