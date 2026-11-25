package api

import (
	"strings"
	"testing"
)

// ── H14：checkpoint 主记录与 manifest 必须原子提交 ───────────────────────────
//
// 旧实现是两次独立语句：主记录写成功、清单写失败时，库里留下一条 `state='ok'`
// 但没有清单的 checkpoint —— 前端显示"这份快照可用"，恢复时才发现清单是空的
// （"恢复成功但文件没回来"）。这一组用源码守卫钉住事务边界（真实回滚需要 PG，
// 由团队环境 / CI 的 postgres service 覆盖）。

func checkpointIngestSrc(t *testing.T) string {
	t.Helper()
	return readRepoFile(t, "internal/api/project_checkpoints.go")
}

func TestCheckpointIngestRunsInsideATransaction(t *testing.T) {
	src := checkpointIngestSrc(t)
	if !strings.Contains(src, "db.GlobalDBManager.Begin(ctx)") {
		t.Fatal("入库必须开事务：主记录与清单是两次写")
	}
	if !strings.Contains(src, "tx.Commit(ctx)") {
		t.Fatal("必须有显式提交（否则 defer 会把写入回滚掉）")
	}
	// 主记录与清单都必须走 tx，不能一半走池子
	if strings.Contains(src, "db.GlobalDBManager.Exec(ctx, `INSERT INTO project_checkpoints") {
		t.Fatal("主记录写入不得绕过事务")
	}
	if !strings.Contains(src, `sb.WriteString(" ON CONFLICT DO NOTHING")`) {
		t.Fatal("清单插入语句不见了")
	}
	idx := strings.Index(src, `sb.WriteString(" ON CONFLICT DO NOTHING")`)
	tail := src[idx:]
	if !strings.Contains(tail[:400], "tx.Exec(ctx") {
		t.Fatal("清单插入必须走同一个事务")
	}
}

func TestCheckpointIngestRollsBackOnManifestFailure(t *testing.T) {
	src := checkpointIngestSrc(t)
	// 两条失败路径都要 Rollback：主记录失败、清单失败
	if strings.Count(src, "_ = tx.Rollback(ctx)") < 2 {
		t.Fatal("主记录失败与清单失败都必须回滚")
	}
	if strings.Contains(src, "checkpoint stored but untracked manifest failed") {
		t.Fatal("不得再返回\"主记录已存但清单失败\"——那正是半条记录的来源")
	}
}

func TestCheckpointIngestCommitsBeforeResponding(t *testing.T) {
	src := checkpointIngestSrc(t)
	commitAt := strings.Index(src, "tx.Commit(ctx)")
	okAt := strings.Index(src, `out := map[string]interface{}{"stored": true`)
	if commitAt == -1 || okAt == -1 {
		t.Fatal("找不到提交点或响应点")
	}
	if commitAt > okAt {
		t.Fatal("必须先提交再回 200，否则调用方以为入库成功而实际被回滚")
	}
}
