package api

// admin_service_test.go —— 「服务」页的配置归一与校验。
//
// 这里钉住的是**会静默生效错的**那几处：监听地址的空串语义、主机名的
// DNS 依赖、对外地址缺协议、以及 Cookie Secure 的连带后果。它们都不会抛错，
// 只会在用户改完这一页、重启之后表现为"打不开 / 登不上"。

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestNormalizeBindHostUnifiesWildcards(t *testing.T) {
	// 空串、`0.0.0.0`、`::`、`*` 在 `net.Listen` 里都是"全部网卡"。
	// 不归一就会让页面显示"局域网"，实际存的是空串 —— 看起来改了、对比又不等。
	for _, in := range []string{"", "   ", "0.0.0.0", "::", "*", "[::]"} {
		if got := normalizeBindHost(in); got != bindAllInterfaces {
			t.Errorf("normalizeBindHost(%q) = %q, want %q", in, got, bindAllInterfaces)
		}
	}
	if got := normalizeBindHost("127.0.0.1"); got != "127.0.0.1" {
		t.Errorf("回环地址不该被改写，得到 %q", got)
	}
}

func TestNormalizeBindHostInputAcceptsIPsOnly(t *testing.T) {
	// 主机名要靠 DNS 解析，而解析结果可能在启动时并不存在 ⇒ 网关起不来，
	// 且错误发生在用户改完配置重启之后。因此只收 IP 字面量。
	if _, err := normalizeBindHostInput("localhost"); err == nil {
		t.Error("主机名应被拒绝：重启时可能解析不到，表现为网关起不来")
	}
	if _, err := normalizeBindHostInput("999.1.1.1"); err == nil {
		t.Error("非法 IP 应被拒绝")
	}
	got, err := normalizeBindHostInput(" 127.0.0.1 ")
	if err != nil || got != bindLoopback {
		t.Errorf("回环地址应被接受并去空白，得到 %q / %v", got, err)
	}
	got, err = normalizeBindHostInput("")
	if err != nil || got != bindAllInterfaces {
		t.Errorf("空串应等价于全部网卡，得到 %q / %v", got, err)
	}
}

func TestNormalizePublicBaseURLRejectsUnusableValues(t *testing.T) {
	// 分享链接按它拼，拼出来的东西必须在任何客户端都能打开。
	cases := []struct {
		in      string
		want    string
		wantErr bool
	}{
		{"", "", false}, // 清空 = 未配置
		{"https://chat.example.com/", "https://chat.example.com", false},
		{"http://192.168.1.10:8080", "http://192.168.1.10:8080", false},
		{"chat.example.com", "", true},   // 缺协议
		{"ftp://example.com", "", true},  // 非 http(s)
		{"https://", "", true},           // 缺主机
	}
	for _, c := range cases {
		got, err := normalizePublicBaseURL(c.in)
		if c.wantErr {
			if err == nil {
				t.Errorf("normalizePublicBaseURL(%q) 应报错，得到 %q", c.in, got)
			}
			continue
		}
		if err != nil {
			t.Errorf("normalizePublicBaseURL(%q) 意外报错: %v", c.in, err)
			continue
		}
		if got != c.want {
			t.Errorf("normalizePublicBaseURL(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestServiceConfigWarningFlagsRealTraps(t *testing.T) {
	// ① 只听本机却配了对外地址：域名指向的机器上没有东西在听那个端口。
	if w := serviceConfigWarning(bindLoopback, "https://chat.example.com"); w == "" {
		t.Error("127.0.0.1 + 对外地址 应提示其它设备访问不到")
	}
	// ② https 对外地址会打开 Cookie Secure；仍用 http 访问的设备发不出 cookie。
	if w := serviceConfigWarning(bindAllInterfaces, "https://chat.example.com"); !strings.Contains(w, "Secure") {
		t.Errorf("https 对外地址应提示 Cookie Secure 的连带后果，得到 %q", w)
	}
	// 正常的局域网组合不该天天弹提示 —— 一条总是出现的提示会被学会忽略。
	if w := serviceConfigWarning(bindAllInterfaces, "http://192.168.1.10:8080"); w != "" {
		t.Errorf("局域网 http 组合不该有警示，得到 %q", w)
	}
	if w := serviceConfigWarning(bindAllInterfaces, ""); w != "" {
		t.Errorf("未配对外地址不该有警示，得到 %q", w)
	}
}

func TestSavedServiceConfigAdoptsExplicitEmpty(t *testing.T) {
	// 用户显式清空对外地址后，库里就是空串。若这里按"非空才采用"回落成生效值，
	// 页面会显示"没改过"，而实际已改 —— 于是反复保存、反复看起来没生效。
	bind, url := savedServiceConfig("0.0.0.0", "https://chat.example.com", map[string]interface{}{
		"public_base_url": "",
	})
	if url != "" {
		t.Errorf("显式清空必须被采用，得到 %q", url)
	}
	if bind != bindAllInterfaces {
		t.Errorf("未配置 bind_host 时回落成生效值，得到 %q", bind)
	}

	// 存的是空串、生效的是 0.0.0.0：两者同义，不该显示成一次待生效改动。
	bind, _ = savedServiceConfig(bindAllInterfaces, "", map[string]interface{}{"bind_host": ""})
	if bind != bindAllInterfaces {
		t.Errorf("空串应归一成 0.0.0.0，得到 %q", bind)
	}

	// 读不到配置（nil）时完全回落成生效值，页面不会凭空报"需要重启"。
	bind, url = savedServiceConfig(bindLoopback, "http://a.example.com", nil)
	if bind != bindLoopback || url != "http://a.example.com" {
		t.Errorf("无持久化配置时应回落成生效值，得到 %q / %q", bind, url)
	}
}

func TestServiceRestartRequiredCoversBothFields(t *testing.T) {
	base := AdminServiceInfo{
		BindHost:           bindAllInterfaces,
		PublicBaseURL:      "https://chat.example.com",
		SavedBindHost:      bindAllInterfaces,
		SavedPublicBaseURL: "https://chat.example.com",
	}
	if serviceRestartRequired(base) {
		t.Error("两项一致时不该要求重启")
	}

	// 只改对外地址也要重启：它在启动时读入并据此推导 CookieSecure，
	// 不重启则进程里仍是旧值 —— 页面若说"已生效"就是在骗人。
	urlOnly := base
	urlOnly.SavedPublicBaseURL = "http://chat.example.com"
	if !serviceRestartRequired(urlOnly) {
		t.Error("只改对外地址也必须要求重启")
	}

	hostOnly := base
	hostOnly.SavedBindHost = bindLoopback
	if !serviceRestartRequired(hostOnly) {
		t.Error("改监听地址必须要求重启")
	}
}

func TestServiceRoutesAreExposed(t *testing.T) {
	// 与 TestAdminDatastoreRoutesAreExposed 同一定式：handler 写好但没接线时，
	// 前端拿到的是 404，而 404 与"功能没做"在界面上长得一模一样。
	mux := http.NewServeMux()
	auth := &middlewareProbe{}
	rl := &middlewareProbe{}
	registerAdminRoutes(mux, auth.reject(http.StatusUnauthorized), rl.passthrough, &AdminHandler{}, nil)

	for _, c := range []struct{ method, path string }{
		{http.MethodGet, "/v1/admin/service"},
		{http.MethodPut, "/v1/admin/service"},
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
