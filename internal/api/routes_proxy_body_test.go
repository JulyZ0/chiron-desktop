package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// decodeOptionalJSON 的可选 body 语义。
//
// 回归背景：`POST /v1/kb/{id}/build` 是**不带 body** 的请求（重建索引没有参数）。
// 旧实现无条件 DecodeJSON，空 body 得到 io.EOF 被当成"请求体非法"，前端点
// 「构建索引」就收到 `invalid request body` —— 而请求完全正常。
func TestDecodeOptionalJSONEmptyBodyIsOK(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{"完全没有 body", ""},
		{"只有空白字符", "   \n\t "},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := httptest.NewRequest(http.MethodPost, "/v1/kb/kb-1/build", strings.NewReader(c.body))
			w := httptest.NewRecorder()

			body, ok := decodeOptionalJSON(w, r)
			if !ok {
				t.Fatalf("空 body 必须被放行，实际拒绝了；响应=%s", w.Body.String())
			}
			// 必须返回非 nil 的 map：下游 mutateBody/guardBody 会直接写它
			if body == nil {
				t.Fatal("空 body 应返回空 map 而不是 nil，否则调用方写 body 会 panic")
			}
			if len(body) != 0 {
				t.Errorf("空 body 应解析出空 map，实际 %v", body)
			}
			if w.Body.Len() != 0 {
				t.Errorf("放行时不应写出任何响应，实际写出：%s", w.Body.String())
			}
		})
	}
}

// 合法 JSON 必须正常解析出来 —— 别让"放行空 body"变成"忽略一切 body"。
func TestDecodeOptionalJSONParsesBody(t *testing.T) {
	r := httptest.NewRequest(http.MethodPost, "/v1/kb/kb-1/build", strings.NewReader(`{"force":true}`))
	w := httptest.NewRecorder()

	body, ok := decodeOptionalJSON(w, r)
	if !ok {
		t.Fatalf("合法 JSON 不应被拒绝；响应=%s", w.Body.String())
	}
	if got, _ := body["force"].(bool); !got {
		t.Errorf("force 应为 true，实际 %v", body["force"])
	}
}

// JSON 为字面 null 时，解码后 body 是 nil —— 必须归一成空 map，
// 否则 mutateBody/guardBody 往里写字段会 panic。
func TestDecodeOptionalJSONNullBecomesEmptyMap(t *testing.T) {
	r := httptest.NewRequest(http.MethodPost, "/v1/kb/kb-1/build", strings.NewReader("null"))
	w := httptest.NewRecorder()

	body, ok := decodeOptionalJSON(w, r)
	if !ok {
		t.Fatalf("null 应按空 body 处理；响应=%s", w.Body.String())
	}
	if body == nil {
		t.Fatal("null 必须归一成空 map")
	}
}

// 语法错误仍然要 400：把错误 body 静默当成空 body 会掩盖前端参数写错，
// 那类 bug 会变成"请求发了但参数被忽略"，比直接报错难查得多。
func TestDecodeOptionalJSONRejectsMalformed(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{"语法错误", `{"force":`},
		{"类型不符", `[1,2,3]`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := httptest.NewRequest(http.MethodPost, "/v1/kb/kb-1/build", strings.NewReader(c.body))
			w := httptest.NewRecorder()

			_, ok := decodeOptionalJSON(w, r)
			if ok {
				t.Fatal("非法 JSON 必须被拒绝，否则前端参数写错会被静默忽略")
			}
			if !strings.Contains(w.Body.String(), ErrInvalidReq) {
				t.Errorf("应返回 %q，实际响应：%s", ErrInvalidReq, w.Body.String())
			}
		})
	}
}
