package engine

// R9：网关把请求 trace_id 透传给引擎（X-Chiron-Trace-Id）。

import (
	"context"
	"net/http/httptest"
	"testing"

	"github.com/athenavi/chiron/internal/monitor"
)

func TestInjectInternalTokenAddsTraceID(t *testing.T) {
	c := &PythonClient{internalToken: "tok"}
	ctx, span := monitor.StartSpan(context.Background(), "test", "server")
	defer span.End()

	req := httptest.NewRequest("POST", "http://engine/v1/x", nil).WithContext(ctx)
	c.injectInternalToken(req)

	if got := req.Header.Get("X-Internal-Token"); got != "tok" {
		t.Fatalf("internal token = %q", got)
	}
	if got := req.Header.Get("X-Chiron-Trace-Id"); got != span.TraceID.String() {
		t.Fatalf("trace id = %q, want %q", got, span.TraceID.String())
	}

	// 没有网关 Span（直接调用引擎/后台任务）时不注入，避免写空头。
	plain := httptest.NewRequest("GET", "http://engine/v1/x", nil)
	c.injectInternalToken(plain)
	if got := plain.Header.Get("X-Chiron-Trace-Id"); got != "" {
		t.Fatalf("unexpected trace id without span: %q", got)
	}
}
