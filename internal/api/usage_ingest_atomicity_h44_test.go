package api

import (
	"strings"
	"testing"
)

// ── H44：turn 用量入库必须整轮原子（DELETE + INSERT 同事务）──────────────────
//
// 旧实现"先删旧行，再逐条 INSERT"：中途任何一次失败都会留下"旧行已删、新行只写
// 一半"的状态 —— 用量少记，而它是按 turn 结算的账，少记等于少收，事后无从查证。

func usageIngestSrc(t *testing.T) string {
	t.Helper()
	return readRepoFile(t, "internal/api/usage_events_internal.go")
}

func TestUsageIngestWrapsDeleteAndInsertsInOneTransaction(t *testing.T) {
	src := usageIngestSrc(t)
	if !strings.Contains(src, "db.GlobalDBManager.Begin(") {
		t.Fatal("用量入库必须开事务：删除与插入是一个整体")
	}
	if !strings.Contains(src, "tx.Exec(r.Context(),") {
		t.Fatal("DELETE 必须走事务")
	}
	if !strings.Contains(src, "recordUsageEventTx(storeCtx, tx,") {
		t.Fatal("逐条 INSERT 必须走同一个事务")
	}
	if strings.Contains(src, "RecordUsageEvent(storeCtx,") {
		t.Fatal("不得再绕过事务直接写池子")
	}
	if !strings.Contains(src, "tx.Commit(") {
		t.Fatal("必须有显式提交")
	}
	if !strings.Contains(src, "_ = tx.Rollback(") {
		t.Fatal("失败路径必须回滚")
	}
}

func TestUsageIngestCommitsBeforeResponding(t *testing.T) {
	src := usageIngestSrc(t)
	commitAt := strings.Index(src, "tx.Commit(")
	okAt := strings.Index(src, `OK(w, map[string]interface{}{"stored": stored})`)
	if commitAt == -1 || okAt == -1 {
		t.Fatal("找不到提交点或响应点")
	}
	if commitAt > okAt {
		t.Fatal("必须先提交再回 200")
	}
}

func TestUsageIngestRollsBackOnDeleteFailure(t *testing.T) {
	src := usageIngestSrc(t)
	start := strings.Index(src, "stale rows delete failed")
	if start == -1 {
		t.Fatal("删除失败分支不见了")
	}
	tail := src[start:]
	if !strings.Contains(tail[:400], "InternalError(w,") {
		t.Fatal("删除失败必须整体失败（旧实现是警告后继续写）")
	}
}

// 事务版本与池子版本必须同口径（同样过滤空行、同样补 provider）。
func TestRecordUsageEventTxSharesTheSameGuards(t *testing.T) {
	src := readRepoFile(t, "internal/api/usage_events.go")
	start := strings.Index(src, "func recordUsageEventTx(")
	if start == -1 {
		t.Fatal("缺少 recordUsageEventTx")
	}
	body := src[start:]
	if !strings.Contains(body, `ev.Model == "" && ev.InputTokens == 0`) {
		t.Fatal("空行过滤必须与池子版本一致")
	}
	if !strings.Contains(body, "resolveProviderForModel(") {
		t.Fatal("provider 归因必须与池子版本一致")
	}
	if !strings.Contains(body, "tx.Exec(") {
		t.Fatal("应写入调用方的事务")
	}
}
