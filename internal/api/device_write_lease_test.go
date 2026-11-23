package api

// R5：无 Redis 时写租约必须默认拒绝（fail-closed）。

import (
	"context"
	"testing"
)

func TestDeviceWriteLeaseDefaultsDenyWithoutRedis(t *testing.T) {
	if deviceWriteAllowed(context.Background(), "any-project") {
		t.Fatal("Redis 不可用时 client 项目写权限必须默认拒绝")
	}
}
