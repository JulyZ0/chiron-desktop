package api

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/gorilla/websocket"
)

const (
	wsAuthCheckTimeout          = 3 * time.Second
	wsAuthCloseWriteTimeout     = time.Second
	wsAuthDefaultRevalidateWait = 15 * time.Second
)

// Missing or unavailable notifications fall back to bounded revalidation.
var wsAuthRevalidateInterval = wsAuthDefaultRevalidateWait

const wsAuthEpochEventType = "epoch_revoked"

type wsAuthEpochEvent struct {
	Type   string `json:"type"`
	UserID string `json:"user_id"`
	Epoch  int64  `json:"epoch"`
}

type wsAuthError struct {
	status  int
	code    string
	message string
	cause   error
}

func (e *wsAuthError) Error() string {
	if e.cause != nil {
		return e.message + ": " + e.cause.Error()
	}
	return e.message
}

func (e *wsAuthError) write(w http.ResponseWriter) {
	JSONWithCode(w, e.status, e.code, nil, e.message)
}

func writeWebSocketAuthError(w http.ResponseWriter, err error) {
	if failure, ok := err.(*wsAuthError); ok {
		failure.write(w)
		return
	}
	Unauthorized(w, "invalid or expired token")
}

func validateWebSocketToken(ctx context.Context, a *auth.Authenticator, token, path string) (*auth.Claims, error) {
	claims, err := a.ValidateToken(token)
	if err != nil || claims == nil {
		return nil, &wsAuthError{status: http.StatusUnauthorized, message: "invalid or expired token", cause: err}
	}
	if err := validateWebSocketClaims(ctx, claims, path); err != nil {
		return nil, err
	}
	return claims, nil
}

func validateWebSocketClaims(ctx context.Context, claims *auth.Claims, path string) error {
	epoch, err := sessionEpoch(ctx, claims.UserID)
	if err != nil {
		return &wsAuthError{status: http.StatusUnauthorized, message: "authentication service unavailable", cause: err}
	}
	if epoch > 0 && claims.IssuedAt != nil && epoch >= claims.IssuedAt.Unix() {
		return &wsAuthError{status: http.StatusUnauthorized, message: "token has been revoked"}
	}
	if claims.ID != "" {
		blacklisted, err := checkJWTBlacklisted(ctx, claims.ID)
		if err != nil {
			return &wsAuthError{status: http.StatusUnauthorized, message: "authentication service unavailable", cause: err}
		}
		if blacklisted {
			return &wsAuthError{status: http.StatusUnauthorized, message: "token has been revoked"}
		}
	}
	if claims.MustChangePassword && !allowBeforePasswordChange(path) {
		return &wsAuthError{
			status: http.StatusForbidden, code: "password_change_required",
			message: "password change required before using this service",
		}
	}
	return nil
}

type wsAuthConnection struct {
	jti          string
	userID       string
	issuedAtUnix int64
	conn         *websocket.Conn
}

var (
	wsAuthConnections sync.Map
	wsAuthPubSubOnce  sync.Once
)

func closeWebSocketForAuth(conn *websocket.Conn) {
	_ = conn.WriteControl(websocket.CloseMessage,
		websocket.FormatCloseMessage(websocket.ClosePolicyViolation, "authentication revoked"),
		time.Now().Add(wsAuthCloseWriteTimeout))
	_ = conn.Close()
}

func monitorWebSocketAuth(conn *websocket.Conn, claims *auth.Claims, a *auth.Authenticator, token, path string) func() {
	entry := &wsAuthConnection{jti: claims.ID, userID: claims.UserID, conn: conn}
	if claims.IssuedAt != nil {
		entry.issuedAtUnix = claims.IssuedAt.Unix()
	}
	wsAuthConnections.Store(entry, struct{}{})
	startWebSocketBlacklistWatcher()

	ctx, cancel := context.WithCancel(context.Background())
	revalidateWait := wsAuthRevalidateInterval
	go func() {
		ticker := time.NewTicker(revalidateWait)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				checkCtx, stop := context.WithTimeout(context.Background(), wsAuthCheckTimeout)
				_, authErr := validateWebSocketToken(checkCtx, a, token, path)
				stop()
				if authErr != nil {
					slog.Warn("websocket session revoked during revalidation", "path", path, "error", authErr)
					closeWebSocketForAuth(conn)
					return
				}
			}
		}
	}()
	return func() {
		cancel()
		wsAuthConnections.Delete(entry)
	}
}

func startWebSocketBlacklistWatcher() {
	wsAuthPubSubOnce.Do(func() {
		if db.Redis == nil {
			return
		}
		go func() {
			rdb := db.Redis
			pubsub := rdb.Subscribe(context.Background(), db.RedisKey("jwt:blacklist:sync"))
			defer pubsub.Close()
			for {
				msg, err := pubsub.ReceiveMessage(context.Background())
				if err != nil {
					slog.Warn("websocket blacklist subscription stopped", "error", err)
					return
				}
				handleWebSocketBlacklistEvent(msg.Payload)
			}
		}()
	})
}

func handleWebSocketBlacklistEvent(payload string) {
	var event wsAuthEpochEvent
	if err := json.Unmarshal([]byte(payload), &event); err == nil && event.Type != "" {
		if event.Type == wsAuthEpochEventType {
			handleWebSocketEpochEvent(event.UserID, event.Epoch)
		}
		return
	}
	handleWebSocketJTIEvent(payload)
}

func handleWebSocketJTIEvent(jti string) {
	if jti == "" {
		return
	}
	markJWTBlacklisted(jti)
	wsAuthConnections.Range(func(key, _ any) bool {
		entry := key.(*wsAuthConnection)
		if entry.jti == jti {
			closeWebSocketForAuth(entry.conn)
		}
		return true
	})
}

func handleWebSocketEpochEvent(userID string, epoch int64) {
	if userID == "" || epoch <= 0 {
		return
	}
	wsAuthConnections.Range(func(key, _ any) bool {
		entry := key.(*wsAuthConnection)
		if entry.userID == userID && entry.issuedAtUnix <= epoch {
			closeWebSocketForAuth(entry.conn)
		}
		return true
	})
}
