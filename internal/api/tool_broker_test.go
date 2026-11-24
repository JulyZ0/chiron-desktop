package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// Broker 的核心职责有两条：**服务端独立判定**、**可以收紧用户的模式选择**。
// 这组断言主要钉住后者 —— 模式选择能表达"我愿意多放手"，不能表达"我愿意承受不可撤销的
// 后果"。所以 yolo（跳过全部确认）下的 delete / external，服务端仍然要求人工确认。
//
// 顺带钉住"服务端不代替引擎执行"：它只回答 allowed + 该走哪些关。

func authorize(t *testing.T, body string) toolAuthorizeResponse {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/internal/tool-authorize", strings.NewReader(body))
	rec := httptest.NewRecorder()
	ToolAuthorizeHandler(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var envelope struct {
		Data toolAuthorizeResponse `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode: %v (%s)", err, rec.Body.String())
	}
	return envelope.Data
}

func TestToolAuthorizeTightensYoloForIrreversible(t *testing.T) {
	got := authorize(t, `{"tenant_id":"t","user_id":"u","tool_name":"shell_exec",
		"arguments":{"command":"rm -rf /tmp"},"tools_mode":"yolo"}`)

	if !got.Allowed {
		t.Error("allowed should stay true —— 服务端是判定者，不是默认否决者")
	}
	if got.Level != ToolLevelDelete {
		t.Errorf("level = %q, want delete", got.Level)
	}
	if !got.RequiresUserApproval {
		t.Error("yolo 不能免除不可逆操作的人工确认")
	}
	if !got.Enforced {
		t.Error("覆盖了用户模式选择时必须标记 enforced（前端要能解释为什么还要确认）")
	}
	if !got.RequiresSecondCheck {
		t.Error("delete 需要二次校验")
	}
	if got.Rollback != "none" {
		t.Errorf("rollback = %q, want none", got.Rollback)
	}
}

func TestToolAuthorizeTightensYoloForExternal(t *testing.T) {
	got := authorize(t, `{"tool_name":"web_fetch","arguments":{"url":"https://x"},"tools_mode":"yolo"}`)
	if got.Level != ToolLevelExternal {
		t.Errorf("level = %q, want external", got.Level)
	}
	if !got.RequiresUserApproval || !got.Enforced {
		t.Error("外部触达在 yolo 下同样需要确认，且要标记 enforced")
	}
}

func TestToolAuthorizeLeavesReadAndWriteAlone(t *testing.T) {
	// 只读：不确认、不二次校验、不收紧
	read := authorize(t, `{"tool_name":"read_file","arguments":{"path":"a.txt"},"tools_mode":"yolo"}`)
	if read.Level != ToolLevelRead || read.RequiresUserApproval || read.Enforced {
		t.Errorf("read 不应被加任何关：%+v", read)
	}
	// 普通写入：auto 模式下不确认（与引擎判定一致），也不算"被收紧"
	write := authorize(t, `{"tool_name":"write_file","arguments":{"path":"a.txt"},"tools_mode":"auto"}`)
	if write.Level != ToolLevelWrite {
		t.Errorf("level = %q, want write", write.Level)
	}
	if write.Enforced {
		t.Error("write 未被收紧，不应标记 enforced")
	}
	if write.Rollback != "auto" {
		t.Errorf("write_file 的副作用可回滚，rollback = %q", write.Rollback)
	}
}

func TestToolAuthorizeDefaultsModeToAuto(t *testing.T) {
	// 缺省模式按 auto：delete 仍要确认（而不是像 ask 那样把所有 write 也拦下）
	got := authorize(t, `{"tool_name":"shell_exec","arguments":{"command":"rm -rf /"}}`)
	if !got.RequiresUserApproval {
		t.Error("缺省应保守按 auto，delete 仍要确认")
	}
	if got.Enforced {
		t.Error("auto 下 delete 本来就要确认，不算收紧")
	}
}

func TestToolAuthorizeRejectsMissingToolName(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/v1/internal/tool-authorize", strings.NewReader(`{}`))
	rec := httptest.NewRecorder()
	ToolAuthorizeHandler(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

// tenant_id 的两种形态都必须收 —— 这是**故障隔离**问题，不是宽松问题。
//
// 历史事故：引擎按 JSON number 发送，而本结构体声明为 string，`json.Decode`
// 整段失败 → 400 → 引擎 fail-closed 拒掉**所有** delete/external 级工具
// （`shell_exec`、`web_fetch`…）。报错只有 "HTTP 400"，既没有字段名，也与
// "授权服务挂了"表现一致，排查代价极高。
//
// 而且当用户跑的是落后一步的打包版时，收紧服务端解码等于让旧客户端全线瘫痪。
// 所以两种形态都在这里收下，由 `parseTenantID` 统一归一。
func TestToolAuthorizeAcceptsBothTenantIDForms(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{"string", `{"tenant_id":"7","tool_name":"shell_exec","tools_mode":"auto"}`},
		{"number", `{"tenant_id":7,"tool_name":"shell_exec","tools_mode":"auto"}`},
		{"number as float", `{"tenant_id":7.0,"tool_name":"shell_exec","tools_mode":"auto"}`},
		{"null", `{"tenant_id":null,"tool_name":"shell_exec","tools_mode":"auto"}`},
		{"absent", `{"tool_name":"shell_exec","tools_mode":"auto"}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "/v1/internal/tool-authorize", strings.NewReader(tc.body))
			rec := httptest.NewRecorder()
			ToolAuthorizeHandler(rec, req)
			if rec.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200（tenant_id 形态不该让整个请求失败）: body = %s",
					rec.Code, rec.Body.String())
			}
			var envelope struct {
				Data toolAuthorizeResponse `json:"data"`
			}
			if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
				t.Fatalf("decode: %v (%s)", err, rec.Body.String())
			}
			// 判定必须照常生效：收下请求不等于放行危险工具。
			if !envelope.Data.Allowed {
				t.Error("allowed 应为 true")
			}
			if !envelope.Data.RequiresUserApproval || !envelope.Data.RequiresSecondCheck {
				t.Errorf("delete 级仍应要求确认 + 二次校验：%+v", envelope.Data)
			}
		})
	}
}

// 两种形态必须归一到**同一个**租户值，否则审计会把同一租户记成两条。
func TestFlexTenantIDNormalizesBothForms(t *testing.T) {
	for _, body := range []string{`"7"`, `7`, `7.0`} {
		var got struct {
			TenantID flexTenantID `json:"tenant_id"`
		}
		if err := json.Unmarshal([]byte(`{"tenant_id":`+body+`}`), &got); err != nil {
			t.Fatalf("%s: decode: %v", body, err)
		}
		if id := parseTenantID(string(got.TenantID)); id != 7 {
			t.Errorf("%s: parseTenantID = %d, want 7", body, id)
		}
	}
}

func TestSystemRoutesDoNotExposePublicToolExecution(t *testing.T) {
	mux := http.NewServeMux()
	passthrough := func(next http.Handler) http.Handler { return next }
	registerSystemRoutes(mux, passthrough, passthrough, passthrough, NewToolHandler(nil, nil), nil, nil, nil)

	t.Run("execute route removed", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodPost, "/v1/tools/execute", strings.NewReader(
			`{"name":"shell_exec","input":{"command":"rm -rf /tmp/h1-probe"}}`,
		))
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		if rec.Code != http.StatusNotFound {
			t.Fatalf("POST /v1/tools/execute status = %d, want 404; body = %s", rec.Code, rec.Body.String())
		}
	})

	t.Run("tool listing retained", func(t *testing.T) {
		req := httptest.NewRequest(http.MethodGet, "/v1/tools", nil)
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("GET /v1/tools status = %d, want 200; body = %s", rec.Code, rec.Body.String())
		}
	})
}
