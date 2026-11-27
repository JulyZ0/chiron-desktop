package db

import (
	"context"
	"testing"
	"time"
)

func TestDBOpContextAddsDefaultDeadline(t *testing.T) {
	ctx, cancel := dbOpContext(context.Background())
	defer cancel()
	deadline, ok := ctx.Deadline()
	if !ok {
		t.Fatal("无 deadline 的调用必须补上默认上限")
	}
	if remaining := time.Until(deadline); remaining > defaultDBOpTimeout+time.Second {
		t.Fatalf("默认上限过长: %v", remaining)
	}
}

func TestDBOpContextKeepsCallerDeadline(t *testing.T) {
	parent, parentCancel := context.WithTimeout(context.Background(), time.Minute)
	defer parentCancel()
	ctx, cancel := dbOpContext(parent)
	defer cancel()
	got, ok := ctx.Deadline()
	want, _ := parent.Deadline()
	if !ok || !got.Equal(want) {
		t.Fatalf("调用方 deadline 被覆盖: got %v want %v", got, want)
	}
}

type fakeScanRow struct{ scanned bool }

func (r *fakeScanRow) Scan(...any) error { r.scanned = true; return nil }

func TestCancelOnScanRowReleasesContext(t *testing.T) {
	row := &fakeScanRow{}
	cancelled := 0
	wrapped := &cancelOnScanRow{Row: row, cancel: func() { cancelled++ }}
	if err := wrapped.Scan(); err != nil {
		t.Fatalf("scan: %v", err)
	}
	if !row.scanned {
		t.Fatal("Scan 未透传给底层 row")
	}
	if cancelled != 1 {
		t.Fatalf("Scan 后应释放一次 ctx，got %d", cancelled)
	}
}
