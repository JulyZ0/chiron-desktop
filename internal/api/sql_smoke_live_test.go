package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

// sha256Hex 与 `AuthMiddleware` / `generateAPIKey` 的算法保持完全一致：
// sha256 → hex。测试必须用同一条路径生成 key_hash，否则插进去的 key
// 永远对不上鉴权查询，"key 无效"会变成测试自身的 bug。
func sha256Hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

// 6 处「只做过字符串断言」的 SQL 的**真实 PostgreSQL** 冒烟用例。
//
// ## 为什么需要这份文件
//
// 这批 SQL 此前只有单测，而单测做的是 `strings.Contains(sql, "...")` 这一类断言 ——
// 它能证明「WHERE 里写了 user_id = $1」，却证明不了：
//
//   - 列名在库里真的存在（拼错 `last_used_tm` 单测全绿）；
//   - 列数、顺序与 Scan 的参数一一对应（多选一列会让 Scan 静默错位）；
//   - 类型能对上（`last_used_at` 是 timestamp 无时区，写入却用 now() 得 timestamptz）；
//   - 语法在目标 PG 版本上成立（`make_interval(days => $2)`、`SUM(...) OVER ()`）。
//
// 这些都是「跑一次就知道、不跑就不知道」的东西，而它们出错的后果是运行时 500。
//
// ## 取值口径
//
// 与既有 `*_live_test.go` 一致：`CHIRON_TEST_POSTGRES_DSN` 未设置则 `t.Skip`。
// 也就是说本机不设 DSN 时这份文件只贡献编译检查；CI 的 real-stack job 注入 DSN
// 后才真正执行。**注意 run 的模式要带 `Live`**（`-run Live`）。
//
// ## 数据隔离
//
// 每个用例自建 tenant + user，结束时按逆序清理。都用随机 uuid 作标记，
// 与并发跑的其他用例互不影响。

// liveSmokePool 建一个真实 PostgreSQL 连接池（DSN 守卫、30s 连接超时、Cleanup 关闭）。
//
// 未设置 `CHIRON_TEST_POSTGRES_DSN` 时 Skip —— 本机默认没有真库，这份文件那时
// 只贡献编译检查；CI 的 real-stack job 注入 DSN 后才真正执行（`-run Live`）。
func liveSmokePool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	dsn := os.Getenv("CHIRON_TEST_POSTGRES_DSN")
	if dsn == "" {
		t.Skip("CHIRON_TEST_POSTGRES_DSN 未设置：跳过真实 PostgreSQL 用例")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		t.Fatalf("解析 DSN 失败: %v", err)
	}
	// 并发用例要真正并发：连接数必须不小于并发度，否则一部分 goroutine 只是排队，
	// 竞争窗口被人为缩窄，测不出问题。
	cfg.MaxConns = 32
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatalf("连接 PostgreSQL 失败: %v", err)
	}
	t.Cleanup(pool.Close)
	return pool
}

// liveSmokeFixture 建一套「租户 + 用户」，返回 (tenantID, userID)。
//
// `role` 必须显式写成 'user'：`users.role` 是 `varchar(16)` 且**可 NULL**，
// 而 `apiKeyAuthQuery` 把 `u.role` 扫进 `string` —— 为 NULL 时 Scan 直接报
// "cannot scan NULL into *string"，表现为 API Key 鉴权 500。
// 显式设值既是贴近生产（注册流程会写 role），也让这个坑在真库上一跑就现形。
func liveSmokeFixture(t *testing.T, pool *pgxpool.Pool) (tenantID int16, userID string) {
	t.Helper()
	ctx := context.Background()

	if err := pool.QueryRow(ctx,
		`INSERT INTO tenants (name, status) VALUES ($1, 'active') RETURNING id`,
		"sql-smoke-"+uuid.NewString()[:8]).Scan(&tenantID); err != nil {
		t.Fatalf("建租户失败: %v", err)
	}

	userID = uuid.NewString()
	if _, err := pool.Exec(ctx,
		`INSERT INTO users (id, tenant_id, name, role) VALUES ($1, $2, $3, 'user')`,
		userID, tenantID, "sql-smoke"); err != nil {
		t.Fatalf("建用户失败: %v", err)
	}

	t.Cleanup(func() {
		// 逆序清理：先删引用方，再删被引用方，避免将来补上外键后顺序失效。
		cleanup := []struct {
			sql  string
			args []interface{}
		}{
			{`DELETE FROM usage_events WHERE user_id = $1`, []interface{}{userID}},
			{`DELETE FROM api_keys WHERE user_id = $1`, []interface{}{userID}},
			{`DELETE FROM users WHERE id = $1`, []interface{}{userID}},
			{`DELETE FROM tenants WHERE id = $1`, []interface{}{tenantID}},
		}
		for _, c := range cleanup {
			if _, err := pool.Exec(context.Background(), c.sql, c.args...); err != nil {
				t.Logf("清理失败 %q: %v", c.sql, err)
			}
		}
	})
	return tenantID, userID
}

// 以下降级说明替代原 ① ② ③ 三组用例：
//   - ① 账单导出（exportCountQuery/exportRowsQuery）与 ③ 开票
//     （applyInvoiceQuery/invoice_requests/payments/credit_transactions）
//     都随计费域整体删除，被测 SQL 已不存在；
//   - ② 用量分桶聚合（usageBreakdownQuery/usageGroupColumn）随计费统计接口删除。
// 保留 ④⑤⑥：`api_keys` 鉴权与吊销是本地形态仍在走的关键路径。

func TestLiveAPIKeyAuthAndMarkUsed(t *testing.T) {
	pool := liveSmokePool(t)
	tenantID, userID := liveSmokeFixture(t, pool)
	ctx := context.Background()

	const plainKey = "sk-livesmoketestkey0000000000000000000000000000000000000000000000"
	keyHash := sha256Hex(plainKey)
	keyID := uuid.NewString()

	if _, err := pool.Exec(ctx,
		`INSERT INTO api_keys (id, user_id, name, key_hash, created_at, revoked)
		 VALUES ($1, $2, 'smoke', $3, now(), false)`,
		keyID, userID, keyHash); err != nil {
		t.Fatalf("建 api key 失败: %v", err)
	}

	// ⑤ 鉴权查询：读回 key 本身的 id，而不是只读 user —— 这是"补选 ak.id 的 JOIN"。
	var gotKeyID, gotUserID, gotRole, gotTenantID string
	var gotIssuedAt int64
	if err := pool.QueryRow(ctx, apiKeyAuthQuery, keyHash).
		Scan(&gotKeyID, &gotUserID, &gotRole, &gotTenantID, &gotIssuedAt); err != nil {
		t.Fatalf("apiKeyAuthQuery 执行失败（列数/顺序/类型不匹配？）: %v", err)
	}
	if gotKeyID != keyID {
		t.Errorf("第 1 列 keyID = %q，期望 %q", gotKeyID, keyID)
	}
	if gotUserID != userID {
		t.Errorf("第 2 列 userID = %q，期望 %q", gotUserID, userID)
	}
	if gotRole != "user" {
		t.Errorf("第 3 列 role = %q，期望 user", gotRole)
	}
	if gotTenantID != fmt.Sprintf("%d", tenantID) {
		t.Errorf("第 4 列 tenantID = %q，期望 %d（COALESCE 转文本）", gotTenantID, tenantID)
	}
	// 第 5 列：刚插入的 key，签发时刻应当落在"现在"附近。容差 10 分钟足以覆盖
	// 会话时区换算与插入/查询之间的耗时，同时能抓住"偏差一个时区"（≥ 整数小时）。
	if delta := time.Since(time.Unix(gotIssuedAt, 0)); delta > 10*time.Minute || delta < -10*time.Minute {
		t.Errorf("第 5 列 issuedAt 与当前时间相差 %v，期望在 10 分钟内（时区换算错位？）", delta)
	}

	// 已吊销的 key 必须查不到 —— 否则"吊销"只是列表里少一行。
	if _, err := pool.Exec(ctx, apiKeyRevokeQuery, keyID, userID); err != nil {
		t.Fatalf("吊销失败: %v", err)
	}
	if err := pool.QueryRow(ctx, apiKeyAuthQuery, keyHash).
		Scan(&gotKeyID, &gotUserID, &gotRole, &gotTenantID, &gotIssuedAt); err == nil {
		t.Error("已吊销的 key 仍能通过鉴权查询")
	}

	// 重复吊销影响 0 行（→ handler 回 404，且并发双请求只有一个生效）。
	tag, err := pool.Exec(ctx, apiKeyRevokeQuery, keyID, userID)
	if err != nil {
		t.Fatalf("重复吊销失败: %v", err)
	}
	if tag.RowsAffected() != 0 {
		t.Errorf("重复吊销影响 %d 行，期望 0", tag.RowsAffected())
	}

	// ⑥ 回写 last_used_at：新建时该列为 NULL。
	activeKeyID := uuid.NewString()
	if _, err := pool.Exec(ctx,
		`INSERT INTO api_keys (id, user_id, name, key_hash, created_at, revoked)
		 VALUES ($1, $2, 'mark-used', $3, now(), false)`,
		activeKeyID, userID, sha256Hex(plainKey+"-2")); err != nil {
		t.Fatalf("建第二把 key 失败: %v", err)
	}
	var before *time.Time
	if err := pool.QueryRow(ctx,
		`SELECT last_used_at FROM api_keys WHERE id = $1`, activeKeyID).Scan(&before); err != nil {
		t.Fatalf("读 last_used_at 失败: %v", err)
	}
	if before != nil {
		t.Errorf("新建 key 的 last_used_at 应为 NULL，实际 %v", before)
	}

	if _, err := pool.Exec(ctx, apiKeyMarkUsedQuery, activeKeyID); err != nil {
		t.Fatalf("apiKeyMarkUsedQuery 执行失败（timestamp 与 now() 隐式转换有问题？）: %v", err)
	}
	var after *time.Time
	if err := pool.QueryRow(ctx,
		`SELECT last_used_at FROM api_keys WHERE id = $1`, activeKeyID).Scan(&after); err != nil {
		t.Fatalf("回读 last_used_at 失败: %v", err)
	}
	if after == nil {
		t.Fatal("回写后 last_used_at 仍为 NULL")
	}
	// 时间必须接近"数据库的现在"。
	//
	// 这里**不能**用 Go 侧的 `time.Since(*after)`：`api_keys.last_used_at` 是
	// `timestamp without time zone`（naive），而 `now()` 是 timestamptz，隐式转换会
	// 按**数据库会话时区**落成墙钟值（本机实测会话时区 Asia/Shanghai ⇒ 存进去的是
	// +08 的本地时间）。拿它跟 Go 进程的 UTC 时刻相减，偏差恒为 8 小时 ——那是
	// L4-3 已记录的存量语义，不是这段代码的缺陷。
	//
	// 因此把比较放到数据库侧，naive 减 naive，只验证"确实写入了当下"。
	var driftSec float64
	if err := pool.QueryRow(ctx,
		`SELECT EXTRACT(EPOCH FROM (now()::timestamp - last_used_at))::float8
		   FROM api_keys WHERE id = $1`, activeKeyID).Scan(&driftSec); err != nil {
		t.Fatalf("计算 last_used_at 偏差失败: %v", err)
	}
	if driftSec < -60 || driftSec > 60 {
		t.Errorf("last_used_at 与数据库会话当前时刻偏差 %.0f 秒，期望 1 分钟内（同一会话时区下的 naive 比较）", driftSec)
	}
}
