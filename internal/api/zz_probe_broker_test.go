package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// 探针：引擎发的是 int，Go 侧结构体是 string —— 看是否 400。
func TestProbeNumericTenantID(t *testing.T) {
	for _, body := range []string{
		`{"tenant_id":1,"tool_name":"shell_exec","arguments":{"command":"git branch -a"},"tools_mode":"auto"}`,
		`{"tenant_id":"1","tool_name":"shell_exec","arguments":{"command":"git branch -a"},"tools_mode":"auto"}`,
	} {
		req := httptest.NewRequest(http.MethodPost, "/v1/internal/tool-authorize", strings.NewReader(body))
		rec := httptest.NewRecorder()
		ToolAuthorizeHandler(rec, req)
		t.Logf("body=%s -> status=%d resp=%s", body, rec.Code, rec.Body.String())
	}
}
