package db

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"runtime"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// ErrDatabaseNotAvailable 数据库未初始化错误
var ErrDatabaseNotAvailable = errors.New("database not available")

// DBManager 统一数据库管理器，集中处理所有数据库操作和 nil 检查
type DBManager struct {
	// 自动调优相关
	mu                sync.RWMutex
	lastTuneTime      time.Time
	tuneInterval      time.Duration
	minConns          int32
	maxConns          int32
	targetUtilization float64 // 目标连接使用率 (0.7 = 70%)
	autoTunerRunning  bool
}

// NewDBManager 创建数据库管理器实例
func NewDBManager() *DBManager {
	return &DBManager{}
}

// GetPool 获取主数据库连接池，如果未初始化则返回错误
func (m *DBManager) GetPool() (*pgxpool.Pool, error) {
	PoolMu.RLock()
	p := Pool
	PoolMu.RUnlock()
	if p == nil {
		return nil, ErrDatabaseNotAvailable
	}
	return p, nil
}

// GetReadPool 获取读操作连接池（优先使用只读副本），如果未初始化则返回错误
func (m *DBManager) GetReadPool() (*pgxpool.Pool, error) {
	PoolMu.RLock()
	p := Pool
	PoolMu.RUnlock()
	if Router != nil {
		pool := Router.Read()
		if pool == nil {
			return nil, ErrDatabaseNotAvailable
		}
		return pool, nil
	}
	if p == nil {
		return nil, ErrDatabaseNotAvailable
	}
	return p, nil
}

// Query 执行查询并返回结果集
func (m *DBManager) Query(ctx context.Context, sql string, args ...interface{}) (pgx.Rows, error) {
	pool, err := m.GetReadPool()
	if err != nil {
		return nil, fmt.Errorf("db manager: %w", err)
	}
	return pool.Query(ctx, sql, args...)
}

// QueryRow 执行查询并返回单行结果
func (m *DBManager) QueryRow(ctx context.Context, sql string, args ...interface{}) pgx.Row {
	pool, err := m.GetReadPool()
	if err != nil {
		return errorRow{err: err}
	}
	return pool.QueryRow(ctx, sql, args...)
}

// Exec 执行写操作（INSERT/UPDATE/DELETE）
func (m *DBManager) Exec(ctx context.Context, sql string, args ...interface{}) (pgconn.CommandTag, error) {
	pool, err := m.GetPool()
	if err != nil {
		return pgconn.CommandTag{}, fmt.Errorf("db manager: %w", err)
	}
	return pool.Exec(ctx, sql, args...)
}

// Begin 开始事务
func (m *DBManager) Begin(ctx context.Context) (pgx.Tx, error) {
	pool, err := m.GetPool()
	if err != nil {
		return nil, fmt.Errorf("db manager: %w", err)
	}
	return pool.Begin(ctx)
}

// Ping 检查数据库连接状态
func (m *DBManager) Ping(ctx context.Context) error {
	pool, err := m.GetPool()
	if err != nil {
		return err
	}
	return pool.Ping(ctx)
}

// IsAvailable 检查数据库是否可用
func (m *DBManager) IsAvailable() bool {
	PoolMu.RLock()
	p := Pool
	PoolMu.RUnlock()
	return p != nil
}

// FetchOne 执行查询并返回第一行结果（用于 Python 引擎调用）
func (m *DBManager) FetchOne(ctx context.Context, sql string, args ...interface{}) (map[string]interface{}, error) {
	pool, err := m.GetReadPool()
	if err != nil {
		return nil, fmt.Errorf("db manager: %w", err)
	}

	rows, err := pool.Query(ctx, sql, args...)
	if err != nil {
		return nil, fmt.Errorf("query failed: %w", err)
	}
	defer rows.Close()

	if !rows.Next() {
		return nil, nil
	}

	columns := rows.FieldDescriptions()
	values := make([]interface{}, len(columns))
	valuePtrs := make([]interface{}, len(columns))
	for i := range values {
		valuePtrs[i] = &values[i]
	}

	if err := rows.Scan(valuePtrs...); err != nil {
		return nil, fmt.Errorf("scan failed: %w", err)
	}

	result := make(map[string]interface{})
	for i, col := range columns {
		result[col.Name] = values[i]
	}

	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("rows iteration failed: %w", err)
	}

	return result, nil
}

// FetchAll 执行查询并返回所有结果（用于 Python 引擎调用）
func (m *DBManager) FetchAll(ctx context.Context, sql string, args ...interface{}) ([]map[string]interface{}, error) {
	pool, err := m.GetReadPool()
	if err != nil {
		return nil, fmt.Errorf("db manager: %w", err)
	}

	rows, err := pool.Query(ctx, sql, args...)
	if err != nil {
		return nil, fmt.Errorf("query failed: %w", err)
	}
	defer rows.Close()

	var results []map[string]interface{}
	columns := rows.FieldDescriptions()

	for rows.Next() {
		values := make([]interface{}, len(columns))
		valuePtrs := make([]interface{}, len(columns))
		for i := range values {
			valuePtrs[i] = &values[i]
		}

		if err := rows.Scan(valuePtrs...); err != nil {
			return nil, fmt.Errorf("scan failed: %w", err)
		}

		row := make(map[string]interface{})
		for i, col := range columns {
			row[col.Name] = values[i]
		}
		results = append(results, row)
	}

	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("rows iteration failed: %w", err)
	}

	return results, nil
}

// Execute 执行 SQL 并返回影响行数（用于 Python 引擎调用）
func (m *DBManager) Execute(ctx context.Context, sql string, args ...interface{}) (int64, error) {
	pool, err := m.GetPool()
	if err != nil {
		return 0, fmt.Errorf("db manager: %w", err)
	}

	tag, err := pool.Exec(ctx, sql, args...)
	if err != nil {
		return 0, fmt.Errorf("exec failed: %w", err)
	}

	return tag.RowsAffected(), nil
}

// BatchExecute 批量执行 SQL（用于 Python 引擎调用）
func (m *DBManager) BatchExecute(ctx context.Context, queries []string) error {
	pool, err := m.GetPool()
	if err != nil {
		return fmt.Errorf("db manager: %w", err)
	}

	tx, err := pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin transaction failed: %w", err)
	}
	defer tx.Rollback(ctx)

	for _, query := range queries {
		if _, err := tx.Exec(ctx, query); err != nil {
			return fmt.Errorf("batch exec failed: %w", err)
		}
	}

	return tx.Commit(ctx)
}

// HealthCheck 健康检查（包含详细统计信息）
func (m *DBManager) HealthCheck(ctx context.Context) map[string]interface{} {
	result := map[string]interface{}{
		"available": m.IsAvailable(),
		"timestamp": time.Now().Unix(),
	}

	if !m.IsAvailable() {
		return result
	}

	pool, _ := m.GetPool()
	if pool != nil {
		stats := pool.Stat()
		result["stats"] = map[string]interface{}{
			"total_conns":      stats.TotalConns(),
			"idle_conns":       stats.IdleConns(),
			"acquired_conns":   stats.AcquiredConns(),
			"empty_acquire":    stats.EmptyAcquireCount(),
			"acquire_duration": stats.AcquireDuration().Milliseconds(),
		}

		// 测试连接
		pingCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		err := pool.Ping(pingCtx)
		result["ping_ok"] = err == nil
		if err != nil {
			result["ping_error"] = "database ping failed"
		}
	}

	return result
}

// errorRow 实现 pgx.Row 接口，用于返回错误
type errorRow struct {
	err error
}

func (r errorRow) Scan(dest ...interface{}) error {
	return r.err
}

// GlobalDBManager 全局数据库管理器实例
var GlobalDBManager = NewDBManager()

// TxFunc 事务函数类型
type TxFunc func(tx pgx.Tx) error

// WithTransaction 在事务中执行函数
func (m *DBManager) WithTransaction(ctx context.Context, fn TxFunc) error {
	pool, err := m.GetPool()
	if err != nil {
		return fmt.Errorf("db manager: %w", err)
	}

	tx, err := pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin transaction failed: %w", err)
	}
	defer tx.Rollback(ctx)

	if err := fn(tx); err != nil {
		return err
	}

	return tx.Commit(ctx)
}

// QueryRowWithScan 执行查询并扫描到指定目标
func (m *DBManager) QueryRowWithScan(ctx context.Context, dest interface{}, sql string, args ...interface{}) error {
	pool, err := m.GetReadPool()
	if err != nil {
		return fmt.Errorf("db manager: %w", err)
	}

	row := pool.QueryRow(ctx, sql, args...)
	return row.Scan(dest)
}

// QueryWithRows 执行查询并返回原始 rows（调用者负责关闭）
func (m *DBManager) QueryWithRows(ctx context.Context, sql string, args ...interface{}) (pgx.Rows, error) {
	pool, err := m.GetReadPool()
	if err != nil {
		return nil, fmt.Errorf("db manager: %w", err)
	}
	return pool.Query(ctx, sql, args...)
}

// ExecWithResult 执行 SQL 并返回 CommandTag
func (m *DBManager) ExecWithResult(ctx context.Context, sql string, args ...interface{}) (pgconn.CommandTag, error) {
	pool, err := m.GetPool()
	if err != nil {
		return pgconn.CommandTag{}, fmt.Errorf("db manager: %w", err)
	}
	return pool.Exec(ctx, sql, args...)
}

// BeginTx 开始事务（供高级用法）
func (m *DBManager) BeginTx(ctx context.Context) (pgx.Tx, error) {
	pool, err := m.GetPool()
	if err != nil {
		return nil, fmt.Errorf("db manager: %w", err)
	}
	return pool.Begin(ctx)
}

// SetTuneInterval 设置自动调优检查间隔
func (m *DBManager) SetTuneInterval(interval time.Duration) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.tuneInterval = interval
}

// AutoTuneConnectionPool 根据当前负载自动调整连接池大小
func (m *DBManager) AutoTuneConnectionPool(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if time.Since(m.lastTuneTime) < m.tuneInterval {
		return nil // 未到调优时间
	}

	pool, err := m.GetPool()
	if err != nil {
		return err
	}

	stats := pool.Stat()
	totalConns := stats.TotalConns()
	usedConns := stats.AcquiredConns()

	if totalConns == 0 {
		return nil
	}

	utilization := float64(usedConns) / float64(totalConns)

	slog.Info("connection pool stats",
		"total", totalConns,
		"used", usedConns,
		"utilization", fmt.Sprintf("%.2f%%", utilization*100),
		"idle", stats.IdleConns(),
		"max_conns", stats.MaxConns())

	// 动态调整逻辑
	currentMax := m.maxConns
	if currentMax == 0 {
		currentMax = stats.MaxConns()
	}

	newMax := currentMax

	if utilization > 0.85 {
		// 使用率过高，增加20%
		increase := int32(math.Ceil(float64(currentMax) * 0.2))
		newMax = currentMax + increase
		if newMax > 200 { // 硬上限
			newMax = 200
		}
		slog.Warn("connection pool under high load, increasing max connections",
			"from", currentMax, "to", newMax,
			"cpu_goroutines", runtime.NumGoroutine())
	} else if utilization < 0.3 && currentMax > m.minConns {
		// 使用率过低，减少20%
		decrease := int32(math.Ceil(float64(currentMax-m.minConns) * 0.2))
		newMax = currentMax - decrease
		if newMax < m.minConns {
			newMax = m.minConns
		}
		slog.Info("connection pool underutilized, decreasing max connections",
			"from", currentMax, "to", newMax)
	}

	if newMax != currentMax {
		// 注意：pgxpool不支持运行时修改MaxConns
		// 这里记录日志和监控指标，实际调整需要重启或通过配置热更新
		slog.Info("connection pool tuning recommendation",
			"current_max", currentMax,
			"recommended_max", newMax,
			"action", "update POSTGRES_MAX_CONN env var and restart")

		// 记录到监控系统
		recordTuningRecommendation(newMax, utilization)
	}

	m.lastTuneTime = time.Now()
	return nil
}

// StartAutoTuner 启动定期自动调优协程
func (m *DBManager) StartAutoTuner(ctx context.Context) {
	m.mu.Lock()
	if m.autoTunerRunning {
		m.mu.Unlock()
		return
	}
	m.autoTunerRunning = true
	m.mu.Unlock()

	if m.tuneInterval == 0 {
		m.tuneInterval = 5 * time.Minute // 默认5分钟
	}

	go func() {
		ticker := time.NewTicker(m.tuneInterval)
		defer ticker.Stop()

		for {
			select {
			case <-ctx.Done():
				m.mu.Lock()
				m.autoTunerRunning = false
				m.mu.Unlock()
				slog.Info("connection pool auto-tuner stopped")
				return
			case <-ticker.C:
				if err := m.AutoTuneConnectionPool(ctx); err != nil {
					slog.Error("auto-tune connection pool failed", "error", err)
				}
			}
		}
	}()

	slog.Info("connection pool auto-tuner started", "interval", m.tuneInterval)
}

// GetPoolStats 获取连接池统计信息
func (m *DBManager) GetPoolStats() map[string]interface{} {
	pool, err := m.GetPool()
	if err != nil {
		return map[string]interface{}{"error": err.Error()}
	}

	stats := pool.Stat()
	m.mu.RLock()
	defer m.mu.RUnlock()

	return map[string]interface{}{
		"acquired_conns":      stats.AcquiredConns(),
		"idle_conns":          stats.IdleConns(),
		"total_conns":         stats.TotalConns(),
		"max_conns":           stats.MaxConns(),
		"empty_acquire_count": stats.EmptyAcquireCount(),
		"acquire_duration_ms": stats.AcquireDuration().Milliseconds(),
		"tune_interval":       m.tuneInterval.String(),
		"last_tune_time":      m.lastTuneTime,
		"auto_tuner_running":  m.autoTunerRunning,
	}
}

// recordTuningRecommendation 记录调优建议到监控系统
func recordTuningRecommendation(newMax int32, utilization float64) {
	// 可以将建议写入监控指标或配置文件
	slog.Debug("pool tuning recommendation recorded",
		"recommended_max", newMax,
		"current_utilization", utilization,
		"timestamp", time.Now().Unix())
}
