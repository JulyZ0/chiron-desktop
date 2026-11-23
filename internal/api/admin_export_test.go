package api

// 租户数据导出：清单不得包含秘密列、驱动值归一与时间格式。
// 真实 ZIP 流需要 live PG，由部署联调覆盖。

import (
	"bytes"
	"testing"
	"time"
)

func TestExportTablesExcludeSecrets(t *testing.T) {
	forbidden := []string{
		"password_hash", "key_hash", "token_hash", "encrypted_key",
		"api_key", "private_key", "secret", "dsn", "system_settings",
	}
	for _, table := range exportTables {
		for _, needle := range forbidden {
			if bytes.Contains(bytes.ToLower([]byte(table.Query)), []byte(needle)) {
				t.Fatalf("table %s exports forbidden column/table %q", table.Name, needle)
			}
		}
	}
	// 清单必须覆盖核心会话数据；漏一张表比多一张更难发现。
	names := map[string]bool{}
	for _, table := range exportTables {
		names[table.Name] = true
	}
	for _, required := range []string{"users", "sessions", "messages", "turns", "usage_events", "projects", "audit_logs"} {
		if !names[required] {
			t.Fatalf("export manifest is missing %q", required)
		}
	}
}

func TestNormalizeExportValue(t *testing.T) {
	if got := normalizeExportValue(nil); got != nil {
		t.Fatalf("nil -> %#v", got)
	}
	if got := normalizeExportValue([]byte(`{"a":1}`)); got != `{"a":1}` {
		t.Fatalf("utf8 bytes -> %#v", got)
	}
	invalid := []byte{0xff, 0xfe, 0x00}
	got, ok := normalizeExportValue(invalid).(map[string]string)
	if !ok || got["base64"] == "" {
		t.Fatalf("invalid utf8 should be base64-wrapped, got %#v", got)
	}
	ts := time.Date(2026, 11, 23, 12, 0, 0, 123, time.FixedZone("x", 8*3600))
	if got := normalizeExportValue(ts); got != "2026-11-23T04:00:00.000000123Z" {
		t.Fatalf("time should be UTC RFC3339Nano, got %v", got)
	}
}
