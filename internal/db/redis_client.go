package db

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/redis/go-redis/v9"
)

// RedisClient defines the interface for Redis operations.
// Supports single, cluster, and sentinel modes.
type RedisClient interface {
	Get(ctx context.Context, key string) *redis.StringCmd
	Set(ctx context.Context, key string, value interface{}, expiration time.Duration) *redis.StatusCmd
	Del(ctx context.Context, keys ...string) *redis.IntCmd
	Incr(ctx context.Context, key string) *redis.IntCmd
	Expire(ctx context.Context, key string, expiration time.Duration) *redis.BoolCmd
	Ping(ctx context.Context) *redis.StatusCmd
	Subscribe(ctx context.Context, channels ...string) *redis.PubSub
	Close() error
	Stats() *redis.PoolStats
	// Do 执行任意 Redis 命令（慢日志/FLUSH 等管理操作）
	Do(ctx context.Context, args ...interface{}) *redis.Cmd
	Scan(ctx context.Context, cursor uint64, match string, count int64) *redis.ScanCmd
	Eval(ctx context.Context, script string, keys []string, args ...interface{}) *redis.Cmd
	EvalSha(ctx context.Context, sha1 string, keys []string, args ...interface{}) *redis.Cmd
	ScriptExists(ctx context.Context, hashes ...string) *redis.BoolSliceCmd
	ScriptLoad(ctx context.Context, script string) *redis.StringCmd
	XAdd(ctx context.Context, a *redis.XAddArgs) *redis.StringCmd
	XAck(ctx context.Context, stream, group string, ids ...string) *redis.IntCmd
	XGroupCreateMkStream(ctx context.Context, stream, group, start string) *redis.StatusCmd
	XReadGroup(ctx context.Context, a *redis.XReadGroupArgs) *redis.XStreamSliceCmd
	// XRange returns stream entries between start and stop; when start is "+"
	// the scan is reversed (latest first). Optional count limits the results.
	XRange(ctx context.Context, stream, start, stop string, count ...int64) *redis.XMessageSliceCmd
	XLen(ctx context.Context, stream string) *redis.IntCmd
	// XPendingExt 返回消费组 pending 明细（含 idle/delivery 计数），崩溃恢复认领用。
	XPendingExt(ctx context.Context, a *redis.XPendingExtArgs) *redis.XPendingExtCmd
	// XClaim 把 idle 超阈值的 pending 消息认领到本 consumer（崩溃恢复）。
	XClaim(ctx context.Context, a *redis.XClaimArgs) *redis.XMessageSliceCmd
	Exists(ctx context.Context, keys ...string) *redis.IntCmd
	Publish(ctx context.Context, channel string, message interface{}) *redis.IntCmd
	// ScanAll 跨节点遍历所有匹配 match 的键。
	// 单机/哨兵：普通 SCAN 游标循环；Cluster：遍历全部 master（每 master 独立游标）。
	// 注意：直接用 Scan 在 Cluster 下只覆盖单个节点，会漏键（模型发现曾因此漏读 provider key）。
	ScanAll(ctx context.Context, match string, count int64) ([]string, error)
	// ForEachMaster 在每个 master 上执行 fn（单机/哨兵仅执行一次）。
	// 用于 FLUSHDB 这类必须逐节点执行、无法跨节点的管理命令。
	ForEachMaster(ctx context.Context, fn func(ctx context.Context, rdb RedisClient) error) error
}

// SingleRedis implements RedisClient for a single Redis instance.
type SingleRedis struct {
	client *redis.Client
}

// NewSingleRedis creates a new single Redis client.
func NewSingleRedis(addr, password string, db int, poolSize int) (*SingleRedis, error) {
	if poolSize <= 0 {
		poolSize = 50
	}
	opts := &redis.Options{
		Addr:         addr,
		Password:     password,
		DB:           db,
		DialTimeout:  3 * time.Second,
		ReadTimeout:  3 * time.Second,
		WriteTimeout: 3 * time.Second,
		PoolSize:     poolSize,
		MinIdleConns: 0,
		MaxRetries:   0,
		PoolTimeout:  2 * time.Second,
	}

	client := redis.NewClient(opts)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := client.Ping(ctx).Err(); err != nil {
		return nil, fmt.Errorf("redis ping: %w", err)
	}

	slog.Info("redis connected (single)", "addr", addr, "db", db)
	return &SingleRedis{client: client}, nil
}

func (s *SingleRedis) Get(ctx context.Context, key string) *redis.StringCmd {
	return s.client.Get(ctx, key)
}

func (s *SingleRedis) Set(ctx context.Context, key string, value interface{}, expiration time.Duration) *redis.StatusCmd {
	return s.client.Set(ctx, key, value, expiration)
}

func (s *SingleRedis) Del(ctx context.Context, keys ...string) *redis.IntCmd {
	return s.client.Del(ctx, keys...)
}

func (s *SingleRedis) Incr(ctx context.Context, key string) *redis.IntCmd {
	return s.client.Incr(ctx, key)
}

func (s *SingleRedis) Expire(ctx context.Context, key string, expiration time.Duration) *redis.BoolCmd {
	return s.client.Expire(ctx, key, expiration)
}

func (s *SingleRedis) Ping(ctx context.Context) *redis.StatusCmd {
	return s.client.Ping(ctx)
}

func (s *SingleRedis) Subscribe(ctx context.Context, channels ...string) *redis.PubSub {
	return s.client.Subscribe(ctx, channels...)
}

func (s *SingleRedis) Close() error {
	return s.client.Close()
}

func (s *SingleRedis) Do(ctx context.Context, args ...interface{}) *redis.Cmd {
	return s.client.Do(ctx, args...)
}

func (s *SingleRedis) Stats() *redis.PoolStats {
	return s.client.PoolStats()
}

func (s *SingleRedis) Scan(ctx context.Context, cursor uint64, match string, count int64) *redis.ScanCmd {
	return s.client.Scan(ctx, cursor, match, count)
}

// scanAllOn 在单个节点上完成 SCAN 游标循环（单机/哨兵与 Cluster 的每个 master 复用）。
func scanAllOn(ctx context.Context, client *redis.Client, match string, count int64) ([]string, error) {
	if count <= 0 {
		count = 500
	}
	var cursor uint64
	var out []string
	for {
		keys, next, err := client.Scan(ctx, cursor, match, count).Result()
		if err != nil {
			return nil, err
		}
		out = append(out, keys...)
		cursor = next
		if cursor == 0 {
			return out, nil
		}
	}
}

// ScanAll 单机只有一组键空间：直接做完整游标扫描。
func (s *SingleRedis) ScanAll(ctx context.Context, match string, count int64) ([]string, error) {
	return scanAllOn(ctx, s.client, match, count)
}

// ForEachMaster 单机只有一个节点：直接执行一次。
func (s *SingleRedis) ForEachMaster(ctx context.Context, fn func(ctx context.Context, rdb RedisClient) error) error {
	return fn(ctx, s)
}

func (s *SingleRedis) Eval(ctx context.Context, script string, keys []string, args ...interface{}) *redis.Cmd {
	return s.client.Eval(ctx, script, keys, args...)
}

func (s *SingleRedis) EvalSha(ctx context.Context, sha1 string, keys []string, args ...interface{}) *redis.Cmd {
	return s.client.EvalSha(ctx, sha1, keys, args...)
}

func (s *SingleRedis) ScriptExists(ctx context.Context, hashes ...string) *redis.BoolSliceCmd {
	return s.client.ScriptExists(ctx, hashes...)
}

func (s *SingleRedis) ScriptLoad(ctx context.Context, script string) *redis.StringCmd {
	return s.client.ScriptLoad(ctx, script)
}

func (s *SingleRedis) XAdd(ctx context.Context, a *redis.XAddArgs) *redis.StringCmd {
	return s.client.XAdd(ctx, a)
}

func (s *SingleRedis) XAck(ctx context.Context, stream, group string, ids ...string) *redis.IntCmd {
	return s.client.XAck(ctx, stream, group, ids...)
}

func (s *SingleRedis) XGroupCreateMkStream(ctx context.Context, stream, group, start string) *redis.StatusCmd {
	return s.client.XGroupCreateMkStream(ctx, stream, group, start)
}

func (s *SingleRedis) XReadGroup(ctx context.Context, a *redis.XReadGroupArgs) *redis.XStreamSliceCmd {
	return s.client.XReadGroup(ctx, a)
}

func (s *SingleRedis) XRange(ctx context.Context, stream, start, stop string, count ...int64) *redis.XMessageSliceCmd {
	if start == "+" {
		if len(count) > 0 {
			return s.client.XRevRangeN(ctx, stream, start, stop, count[0])
		}
		return s.client.XRevRange(ctx, stream, start, stop)
	}
	if len(count) > 0 {
		return s.client.XRangeN(ctx, stream, start, stop, count[0])
	}
	return s.client.XRange(ctx, stream, start, stop)
}

func (s *SingleRedis) XLen(ctx context.Context, stream string) *redis.IntCmd {
	return s.client.XLen(ctx, stream)
}

func (s *SingleRedis) XPendingExt(ctx context.Context, a *redis.XPendingExtArgs) *redis.XPendingExtCmd {
	return s.client.XPendingExt(ctx, a)
}

func (s *SingleRedis) XClaim(ctx context.Context, a *redis.XClaimArgs) *redis.XMessageSliceCmd {
	return s.client.XClaim(ctx, a)
}

func (s *SingleRedis) Exists(ctx context.Context, keys ...string) *redis.IntCmd {
	return s.client.Exists(ctx, keys...)
}

func (s *SingleRedis) Publish(ctx context.Context, channel string, message interface{}) *redis.IntCmd {
	return s.client.Publish(ctx, channel, message)
}

// ClusterRedis implements RedisClient for a Redis cluster.
type ClusterRedis struct {
	client *redis.ClusterClient
}

// NewClusterRedis creates a new Redis cluster client.
func NewClusterRedis(addrs []string, password string, poolSize int) (*ClusterRedis, error) {
	if poolSize <= 0 {
		poolSize = 50
	}
	opts := &redis.ClusterOptions{
		Addrs:        addrs,
		Password:     password,
		DialTimeout:  3 * time.Second,
		ReadTimeout:  3 * time.Second,
		WriteTimeout: 3 * time.Second,
		PoolSize:     poolSize,
		MinIdleConns: 0,
		MaxRetries:   0,
		PoolTimeout:  2 * time.Second,
	}

	client := redis.NewClusterClient(opts)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := client.Ping(ctx).Err(); err != nil {
		return nil, fmt.Errorf("redis cluster ping: %w", err)
	}

	slog.Info("redis connected (cluster)", "addrs", addrs)
	return &ClusterRedis{client: client}, nil
}

func (c *ClusterRedis) Get(ctx context.Context, key string) *redis.StringCmd {
	return c.client.Get(ctx, key)
}

func (c *ClusterRedis) Set(ctx context.Context, key string, value interface{}, expiration time.Duration) *redis.StatusCmd {
	return c.client.Set(ctx, key, value, expiration)
}

func (c *ClusterRedis) Del(ctx context.Context, keys ...string) *redis.IntCmd {
	return c.client.Del(ctx, keys...)
}

func (c *ClusterRedis) Incr(ctx context.Context, key string) *redis.IntCmd {
	return c.client.Incr(ctx, key)
}

func (c *ClusterRedis) Expire(ctx context.Context, key string, expiration time.Duration) *redis.BoolCmd {
	return c.client.Expire(ctx, key, expiration)
}

func (c *ClusterRedis) Ping(ctx context.Context) *redis.StatusCmd {
	return c.client.Ping(ctx)
}

func (c *ClusterRedis) Subscribe(ctx context.Context, channels ...string) *redis.PubSub {
	return c.client.Subscribe(ctx, channels...)
}

func (c *ClusterRedis) Close() error {
	return c.client.Close()
}

func (c *ClusterRedis) Do(ctx context.Context, args ...interface{}) *redis.Cmd {
	return c.client.Do(ctx, args...)
}

func (c *ClusterRedis) Stats() *redis.PoolStats {
	return c.client.PoolStats()
}

func (c *ClusterRedis) Scan(ctx context.Context, cursor uint64, match string, count int64) *redis.ScanCmd {
	return c.client.Scan(ctx, cursor, match, count)
}

// ScanAll 遍历全部 master：Cluster 下 SCAN 只作用于被路由到的单个节点，
// 必须逐 master 扫描才能不漏键（模型发现据此修复 provider key 漏读）。
func (c *ClusterRedis) ScanAll(ctx context.Context, match string, count int64) ([]string, error) {
	var out []string
	err := c.client.ForEachMaster(ctx, func(ctx context.Context, node *redis.Client) error {
		keys, err := scanAllOn(ctx, node, match, count)
		if err != nil {
			return err
		}
		out = append(out, keys...)
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

// ForEachMaster 在每个 master 上执行 fn（FLUSHDB 等命令无法跨节点执行）。
func (c *ClusterRedis) ForEachMaster(ctx context.Context, fn func(ctx context.Context, rdb RedisClient) error) error {
	return c.client.ForEachMaster(ctx, func(ctx context.Context, node *redis.Client) error {
		return fn(ctx, &SingleRedis{client: node})
	})
}

func (c *ClusterRedis) Eval(ctx context.Context, script string, keys []string, args ...interface{}) *redis.Cmd {
	return c.client.Eval(ctx, script, keys, args...)
}

func (c *ClusterRedis) EvalSha(ctx context.Context, sha1 string, keys []string, args ...interface{}) *redis.Cmd {
	return c.client.EvalSha(ctx, sha1, keys, args...)
}

func (c *ClusterRedis) ScriptExists(ctx context.Context, hashes ...string) *redis.BoolSliceCmd {
	return c.client.ScriptExists(ctx, hashes...)
}

func (c *ClusterRedis) ScriptLoad(ctx context.Context, script string) *redis.StringCmd {
	return c.client.ScriptLoad(ctx, script)
}

func (c *ClusterRedis) XAdd(ctx context.Context, a *redis.XAddArgs) *redis.StringCmd {
	return c.client.XAdd(ctx, a)
}

func (c *ClusterRedis) XAck(ctx context.Context, stream, group string, ids ...string) *redis.IntCmd {
	return c.client.XAck(ctx, stream, group, ids...)
}

func (c *ClusterRedis) XGroupCreateMkStream(ctx context.Context, stream, group, start string) *redis.StatusCmd {
	return c.client.XGroupCreateMkStream(ctx, stream, group, start)
}

func (c *ClusterRedis) XReadGroup(ctx context.Context, a *redis.XReadGroupArgs) *redis.XStreamSliceCmd {
	return c.client.XReadGroup(ctx, a)
}

func (c *ClusterRedis) XRange(ctx context.Context, stream, start, stop string, count ...int64) *redis.XMessageSliceCmd {
	if start == "+" {
		if len(count) > 0 {
			return c.client.XRevRangeN(ctx, stream, start, stop, count[0])
		}
		return c.client.XRevRange(ctx, stream, start, stop)
	}
	if len(count) > 0 {
		return c.client.XRangeN(ctx, stream, start, stop, count[0])
	}
	return c.client.XRange(ctx, stream, start, stop)
}

func (c *ClusterRedis) XLen(ctx context.Context, stream string) *redis.IntCmd {
	return c.client.XLen(ctx, stream)
}

func (c *ClusterRedis) XPendingExt(ctx context.Context, a *redis.XPendingExtArgs) *redis.XPendingExtCmd {
	return c.client.XPendingExt(ctx, a)
}

func (c *ClusterRedis) XClaim(ctx context.Context, a *redis.XClaimArgs) *redis.XMessageSliceCmd {
	return c.client.XClaim(ctx, a)
}

func (c *ClusterRedis) Exists(ctx context.Context, keys ...string) *redis.IntCmd {
	return c.client.Exists(ctx, keys...)
}

func (c *ClusterRedis) Publish(ctx context.Context, channel string, message interface{}) *redis.IntCmd {
	return c.client.Publish(ctx, channel, message)
}

// FailoverRedis implements RedisClient for Redis Sentinel (master-slave with automatic failover).
type FailoverRedis struct {
	client *redis.Client
}

// NewFailoverRedis creates a new Redis client with Sentinel failover.
func NewFailoverRedis(masterName string, sentinelAddrs []string, password string, db int, poolSize int) (*FailoverRedis, error) {
	if poolSize <= 0 {
		poolSize = 50
	}
	opts := &redis.FailoverOptions{
		MasterName:    masterName,
		SentinelAddrs: sentinelAddrs,
		Password:      password,
		DB:            db,
		DialTimeout:   3 * time.Second,
		ReadTimeout:   3 * time.Second,
		WriteTimeout:  3 * time.Second,
		PoolSize:      poolSize,
		MinIdleConns:  0,
		MaxRetries:    0,
		PoolTimeout:   2 * time.Second,
	}

	client := redis.NewFailoverClient(opts)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := client.Ping(ctx).Err(); err != nil {
		return nil, fmt.Errorf("redis sentinel ping: %w", err)
	}

	slog.Info("redis connected (sentinel)", "master", masterName, "sentinels", sentinelAddrs)
	return &FailoverRedis{client: client}, nil
}

func (f *FailoverRedis) Get(ctx context.Context, key string) *redis.StringCmd {
	return f.client.Get(ctx, key)
}

func (f *FailoverRedis) Set(ctx context.Context, key string, value interface{}, expiration time.Duration) *redis.StatusCmd {
	return f.client.Set(ctx, key, value, expiration)
}

func (f *FailoverRedis) Del(ctx context.Context, keys ...string) *redis.IntCmd {
	return f.client.Del(ctx, keys...)
}

func (f *FailoverRedis) Incr(ctx context.Context, key string) *redis.IntCmd {
	return f.client.Incr(ctx, key)
}

func (f *FailoverRedis) Expire(ctx context.Context, key string, expiration time.Duration) *redis.BoolCmd {
	return f.client.Expire(ctx, key, expiration)
}

func (f *FailoverRedis) Ping(ctx context.Context) *redis.StatusCmd {
	return f.client.Ping(ctx)
}

func (f *FailoverRedis) Subscribe(ctx context.Context, channels ...string) *redis.PubSub {
	return f.client.Subscribe(ctx, channels...)
}

func (f *FailoverRedis) Close() error {
	return f.client.Close()
}

func (f *FailoverRedis) Do(ctx context.Context, args ...interface{}) *redis.Cmd {
	return f.client.Do(ctx, args...)
}

func (f *FailoverRedis) Stats() *redis.PoolStats {
	return f.client.PoolStats()
}

func (f *FailoverRedis) Scan(ctx context.Context, cursor uint64, match string, count int64) *redis.ScanCmd {
	return f.client.Scan(ctx, cursor, match, count)
}

// ScanAll 哨兵模式下只有一个 master：做完整游标扫描即可。
func (f *FailoverRedis) ScanAll(ctx context.Context, match string, count int64) ([]string, error) {
	return scanAllOn(ctx, f.client, match, count)
}

// ForEachMaster 哨兵模式只有一个 master：直接执行一次。
func (f *FailoverRedis) ForEachMaster(ctx context.Context, fn func(ctx context.Context, rdb RedisClient) error) error {
	return fn(ctx, f)
}

func (f *FailoverRedis) Eval(ctx context.Context, script string, keys []string, args ...interface{}) *redis.Cmd {
	return f.client.Eval(ctx, script, keys, args...)
}

func (f *FailoverRedis) EvalSha(ctx context.Context, sha1 string, keys []string, args ...interface{}) *redis.Cmd {
	return f.client.EvalSha(ctx, sha1, keys, args...)
}

func (f *FailoverRedis) ScriptExists(ctx context.Context, hashes ...string) *redis.BoolSliceCmd {
	return f.client.ScriptExists(ctx, hashes...)
}

func (f *FailoverRedis) ScriptLoad(ctx context.Context, script string) *redis.StringCmd {
	return f.client.ScriptLoad(ctx, script)
}

func (f *FailoverRedis) XAdd(ctx context.Context, a *redis.XAddArgs) *redis.StringCmd {
	return f.client.XAdd(ctx, a)
}

func (f *FailoverRedis) XAck(ctx context.Context, stream, group string, ids ...string) *redis.IntCmd {
	return f.client.XAck(ctx, stream, group, ids...)
}

func (f *FailoverRedis) XGroupCreateMkStream(ctx context.Context, stream, group, start string) *redis.StatusCmd {
	return f.client.XGroupCreateMkStream(ctx, stream, group, start)
}

func (f *FailoverRedis) XReadGroup(ctx context.Context, a *redis.XReadGroupArgs) *redis.XStreamSliceCmd {
	return f.client.XReadGroup(ctx, a)
}

func (f *FailoverRedis) XRange(ctx context.Context, stream, start, stop string, count ...int64) *redis.XMessageSliceCmd {
	if start == "+" {
		if len(count) > 0 {
			return f.client.XRevRangeN(ctx, stream, start, stop, count[0])
		}
		return f.client.XRevRange(ctx, stream, start, stop)
	}
	if len(count) > 0 {
		return f.client.XRangeN(ctx, stream, start, stop, count[0])
	}
	return f.client.XRange(ctx, stream, start, stop)
}

func (f *FailoverRedis) XLen(ctx context.Context, stream string) *redis.IntCmd {
	return f.client.XLen(ctx, stream)
}

func (f *FailoverRedis) XPendingExt(ctx context.Context, a *redis.XPendingExtArgs) *redis.XPendingExtCmd {
	return f.client.XPendingExt(ctx, a)
}

func (f *FailoverRedis) XClaim(ctx context.Context, a *redis.XClaimArgs) *redis.XMessageSliceCmd {
	return f.client.XClaim(ctx, a)
}

func (f *FailoverRedis) Exists(ctx context.Context, keys ...string) *redis.IntCmd {
	return f.client.Exists(ctx, keys...)
}

func (f *FailoverRedis) Publish(ctx context.Context, channel string, message interface{}) *redis.IntCmd {
	return f.client.Publish(ctx, channel, message)
}

// RedisConfig holds the configuration for Redis connection.
type RedisConfig struct {
	Mode string `json:"mode"` // "single", "cluster", "sentinel"

	// Single mode config
	Addr     string `json:"addr"`
	Password string `json:"password"`
	DB       int    `json:"db"`
	PoolSize int    `json:"pool_size"`

	// Cluster mode config
	Addrs []string `json:"addrs"`

	// Sentinel mode config
	MasterName    string   `json:"master_name"`
	SentinelAddrs []string `json:"sentinel_addrs"`
}

// NewRedisClient creates a RedisClient based on the provided config.
func NewRedisClient(cfg RedisConfig) (RedisClient, error) {
	switch cfg.Mode {
	case "single", "":
		return NewSingleRedis(cfg.Addr, cfg.Password, cfg.DB, cfg.PoolSize)
	case "cluster":
		if len(cfg.Addrs) == 0 {
			return nil, fmt.Errorf("cluster mode requires at least one address")
		}
		return NewClusterRedis(cfg.Addrs, cfg.Password, cfg.PoolSize)
	case "sentinel":
		if cfg.MasterName == "" {
			return nil, fmt.Errorf("sentinel mode requires master_name")
		}
		if len(cfg.SentinelAddrs) == 0 {
			return nil, fmt.Errorf("sentinel mode requires at least one sentinel address")
		}
		return NewFailoverRedis(cfg.MasterName, cfg.SentinelAddrs, cfg.Password, cfg.DB, cfg.PoolSize)
	default:
		return nil, fmt.Errorf("unknown redis mode: %s", cfg.Mode)
	}
}
