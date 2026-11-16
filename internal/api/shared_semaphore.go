package api

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// ── SharedSemaphore：跨实例信号量 ───────────────────────────────
//
// 多网关实例下，进程内 chan 信号量只约束单实例，全局并发上限随实例数放大。
// SharedSemaphore 以 Redis Lua 原子计数为权威（key: sem:<name>，INCR 至多 limit，
// 超限 DECR 还原；acquire 成功即续期 TTL），Redis 不可用时回退到进程内 chan
// 兑底（fail-open + 降级告警，此时多实例下为近似限制，与既有行为一致）。
//
// 续期修复：TTL 此前只在 acquire 时设置一次，而任务时长可逼近甚至超过
// semaphoreTTL —— 槽位被 Redis 提前回收后，其它实例会拿到同一个槽位，
// **全局并发上限静默失效**（既不报错也无日志）。现在本实例只要还持有至少一个
// 槽位，就由 renewLoop 周期续期。计数是聚合的（一个 key 一个整数），所以续期
// 粒度为"本实例是否还有人持有"，无需 per-holder 记录；多实例同时 EXPIRE 同一
// key 是幂等的，语义上等价于"只要还有活跃持有者，key 就不过期"。

// semaphoreKeyPrefix 跨实例信号量 Redis 计数键前缀（统一 RedisKey，多环境隔离；空前缀 = 存量兼容）。
var semaphoreKeyPrefix = db.RedisKey("sem:")

const (
	// semaphoreTTL Redis 计数键 TTL：持有者崩溃时计数在 TTL 后自动清零，
	// 避免永久泄漏；正常 release 显式 DECR，持有期间由 renewLoop 续期。
	semaphoreTTL = 5 * time.Minute
	// semaphoreRenew 续期间隔：远小于 semaphoreTTL，留足抖动重试余量。
	semaphoreRenew = 100 * time.Second
	// semaphorePoll 阻塞等待全局槽位的轮询间隔。
	semaphorePoll = 100 * time.Millisecond
)

const semAcquireLua = `
local v = redis.call('INCR', KEYS[1])
if v > tonumber(ARGV[1]) then
  redis.call('DECR', KEYS[1])
  return 0
end
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
`

// semReleaseLua 释放一个槽位。
//
// 必须先读计数再减：无条件 DECR 在 key 已因 TTL 消失时会**创建一个 -1 的 key**，
// 让后续 acquire 从负数起步 —— 等于白送槽位，而且偏差会一直留着（并发上限被进一步
// 突破）。计数不存在或已 <= 0 时什么都不做。
const semReleaseLua = `
local cur = redis.call('GET', KEYS[1])
if not cur then
  return 0
end
local n = tonumber(cur)
if not n or n <= 0 then
  return 0
end
redis.call('DECR', KEYS[1])
return 1
`

// SharedSemaphore 全局/租户级并发信号量。
// limit <= 0 时视为不限制（与调用方约定；构造时归一到 1 的语义见 NewSharedSemaphore）。
type SharedSemaphore struct {
	name  string
	limit int64
	local chan struct{} // Redis 不可用时的本地兑底
	rdb   db.RedisClient

	mu     sync.Mutex
	warned bool

	// 续期状态：heldSlots 是本实例持有的 Redis 槽位数（引用计数），
	// 从 0 → 1 时启动 renewLoop，回落到 0 时停止。
	renewMu     sync.Mutex
	heldSlots   int
	renewCancel context.CancelFunc
}

// NewSharedSemaphore 构造命名信号量。rdb 可为 nil（纯本地模式）。
// limit <= 0 表示不限制（不占用 Redis/本地槽位，恒可获取）。
func NewSharedSemaphore(rdb db.RedisClient, name string, limit int) *SharedSemaphore {
	if limit < 0 {
		limit = 0
	}
	localCap := limit
	if localCap == 0 {
		localCap = 1 // 不限制模式仍给本地一个"恒成功"槽位占位；见 TryAcquire 短路
	}
	return &SharedSemaphore{
		name:  name,
		limit: int64(limit),
		local: make(chan struct{}, localCap),
		rdb:   rdb,
	}
}

// TryAcquire 非阻塞获取。返回的 release 必须调用；未获取时返回 ok=false。
func (s *SharedSemaphore) TryAcquire(ctx context.Context) (release func(), ok bool) {
	if s.limit == 0 {
		return func() {}, true // 不限制
	}
	if s.rdb != nil {
		res := s.rdb.Eval(ctx, semAcquireLua, []string{semaphoreKeyPrefix + s.name},
			s.limit, int(semaphoreTTL.Seconds()))
		resErr := res.Err()
		if resErr == nil {
			if n, err := res.Int(); err == nil && n == 1 {
				s.trackRenewal()
				var once sync.Once
				return func() {
					once.Do(func() {
						s.untrackRenewal()
						// 释放为尽力而为：计数键崩溃后由 TTL 兜底，故忽略错误。
						_ = s.rdb.Eval(context.Background(), semReleaseLua,
							[]string{semaphoreKeyPrefix + s.name}).Err()
					})
				}, true
			}
			return nil, false // Redis 计数已满
		}
		s.warnDegrade(resErr)
	}
	select {
	case s.local <- struct{}{}:
		return func() { <-s.local }, true
	default:
		return nil, false
	}
}

// Acquire 阻塞直到获取槽位或 ctx 取消。Redis 为权威时轮询全局槽位。
func (s *SharedSemaphore) Acquire(ctx context.Context) (release func(), ok bool) {
	if s.limit == 0 {
		return func() {}, true
	}
	for {
		if release, ok := s.TryAcquire(ctx); ok {
			return release, true
		}
		select {
		case <-ctx.Done():
			return nil, false
		case <-time.After(semaphorePoll):
		}
	}
}

// trackRenewal 登记一个本实例持用的 Redis 槽位；首个槽位到来时启动续期循环。
func (s *SharedSemaphore) trackRenewal() {
	s.renewMu.Lock()
	defer s.renewMu.Unlock()
	s.heldSlots++
	if s.heldSlots != 1 {
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	s.renewCancel = cancel
	if s.rdb != nil {
		go s.renewLoop(ctx)
	}
}

// untrackRenewal 注销一个槽位；回落到 0 时停止续期循环。
// 多余的调用（计数已为 0）不产生副作用，避免把计数减成负数。
func (s *SharedSemaphore) untrackRenewal() {
	s.renewMu.Lock()
	defer s.renewMu.Unlock()
	if s.heldSlots > 0 {
		s.heldSlots--
	}
	if s.heldSlots == 0 && s.renewCancel != nil {
		s.renewCancel()
		s.renewCancel = nil
	}
}

// renewLoop 在持有槽位期间周期续期计数键的 TTL。
//
// 没有它时，时长 ≥ semaphoreTTL 的任务会让自己的槽位被 Redis 回收，其它实例随即
// 获得同一槽位 —— 全局并发上限静默失效。续期失败不改变本地持有语义（下一个 tick
// 再试）；真的持续失败时仍有 TTL 兜底，退化为修复前的行为。
func (s *SharedSemaphore) renewLoop(ctx context.Context) {
	ticker := time.NewTicker(semaphoreRenew)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := s.rdb.Expire(ctx, semaphoreKeyPrefix+s.name, semaphoreTTL).Err(); err != nil {
				slog.Debug("shared semaphore renew failed", "sem", s.name, "error", err)
			}
		}
	}
}

func (s *SharedSemaphore) warnDegrade(err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.warned {
		s.warned = true
		slog.Warn("shared semaphore degraded to in-process (Redis unavailable)",
			"sem", s.name, "error", err)
	}
}
