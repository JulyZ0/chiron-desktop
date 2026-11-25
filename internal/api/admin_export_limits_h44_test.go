package api

import (
	"strings"
	"testing"
)

// ── H44：租户导出的上限与并发闸门 ────────────────────────────────────────────
//
// 旧实现"边写边流"：`WriteHeader(200)` 之后才发现表很大，只能中途 zw.Close() ——
// 客户端拿到**状态码 200 但内容不全**的 zip（manifest 还缺）。也没有并发限制：
// N 个导出各自吃一份内存与连接。

func TestExportBudgetCountsRowsAndStopsAtLimit(t *testing.T) {
	budget := &exportBudget{limits: exportLimits{MaxRows: 2, MaxBytes: 1000}}
	if err := budget.addRow(10); err != nil {
		t.Fatalf("第一条应放行: %v", err)
	}
	if err := budget.addRow(10); err != nil {
		t.Fatalf("第二条应放行: %v", err)
	}
	err := budget.addRow(10)
	if err == nil {
		t.Fatal("超过行上限必须报错")
	}
	if !strings.Contains(err.Error(), "rows >") {
		t.Fatalf("错误信息应说明是行数超限: %v", err)
	}
}

func TestExportBudgetStopsAtByteLimit(t *testing.T) {
	budget := &exportBudget{limits: exportLimits{MaxRows: 100, MaxBytes: 20}}
	if err := budget.addRow(15); err != nil {
		t.Fatalf("第一条应放行: %v", err)
	}
	if err := budget.addRow(10); err == nil {
		t.Fatal("超过字节上限必须报错")
	}
}

func TestExportBudgetUnlimitedWhenZero(t *testing.T) {
	budget := &exportBudget{limits: exportLimits{MaxRows: 0, MaxBytes: 0}}
	for i := 0; i < 1000; i++ {
		if err := budget.addRow(1 << 20); err != nil {
			t.Fatalf("0 表示不限，不应报错: %v", err)
		}
	}
}

func TestExportLimitsDefaults(t *testing.T) {
	limits := exportLimitsFromEnv()
	if limits.MaxRows <= 0 || limits.MaxBytes <= 0 {
		t.Fatalf("默认上限必须为正: %+v", limits)
	}
	t.Setenv("CHIRON_EXPORT_MAX_ROWS", "7")
	t.Setenv("CHIRON_EXPORT_MAX_BYTES", "1234")
	overridden := exportLimitsFromEnv()
	if overridden.MaxRows != 7 || overridden.MaxBytes != 1234 {
		t.Fatalf("环境变量应覆盖默认值: %+v", overridden)
	}
	// 坏值回落到默认，不能把上限变成 0（那等于关掉闸门）
	t.Setenv("CHIRON_EXPORT_MAX_ROWS", "abc")
	if exportLimitsFromEnv().MaxRows <= 0 {
		t.Fatal("非法配置必须回落到安全默认值")
	}
}

func TestAcquireExportRejectsConcurrentExport(t *testing.T) {
	release, err := acquireExport(9001)
	if err != nil {
		t.Fatalf("首次获取应成功: %v", err)
	}
	if _, err := acquireExport(9001); err != ErrExportInProgress {
		t.Fatalf("同租户并发导出必须拒绝，得到 %v", err)
	}
	release()
	if _, err := acquireExport(9001); err != nil {
		t.Fatalf("释放后应可再次获取: %v", err)
	}
	// 不同租户互不影响
	if _, err := acquireExport(9002); err != nil {
		t.Fatalf("不同租户不该互相阻塞: %v", err)
	}
}

// 源码守卫：handler 必须先写临时文件，再写响应头。
func TestExportHandlerBuffersBeforeWritingHeaders(t *testing.T) {
	src := readRepoFile(t, "internal/api/admin_export.go")
	start := strings.Index(src, "func (h *AdminHandler) ExportTenantData")
	if start == -1 {
		t.Fatal("找不到导出 handler")
	}
	body := src[start:]

	tempAt := strings.Index(body, "os.CreateTemp")
	headerAt := strings.Index(body, `w.Header().Set("Content-Type", "application/zip")`)
	if tempAt == -1 {
		t.Fatal("必须先把 zip 写进临时文件")
	}
	if headerAt == -1 {
		t.Fatal("找不到响应头设置")
	}
	if tempAt > headerAt {
		t.Fatal("临时文件必须在写响应头之前创建（否则超限时已无法改状态码）")
	}
	if !strings.Contains(body, "http.StatusRequestEntityTooLarge") {
		t.Fatal("超限应返回 413 而不是半截 zip")
	}
	if !strings.Contains(body, "io.Copy(w, tmp)") {
		t.Fatal("最后应把临时文件内容拷给客户端")
	}
}

func TestExportHandlerPassesBudgetToTableWriter(t *testing.T) {
	src := readRepoFile(t, "internal/api/admin_export.go")
	if !strings.Contains(src, "writeNDJSONTable(ctx, zw, table, tenantID, budget)") {
		t.Fatal("表写入必须带预算（否则上限形同虚设）")
	}
	if !strings.Contains(src, "budget.addRow(") {
		t.Fatal("逐行累计缺失")
	}
}
