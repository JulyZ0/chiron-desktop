package api

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

// 与 routes_desktop_test.go 同判据：请求只有匹配到已注册的模式才会进入中间件链，
// 所以"探针记到了这个路径"等价于"该路径已注册"。

func TestAudioTranscribeRouteIsRegistered(t *testing.T) {
	mux := http.NewServeMux()
	authProbe := &middlewareProbe{}
	rlProbe := &middlewareProbe{}
	// 路由是直接注册在 gateway_router 里的（没有独立的 register* 函数），这里按同样
	// 的模式串注册一份探针，锁住"路径 + 方法 + 中间件顺序"这三件事。
	mux.Handle("POST /v1/audio/transcribe", authProbe.reject(http.StatusUnauthorized)(rlProbe.passthrough(AudioTranscribeHandler(nil))))

	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/audio/transcribe", strings.NewReader("x")))

	if !authProbe.hit("/v1/audio/transcribe") {
		t.Fatal("POST /v1/audio/transcribe 未经过 authMW：匿名请求会带着 tenant_id=0 打到引擎")
	}
	if rec.Code != http.StatusUnauthorized {
		t.Errorf("未认证的转写请求应被 authMW 拒绝，得到 %d", rec.Code)
	}
}

// TestAudioTranscribeRejectsBodyOverLimit：网关先按 26 MiB 挡一道，避免把明显
// 异常的请求转给引擎（引擎侧另有一道 25 MiB 的业务上限）。
func TestAudioTranscribeRejectsBodyOverLimit(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/audio/transcribe", strings.NewReader("x"))
	req.ContentLength = audioMaxBodyBytes + 1
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 1}))

	AudioTranscribeHandler(engine.NewPythonClient("http://127.0.0.1:1")).ServeHTTP(rec, req)

	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("超限请求应返回 413，得到 %d：%s", rec.Code, rec.Body.String())
	}
}

// TestAudioTranscribeRequiresTenant：防御"路由被挪到没有 authMW 的地方"。
func TestAudioTranscribeRequiresTenant(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/audio/transcribe", strings.NewReader("x"))
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1"}))
	AudioTranscribeHandler(engine.NewPythonClient("http://127.0.0.1:1")).ServeHTTP(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Errorf("缺租户上下文应返回 401，得到 %d", rec.Code)
	}
}

// TestAudioTranscribeWithoutEngine：引擎未接线时必须是明确的 503，
// 而不是 200 空文本 —— 后者会让前端把"没转写"当成"你什么都没说"。
func TestAudioTranscribeWithoutEngine(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/audio/transcribe", strings.NewReader("x"))
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 1}))
	AudioTranscribeHandler(nil).ServeHTTP(rec, req)

	if rec.Code != http.StatusServiceUnavailable {
		t.Errorf("引擎不可用应返回 503，得到 %d", rec.Code)
	}
}

// TestAudioTranscribeProxyForwardsVerbatim 跑一遍真实代理路径（httptest 当引擎），
// 钉住三件事：
//
//  1. 音频字节**逐字节**转发 —— 网关不解析、不改写，用户的声音原样到引擎；
//  2. 响应体逐字节透传 —— 引擎的 503 原因（"缺少 faster-whisper"）必须原样到达，
//     否则前端只能笼统说"转写失败"；
//  3. 身份由 claims **重建** —— 客户端伪造的 ?user_id=attacker 不作数。
func TestAudioTranscribeProxyForwardsVerbatim(t *testing.T) {
	const engineBody = `{"text":"你好，世界","language":"zh","duration":2.5}`
	const audio = "\x1aE\xdf\xa3fake-webm-bytes"

	var gotPath string
	var gotQuery string
	var gotBody string
	var gotContentType string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotQuery = r.URL.RawQuery
		gotContentType = r.Header.Get("Content-Type")
		b, _ := io.ReadAll(r.Body)
		gotBody = string(b)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, engineBody)
	}))
	defer srv.Close()

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost,
		"/v1/audio/transcribe?tenant_id=999&user_id=attacker", strings.NewReader(audio))
	req.Header.Set("Content-Type", "audio/webm")
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 7}))

	AudioTranscribeHandler(engine.NewPythonClient(srv.URL)).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("代理应透传引擎的 200，得到 %d：%s", rec.Code, rec.Body.String())
	}
	if got := rec.Body.String(); got != engineBody {
		t.Errorf("响应体不是逐字节透传：\n got=%s\nwant=%s", got, engineBody)
	}
	if gotPath != audioTranscribePath {
		t.Errorf("转发路径不对：得到 %q，期望 %q", gotPath, audioTranscribePath)
	}
	if gotBody != audio {
		t.Errorf("音频字节被改写：\n got=%q\nwant=%q", gotBody, audio)
	}
	if gotContentType != "audio/webm" {
		t.Errorf("Content-Type 应原样转发（引擎要按它判格式）：得到 %q", gotContentType)
	}
	// RawQuery 里必须只剩按 claims 重建的那两个参数。
	if gotQuery != "tenant_id=7&user_id=u1" {
		t.Errorf("转发身份没有按 claims 重建：得到 %q，期望 tenant_id=7&user_id=u1", gotQuery)
	}
	if strings.Contains(gotQuery, "attacker") || strings.Contains(gotQuery, "999") {
		t.Errorf("客户端伪造的身份被转发出去了：%q", gotQuery)
	}
}

// TestAudioAvailabilityProxyForwards 覆盖探测端点：它必须打引擎的 availability 路径，
// 且不带音频体。
func TestAudioAvailabilityProxyForwards(t *testing.T) {
	var gotPath string
	var gotQuery string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		gotQuery = r.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"available":false,"reason":"missing faster-whisper"}`)
	}))
	defer srv.Close()

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/v1/audio/availability", nil)
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 7}))

	AudioAvailabilityHandler(engine.NewPythonClient(srv.URL)).ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("探测应透传 200，得到 %d：%s", rec.Code, rec.Body.String())
	}
	if gotPath != audioAvailabilityPath {
		t.Errorf("转发路径不对：得到 %q，期望 %q", gotPath, audioAvailabilityPath)
	}
	if gotQuery != "tenant_id=7&user_id=u1" {
		t.Errorf("探测请求的身份同样要按 claims 重建：得到 %q", gotQuery)
	}
}
