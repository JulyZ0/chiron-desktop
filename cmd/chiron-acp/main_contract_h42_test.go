package main

import (
	"bufio"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// ── H42：ACP submit 的返回契约与分发不阻塞 ────────────────────────────────────
//
// 修复前的两个事实性缺陷：
//  1. 网关回 **202 + JSON 回执**（本仓 routes_agent.go 就是这种）时，扫描循环
//     一次都不进，函数返回默认的 end_turn —— 编辑器显示"答完了"，其实一个字都没有；
//  2. dispatch 在 stdin 循环里**同步**跑完 session/prompt，于是这一轮期间的
//     session/cancel 与权限回执全部排队：用户点了取消，要等模型自己跑完才被读到。
//
// 这一组把两条都钉住。

func promptGateway(t *testing.T, handler http.HandlerFunc) (*conn, *strings.Builder) {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	c := newConn(srv.URL, "tok")
	var sb strings.Builder
	c.out = bufio.NewWriter(&sb)
	return c, &sb
}

func TestStreamPromptRejects202Acknowledgement(t *testing.T) {
	c, _ := promptGateway(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		fmt.Fprint(w, `{"success":true,"data":{"session_id":"s1","status":"accepted"}}`)
	})

	_, err := c.streamPrompt(t.Context(), "s1", "hi")
	if err == nil {
		t.Fatal("202 回执没有事件流，必须报错，不能报 end_turn（假成功）")
	}
	if !strings.Contains(err.Error(), "202") {
		t.Fatalf("错误里应点明 202 回执：%v", err)
	}
}

func TestStreamPromptRejectsJSONWithoutTerminal(t *testing.T) {
	c, _ := promptGateway(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"success":true,"data":{"session_id":"s1"}}`)
	})

	if _, err := c.streamPrompt(t.Context(), "s1", "hi"); err == nil {
		t.Fatal("只有 JSON 回执（无 stop_reason）时必须报错")
	}
}

func TestStreamPromptAcceptsJSONWithExplicitStop(t *testing.T) {
	c, _ := promptGateway(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"success":true,"stop_reason":"end_turn"}`)
	})

	stop, err := c.streamPrompt(t.Context(), "s1", "hi")
	if err != nil {
		t.Fatalf("显式终态应被接受：%v", err)
	}
	if stop != stopEndTurn {
		t.Fatalf("want end_turn, got %s", stop)
	}
}

func TestStreamPromptRejectsJSONErrorEnvelope(t *testing.T) {
	c, _ := promptGateway(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		// 即使带了 stop_reason，错误 envelope 也不算"助手终态"。
		fmt.Fprint(w, `{"success":false,"error":"boom","stop_reason":"end_turn"}`)
	})

	if _, err := c.streamPrompt(t.Context(), "s1", "hi"); err == nil {
		t.Fatal("错误 envelope 不得被当成成功终态")
	}
}

func TestStreamPromptRejectsStreamWithoutTerminalEvent(t *testing.T) {
	c, _ := promptGateway(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		// 有文本、有内容，但**没有** done/turn_done 就断流（网关半路挂掉）。
		fmt.Fprint(w, "data: {\"type\":\"text\",\"content\":\"half\"}\n\n")
	})

	if _, err := c.streamPrompt(t.Context(), "s1", "hi"); err == nil {
		t.Fatal("没有终态事件就断流，必须报错")
	}
}

func TestStreamPromptRejectsEmptyTurnDone(t *testing.T) {
	c, _ := promptGateway(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"type\":\"turn_done\"}\n\n")
	})

	if _, err := c.streamPrompt(t.Context(), "s1", "hi"); err == nil {
		t.Fatal("一个产物都没有的 turn_done 不得报成功")
	}
}

func TestStreamPromptAcceptsTurnDoneAfterText(t *testing.T) {
	c, _ := promptGateway(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		fmt.Fprint(w, "data: {\"type\":\"text\",\"content\":\"hello\"}\n\ndata: {\"type\":\"turn_done\"}\n\n")
	})

	stop, err := c.streamPrompt(t.Context(), "s1", "hi")
	if err != nil {
		t.Fatalf("正常一轮应成功：%v", err)
	}
	if stop != stopEndTurn {
		t.Fatalf("want end_turn, got %s", stop)
	}
}

func TestStreamPromptSniffsSSEWithoutContentType(t *testing.T) {
	// 经反代/测试桩时 Content-Type 可能丢失；帧形态仍能认出事件流。
	c, _ := promptGateway(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		fmt.Fprint(w, "data: {\"type\":\"text\",\"content\":\"hi\"}\n\ndata: {\"type\":\"done\"}\n\n")
	})

	if _, err := c.streamPrompt(t.Context(), "s1", "hi"); err != nil {
		t.Fatalf("SSE 帧形态应被识别：%v", err)
	}
}

// TestDispatchDoesNotBlockCancel —— H42 的核心：长 prompt 不能堵住 stdin 循环。
//
// 做法：让网关在 prompt 上**一直挂着**（直到测试结束），然后立刻分发一条
// session/cancel。若 dispatch 仍是同步的，dispatch(cancel) 根本轮不到执行
// （测试会超时）；改成 goroutine 后，取消能在毫秒级被处理。
func TestDispatchDoesNotBlockCancel(t *testing.T) {
	release := make(chan struct{})
	var once sync.Once
	defer once.Do(func() { close(release) })

	var mu sync.Mutex
	cancelled := false

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/agent/submit":
			<-release // 模拟"这一轮要跑很久"
			w.Header().Set("Content-Type", "text/event-stream")
			fmt.Fprint(w, "data: {\"type\":\"text\",\"content\":\"late\"}\n\ndata: {\"type\":\"done\"}\n\n")
		case "/v1/agent/interrupt":
			mu.Lock()
			cancelled = true
			mu.Unlock()
			fmt.Fprint(w, `{"success":true}`)
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()

	c := newConn(srv.URL, "tok")
	var sb strings.Builder
	c.out = bufio.NewWriter(&sb)

	ctx := t.Context()
	c.dispatch(ctx, []byte(`{"jsonrpc":"2.0","id":7,"method":"session/prompt","params":{"sessionId":"s1","prompt":[{"type":"text","text":"hi"}]}}`))

	// 立刻分发取消：同步实现下这里会一直等到 release 关闭。
	done := make(chan struct{})
	go func() {
		c.dispatch(ctx, []byte(`{"jsonrpc":"2.0","method":"session/cancel","params":{"sessionId":"s1"}}`))
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("session/cancel 被长 prompt 阻塞：dispatch 必须异步（H42）")
	}

	mu.Lock()
	got := cancelled
	mu.Unlock()
	if !got {
		t.Fatal("取消没有打到网关 /v1/agent/interrupt")
	}

	once.Do(func() { close(release) })
}
