package session

import (
	"strings"
	"testing"
)

// ── H14：一轮对话的三次写必须同事务 ──────────────────────────────────────────
//
// 旧实现是三条独立语句、各自吞掉错误：用户消息写成功而助手消息失败时，库里留下
// "用户问了一句、模型没答"的半轮对话 —— 刷新后看到问题孤零零挂着，重新提问又会
// 把同一轮再写一遍。这一组用源码守卫钉住事务边界（真实回滚需 PG，由 CI 覆盖）。

func saveMessagesSrc(t *testing.T) string {
	t.Helper()
	return readSessionFile(t, "manager_messages.go")
}

func readSessionFile(t *testing.T, name string) string {
	t.Helper()
	raw, err := readFileRelative(name)
	if err != nil {
		t.Fatalf("读取 %s: %v", name, err)
	}
	return raw
}

func TestSaveMessagesWrapsAllWritesInOneTransaction(t *testing.T) {
	src := saveMessagesSrc(t)
	if !strings.Contains(src, "m.pool.Begin(ctx)") {
		t.Fatal("SaveMessages 必须开事务：用户消息、助手消息、标题是三次写")
	}
	if !strings.Contains(src, "tx.Commit(ctx)") {
		t.Fatal("必须有显式提交")
	}
	// 三次写都要走 tx
	body := src[strings.Index(src, "func (m *Manager) SaveMessages"):]
	if strings.Contains(body, "m.pool.Exec(ctx") {
		t.Fatal("事务内不得再用池子直接写（那样不受事务保护）")
	}
	for _, marker := range []string{"tx.Exec(ctx", "'user'", "'assistant'", "UPDATE sessions SET title"} {
		if !strings.Contains(body, marker) {
			t.Fatalf("事务内缺少 %q", marker)
		}
	}
}

func TestSaveMessagesRollsBackOnAnyFailure(t *testing.T) {
	src := saveMessagesSrc(t)
	if !strings.Contains(src, "_ = tx.Rollback(ctx)") {
		t.Fatal("失败路径必须回滚")
	}
	if !strings.Contains(src, "committed = true") {
		t.Fatal("提交后要把 committed 置真，否则 defer 会把成功写入回滚掉")
	}
	commitAt := strings.Index(src, "tx.Commit(ctx)")
	flagAt := strings.Index(src, "committed = true")
	if commitAt == -1 || flagAt == -1 || flagAt < commitAt {
		t.Fatal("committed 必须在 Commit 成功之后才置真")
	}
}
