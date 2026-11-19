package api

// admin_knowledge_test.go —— 「知识库」页的取值归一、分叉判定与路由接线。
//
// 这一页有两处"不会抛错、但会静默失效"的地方，正是这里要钉住的：
//
//   ① 地址归一。引擎拿它拼 `/v1/answer`，裸 `pi:8100` 会被 URL 解析成
//      scheme=pi，请求必然失败 —— 而失败出现在用户点开知识库那一刻，不是保存时。
//   ② 分叉判定。引擎读不到状态时，"未生效"不等于"分叉"；把两者混为一谈，
//      页面就会在引擎没起时狂报"配置不一致"，教用户忽略这条提示。

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestNormalizeKnowledgeModeRejectsUnknown(t *testing.T) {
	// 与引擎侧不同：那边不认识就回落 local（配置通道不该拒收整条），
	// 这边是用户显式保存，必须报错 —— 静默改写他的选择是更坏的行为。
	if got, err := normalizeKnowledgeMode(" REMOTE "); err != nil || got != knowledgeModeRemote {
		t.Errorf("大小写与空白应被归一成 remote，得到 %q / %v", got, err)
	}
	if got, err := normalizeKnowledgeMode(""); err != nil || got != knowledgeModeLocal {
		t.Errorf("空串应等价于 local，得到 %q / %v", got, err)
	}
	if _, err := normalizeKnowledgeMode("selfhosted"); err == nil {
		t.Error("未知模式必须报错：静默回落会让用户以为已经切过去了")
	}
}

func TestNormalizeKnowledgeServiceURLRequiresSchemeAndHost(t *testing.T) {
	cases := []struct {
		in      string
		want    string
		wantErr bool
	}{
		{"", "", false}, // 本地模式下不填，合法
		{"http://127.0.0.1:8100/", "http://127.0.0.1:8100", false},
		{"  https://pi.internal/  ", "https://pi.internal", false},
		// 裸 host:port 会被 Parse 成 scheme=pi ⇒ 引擎侧请求必然失败。
		{"pi:8100", "", true},
		{"ftp://example.com", "", true},
		{"http://", "", true},
	}
	for _, c := range cases {
		got, err := normalizeKnowledgeServiceURL(c.in)
		if c.wantErr {
			if err == nil {
				t.Errorf("normalizeKnowledgeServiceURL(%q) 应报错，得到 %q", c.in, got)
			}
			continue
		}
		if err != nil {
			t.Errorf("normalizeKnowledgeServiceURL(%q) 意外报错: %v", c.in, err)
			continue
		}
		if got != c.want {
			t.Errorf("normalizeKnowledgeServiceURL(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestKnowledgeDivergedOnlyWhenBothSidesKnown(t *testing.T) {
	// 引擎没启动 ⇒ runtime 为空。此时每一项保存值都"尚未生效"，
	// 但那是读不到，不是分叉 —— 页面不该因此报警。
	if knowledgeDiverged(
		map[string]interface{}{"mode": "remote", "service_url": "http://a:8100"},
		nil,
	) {
		t.Error("引擎无运行时事实时不该判为分叉")
	}
	if knowledgeDiverged(nil, map[string]interface{}{"mode": "remote"}) {
		t.Error("库里无配置时不该判为分叉")
	}

	// 两边都说得清：模式不同 ⇒ 分叉。
	if !knowledgeDiverged(
		map[string]interface{}{"mode": "remote"},
		map[string]interface{}{"mode": "local"},
	) {
		t.Error("模式不一致应判为分叉")
	}

	// 末尾斜杠不算差异：否则归一没统一时页面会误报"未采纳"。
	if knowledgeDiverged(
		map[string]interface{}{"service_url": "http://pi:8100/"},
		map[string]interface{}{"service_url": "http://pi:8100"},
	) {
		t.Error("仅末尾斜杠不同不该判为分叉")
	}
	if !knowledgeDiverged(
		map[string]interface{}{"service_url": "http://pi:8100"},
		map[string]interface{}{"service_url": "http://pi:9100"},
	) {
		t.Error("地址不同应判为分叉")
	}

	// 保存值里没有该项时不比 —— 缺席不等于"改成了空"。
	if knowledgeDiverged(
		map[string]interface{}{"mode": "remote"},
		map[string]interface{}{"mode": "remote", "service_url": "http://pi:8100"},
	) {
		t.Error("保存值缺席的字段不该参与比较")
	}
}

func TestKnowledgeRoutesAreExposed(t *testing.T) {
	// 与 TestServiceRoutesAreExposed 同一定式：handler 写好但没接线时，
	// 前端拿到 404，而 404 与"功能没做"在界面上长得一模一样。
	mux := http.NewServeMux()
	auth := &middlewareProbe{}
	rl := &middlewareProbe{}
	registerAdminRoutes(mux, auth.reject(http.StatusUnauthorized), rl.passthrough, &AdminHandler{}, nil)

	for _, c := range []struct{ method, path string }{
		{http.MethodGet, "/v1/admin/knowledge"},
		{http.MethodPut, "/v1/admin/knowledge"},
		{http.MethodPost, "/v1/admin/knowledge/test"},
	} {
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(c.method, c.path, nil))
		if !auth.hit(c.path) {
			t.Errorf("%s %s 没有匹配到任何注册模式 ⇒ 前端会拿到 404", c.method, c.path)
			continue
		}
		if rec.Code != http.StatusUnauthorized {
			t.Errorf("%s %s 应被鉴权拦在 401，得到 %d", c.method, c.path, rec.Code)
		}
	}
}
