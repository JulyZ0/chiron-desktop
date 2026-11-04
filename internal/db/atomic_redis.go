package db

import (
	"context"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"
)

// AtomicRedis wraps a RedisClient and supports atomic hot-swapping.
// All RedisClient methods delegate to the current backend via atomic load.
type AtomicRedis struct {
	current atomic.Pointer[RedisClient]
}

// NewAtomicRedis creates an AtomicRedis with the given initial backend.
func NewAtomicRedis(initial RedisClient) *AtomicRedis {
	a := &AtomicRedis{}
	a.current.Store(&initial)
	return a
}

func (a *AtomicRedis) load() RedisClient {
	p := a.current.Load()
	if p == nil {
		// 未初始化时返回 nil 而非 panic，调用方已处理 nil 情况
		return nil
	}
	return *p
}

func (a *AtomicRedis) Get(ctx context.Context, key string) *redis.StringCmd {
	return a.load().Get(ctx, key)
}

func (a *AtomicRedis) Set(ctx context.Context, key string, value interface{}, expiration time.Duration) *redis.StatusCmd {
	return a.load().Set(ctx, key, value, expiration)
}

func (a *AtomicRedis) Del(ctx context.Context, keys ...string) *redis.IntCmd {
	return a.load().Del(ctx, keys...)
}

func (a *AtomicRedis) Incr(ctx context.Context, key string) *redis.IntCmd {
	return a.load().Incr(ctx, key)
}

func (a *AtomicRedis) Expire(ctx context.Context, key string, expiration time.Duration) *redis.BoolCmd {
	return a.load().Expire(ctx, key, expiration)
}

func (a *AtomicRedis) Ping(ctx context.Context) *redis.StatusCmd {
	return a.load().Ping(ctx)
}

func (a *AtomicRedis) Subscribe(ctx context.Context, channels ...string) *redis.PubSub {
	return a.load().Subscribe(ctx, channels...)
}

func (a *AtomicRedis) Close() error {
	return a.load().Close()
}

func (a *AtomicRedis) Do(ctx context.Context, args ...interface{}) *redis.Cmd {
	return a.load().Do(ctx, args...)
}

func (a *AtomicRedis) Stats() *redis.PoolStats {
	return a.load().Stats()
}

func (a *AtomicRedis) Scan(ctx context.Context, cursor uint64, match string, count int64) *redis.ScanCmd {
	return a.load().Scan(ctx, cursor, match, count)
}

func (a *AtomicRedis) Eval(ctx context.Context, script string, keys []string, args ...interface{}) *redis.Cmd {
	return a.load().Eval(ctx, script, keys, args...)
}

func (a *AtomicRedis) EvalSha(ctx context.Context, sha1 string, keys []string, args ...interface{}) *redis.Cmd {
	return a.load().EvalSha(ctx, sha1, keys, args...)
}

func (a *AtomicRedis) ScriptExists(ctx context.Context, hashes ...string) *redis.BoolSliceCmd {
	return a.load().ScriptExists(ctx, hashes...)
}

func (a *AtomicRedis) ScriptLoad(ctx context.Context, script string) *redis.StringCmd {
	return a.load().ScriptLoad(ctx, script)
}

func (a *AtomicRedis) Exists(ctx context.Context, keys ...string) *redis.IntCmd {
	return a.load().Exists(ctx, keys...)
}

func (a *AtomicRedis) XAdd(ctx context.Context, xArgs *redis.XAddArgs) *redis.StringCmd {
	return a.load().XAdd(ctx, xArgs)
}

func (a *AtomicRedis) XAck(ctx context.Context, stream, group string, ids ...string) *redis.IntCmd {
	return a.load().XAck(ctx, stream, group, ids...)
}

func (a *AtomicRedis) XGroupCreateMkStream(ctx context.Context, stream, group, start string) *redis.StatusCmd {
	return a.load().XGroupCreateMkStream(ctx, stream, group, start)
}

func (a *AtomicRedis) XReadGroup(ctx context.Context, args *redis.XReadGroupArgs) *redis.XStreamSliceCmd {
	return a.load().XReadGroup(ctx, args)
}

func (a *AtomicRedis) XRange(ctx context.Context, stream, start, stop string, count ...int64) *redis.XMessageSliceCmd {
	return a.load().XRange(ctx, stream, start, stop, count...)
}

func (a *AtomicRedis) XLen(ctx context.Context, stream string) *redis.IntCmd {
	return a.load().XLen(ctx, stream)
}

func (a *AtomicRedis) XPendingExt(ctx context.Context, args *redis.XPendingExtArgs) *redis.XPendingExtCmd {
	return a.load().XPendingExt(ctx, args)
}

func (a *AtomicRedis) XClaim(ctx context.Context, args *redis.XClaimArgs) *redis.XMessageSliceCmd {
	return a.load().XClaim(ctx, args)
}

func (a *AtomicRedis) Publish(ctx context.Context, channel string, message interface{}) *redis.IntCmd {
	return a.load().Publish(ctx, channel, message)
}

func (a *AtomicRedis) ScanAll(ctx context.Context, match string, count int64) ([]string, error) {
	return a.load().ScanAll(ctx, match, count)
}

func (a *AtomicRedis) ForEachMaster(ctx context.Context, fn func(ctx context.Context, rdb RedisClient) error) error {
	return a.load().ForEachMaster(ctx, fn)
}

// Swap atomically replaces the underlying RedisClient backend.
func (a *AtomicRedis) Swap(new RedisClient) {
	a.current.Store(&new)
}

// Mode returns the type name of the current backend: "single", "cluster", "sentinel", or "unknown".
func (a *AtomicRedis) Mode() string {
	switch a.load().(type) {
	case *SingleRedis:
		return "single"
	case *ClusterRedis:
		return "cluster"
	case *FailoverRedis:
		return "sentinel"
	default:
		return "unknown"
	}
}

// LoadRaw returns the current underlying RedisClient (for inspection).
func (a *AtomicRedis) LoadRaw() RedisClient {
	return a.load()
}
