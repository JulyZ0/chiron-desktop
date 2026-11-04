package broadcast

import (
	"strings"
	"testing"
)

// TestFormatSSEIncludesIDLine: 带 ID 的事件必须输出 SSE id: 行（Last-Event-ID 重连依据）。
func TestFormatSSEIncludesIDLine(t *testing.T) {
	s := FormatSSE(Event{ID: "1750000000000-0", Type: "x", SessionID: "s1", Data: map[string]string{"a": "b"}})
	if !strings.HasPrefix(s, "id: 1750000000000-0\n") {
		t.Fatalf("expected leading id: line, got %q", s)
	}
	if !strings.Contains(s, "\ndata: ") {
		t.Fatalf("expected data: line after id, got %q", s)
	}
}

// TestFormatSSEWithoutIDHasNoIDLine: 无缓冲（系统事件等）不输出 id 行，保持旧格式兼容。
func TestFormatSSEWithoutIDHasNoIDLine(t *testing.T) {
	s := FormatSSE(Event{Type: "connected", Data: map[string]string{}})
	if strings.Contains(s, "id:") {
		t.Fatalf("unexpected id: line in %q", s)
	}
}

// TestLocalOnlyHubSkipsSessionBuffer: Redis 不可用（localOnly）时，
// 会话事件 Publish 不写缓冲、ReplayAfter 返回空——实时 fanout 语义不受影响。
func TestLocalOnlyHubSkipsSessionBuffer(t *testing.T) {
	h := NewHub(nil) // localOnly: 无 Redis
	defer h.Close()
	h.Publish(Event{Type: "turn_done", SessionID: "s1", Data: map[string]string{}})
	evs, err := h.ReplayAfter(t.Context(), "s1", "0-0")
	if err != nil {
		t.Fatalf("ReplayAfter returned error: %v", err)
	}
	if len(evs) != 0 {
		t.Fatalf("expected empty replay on local-only hub, got %d events", len(evs))
	}
}
