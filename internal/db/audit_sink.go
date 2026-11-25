package db

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
)

// ── AuditSink：Redis Stream → PG audit_logs 批量落库 ─────────────────────
//
// 作为 AuditConsumer 的 handler 回调使用：
//
//	sink := db.NewDefaultAuditSink()
//	consumer := db.NewAuditConsumer(redis, sink.Handle)
//	go consumer.Start(ctx)
//
// 批量策略：≥100 条或 1s 窗口先到先发。
// 失败策略：批量 INSERT 失败仅 slog.Error 并丢弃该批（Handle 恒返回 nil，
// 消费者照常 ACK）——宁可丢不可堵，避免毒消息在消费组内无限重投；
// 关键事件（如登录审计）已有 handler 显式记录兜底。

var auditSinkUUIDRe = regexp.MustCompile(
	`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

// AuditSink 审计日志批量落库 sink。
type AuditSink struct {
	mu        sync.Mutex
	buf       []AuditEntry
	writer    func(ctx context.Context, entries []AuditEntry) error
	batchSize int
	window    time.Duration
	stopCh    chan struct{}
	doneCh    chan struct{}

	// H44：失败不再"整批丢弃"。
	//
	// 旧实现是 `writer` 失败就 slog.Error + 丢掉整批 —— 审计是**事后追溯的唯一凭据**，
	// 一次 PG 抖动（重启、连接池耗尽）就能让那段时间的登录/导出/删除记录**永久消失**，
	// 而且日志里只是一行 error，没人会发现少了多少条。
	//
	// 现在：失败批**放回队首**等下一个窗口重试；缓冲超过上限才丢**最旧的**（并计数），
	// 这样内存有界、丢失可见。
	written  int64
	dropped  int64
	failures int64
	// retries 是连续失败次数（成功后清零），用于退避与告警阈值。
	retries int
}

// AuditSinkStats 是 sink 的可观测快照（供 /metrics 与诊断用）。
type AuditSinkStats struct {
	Buffered int64 `json:"buffered"`
	Written  int64 `json:"written"`
	Dropped  int64 `json:"dropped"`
	Failures int64 `json:"failures"`
}

// 上限：缓冲最多保留 batchSize * maxBufferFactor 条，超出丢最旧。
const maxBufferFactor = 20

// NewAuditSink 创建 sink 并启动窗口定时 flush 协程。
// batchSize<=0 → 100；window<=0 → 1s。
func NewAuditSink(writer func(ctx context.Context, entries []AuditEntry) error,
	batchSize int, window time.Duration) *AuditSink {
	if batchSize <= 0 {
		batchSize = 100
	}
	if window <= 0 {
		window = time.Second
	}
	s := &AuditSink{
		writer:    writer,
		batchSize: batchSize,
		window:    window,
		stopCh:    make(chan struct{}),
		doneCh:    make(chan struct{}),
	}
	go s.loop()
	return s
}

// NewDefaultAuditSink 创建默认 sink（PG 批量写入，100 条 / 1s 窗口）。
func NewDefaultAuditSink() *AuditSink {
	return NewAuditSink(WriteAuditLogs, 100, time.Second)
}

// Handle 实现 AuditConsumer 的 handler 回调签名。
// 恒返回 nil：写入失败在 Flush 内记日志丢弃，保证消费者 ACK，不堵塞消费组。
func (s *AuditSink) Handle(ctx context.Context, entry AuditEntry) error {
	s.mu.Lock()
	s.buf = append(s.buf, entry)
	full := len(s.buf) >= s.batchSize
	s.mu.Unlock()
	if full {
		s.Flush(ctx)
	}
	return nil
}

// loop 按窗口周期触发 flush，Close 时收尾。
func (s *AuditSink) loop() {
	defer close(s.doneCh)
	ticker := time.NewTicker(s.window)
	defer ticker.Stop()
	for {
		select {
		case <-ticker.C:
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			s.Flush(ctx)
			cancel()
		case <-s.stopCh:
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			s.Flush(ctx)
			cancel()
			return
		}
	}
}

// Flush 取出当前缓冲并调用 writer 落库；失败 slog.Error 并丢弃该批。
func (s *AuditSink) Flush(ctx context.Context) {
	s.mu.Lock()
	if len(s.buf) == 0 {
		s.mu.Unlock()
		return
	}
	batch := s.buf
	s.buf = nil
	s.mu.Unlock()

	if s.writer == nil {
		return
	}
	if err := s.writer(ctx, batch); err != nil {
		s.recordFailure(batch, err)
		return
	}

	s.mu.Lock()
	s.written += int64(len(batch))
	s.retries = 0
	s.mu.Unlock()
}

// recordFailure 把失败的批次放回队首并计数；缓冲超上限时丢最旧的。
func (s *AuditSink) recordFailure(batch []AuditEntry, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	s.failures++
	s.retries++
	// 放回队首：保持时间顺序，下一窗口先重试这批，再处理新到的。
	s.buf = append(batch, s.buf...)

	limit := s.batchSize * maxBufferFactor
	if limit < s.batchSize {
		limit = s.batchSize
	}
	if over := len(s.buf) - limit; over > 0 {
		s.buf = s.buf[over:]
		s.dropped += int64(over)
		slog.Error("audit sink: buffer over limit, dropped oldest entries",
			"dropped", over, "total_dropped", s.dropped, "limit", limit)
	}
	// 连续失败要给运维一个"该看一眼"的信号（PG 挂了 / 表被锁 / 权限没了）。
	if s.retries == 3 || s.retries%10 == 0 {
		slog.Error("audit sink: batch insert failing, entries requeued",
			"count", len(batch), "consecutive_failures", s.retries,
			"buffered", len(s.buf), "error", err)
	} else {
		slog.Warn("audit sink: batch insert failed, entries requeued",
			"count", len(batch), "error", err)
	}
}

// Stats 返回当前统计（buffered 需要取锁，其余读计数）。
func (s *AuditSink) Stats() AuditSinkStats {
	s.mu.Lock()
	defer s.mu.Unlock()
	return AuditSinkStats{
		Buffered: int64(len(s.buf)),
		Written:  s.written,
		Dropped:  s.dropped,
		Failures: s.failures,
	}
}

// Close 停止窗口协程并 flush 残留缓冲（阻塞至完成）。
func (s *AuditSink) Close() {
	close(s.stopCh)
	<-s.doneCh
}

// ── 默认 PG 批量写入 ─────────────────────────────────────────────────────

// WriteAuditLogs 将审计条目批量 INSERT 到 audit_logs。
// 字段映射：tenant_id 非法/空 → 默认租户；user_id 非法/空 → NULL；
// resource_type 按 resource 路径前缀归类（ent/admin/api/general）。
func WriteAuditLogs(ctx context.Context, entries []AuditEntry) error {
	pool := Pool
	if pool == nil {
		return errors.New("audit sink: postgres pool unavailable")
	}
	if len(entries) == 0 {
		return nil
	}

	var sb strings.Builder
	sb.WriteString(`INSERT INTO audit_logs
		(id, tenant_id, user_id, action, resource_type, resource_id, details, ip_address) VALUES `)
	args := make([]any, 0, len(entries)*8)
	idx := 0
	next := func() string { idx++; return fmt.Sprintf("$%d", idx) }

	for i, e := range entries {
		// tenant_id 现为 smallint：条目里是正整数字串则采用，否则回退默认租户。
		tenantID := DefaultTenantID
		if v, perr := strconv.ParseInt(strings.TrimSpace(e.TenantID), 10, 16); perr == nil && v > 0 {
			tenantID = int16(v)
		}
		var userID any
		if auditSinkUUIDRe.MatchString(e.UserID) {
			userID = e.UserID
		}
		details, err := json.Marshal(struct {
			Detail  string `json:"detail"`
			Success bool   `json:"success"`
			Error   string `json:"error"`
		}{
			Detail:  e.Detail,
			Success: e.Success,
			Error:   e.Error,
		})
		if err != nil {
			slog.Warn("audit sink: marshal details", "error", err)
			details = []byte(`{}`)
		}
		if i > 0 {
			sb.WriteString(", ")
		}
		sb.WriteString("(" + next() + ", " + next() + ", " + next() + ", " + next() + ", " +
			next() + ", " + next() + ", " + next() + ", " + next() + ")")
		args = append(args, generateUUID(), tenantID, userID, clipVarchar(e.Action, 64),
			auditResourceType(e.Resource), clipVarchar(e.Resource, 64),
			string(details), nilableIP(e.IP))
	}

	_, err := pool.Exec(ctx, sb.String(), args...)
	return err
}

// auditResourceType 按资源路径前缀归类 resource_type（varchar(64)）。
func auditResourceType(resource string) string {
	switch {
	case resource == "":
		return "general"
	case strings.HasPrefix(resource, "/admin") || strings.HasPrefix(resource, "/v1/admin"):
		return "admin"
	case strings.HasPrefix(resource, "/"):
		return "api"
	default:
		return "general"
	}
}

// clipVarchar 截断到 varchar(n) 上限（按字符截断，安全处理 UTF-8）。
func clipVarchar(s string, n int) string {
	runes := []rune(s)
	if len(runes) > n {
		return string(runes[:n])
	}
	return s
}

// nilableIP 空 IP 落 NULL（ip_address 列可空）。
func nilableIP(ip string) any {
	if ip == "" {
		return nil
	}
	return clipVarchar(ip, 45)
}

// generateUUID 生成 UUID v4 字符串（36字符，含连字符）。
func generateUUID() string {
	return uuid.New().String()
}
