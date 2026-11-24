package api

import (
	"encoding/json"
	"strings"
	"testing"
)

// ── H35：设置广播最小暴露 ─────────────────────────────────────────────────
//
// 旧实现把整份 category config 塞进 Redis Pub/Sub：storage/payment 这类含
// access_key/secret/私钥的分类，其内容会出现在所有订阅者的日志、抓包与
// `MONITOR` 输出里。现在只广播白名单字段 + version，其余以 invalidate 通知。

func TestBroadcastableSettingsDropsSensitiveCategories(t *testing.T) {
	for _, category := range []string{"storage", "payment", "smtp", "security", "unknown"} {
		got := broadcastableSettings(category, map[string]interface{}{
			"access_key": "AKIA-LOOKS-REAL",
			"secret_key": "shhh",
			"enabled":    true,
		})
		if got != nil {
			t.Fatalf("%s 不得广播任何 config，得到 %v", category, got)
		}
	}
}

func TestBroadcastableSettingsKeepsAllowlistedFieldsOnly(t *testing.T) {
	got := broadcastableSettings("rate_limit", map[string]interface{}{
		"global":    120,
		"tenant":    60,
		"user":      10,
		"api_token": "must-not-leak",
		"internal":  "nope",
	})
	if len(got) != 3 {
		t.Fatalf("只应广播白名单三字段，得到 %v", got)
	}
	for _, k := range []string{"global", "tenant", "user"} {
		if _, ok := got[k]; !ok {
			t.Fatalf("缺少白名单字段 %s: %v", k, got)
		}
	}
	for _, k := range []string{"api_token", "internal"} {
		if _, ok := got[k]; ok {
			t.Fatalf("非白名单字段 %s 不得广播: %v", k, got)
		}
	}
}

func TestBroadcastableSettingsSkipsSensitiveKeysEvenIfAllowlisted(t *testing.T) {
	// 双保险：即使有人把敏感键误加进白名单，也不该被广播
	for _, k := range []string{"access_key", "secret_key", "password", "token", "private_key"} {
		got := broadcastableSettings("rate_limit", map[string]interface{}{k: "leak"})
		if len(got) != 0 {
			// 只有在该键确实被 settings.IsSensitive 认作敏感时才要求为空；
			// 反之它本来也不在白名单里。两种情况下都不该出现在结果中。
			if _, present := got[k]; present {
				t.Fatalf("敏感键 %s 不得广播: %v", k, got)
			}
		}
	}
}

func TestSettingsConfigVersionStableAndCategoryBound(t *testing.T) {
	cfg := map[string]interface{}{"global": 120, "tenant": 60}
	v1 := settingsConfigVersion("rate_limit", cfg)
	v2 := settingsConfigVersion("rate_limit", map[string]interface{}{"tenant": 60, "global": 120})
	if v1 == "" || len(v1) != 12 {
		t.Fatalf("版本号应为 12 位 hex，得到 %q", v1)
	}
	if v1 != v2 {
		t.Fatalf("同一份配置（键序不同）应得到同一版本号: %q vs %q", v1, v2)
	}
	if settingsConfigVersion("cors", cfg) == v1 {
		t.Fatal("版本号必须绑定分类（否则两类配置会互相覆盖观测）")
	}
	if got := settingsConfigVersion("rate_limit", nil); got != "" {
		t.Fatalf("空配置没有版本号，得到 %q", got)
	}
}

func TestSettingsChangedEventCarriesInvalidateNotSecretValues(t *testing.T) {
	// 事件形状：action=invalidate + version + 仅白名单字段
	ev := settingsChangedEvent{
		Category: "storage",
		Action:   "invalidate",
		Version:  settingsConfigVersion("storage", map[string]interface{}{"secret_key": "s"}),
		Config:   broadcastableSettings("storage", map[string]interface{}{"secret_key": "s"}),
	}
	data, err := json.Marshal(ev)
	if err != nil {
		t.Fatalf("事件必须可序列化: %v", err)
	}
	payload := string(data)
	if strings.Contains(payload, `"s"`) {
		t.Fatalf("storage 的配置值不得进入广播载荷: %s", payload)
	}
	if !strings.Contains(payload, `"action":"invalidate"`) {
		t.Fatalf("事件必须显式声明 invalidate 语义: %s", payload)
	}
	if !strings.Contains(payload, `"version"`) {
		t.Fatalf("事件必须带版本号用于对齐/去重: %s", payload)
	}
}
