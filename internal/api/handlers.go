package api

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/session"
)

func handleHealth(w http.ResponseWriter, r *http.Request) {
	JSON(w, http.StatusOK, APIResponse{
		Success: true,
		Data:    map[string]string{"status": "ok"},
	})
}

func handleReadiness(w http.ResponseWriter, r *http.Request) {
	// 产品决策(2026-08-22)：Redis 为必需依赖；就绪检查反映真实依赖状态，
	// 供编排器(compose/K8s)在 Redis 故障时触发重启。
	deps := map[string]string{
		"postgres": "up",
		"redis":    "up",
	}
	ready := true
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if !db.GlobalDBManager.IsAvailable() {
		deps["postgres"] = "down"
		ready = false
	} else if err := db.GlobalDBManager.Ping(ctx); err != nil {
		deps["postgres"] = "down"
		ready = false
	}
	if db.Redis == nil || db.Redis.Ping(ctx).Err() != nil {
		deps["redis"] = "down"
		ready = false
	}
	if !ready {
		JSON(w, http.StatusServiceUnavailable, APIResponse{
			Success: false,
			Error:   "dependencies not ready",
			Data:    deps,
		})
		return
	}
	JSON(w, http.StatusOK, APIResponse{
		Success: true,
		Data:    deps,
	})
}

// errNotSessionOwner 表示请求者不是该会话的主人（取消/接管都要拦）。
var errNotSessionOwner = errors.New("session does not belong to the current user")

// userStopSignalTTL 与引擎 `app/agent/interrupt.py` 的 `INTERRUPT_TTL_SECONDS` 逐字对齐。
// 不够长会漏判：工具执行到一半的 run 要到下一个**轮次边界**才检查信号，超过 TTL 就
// 读不到了，用户刚停掉的 run 会被当成"外因中断"复活。
const userStopSignalTTL = 300 * time.Second

// markUserStop 在**用户显式停止**时给引擎留一个可判定的意图信号。
//
// 为什么必须有：网关取消一轮的手段是 `cancel()` 本地 ctx ⇒ 引擎看到的是 ASGI 取消 /
// SSE 中断，这与「网关单轮超时」「客户端断开」在引擎侧**完全同构**。引擎要决定
// "这条是不是用户下的令"，只能靠这个信号（见 `python-engine/app/agent/runtime.py`
// finally 段与 `app/agent/interrupt.py`）：有 ⇒ 写终态 `cancelled`；没有 ⇒ 保留现场
// 待续跑。不写它，用户点停止的 run 会被自动复活并继续烧 token —— 那比"多跑一轮"严重。
//
// 键名必须与 `interrupt.interrupt_key()` 逐字一致（`rkey("engine:interrupt:") + sid`，
// 两侧都由 `REDIS_KEY_PREFIX` 组装）。
//
// 失败只 warn：留不下信号时引擎退回保守方向（`cancelled`），不会误复活。
func markUserStop(ctx context.Context, sessionID string) {
	if db.Redis == nil || sessionID == "" {
		return
	}
	// 独立短超时：调用点常挂在请求 ctx 上，而取消动作本身可能已让它失效
	// （`sc.cancel()` 之后、或请求已经返回）。留信号失败只降级 —— 引擎会退回
	// 保守方向（写 `cancelled`），不会误复活 —— 所以不必也不该阻塞取消。
	wctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 2*time.Second)
	defer cancel()
	key := db.RedisKey("engine:interrupt:") + sessionID
	if err := db.Redis.Set(wctx, key, "1", userStopSignalTTL).Err(); err != nil {
		slog.Warn("mark user stop failed; engine may treat this cancel as transport loss",
			"session_id", sessionID, "error", err)
	}
}

// cancelSessionRun 是"取消某会话正在跑的一轮"的唯一实现。
//
// /v1/agent/cancel 与 super-agent 打断共用它，避免两条路径的归属校验口径漂移 ——
// 取消是"从外部作用于正在运行的 agent 循环"，归属判断错一次就是跨用户 DoS。
// 返回值是给前端的状态词，HTTP 码由调用方决定。
func cancelSessionRun(ctx context.Context, sessionMgr *session.Manager, sessionID, userID string) (string, error) {
	if v, ok := sessionCancels.Load(sessionID); ok {
		sc := v.(*sessionCancel)
		if sc.userID != userID {
			return "", errNotSessionOwner
		}
		// 原子认领：期间若条目已被替换/删除（run 已结束或被新 run 接管），就不取消。
		if !sessionCancels.CompareAndDelete(sessionID, v) {
			return "no_active_task", nil
		}
		// 用户**显式**停止：先告知子 Agent（父回合的取消不连带取消它们），再取消本地 ctx。
		if err := BroadcastSubagentSessionCancel(ctx, sessionID, "parent"); err != nil {
			slog.Warn("subagent session cancel broadcast failed",
				"session_id", sessionID, "error", err)
		}
		// 顺序要紧：先落「这是用户下的令」，再 cancel()。反过来，引擎可能在
		// cancel 生效的瞬间就跑到 finally 判据，读不到信号 ⇒ 把主动停止当成外因
		// 中断并保留现场，下一轮 reconciler 会把它自动复活。
		markUserStop(ctx, sessionID)
		sc.cancel()
		slog.Info("session cancelled", "session_id", sessionID)
		return "cancelled", nil
	}

	// 本实例没有该会话的活动任务：可能跑在别的副本上，需要广播。
	// 广播载荷里只有 sessionID（给子 Agent 的那条更不带身份），接收端无从验证 ——
	// 因此必须在这里先校验归属，否则任何登录用户拿到 sessionID 就能取消别人的运行。
	if sessionMgr != nil {
		sess, sessErr := sessionMgr.GetSession(ctx, sessionID)
		if sessErr != nil {
			if !errors.Is(sessErr, session.ErrSessionNotFound) {
				return "", sessErr
			}
			// 会话不存在 ⇒ 没有可取消的任务，也不广播（否则所有副本都去找一个不存在的会话）
			return "no_active_task", nil
		}
		if sess == nil || sess.UserID != userID {
			return "", errNotSessionOwner
		}
	}
	if err := CancelSessionBroadcast(ctx, sessionID, userID); err == nil {
		return "cancel_requested", nil
	}
	return "no_active_task", nil
}

func handleCancel(w http.ResponseWriter, r *http.Request, sessionMgr *session.Manager) {
	sessionID := r.URL.Query().Get("session_id")
	if sessionID == "" {
		BadRequest(w, "session_id is required")
		return
	}
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	status, err := cancelSessionRun(r.Context(), sessionMgr, sessionID, claims.UserID)
	switch {
	case errors.Is(err, errNotSessionOwner):
		Forbidden(w, "session does not belong to the current user")
		return
	case err != nil:
		InternalError(w, "session check failed")
		return
	}
	OK(w, map[string]string{"status": status, "session_id": sessionID})
}
