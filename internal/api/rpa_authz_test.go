package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// ── H40：RPA 身份 fail-closed 回归 ──
//
// 这组用例钉住三个"空身份 = 全量放行"的口子：
//  1. /v1/rpa/exec 缺 user_id 时仍会把命令投给连接 owner；
//  2. /v1/rpa/clients 缺 user_id 时退化为全量列举；
//  3. 跨实例投递只在 user_id 非空时才比对 owner。
//
// 三者共同构成"任何漏传身份的调用方都能接管别人浏览器连接"的链，
// 修完后每一条都必须被拒绝，且拒绝发生在触达连接之前。

func newTestRPAHub(t *testing.T) *RPAHub {
	t.Helper()
	hub := NewRPAHub(nil) // localOnly：不触达 Redis
	if !hub.Register(&RPAClient{ID: "client-u1", UserID: "user-1"}) {
		t.Fatal("register user-1 client failed")
	}
	if !hub.Register(&RPAClient{ID: "client-u2", UserID: "user-2"}) {
		t.Fatal("register user-2 client failed")
	}
	return hub
}

func rpaInternalRequest(method, target, body string) *http.Request {
	req := httptest.NewRequest(method, target, strings.NewReader(body))
	req.Header.Set("X-Internal-Token", "test-internal-token")
	return req
}

func TestRPAExecRejectsMissingUserID(t *testing.T) {
	hub := newTestRPAHub(t)
	handler := RPAExecHandler(hub, "test-internal-token")

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, rpaInternalRequest(http.MethodPost, "/v1/rpa/exec",
		`{"client_id":"client-u1","method":"navigate","params":{"url":"https://x"}}`))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("缺 user_id 的 /v1/rpa/exec 状态=%d，want 400；body=%s", rec.Code, rec.Body.String())
	}

	// 已有连接的 owner 也不能被"匿名调用方"命中：确认 400 发生在触达连接之前。
	rec = httptest.NewRecorder()
	handler.ServeHTTP(rec, rpaInternalRequest(http.MethodPost, "/v1/rpa/exec",
		`{"client_id":"client-u1","method":"navigate","params":{},"user_id":"  "}`))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("空白 user_id 状态=%d，want 400；body=%s", rec.Code, rec.Body.String())
	}
}

func TestRPAClientsRequiresUserIDAndFiltersOwner(t *testing.T) {
	hub := newTestRPAHub(t)
	handler := RPAClientsHandler(hub, "test-internal-token")

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, rpaInternalRequest(http.MethodGet, "/v1/rpa/clients", ""))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("缺 user_id 的 /v1/rpa/clients 状态=%d，want 400；body=%s", rec.Code, rec.Body.String())
	}

	// 带身份时必须只返回该 owner 的连接：跨用户列举 = 枚举命令句柄。
	for userID, want := range map[string]string{"user-1": "client-u1", "user-2": "client-u2"} {
		rec = httptest.NewRecorder()
		handler.ServeHTTP(rec, rpaInternalRequest(http.MethodGet,
			"/v1/rpa/clients?user_id="+userID, ""))
		if rec.Code != http.StatusOK {
			t.Fatalf("user_id=%s 状态=%d，want 200；body=%s", userID, rec.Code, rec.Body.String())
		}
		body := rec.Body.String()
		if !strings.Contains(body, want) {
			t.Errorf("user_id=%s 看不到自己的连接 %s：%s", userID, want, body)
		}
		other := "client-u2"
		if want == "client-u2" {
			other = "client-u1"
		}
		if strings.Contains(body, other) {
			t.Errorf("user_id=%s 越权看到 %s：%s", userID, other, body)
		}
	}
}

func TestRPASendCommandFailsClosedOnMissingOrForeignUser(t *testing.T) {
	hub := newTestRPAHub(t)
	cmd := &RPACommand{Method: "navigate"}

	if _, err := hub.SendCommand(context.Background(), "client-u1", cmd, ""); err == nil {
		t.Fatal("空 user_id 不得下发命令（此前会跳过 owner 校验）")
	}
	if _, err := hub.SendCommand(context.Background(), "client-u1", cmd, "user-2"); err == nil {
		t.Fatal("跨用户下发命令必须被拒绝")
	}
	// 未知连接 + 空身份：拒绝理由必须是身份，而不是"未连接"——
	// 否则调用方会把"身份缺失"误读成"客户端没上线"。
	_, err := hub.SendCommand(context.Background(), "not-connected", cmd, "")
	if err == nil || !strings.Contains(err.Error(), "user_id") {
		t.Fatalf("未知连接 + 空身份应报身份缺失，得到 %v", err)
	}
}

func TestRemoteCmdOwnershipOKRequiresBothSides(t *testing.T) {
	cases := []struct {
		owner, caller string
		want          bool
	}{
		{"user-1", "user-1", true},
		{"user-1", "user-2", false},
		{"", "user-1", false}, // 连接 owner 未知
		{"user-1", "", false}, // 调用方身份未知（旧行为：放行）
		{"", "", false},
		{" user-1 ", "user-1", true},
	}
	for _, tc := range cases {
		if got := remoteCmdOwnershipOK(tc.owner, tc.caller); got != tc.want {
			t.Errorf("remoteCmdOwnershipOK(%q, %q) = %v, want %v", tc.owner, tc.caller, got, tc.want)
		}
	}
}

// API Key 鉴权 SQL 必须自己判定用户状态（不依赖会话纪元是否写成功）。
func TestAPIKeyAuthQueryRequiresActiveUser(t *testing.T) {
	if !strings.Contains(apiKeyAuthQuery, "u.status") {
		t.Fatalf("apiKeyAuthQuery 未校验 users.status —— 禁用用户的长期 API key 仍可用：\n%s",
			apiKeyAuthQuery)
	}
	if !strings.Contains(apiKeyAuthQuery, "= 'active'") {
		t.Fatalf("apiKeyAuthQuery 的状态谓词必须要求 active：\n%s", apiKeyAuthQuery)
	}
}
