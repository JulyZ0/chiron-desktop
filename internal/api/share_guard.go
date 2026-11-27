package api

// share_guard.go —— 分享密码的失败计数与短锁。
//
// 为什么需要它：公开分享页是**免鉴权**入口，密码是唯一门槛。没有失败计数时，
// 一个脚本可以对同一条链接无限次尝试 —— bcrypt 虽然慢，但把网关的 CPU 与
// 数据库连接一起拖住，代价由所有访客承担。
//
// 计数维度是 (share, IP)：同一条链接被不同来源尝试互不影响；同一来源换链接
// 也不会被一条分享的锁误伤。锁只持续 60 秒 —— 目标是打断自动化重试，不是
// 永久封禁（分享密码往往被多人共用，长时间锁定会伤及正常访客）。
//
// Redis 不可用时 **fail-open**：分享页是匿名入口，读路径不该因为旁路防护
// 整体不可用；此时降级为"无计数"，并留一条 warn —— 与访问统计的旁路定位一致。

import (
	"context"
	"log/slog"
	"math"
	"net/http"
	"strconv"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

const (
	// 每个 (share, IP) 在窗口内允许的失败次数，超过即短锁。
	sharePasswordMaxFailures = 5
	// 失败计数窗口：窗口过后重新计数，避免一次误操作永久占着名额。
	sharePasswordFailureWindow = 10 * time.Minute
	// 短锁时长：只打断自动化重试；正常访客 1 分钟后可重试。
	sharePasswordLockTTL = 60 * time.Second
	// Redis 旁路操作的上限：public 页面不能被一个慢 Redis 拖住。
	shareGuardRedisTimeout = 1 * time.Second
)

// sharePasswordGuard 是密码失败计数的存储抽象。nil = 关闭计数（单测默认）。
type sharePasswordGuard interface {
	// locked 返回剩余锁定时长；未锁定返回 (0, false)。
	locked(ctx context.Context, shareID, ipHash string) (time.Duration, bool)
	// recordFailure 记一次失败；达到阈值时返回锁定时长与 true。
	recordFailure(ctx context.Context, shareID, ipHash string) (time.Duration, bool)
	// clearFailures 在密码正确后清空失败计数。
	clearFailures(ctx context.Context, shareID, ipHash string)
}

// redisSharePasswordGuard 用 Redis 存计数，天然跨副本一致。
type redisSharePasswordGuard struct{}

// 两个键用同一个 hash tag `{shareID}`：Redis Cluster 下 EVAL 的多 key 必须落在
// 同一 slot，否则脚本直接被拒。
func sharePasswordFailKey(shareID, ipHash string) string {
	return db.RedisKey("share:{" + shareID + "}:pwfail:" + ipHash)
}

func sharePasswordLockKey(shareID, ipHash string) string {
	return db.RedisKey("share:{" + shareID + "}:pwlock:" + ipHash)
}

func (redisSharePasswordGuard) client(ctx context.Context) (db.RedisClient, context.Context, context.CancelFunc, bool) {
	client, err := db.GlobalRedisManager.GetClient()
	if err != nil {
		return nil, ctx, func() {}, false
	}
	ctx, cancel := context.WithTimeout(ctx, shareGuardRedisTimeout)
	return client, ctx, cancel, true
}

func (g redisSharePasswordGuard) locked(ctx context.Context, shareID, ipHash string) (time.Duration, bool) {
	client, ctx, cancel, ok := g.client(ctx)
	if !ok {
		return 0, false
	}
	defer cancel()
	ttl, err := client.Do(ctx, "PTTL", sharePasswordLockKey(shareID, ipHash)).Int64()
	if err != nil {
		slog.Warn("share: read password lock failed", "err", err)
		return 0, false
	}
	if ttl <= 0 {
		return 0, false
	}
	return time.Duration(ttl) * time.Millisecond, true
}

// recordFailure 用 Lua 保证 INCR / PEXPIRE / SET lock 的原子性：
// 先 INCR 再单独 EXPIRE 会在进程崩溃时留下永不过期的计数键，最终把该访客
// 永久锁死。返回计数，由 Go 侧判定是否达到阈值（阈值变更不必改脚本）。
const sharePasswordFailLua = `
local n = redis.call('INCR', KEYS[1])
if n == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
end
if n >= tonumber(ARGV[2]) then
  redis.call('SET', KEYS[2], '1', 'PX', ARGV[3])
  redis.call('DEL', KEYS[1])
end
return n
`

func (g redisSharePasswordGuard) recordFailure(ctx context.Context, shareID, ipHash string) (time.Duration, bool) {
	client, ctx, cancel, ok := g.client(ctx)
	if !ok {
		return 0, false
	}
	defer cancel()
	keys := []string{
		sharePasswordFailKey(shareID, ipHash),
		sharePasswordLockKey(shareID, ipHash),
	}
	args := []interface{}{
		int64(sharePasswordFailureWindow / time.Millisecond),
		sharePasswordMaxFailures,
		int64(sharePasswordLockTTL / time.Millisecond),
	}
	n, err := client.Eval(ctx, sharePasswordFailLua, keys, args...).Int64()
	if err != nil {
		slog.Warn("share: record password failure failed", "err", err)
		return 0, false
	}
	if n >= sharePasswordMaxFailures {
		return sharePasswordLockTTL, true
	}
	return 0, false
}

func (g redisSharePasswordGuard) clearFailures(ctx context.Context, shareID, ipHash string) {
	client, ctx, cancel, ok := g.client(ctx)
	if !ok {
		return
	}
	defer cancel()
	if err := client.Del(ctx, sharePasswordFailKey(shareID, ipHash)).Err(); err != nil {
		slog.Warn("share: clear password failures failed", "err", err)
	}
}

// rejectSharePasswordLocked 写出 429；Retry-After 用秒（HTTP 标准单位），
// 响应体同时给结构化字段，前端不必解析 Header。
func rejectSharePasswordLocked(w http.ResponseWriter, retry time.Duration) {
	seconds := int(math.Ceil(retry.Seconds()))
	if seconds < 1 {
		seconds = 1
	}
	w.Header().Set("Retry-After", strconv.Itoa(seconds))
	JSONWithCode(w, http.StatusTooManyRequests, CodeShareLocked,
		map[string]interface{}{"retry_after_seconds": seconds}, "too many password attempts")
}
