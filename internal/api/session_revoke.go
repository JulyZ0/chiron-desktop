package api

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/athenavi/chiron/internal/db"
)

// 会话纪元（session epoch）—— 让"改密/密码重置"这个标准止血动作真正生效。
//
// ## 为什么需要它
//
// JWT 是无状态的：改密成功后，**此前签发的 token 仍然有效**直到过期（默认 24h）。
// 也就是说凭据一旦泄露，用户改密并不能把攻击者踢下线 —— 而改密正是业界标准的
// 止血动作。全仓此前没有任何"按用户批量撤销"的机制（只有按 jti 的单次登出黑名单）。
//
// ## 与 jwt:blacklist 的分工
//
// * `jwt:blacklist:<jti>` —— **单次登出**，精确到某一个 token；
// * `jwt:epoch:<userID>`  —— **批量撤销**，一次让该用户此刻之前的**所有** token 失效。
//
// ## 判定方式
//
// 纪元值是该用户被撤销时的 Unix 秒。`authMW` 每次请求比对：
// `epoch > token.iat` ⇒ 该 token 签发于撤销之前 ⇒ 拒绝。
// 精度是**秒**：同一秒内签发又撤销的 token 不会被拦（窗口 1 秒，可接受）。
const sessionEpochKeyPrefix = "jwt:epoch:"

// sessionEpochTTL 略大于 JWT 最大有效期：保证被撤销的 token 在自然过期前，
// 纪元键一直存在（否则纪元一消失，旧 token 又"复活"了）。
const sessionEpochTTL = 25 * time.Hour

// authRevocationChannel 是跨实例撤销事件通道：单次登出广播裸 jti，
// 批量撤销广播 wsAuthEpochEvent（type=epoch_revoked）JSON。
// 同一个 channel 两种负载由 handleWebSocketBlacklistEvent 分流。
const authRevocationChannelSuffix = "jwt:blacklist:sync"

func authRevocationChannel() string {
	return db.RedisKey(authRevocationChannelSuffix)
}

// broadcastSessionEpochRevocation 把"某用户已批量撤销"广播给其它网关实例。
//
// 为什么必须广播：纪元键只解决**新连接**与**下一次重验**；已经建立的长连接
// （/ws/rpa 拿着浏览器、/ws/device-fs 直连用户磁盘）默认要等到 15s 后的周期性
// 重验才断。撤销（改密、禁用、强制下线）是要立刻止血的动作，因此这里主动推事件，
// 收到事件的实例直接关闭该用户的在连长连接。
//
// 广播失败只记日志：Redis 已经写入纪元，新连接与重验仍然会失败，宁可慢 15s 也不能
// 让"撤销成功但日志报错"变成回滚理由。
func broadcastSessionEpochRevocation(userID string, epoch int64) {
	if db.Redis == nil || userID == "" || epoch <= 0 {
		return
	}
	payload, err := json.Marshal(wsAuthEpochEvent{
		Type:   wsAuthEpochEventType,
		UserID: userID,
		Epoch:  epoch,
	})
	if err != nil {
		slog.Warn("session revoke event encode failed", "user_id", userID, "error", err)
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := db.Redis.Publish(ctx, authRevocationChannel(), string(payload)).Err(); err != nil {
		slog.Warn("session revoke event broadcast failed", "user_id", userID, "error", err)
	}
}

// revokeUserSessions 让该用户**当前已签发**的所有 JWT 立即失效。
//
// 返回 error 由调用方决定如何处置：改密本身已经成功，不该因为撤销失败而回滚，
// 但**必须记日志**（否则"以为踢下线了、其实没有"）。
func revokeUserSessions(ctx context.Context, userID string) error {
	if userID == "" {
		return errors.New("session revoke: empty user id")
	}
	if db.Redis == nil {
		return errors.New("session revoke store unavailable (redis not configured)")
	}
	epoch := time.Now().Unix()
	if err := db.Redis.Set(ctx, db.RedisKey(sessionEpochKeyPrefix+userID), epoch, sessionEpochTTL).Err(); err != nil {
		return err
	}
	// 写纪元成功后才广播：先推事件后写键会出现"连接被断开、但重连又能连上"的窗口。
	broadcastSessionEpochRevocation(userID, epoch)
	return nil
}

// tokenRevokedByEpoch 判断这枚 token 是否签发于最近一次批量撤销之前。
//
// Session / Refresh 这类**不在 authMW 后面**的入口必须自己调用：否则用旧 token
// 刷新可以换到一枚新有效期的 token，把"改密/禁用后立即失效"整条链路绕过去。
func tokenRevokedByEpoch(ctx context.Context, userID string, issuedAtUnix int64, hasIssuedAt bool) (bool, error) {
	epoch, err := sessionEpoch(ctx, userID)
	if err != nil {
		return false, err
	}
	return hasIssuedAt && epoch > 0 && epoch > issuedAtUnix, nil
}

// sessionEpoch 读取用户的会话纪元；无记录（从未被撤销）返回 0。
//
// Redis 不可用时返回 error —— 调用方（authMW）按 **fail-close** 处理：拒绝请求，
// 而不是放行（放行会让撤销在故障期间失效）。
func sessionEpoch(ctx context.Context, userID string) (int64, error) {
	if userID == "" {
		return 0, nil
	}
	if db.Redis == nil {
		return 0, errors.New("session revoke store unavailable (redis not configured)")
	}
	epoch, err := db.Redis.Get(ctx, db.RedisKey(sessionEpochKeyPrefix+userID)).Int64()
	if errors.Is(err, redis.Nil) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	return epoch, nil
}
