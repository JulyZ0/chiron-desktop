package api

import (
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// L01 回归：SSE 的写期限豁免必须穿过 LoggingMiddleware 的 responseWriter。
//
// 修前：responseWriter 没有 Unwrap()，ResponseController.SetWriteDeadline 返回
// ErrNotSupported，handler 里的 `_ =` 把它吞掉；于是服务端 WriteTimeout 照常生效，
// 60s 的长流在第 60 秒被切断。这个用例把 WriteTimeout 压到 100ms、让 handler
// 睡 250ms 再写，修前必然写失败/客户端读到截断，修后应完整读到 body。
func TestLoggingMiddlewareAllowsWriteDeadlineExemption(t *testing.T) {
	const body = "stream-alive-after-server-write-timeout"

	h := LoggingMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := http.NewResponseController(w).SetWriteDeadline(time.Time{}); err != nil {
			t.Errorf("SetWriteDeadline: %v（responseWriter 是否漏了 Unwrap？）", err)
			return
		}
		// 超过 server.WriteTimeout；豁免生效时仍可写。
		time.Sleep(250 * time.Millisecond)
		_, _ = io.WriteString(w, body)
	}))

	srv := httptest.NewUnstartedServer(h)
	srv.Config.WriteTimeout = 100 * time.Millisecond
	srv.Start()
	defer srv.Close()

	resp, err := http.Get(srv.URL)
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	defer resp.Body.Close()
	got, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("读响应失败（写期限未被豁免？）: %v", err)
	}
	if string(got) != body {
		t.Fatalf("响应 = %q, want %q", got, body)
	}
}

// 上传端点依赖同一条链放宽读期限：确认 responseWriter 的 Unwrap 能到达底层。
func TestResponseWriterUnwrap(t *testing.T) {
	base := httptest.NewRecorder()
	rw := &responseWriter{ResponseWriter: base}
	if rw.Unwrap() != base {
		t.Fatal("Unwrap 未返回底层 ResponseWriter")
	}
}

// L03 回归：慢速上传不能被全局 ReadTimeout 切断。
//
// ReadTimeout=200ms，客户端分 8 段、每段 40ms 写满 500ms；handler 在读之前调用
// extendUploadReadDeadline。修前（SetReadDeadline 不可达）会在 200ms 左右失败。
func TestUploadReadDeadlineExtension(t *testing.T) {
	h := LoggingMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		extendUploadReadDeadline(w)
		n, err := io.Copy(io.Discard, r.Body)
		if err != nil {
			t.Errorf("读请求体失败: %v", err)
			return
		}
		_, _ = io.WriteString(w, fmt.Sprintf("read=%d", n))
	}))

	srv := httptest.NewUnstartedServer(h)
	srv.Config.ReadTimeout = 200 * time.Millisecond
	srv.Start()
	defer srv.Close()

	pr, pw := io.Pipe()
	go func() {
		defer pw.Close()
		for i := 0; i < 8; i++ {
			if _, err := pw.Write(make([]byte, 64)); err != nil {
				return
			}
			time.Sleep(40 * time.Millisecond)
		}
	}()
	req, err := http.NewRequest(http.MethodPost, srv.URL, pr)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("慢速上传失败（读期限未放宽？）: %v", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if string(body) != "read=512" {
		t.Fatalf("响应 = %q, want read=512", body)
	}
}
