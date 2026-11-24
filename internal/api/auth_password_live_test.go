package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/crypto/bcrypt"
)

func TestLiveFirstPasswordSetPersistsAcrossRestart(t *testing.T) {
	pool := liveSmokePool(t)
	previousPool := db.Pool
	db.Pool = pool
	t.Cleanup(func() { db.Pool = previousPool })

	ctx := context.Background()
	tenantID, _ := liveSmokeFixture(t, pool)
	if tenantID != DefaultTenantID {
		t.Skipf("密码登录固定默认租户，当前 fixture 租户为 %d", tenantID)
	}
	userID := uuid.NewString()
	email := "password-live-" + userID[:8] + "@example.test"
	randomHash, err := bcrypt.GenerateFromPassword([]byte(uuid.NewString()), bcrypt.MinCost)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx,
		`INSERT INTO users (id, tenant_id, email, name, password_hash, password_set,
		                    must_change_password, role, status, created_at, updated_at)
		 VALUES ($1, $2, $3, 'Local', $4, false, false, 'owner', 'active', NOW(), NOW())`,
		userID, tenantID, email, string(randomHash)); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_, _ = pool.Exec(context.Background(), `DELETE FROM users WHERE id = $1`, userID)
	})

	cfg := &config.Config{JWTSecret: "password-live-test-secret-0123456789abcdef", JWTExpiration: time.Hour}
	h := NewAuthHandler(cfg)
	claims := &auth.Claims{UserID: userID, Email: email, Role: "owner", TenantID: tenantID}
	request := func(handler *AuthHandler, oldPassword string) *httptest.ResponseRecorder {
		body, _ := json.Marshal(map[string]string{
			"old_password": oldPassword,
			"new_password": "FirstPassword123!",
		})
		req := httptest.NewRequest(http.MethodPost, "/v1/auth/password", bytes.NewReader(body))
		req = req.WithContext(auth.WithClaims(req.Context(), claims))
		rec := httptest.NewRecorder()
		handler.ChangePassword(rec, req)
		return rec
	}

	rec := request(h, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("本地首次设置空旧密码应成功，得到 %d: %s", rec.Code, rec.Body.String())
	}
	var changed struct {
		Success bool `json:"success"`
		Data    struct {
			Token string       `json:"token"`
			User  UserResponse `json:"user"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &changed); err != nil {
		t.Fatal(err)
	}
	if !changed.Success || changed.Data.Token == "" || !changed.Data.User.PasswordSet || changed.Data.User.MustChangePassword {
		t.Fatalf("首次改密响应状态不正确: %+v", changed)
	}
	newClaims, err := h.auth.ValidateToken(changed.Data.Token)
	if err != nil || newClaims.MustChangePassword {
		t.Fatalf("新 token 必须解除强制改密: claims=%+v err=%v", newClaims, err)
	}

	var passwordSet, mustChange bool
	var changedAt *time.Time
	if err := pool.QueryRow(ctx,
		`SELECT password_set, must_change_password, password_changed_at FROM users WHERE id = $1`,
		userID).Scan(&passwordSet, &mustChange, &changedAt); err != nil {
		t.Fatal(err)
	}
	if !passwordSet || mustChange || changedAt == nil {
		t.Fatalf("首次改密未持久化: password_set=%v must_change=%v changed_at=%v", passwordSet, mustChange, changedAt)
	}
	for _, oldPassword := range []string{"", "WrongPassword123!"} {
		if rec := request(h, oldPassword); rec.Code != http.StatusBadRequest {
			t.Fatalf("已有密码后旧密码 %q 应被拒绝，得到 %d: %s", oldPassword, rec.Code, rec.Body.String())
		}
	}

	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	restartedPool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(restartedPool.Close)
	db.Pool = restartedPool
	restarted := NewAuthHandler(cfg)
	body := fmt.Sprintf(`{"identifier":%q,"password":"FirstPassword123!"}`, email)
	req := httptest.NewRequest(http.MethodPost, "/v1/auth/login", bytes.NewBufferString(body))
	rec = httptest.NewRecorder()
	restarted.Login(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("重启后应能用新密码登录，得到 %d: %s", rec.Code, rec.Body.String())
	}
	var loggedIn struct {
		Data struct {
			User UserResponse `json:"user"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &loggedIn); err != nil {
		t.Fatal(err)
	}
	if !loggedIn.Data.User.PasswordSet || loggedIn.Data.User.MustChangePassword {
		t.Fatalf("重启登录的密码状态不正确: %+v", loggedIn.Data.User)
	}
}
