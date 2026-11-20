package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/config"
)

// ── 分享链接的绝对 URL ──
//
// 守的是那条真实事故：桌面端页面跑在 `file://` 上，前端用 window.location.origin 拼
// 出来的链接是 `file:///share/<token>`，换台机器就打不开。链接必须由后端按部署事实
// 产出，前端只负责展示/复制。

func TestSharePublicURL_UsesPublicBaseURL(t *testing.T) {
	r := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:8790/v1/shares", nil)
	cfg := &config.Config{PublicBaseURL: "https://chat.example.com/"}

	got := sharePublicURL(cfg, r, "tok123")
	want := "https://chat.example.com/s/tok123"
	if got != want {
		t.Fatalf("sharePublicURL: want %q, got %q", want, got)
	}
}

// 未配置 PUBLIC_BASE_URL 时按请求推导 —— 桌面端（bind 127.0.0.1）必须至少产出
// 一个本机可打开的绝对地址，而不是相对路径。
func TestSharePublicURL_FallsBackToRequestHost(t *testing.T) {
	r := httptest.NewRequest(http.MethodPost, "http://127.0.0.1:8790/v1/conversations/x/share", nil)

	got := sharePublicURL(nil, r, "tok123")
	want := "http://127.0.0.1:8790/s/tok123"
	if got != want {
		t.Fatalf("sharePublicURL: want %q, got %q", want, got)
	}
}

func TestSharePublicURL_TLSScheme(t *testing.T) {
	r := httptest.NewRequest(http.MethodGet, "https://chat.example.com/v1/shares", nil)
	if got := sharePublicURL(nil, r, "t"); got != "https://chat.example.com/s/t" {
		t.Fatalf("TLS 请求应产 https 链接, got %q", got)
	}
}

// 可信代理送来的 X-Forwarded-Proto / -Host 才是部署协议与主机的真值。
func TestSharePublicURL_TrustedProxyForwardedHeaders(t *testing.T) {
	r := httptest.NewRequest(http.MethodGet, "http://10.0.0.5:8080/v1/shares", nil)
	r.RemoteAddr = "127.0.0.1:5555" // 直连方在可信网段内
	r.Header.Set("X-Forwarded-Proto", "https")
	r.Header.Set("X-Forwarded-Host", "chat.example.com")
	cfg := &config.Config{TrustedProxyCIDRs: []string{"127.0.0.0/8"}}

	got := sharePublicURL(cfg, r, "tok")
	want := "https://chat.example.com/s/tok"
	if got != want {
		t.Fatalf("可信代理头未被采信: want %q, got %q", want, got)
	}
}

// 不可信来源伪造的 X-Forwarded-* 绝不能进链接：否则请求方可以把分享链接指向任意主机。
func TestSharePublicURL_IgnoresForwardedFromUntrustedPeer(t *testing.T) {
	r := httptest.NewRequest(http.MethodGet, "http://10.0.0.5:8080/v1/shares", nil)
	r.RemoteAddr = "203.0.113.9:4444"
	r.Header.Set("X-Forwarded-Proto", "https")
	r.Header.Set("X-Forwarded-Host", "evil.example.com")
	cfg := &config.Config{TrustedProxyCIDRs: []string{"127.0.0.0/8"}}

	got := sharePublicURL(cfg, r, "tok")
	want := "http://10.0.0.5:8080/s/tok"
	if got != want {
		t.Fatalf("不可信来源的头被采信了: want %q, got %q", want, got)
	}
}

// 空 token 不给链接（前端据此提示"请先配置对外地址"，而不是拿到一条 `/s/` 的废链）。
func TestSharePublicURL_EmptyTokenIsEmpty(t *testing.T) {
	r := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:8790/v1/shares", nil)
	if got := sharePublicURL(nil, r, "  "); got != "" {
		t.Fatalf("空 token 应返回空串, got %q", got)
	}
}

// 前缀统一：对外产出一律 `/s/<token>`，不再产出旧形态 `/share/<token>`。
//
// 旧路由只作为前端**读侧**兼容别名存在（已发出的链接不能 404），但对外再产出旧形态
// 等于让新旧两套规格同时流通 —— 统计、缓存与运营文案都要同时兼容两条路径。
func TestSharePublicURL_UsesShortPathPrefixOnly(t *testing.T) {
	r := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:8790/v1/shares", nil)
	cfg := &config.Config{PublicBaseURL: "https://chat.example.com"}

	got := sharePublicURL(cfg, r, "tok123")
	if !strings.HasPrefix(got, "https://chat.example.com/s/") {
		t.Fatalf("分享链接必须以 /s/ 为前缀, got %q", got)
	}
	if strings.Contains(got, "/share/") {
		t.Fatalf("不应再产出旧的 /share/ 形态, got %q", got)
	}
}

// PUBLIC_BASE_URL 优先于请求头（含可信代理头）：部署方的显式配置是权威值。
func TestSharePublicURL_PublicBaseURLWinsOverForwarded(t *testing.T) {
	r := httptest.NewRequest(http.MethodGet, "http://10.0.0.5:8080/v1/shares", nil)
	r.RemoteAddr = "127.0.0.1:5555"
	r.Header.Set("X-Forwarded-Proto", "http")
	r.Header.Set("X-Forwarded-Host", "internal.local")
	cfg := &config.Config{
		PublicBaseURL:     "https://public.example.com",
		TrustedProxyCIDRs: []string{"127.0.0.0/8"},
	}

	got := sharePublicURL(cfg, r, "tok")
	if !strings.HasPrefix(got, "https://public.example.com/") {
		t.Fatalf("PUBLIC_BASE_URL 应优先: got %q", got)
	}
}

func TestFirstHeaderValue(t *testing.T) {
	cases := map[string]string{
		"":                    "",
		"https":               "https",
		"https,http":          "https",
		"  https , http ":     "https",
		"chat.example.com, x": "chat.example.com",
	}
	for raw, want := range cases {
		if got := firstHeaderValue(raw); got != want {
			t.Errorf("firstHeaderValue(%q): want %q, got %q", raw, want, got)
		}
	}
}
