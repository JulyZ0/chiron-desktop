package api

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/session"
)

// TestSubmitEmitsTurnDoneOnceForTheWholeChain 固定住"`turn_done` 是**链级**收尾信号"。
//
// 缺陷形态（用户可见）：`submitOnce` 每跑完**一段**就发一次 `turn_done`，而前端收到
// 它就关 SSE、收起 loading。Goal FSM 判定本段未完成（撞预算墙 / 清单没做完 / 被单轮
// deadline 截断）后会自动续跑下一段 —— 续跑段的 text / tool_call 于是全送进一条无人
// 订阅的流：界面停在"跑到一半就停"，刷新后结果又在（都已落库）。
//
// 判据要同时成立，缺一不可：
//  1. 续跑**真的发生了**（引擎被调用 ≥ 2 次）—— 否则这条用例根本没覆盖到缺陷路径；
//  2. `turn_done` **恰好一条**，且在所有分段之后 —— 段级发布会让它变成 N 条。
func TestSubmitEmitsTurnDoneOnceForTheWholeChain(t *testing.T) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		n := atomic.AddInt32(&calls, 1)
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		if n == 1 {
			// 第一段：留下一件未完成的事 ⇒ 判定 Incomplete ⇒ 网关续跑下一段。
			_, _ = fmt.Fprint(w, `data: {"type":"todo_updated","content":"[{\"content\":\"跑测试\",\"status\":\"pending\"}]"}`+"\n\n")
		} else {
			// 续跑段：把清单标记完成，让 FSM 收敛（否则会一路续跑到轮数上限，用例变慢）。
			_, _ = fmt.Fprint(w, `data: {"type":"todo_updated","content":"[{\"content\":\"跑测试\",\"status\":\"completed\"}]"}`+"\n\n")
		}
		_, _ = fmt.Fprintf(w, `data: {"type":"text","content":"segment-%d"}`+"\n\n", n)
		_, _ = fmt.Fprint(w, `data: {"type":"done"}`+"\n\n")
	}))
	t.Cleanup(srv.Close)

	hub := broadcast.NewHub(nil) // localOnly：不连 Redis
	sub := hub.Subscribe("observer")
	t.Cleanup(func() { hub.Unsubscribe("observer"); hub.Close() })

	h := NewSubmitHandler(
		engine.NewPythonClient(srv.URL),
		session.NewManager(nil, nil),
		hub,
		nil,
	)
	h.HandleSubmit(context.Background(), "u1", "chain-session", "long task",
		map[string]interface{}{}, nil, nil)

	// Publish 走队列异步送达，读到 turn_done（或超时）为止。
	var turnDone int
	deadline := time.After(5 * time.Second)
drain:
	for {
		select {
		case ev := <-sub:
			if ev.Type == "turn_done" {
				turnDone++
			}
		case <-deadline:
			break drain
		}
		if turnDone > 0 {
			// 再多收一会儿：段级实现会在这之后继续追加 turn_done。
			select {
			case ev := <-sub:
				if ev.Type == "turn_done" {
					turnDone++
				}
			case <-time.After(300 * time.Millisecond):
				break drain
			}
		}
	}

	if got := atomic.LoadInt32(&calls); got < 2 {
		t.Fatalf("续跑没有发生（引擎只被调用 %d 次）—— 这条用例没有覆盖到缺陷路径", got)
	}
	if turnDone != 1 {
		t.Fatalf("turn_done 必须是**链级**唯一一次，实际 %d 次；段级发布会让前端在第一段就关流", turnDone)
	}
}
