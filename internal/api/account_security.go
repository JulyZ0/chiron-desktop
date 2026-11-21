package api

// account_security.go —— 团队账号的登录安全与一次性口令。
//
// 这里只放**跨 handler 复用**的判定：账号状态、登录失败锁定、临时密码生成。
// 建号/重置/禁用等动作本身在 auth_team.go，避免把协议逻辑与安全策略混在一起。

import (
	"context"
	"crypto/rand"
	"fmt"
	"math/big"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

const (
	// 账号状态。`status` 列存在之前的所有存量行按 active 处理（迁移默认值）。
	userStatusActive   = "active"
	userStatusDisabled = "disabled"
	userStatusDeleted  = "deleted"

	// 登录失败锁定：同一邮箱连续失败 5 次锁 15 分钟。
	// 计数放 Redis 而不是 users 列：失败是高频、短生命周期事件，写库会放大登录热路径。
	loginMaxFailures = 5
	loginLockTTL     = 15 * time.Minute
	loginFailPrefix  = "login:fail:"
)

// userStatus 把 NULL/空串归一成 active：迁移前写入的行没有 status 列值。
func userStatus(raw string) string {
	s := strings.ToLower(strings.TrimSpace(raw))
	if s == "" {
		return userStatusActive
	}
	return s
}

// loginFailKey 按邮箱计数。团队模式是单租户，不带 tenant 前缀；
// 跨租户同名邮箱共享锁是可接受的取舍（同租户内本来就无法共存）。
func loginFailKey(email string) string {
	return db.RedisKey(loginFailPrefix + strings.ToLower(strings.TrimSpace(email)))
}

// loginLockRemaining 返回剩余锁定时间；0 表示未锁定。
// Redis 不可用时返回 0（fail-open）：锁是防爆破增强，不该让整个登录不可用。
func loginLockRemaining(ctx context.Context, email string) time.Duration {
	if db.Redis == nil {
		return 0
	}
	n, err := db.Redis.Get(ctx, loginFailKey(email)).Int()
	if err != nil || n < loginMaxFailures {
		return 0
	}
	ttl, err := db.Redis.Do(ctx, "TTL", loginFailKey(email)).Int()
	if err != nil || ttl <= 0 {
		return loginLockTTL
	}
	return time.Duration(ttl) * time.Second
}

// recordLoginFailure 记一次失败并返回是否已达锁定阈值。
// 第一次失败时设置 TTL；后续 INCR 保留原 TTL（不会因为持续攻击而无限延长锁）。
func recordLoginFailure(ctx context.Context, email string) bool {
	if db.Redis == nil {
		return false
	}
	key := loginFailKey(email)
	n, err := db.Redis.Incr(ctx, key).Result()
	if err != nil {
		return false
	}
	if n == 1 {
		db.Redis.Expire(ctx, key, loginLockTTL)
	}
	return n >= loginMaxFailures
}

// clearLoginFailures 登录成功后清零，避免"对了还锁着"。
func clearLoginFailures(ctx context.Context, email string) {
	if db.Redis == nil {
		return
	}
	db.Redis.Del(ctx, loginFailKey(email))
}

// passwordAlphabet 去掉了易混淆字符（0/O、1/l/I），临时密码要能被人抄写。
const passwordAlphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*"

// generateTemporaryPassword 生成满足现有复杂度策略的 18 位临时密码。
//
// 先按类别各放一个，再从全表里补足 —— 纯随机生成有概率恰好缺某一类，
// 那会让管理员拿到一个"系统生成却过不了校验"的密码。
func generateTemporaryPassword() (string, error) {
	pick := func(bucket string) (byte, error) {
		n, err := rand.Int(rand.Reader, big.NewInt(int64(len(bucket))))
		if err != nil {
			return 0, err
		}
		return bucket[n.Int64()], nil
	}

	const (
		upper   = "ABCDEFGHJKLMNPQRSTUVWXYZ"
		lower   = "abcdefghijkmnopqrstuvwxyz"
		digits  = "23456789"
		symbols = "!@#$%^&*"
	)
	out := make([]byte, 0, 18)
	for _, bucket := range []string{upper, lower, digits, symbols} {
		b, err := pick(bucket)
		if err != nil {
			return "", err
		}
		out = append(out, b)
	}
	for len(out) < 18 {
		b, err := pick(passwordAlphabet)
		if err != nil {
			return "", err
		}
		out = append(out, b)
	}
	// 打散类别分组，避免"前四位固定是大写/小写/数字/符号"这种可猜结构。
	for i := len(out) - 1; i > 0; i-- {
		n, err := rand.Int(rand.Reader, big.NewInt(int64(i+1)))
		if err != nil {
			return "", err
		}
		j := int(n.Int64())
		out[i], out[j] = out[j], out[i]
	}
	pwd := string(out)
	if ok, msg := auth.ValidatePasswordComplexity(pwd); !ok {
		// 理论上不可达；真出现时宁可报错，也不要交出一个过不了策略的密码。
		return "", fmt.Errorf("generated password violates policy: %s", msg)
	}
	return pwd, nil
}
