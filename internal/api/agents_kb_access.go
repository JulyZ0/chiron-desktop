package api

// 知识库绑定的授权（H77）。
//
// 为什么必须在这里做：Agent 的 `kb_id` 决定派发时给哪些上下文注入检索结果。此前
// `kb_id` **完全不校验**：
//   - Go 侧 Create/Update 直接把它写进 `agents.kb_id`；
//   - 引擎派发时按 ID 查库，再用**库主**身份检索，把答案与引用原样注入到调用者的对话。
//
// 于是"知道别人的知识库 UUID"就能把对方私有库的正文读进自己的会话 —— 授权被
// "以资源所有者身份调用"绕过去了。
//
// 判据与引擎侧 `app/kb_access.py::kb_access_allowed` 完全同构（自己的 / public /
// 同租户共享），避免两端各写一套：这里松、引擎紧 → 用户能绑但派发时静默丢上下文；
// 这里紧、引擎松 → 能绑别的库，等于没修。

import (
	"context"
	"fmt"

	"github.com/athenavi/chiron/internal/db"
)

// kbAccessStore 是知识库校验所需的窄接口（`db.DBManager` 天然满足），便于单测注入。
type kbAccessStore interface {
	FetchOne(ctx context.Context, sql string, args ...interface{}) (map[string]interface{}, error)
}

// kbStore 返回知识库校验用的存储（`db.GlobalDBManager` 在启动期可能为 nil，
// 此时返回 nil 让 `validateAgentKBID` 明确失败 —— 不放行）。
func kbStore() kbAccessStore {
	if db.GlobalDBManager == nil {
		return nil
	}
	return db.GlobalDBManager
}

// validateAgentKBID 校验 `kbID` 对调用者可见；空串表示"不绑知识库"，直接通过。
//
// 返回 error 时调用方应回 400/403（**不要**回"库不存在"的具体原因给普通用户，
// 否则 kb_id 就成了存在性探针）。
func validateAgentKBID(
	ctx context.Context, store kbAccessStore, kbID, userID string, tenantID int16,
) error {
	kbID = trimSpace(kbID)
	if kbID == "" {
		return nil
	}
	if store == nil {
		// 数据库不可用时**不能放行**（那等于回到"随便绑"）；让调用方明确失败。
		return fmt.Errorf("knowledge base validation unavailable")
	}
	row, err := store.FetchOne(ctx, `
		SELECT COALESCE(user_id::text, '') AS owner_id,
		       COALESCE(visibility, '') AS visibility,
		       COALESCE(tenant_id, 0) AS kb_tenant
		  FROM knowledge_bases
		 WHERE id = $1`, kbID)
	if err != nil {
		return fmt.Errorf("knowledge base lookup failed: %w", err)
	}
	if row == nil {
		return fmt.Errorf("knowledge base not found or not accessible")
	}
	if kbRowAccessible(row, userID, tenantID) {
		return nil
	}
	return fmt.Errorf("knowledge base not found or not accessible")
}

// kbRowAccessible 与引擎侧 `kb_access_allowed` 逐条对应：
//
//  1. 自己的（owner 相同）→ 放行；
//  2. `visibility='public'` → 放行；
//  3. `visibility='tenant'` 且库声明的租户 == 调用者租户 → 放行（worker 里的 NULL
//     租户按 0 处理，与 SQL `tenant_id = $n` 的"NULL 匹配不上"同口径）。
func kbRowAccessible(row map[string]interface{}, userID string, tenantID int16) bool {
	owner := stringOf(row["owner_id"])
	if owner != "" && owner == trimSpace(userID) {
		return true
	}
	switch stringOf(row["visibility"]) {
	case "public":
		return true
	case "tenant":
		kbTenant := int(intOf(row["kb_tenant"]))
		return kbTenant != 0 && kbTenant == int(tenantID)
	}
	return false
}
