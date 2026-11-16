package engine

import (
	"encoding/json"
	"testing"
)

// TestSetAddressesDynamic: 动态全量替换地址表(保序),静态列表不变(发现回退用)。
func TestSetAddressesDynamic(t *testing.T) {
	c := NewPythonClient("http://a:8000", "http://b:8000")

	if changed := c.SetAddresses([]string{"http://b:8000", "http://c:8000"}); !changed {
		t.Fatal("expected addresses changed")
	}
	entries := c.snapshot()
	if len(entries) != 2 {
		t.Fatalf("expected 2 entries, got %d", len(entries))
	}
	if entries[0].url != "http://b:8000" || entries[1].url != "http://c:8000" {
		t.Errorf("unexpected order: %+v", entries)
	}

	// 静态地址必须保持(空注册表时 discovery 回退用)
	static := c.StaticAddresses()
	if len(static) != 2 || static[0] != "http://a:8000" || static[1] != "http://b:8000" {
		t.Errorf("static addresses changed: %v", static)
	}
}

// TestSetAddressesSameNoop: 同序同集时视为未变化(避免 discovery 空转日志)。
func TestSetAddressesSameNoop(t *testing.T) {
	c := NewPythonClient("http://a:8000")
	if changed := c.SetAddresses([]string{"http://a:8000"}); changed {
		t.Fatal("expected no change for identical list")
	}
	if changed := c.SetAddresses([]string{"http://a:8000", "http://a:8000"}); changed {
		t.Fatal("expected no change for duplicate-normalized identical list")
	}
}

// TestSetAddressesKeepsCooldown: 替换地址表时保留既有地址的熔断冷却状态。
func TestSetAddressesKeepsCooldown(t *testing.T) {
	c := NewPythonClient("http://a:8000", "http://b:8000")
	c.markFailure("http://a:8000")

	c.SetAddresses([]string{"http://a:8000", "http://c:8000"})
	entries := c.snapshot()
	if entries[0].url != "http://a:8000" || entries[0].cooldownUntil == 0 {
		t.Errorf("expected a:8000 cooldown preserved, got %+v", entries[0])
	}
	if entries[1].url != "http://c:8000" || entries[1].cooldownUntil != 0 {
		t.Errorf("expected new addr without cooldown, got %+v", entries[1])
	}
}

// TestPickAddressFallbackEmpty: 空地址表时回退 localhost(与历史行为一致)。
func TestPickAddressFallbackEmpty(t *testing.T) {
	c := NewPythonClient("http://a:8000")
	c.SetAddresses(nil)
	if got := c.pickAddress(); got != "http://localhost:8000" {
		t.Errorf("pickAddress on empty = %q, want localhost fallback", got)
	}
}

// TestSortUnique: 排序去重(discovery 扫描结果去重)。
func TestSortUnique(t *testing.T) {
	in := []string{"http://c:8000", "http://a:8000", "http://c:8000", "http://b:8000"}
	got := sortUnique(in)
	want := []string{"http://a:8000", "http://b:8000", "http://c:8000"}
	if len(got) != len(want) {
		t.Fatalf("sortUnique = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("sortUnique = %v, want %v", got, want)
		}
	}
	if sortUnique(nil) != nil {
		t.Error("sortUnique(nil) should be nil")
	}
}

// TestEngineRecordUnmarshal: 引擎注册表 JSON(与 python engine_registry.py 契约)解析。
func TestEngineRecordUnmarshal(t *testing.T) {
	raw := `{"url":"http://engine-0:8000","version":"3.0.0","last_seen":"2026-09-09T00:00:00Z"}`
	var rec engineInstanceRecord
	if err := json.Unmarshal([]byte(raw), &rec); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if rec.URL != "http://engine-0:8000" || rec.Version == "" || rec.LastSeen == "" {
		t.Errorf("unexpected record: %+v", rec)
	}
}
