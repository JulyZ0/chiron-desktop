package api

// ── 检查点 GC 定时化的契约测试 ──
//
// 钉住三件事，每一件对应一个真实故障面：
//
//  1. **开关**：默认启用 24h；`CHIRON_CHECKPOINT_GC_INTERVAL_HOURS` 能关掉；非法值
//     回退默认（静默关掉等于 CAS 只增不减，而磁盘只会在很久以后才告警）。
//  2. **定时任务发的必须是 `dry_run=false`**：预演对周期任务毫无意义，而默认值走
//     反了（Gate 面包屑：引擎端 `dry_run` 缺省 true）会让 GC 跑一年也删不掉一个字节。
//  3. **幂等**：连续两轮都安全 —— 第二轮引擎报 `deleted=0`，本层不得因为"没删东西"
//     而报错或叠加计数（否则运维看到的数字会随运行时长线性增长）。

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/engine"
)

func TestCheckpointGcIntervalDefaultAndSwitch(t *testing.T) {
	// 缺省：启用 + 24h
	t.Setenv(checkpointGcEnvInterval, "")
	interval, enabled := checkpointGcInterval()
	if !enabled || interval != 24*time.Hour {
		t.Fatalf("缺省应为启用且 24h，实际 enabled=%v interval=%v", enabled, interval)
	}

	// 显式关闭的每一种写法
	for _, off := range []string{"0", "off", "OFF", "false", "no", "disabled", "none"} {
		t.Setenv(checkpointGcEnvInterval, off)
		if _, enabled := checkpointGcInterval(); enabled {
			t.Fatalf("%q 应当关闭定时 GC", off)
		}
	}

	// 自定义周期
	t.Setenv(checkpointGcEnvInterval, "6")
	if interval, enabled := checkpointGcInterval(); !enabled || interval != 6*time.Hour {
		t.Fatalf("6 应解析为 6h，实际 enabled=%v interval=%v", enabled, interval)
	}
}

func TestCheckpointGcIntervalFallsBackOnGarbage(t *testing.T) {
	// 非法值不能变成"关闭"：那会让 GC 永久停摆且没有任何症状。
	for _, bad := range []string{"abc", "-3", "0.0", "  "} {
		t.Setenv(checkpointGcEnvInterval, bad)
		interval, enabled := checkpointGcInterval()
		if !enabled {
			t.Fatalf("%q 是非法值，应回退默认（启用），而不是关闭", bad)
		}
		if interval != checkpointGcIntervalDefault {
			t.Fatalf("%q 的回退周期应为默认 %v，实际 %v", bad, checkpointGcIntervalDefault, interval)
		}
	}
}

// stubCheckpointGc 替换引擎调用，记录每次载荷。
func stubCheckpointGc(t *testing.T, replies ...map[string]interface{}) *[]map[string]interface{} {
	t.Helper()
	var seen []map[string]interface{}
	original := postCheckpointGc
	i := 0
	postCheckpointGc = func(
		_ context.Context, _ *engine.PythonClient, payload map[string]interface{}, out *map[string]interface{},
	) error {
		seen = append(seen, payload)
		reply := map[string]interface{}{}
		if i < len(replies) {
			reply = replies[i]
		}
		i++
		*out = reply
		return nil
	}
	t.Cleanup(func() { postCheckpointGc = original })
	return &seen
}

func TestCheckpointGcOnceIsRealRunAndIdempotent(t *testing.T) {
	seen := stubCheckpointGc(t,
		map[string]interface{}{"deleted": 3, "freed_bytes": 300, "dry_run": false},
		map[string]interface{}{"deleted": 0, "freed_bytes": 0, "dry_run": false},
	)

	first, err := checkpointGcOnce(context.Background(), &engine.PythonClient{})
	if err != nil {
		t.Fatalf("第一轮不该失败：%v", err)
	}
	if got := intOf(first["deleted"]); got != 3 {
		t.Fatalf("第一轮应删除 3 个分片，实际 %d", got)
	}

	second, err := checkpointGcOnce(context.Background(), &engine.PythonClient{})
	if err != nil {
		t.Fatalf("第二轮不该失败：%v", err)
	}
	if got := intOf(second["deleted"]); got != 0 {
		t.Fatalf("幂等：第二轮应删 0 个，实际 %d", got)
	}

	if len(*seen) != 2 {
		t.Fatalf("应发出 2 次引擎调用，实际 %d", len(*seen))
	}
	for i, payload := range *seen {
		if payload["dry_run"] != false {
			t.Fatalf("第 %d 轮的 dry_run 必须是 false（预演删不掉任何东西）：%v", i+1, payload["dry_run"])
		}
		if got := intOf(payload["retention_days"]); got != checkpointGcDefaultRetentionDays {
			t.Fatalf("第 %d 轮保留期应为 %d，实际 %d", i+1, checkpointGcDefaultRetentionDays, got)
		}
		if _, ok := payload["roots"]; !ok {
			t.Fatalf("第 %d 轮载荷缺少 roots 字段", i+1)
		}
	}
}

func TestCheckpointGcOnceRequiresEngineClient(t *testing.T) {
	if _, err := checkpointGcOnce(context.Background(), nil); err == nil {
		t.Fatal("没有引擎客户端时应返回错误，而不是静默成功")
	}
}

func TestCheckpointGcRoundSwallowsEngineFailure(t *testing.T) {
	// 一轮失败只记日志：退出循环会让 GC 永久停摆（重启前再也不跑）。
	original := postCheckpointGc
	postCheckpointGc = func(
		_ context.Context, _ *engine.PythonClient, _ map[string]interface{}, _ *map[string]interface{},
	) error {
		return errors.New("engine 503")
	}
	t.Cleanup(func() { postCheckpointGc = original })

	// 不 panic、不阻塞即通过（日志里会有一条 warn）。
	checkpointGcRound(context.Background(), &engine.PythonClient{})
}

func TestCheckpointGcSchedulerDisabledDoesNotRegister(t *testing.T) {
	t.Setenv(checkpointGcEnvInterval, "off")
	called := false
	original := postCheckpointGc
	postCheckpointGc = func(
		_ context.Context, _ *engine.PythonClient, _ map[string]interface{}, _ *map[string]interface{},
	) error {
		called = true
		return nil
	}
	t.Cleanup(func() { postCheckpointGc = original })

	ctx, cancel := context.WithCancel(context.Background())
	StartCheckpointGcScheduler(ctx, &engine.PythonClient{})
	cancel()
	time.Sleep(20 * time.Millisecond)
	if called {
		t.Fatal("关闭状态下不得发起任何 GC 调用")
	}
}
