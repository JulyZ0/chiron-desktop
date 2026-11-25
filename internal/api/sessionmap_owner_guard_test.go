package api

import (
	"strings"
	"testing"
)

// ── H78：会话画布与项目会话绑定的归属一致性（PRD 9.10）─────────────────────
//
// 缺陷形态（三处，都会让"别人的数据被改写/删除"而不报错）：
//
//  1. `SaveWorkspace` 只在**热层命中**时校验 owner：冷缓存（重启 / Redis 淘汰 / 无 Redis）
//     下等于不校验 —— 攻击者拿他人 wsID 发一次 PUT，就能把调用者身份刻进 body、写进热层；
//  2. 后台 flusher 的 `smPersist`  UPDATE/DELETE **只按 id**：于是上一步留下的脏快照
//     会把对方画布整体覆盖；
//  3. `BindProjectSession` 只校验目标 workspace，不校验 session 归属：
//     `?force=true` 能删掉别人在该会话上的项目归属（"移动"语义变成了跨用户抢绑定）。
//
// 这一组用**源码扫描**守住"查询必须带归属谓词"这条结构性约束 —— 与包内既有
// `TestSessionMapEdgeKindGuard` 同一套路：真正要防的是"以后有人又写一条只按 id 的
// UPDATE"，而这种退化不会让任何行为测试变红，只会静默地改坏别人的数据。
//
// 另配一条**行为**检查：`smLoadWorkspaceOwner` 必须在数据库不可用时显式报错
// （fail-closed），而不是返回"没找到"让调用方当成"新建"。

func TestSessionMapPersistCarriesOwnerPredicate(t *testing.T) {
	src := readSessionMapSources(t)

	persistAt := strings.Index(src, "func smPersist(")
	if persistAt < 0 {
		t.Fatal("找不到 smPersist")
	}
	persistBody := src[persistAt:]
	// 只截到下一个顶层函数，避免把别处的 SQL 也算进 smPersist。
	if end := strings.Index(persistBody[len("func smPersist("):], "\nfunc "); end > 0 {
		persistBody = persistBody[:len("func smPersist(")+end]
	}

	update := extractFirstRawSQLAfter(persistBody, "UPDATE session_map_workspaces")
	if update == "" {
		t.Fatal("找不到 smPersist 里的 UPDATE session_map_workspaces")
	}
	for _, want := range []string{"WHERE id = $1", "tenant_id = $6", "user_id = $7"} {
		if !strings.Contains(update, want) {
			t.Fatalf("smPersist 的 UPDATE 必须带归属谓词（缺 %q）：\n%s", want, update)
		}
	}
	if !strings.Contains(persistBody, "RowsAffected") {
		t.Fatal("smPersist 必须检查受影响行数：0 行说明该画布不归调用者，不能当成保存成功")
	}
	if !strings.Contains(persistBody, "USING session_map_workspaces") {
		t.Fatal("smPersist 的节点 DELETE 必须用 USING session_map_workspaces 收敛 owner")
	}
}

func TestSessionMapSaveUsesAuthoritativeOwner(t *testing.T) {
	src := readSessionMapSources(t)

	if !strings.Contains(src, "func smLoadWorkspaceOwner(") {
		t.Fatal("缺少 smLoadWorkspaceOwner：归属必须从 PG 读，不能只看 Redis")
	}
	if !strings.Contains(src, "smLoadWorkspaceOwner(r.Context(), wsID)") {
		t.Fatal("SaveWorkspace 必须调用 smLoadWorkspaceOwner 做归属校验（冷缓存也要同口径）")
	}
	// 反向约束：不能再用"缓存命中才校验"的形态。
	if strings.Contains(src, "if existing, ok := smCacheGet(r.Context(), wsID); ok {\n\t\tif existing.TenantID !=") {
		t.Fatal("SaveWorkspace 仍在用「热层命中才校验 owner」的旧形态（冷缓存即失守）")
	}
}

func TestSessionMapBindRequiresSessionOwner(t *testing.T) {
	src := readSessionMapSources(t)

	if !strings.Contains(src, "func smOwnsSession(") {
		t.Fatal("缺少 smOwnsSession：绑定必须校验会话归属")
	}
	bindStart := strings.Index(src, "func (h *SessionMapHandler) BindProjectSession(")
	if bindStart < 0 {
		t.Fatal("找不到 BindProjectSession")
	}
	bindBody := src[bindStart:]
	if end := strings.Index(bindBody, "func (h *SessionMapHandler) UnbindProjectSession("); end > 0 {
		bindBody = bindBody[:end]
	}
	if !strings.Contains(bindBody, "smOwnsSession(") {
		t.Fatal("BindProjectSession 必须先校验 session 归属，否则 force 会删掉他人的项目绑定")
	}
	if !strings.Contains(bindBody, "USING session_map_workspaces") {
		t.Fatal("绑定里的 DELETE 必须用 owner 子查询收敛，避免误删他人归属")
	}
}

func TestSessionMapOwnerLookupFailsClosed(t *testing.T) {
	// 数据库不可用 ⇒ 必须显式报错。返回 (_, false, nil) 会让调用方当成"画布不存在"
	// 而走进"新建"分支 —— 那正是"授权层不可用反而放行"的经典退化。
	var err error
	_, found, err := smLoadWorkspaceOwner(t.Context(), "any-id")
	if err == nil && found {
		t.Fatal("无数据库时不应报告「找到了归属」")
	}
	if err == nil {
		t.Fatal("无数据库时必须返回错误（fail-closed），不能静默当成「不存在」")
	}
}

// extractFirstRawSQLAfter 从 `anchor` 之后截出第一条含 `needle` 的原始字符串 SQL。
//
// 只做静态断言用，不追求通用解析：锚点写死在用例里，锚点变了就该有人来同步。
func extractFirstRawSQLAfter(src, needle string) string {
	for rest := src; ; {
		start := strings.Index(rest, "`")
		if start < 0 {
			return ""
		}
		end := strings.Index(rest[start+1:], "`")
		if end < 0 {
			return ""
		}
		stmt := rest[start+1 : start+1+end]
		if strings.Contains(stmt, needle) {
			return stmt
		}
		rest = rest[start+1+end+1:]
	}
}
