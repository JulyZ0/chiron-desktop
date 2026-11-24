package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// 「哪个字段该被响应脱敏」的回归 —— 这个包此前**没有任何测试文件**，所以下面那条缺陷
// （登录/注册的 token 被打码）能在 CI 沉默很久。
func TestSensitiveFieldsKeepsClientCredentials(t *testing.T) {
	// 回归：`token` 曾被列进 SensitiveFields，而 sanitizeSensitivePaths 的白名单**包含**
	// `/v1/auth/login` 与 `/v1/auth/register`（意图是脱敏密码传输）⇒ 这两个响应体里的
	// `token` 被替换成 `********`。症状极具迷惑性：浏览器端一切正常（走 SetTokenCookie 的
	// **响应头**，不经 body 脱敏），而任何读 body 的 API/SDK 客户端一律 401
	// （`test_chiron_client_sdk_live.py` 自加入起从未绿过）。
	if isSensitiveField("token") {
		t.Fatal("token 不能脱敏：它是本来就要交给客户端的凭据，脱敏它等于打破认证")
	}
	if isSensitiveField("access_token") {
		t.Fatal("access_token 同理：它也是要交给客户端的凭据")
	}

	// 反向：真正该脱敏的仍必须命中（`isSensitiveField` 用的是**子串**匹配）。
	for _, field := range []string{"password", "client_secret", "private_key", "api_key", "dsn"} {
		if !isSensitiveField(field) {
			t.Fatalf("%q 必须脱敏", field)
		}
	}
}

// 一次性交付凭据必须原样返回：后台「重置密码 / 建号」要把明文交给管理员。
// 子串匹配曾让 temporary_password / initial_password 命中 "password" 被打成星号。
// H85 契约必须走真实中间件，不能只测 sanitizeMap：旧 bug 正是 handler 已生成指纹，
// 但 /v1/admin 响应缓冲阶段又按字段名把 key 打成星号。
func TestLLMKeyPreviewContractThroughSanitizeMiddleware(t *testing.T) {
	const plaintext = "sk-contract-secret-must-not-leak"
	keyHash := llmKeyHash("openai", plaintext)
	wantPreview := llmKeyPreviewMask + keyHash[:llmKeyPreviewHashSize]

	handler := SanitizeResponseMiddleware(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		OK(w, map[string]interface{}{
			"keys": []map[string]interface{}{
				llmKeyListItem("openai", keyHash, "active", "contract", 10),
			},
			// 探针：即使响应误带这些字段，真实中间件也必须继续脱敏。
			"key":      plaintext,
			"key_hash": keyHash,
		})
	}))
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/admin/api-keys", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body=%s)", rec.Code, rec.Body.String())
	}
	var body struct {
		Data struct {
			Keys []map[string]interface{} `json:"keys"`
			Key  string                   `json:"key"`
			Hash string                   `json:"key_hash"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("响应不是合法 JSON: %v (body=%s)", err, rec.Body.String())
	}
	if len(body.Data.Keys) != 1 {
		t.Fatalf("keys 数量 = %d, want 1", len(body.Data.Keys))
	}
	item := body.Data.Keys[0]
	if got := item["key_preview"]; got != wantPreview {
		t.Errorf("key_preview = %v, want %q", got, wantPreview)
	}
	if got := item["id"]; got != keyHash[:12] {
		t.Errorf("id = %v, want 12 位哈希前缀 %q", got, keyHash[:12])
	}
	if _, exists := item["key"]; exists {
		t.Error("Provider Key 列表不得返回 key 字段")
	}
	if _, exists := item["key_hash"]; exists {
		t.Error("Provider Key 列表不得返回 key_hash 字段")
	}
	if body.Data.Key != "********" || body.Data.Hash != "********" {
		t.Errorf("敏感字段未脱敏: key=%q key_hash=%q", body.Data.Key, body.Data.Hash)
	}
	raw := rec.Body.String()
	if strings.Contains(raw, plaintext) || strings.Contains(raw, keyHash) {
		t.Fatalf("响应泄漏明文或完整哈希: %s", raw)
	}
}

func TestSanitizeKeyPreviewOnlyAllowsTruncatedHash(t *testing.T) {
	valid := llmKeyPreviewMask + "01234567"
	for _, tc := range []struct {
		name  string
		value string
		allow bool
	}{
		{"截断哈希", valid, true},
		{"明文", "sk-plaintext-secret", false},
		{"完整哈希", strings.Repeat("a", 64), false},
		{"无掩码哈希", "01234567", false},
		{"过长指纹", llmKeyPreviewMask + "0123456789", false},
		{"过短指纹", llmKeyPreviewMask + "0123456", false},
		{"非十六进制", llmKeyPreviewMask + "0123456g", false},
		{"非规范大小写", llmKeyPreviewMask + "0123456A", false},
		{"空串", "", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			data := map[string]interface{}{"key_preview": tc.value}
			sanitizeMap(data)
			want := tc.value
			if !tc.allow {
				want = "********"
			}
			if got := data["key_preview"]; got != want {
				t.Errorf("key_preview = %v, want %q", got, want)
			}
		})
	}
}

func TestSanitizeKeepsOneTimeDeliveryPasswords(t *testing.T) {
	data := map[string]interface{}{
		"temporary_password": "Temp-pass-123",
		"initial_password":   "Init-pass-456",
		"password_hash":      "sha256:deadbeef",
		"nested": map[string]interface{}{
			"temporary_password": "Temp-pass-789",
		},
	}
	sanitizeMap(data)

	if got := data["temporary_password"]; got != "Temp-pass-123" {
		t.Fatalf("temporary_password 必须原样返回，实际 %v", got)
	}
	if got := data["initial_password"]; got != "Init-pass-456" {
		t.Fatalf("initial_password 必须原样返回，实际 %v", got)
	}
	if got := data["password_hash"]; got == "sha256:deadbeef" {
		t.Fatal("password_hash 仍必须脱敏")
	}
	nested := data["nested"].(map[string]interface{})
	if got := nested["temporary_password"]; got != "Temp-pass-789" {
		t.Fatalf("嵌套的 temporary_password 也必须原样返回，实际 %v", got)
	}
}
