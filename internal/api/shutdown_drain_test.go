package api

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"
)

// L08：停机时审计缓冲必须被**同步**排空，而不是随 worker 一起丢掉。
func TestDrainAuditLogsEmptiesBuffer(t *testing.T) {
	// 其它用例不会启动 audit worker；保险起见先清空通道，避免把别人的任务算进来。
	for len(auditLogCh) > 0 {
		<-auditLogCh
	}
	for i := 0; i < 3; i++ {
		auditLogCh <- auditLogTask{
			ctx:    context.Background(),
			method: "POST",
			path:   "/v1/probe",
			status: 200,
		}
	}

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	DrainAuditLogs(ctx)

	if remaining := len(auditLogCh); remaining != 0 {
		t.Fatalf("审计缓冲未排空，剩余 %d 条", remaining)
	}
}

// 这个测试钉住**顺序**：runs 要在停接之后等，审计/画布要在最后同步刷。
// 顺序反了不会报错，只会让"最后一轮/最后一批审计"在退出时丢掉。
func TestGatewayShutdownDrainsRunsThenFlushes(t *testing.T) {
	src, err := os.ReadFile("../../cmd/chiron/main.go")
	if err != nil {
		t.Fatalf("read main.go: %v", err)
	}
	text := string(src)

	runs := strings.Index(text, "api.DrainInFlightRuns(")
	audit := strings.Index(text, "api.DrainAuditLogs(")
	canvas := strings.Index(text, "api.FlushSessionMap(")
	if runs < 0 || audit < 0 || canvas < 0 {
		t.Fatalf("main.go 缺少停机收尾调用（runs=%d audit=%d canvas=%d）", runs, audit, canvas)
	}
	if !(runs < audit && audit < canvas) {
		t.Fatalf("停机顺序必须是 runs → audit → canvas，实际为 runs=%d audit=%d canvas=%d", runs, audit, canvas)
	}
}
