package api

import (
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/market"
)

// ── 「市场装完 → Agents 页能看到」的接线 ──
//
// 这条链路由两半 SQL 组成，任何一半漂移都会表现成**同一个用户可见故障**：
// 市场说"已安装"，Agents 页却显示空状态。
//
//	写：market_install.go 的 agentInstallQuery（tenant/user/visibility/kind/enabled/时间戳）
//	读：agents.go 的 agentListQuery（tenant_id = $1 AND (user_id = $2 OR visibility='tenant')）
//
// 真实根因（本轮修复）：agents 表 created_at / updated_at **无默认值**、三条写入路径
// 都省略它们 ⇒ 落库 NULL；List 把 NULL 扫进 time.Time 报错后 `continue`，
// 每一行都被静默丢掉。所以这里既钉"写入侧写全了列"，也钉"读取侧兜了 NULL 时间戳"。
//
// 仓库没有 pgxmock（见 api_keys_handler_test.go 的说明），所以测试不连库，而是：
//  ① 用生产的参数构造器 agentInstallArgs 拿到真正会落库的那组值；
//  ② 用生产的可见性函数 agentRowVisible 复核"这行能不能出现在列表里"。
// 两者都是生产代码，不是测试里复制的口径 —— 复制出来的断言会随生产漂移。

// rowFromInstallArgs 把 agentInstallArgs 的返回值还原成一条 agents 行。
// visibility / kind / 时间戳不在这组参数里（它们是 SQL 字面量），由下面的
// 常量断言负责，这里只取有参数的那几列。
func rowFromInstallArgs(args []any) *Agent {
	return &Agent{
		ID:       args[0].(string),
		TenantID: args[1].(int16),
		UserID:   args[2].(string),
		Name:     args[3].(string),
		Enabled:  args[10].(bool),
	}
}

func claimsOf(tenantID int16, userID string) *auth.Claims {
	return &auth.Claims{UserID: userID, TenantID: tenantID}
}

func marketAgentItem() market.Item {
	return market.Item{
		Kind:        market.KindAgent,
		ID:          "agent:code-reviewer",
		Source:      market.SourceBuiltin,
		Name:        "code-reviewer",
		Description: "审查改动",
		Payload:     []byte(`{"system_prompt":"你是审查者","max_turns":3,"timeout_seconds":90}`),
	}
}

// 装完的那一行必须"能被列表看到"：归属取 claims，且状态是启用。
//
// 这直接对应 bug 报告里的现象 —— 若写入的 tenant/user 与列表的过滤口径对不上
// （或 enabled 被写成 false 而前端又把它过滤掉），用户在 Agents 页就只剩空状态。
func TestMarketAgentInstall_RowIsListVisible(t *testing.T) {
	claims := claimsOf(7, "u-a")
	row := rowFromInstallArgs(agentInstallArgs(claims, "agent-1", marketAgentItem(), agentPayload{
		SystemPrompt: "你是审查者",
	}))
	row.Visibility = "private" // 写入侧 SQL 字面量，见下方断言

	if row.TenantID != claims.TenantID {
		t.Errorf("落库 tenant_id = %d，列表按 claims 的 %d 过滤，两个租户口径必须一致",
			row.TenantID, claims.TenantID)
	}
	if row.UserID != claims.UserID {
		t.Errorf("落库 user_id = %q，列表按 claims 的 %q 过滤", row.UserID, claims.UserID)
	}
	if !row.Enabled {
		t.Errorf("市场装完的 Agent 应为 enabled=true（装完就能用），否则前端若按启用态过滤就会显示空状态")
	}
	if !agentRowVisible(claims.TenantID, claims.UserID, row) {
		t.Errorf("装完的行对安装者本人不可见 —— 这正是「装了却显示没有 Agent」")
	}

	// 写入侧必须把"归属/类型/时间戳"显式写出来，不能依赖库里的默认值。
	for _, want := range []string{"'private'", "'chat'", "created_at", "updated_at"} {
		if !strings.Contains(agentInstallQuery, want) {
			t.Errorf("安装写入缺少 %s：\n%s", want, agentInstallQuery)
		}
	}
	if strings.Count(agentInstallQuery, "NOW()") < 2 {
		t.Errorf("created_at / updated_at 必须各写一个 NOW()（DDL 里两列无默认值，省略即 NULL）:\n%s",
			agentInstallQuery)
	}
}

// 两个租户各装一条：各自只看得到自己的，跨租户一律不可见。
func TestMarketAgentInstall_TwoTenantsDoNotLeak(t *testing.T) {
	a := rowFromInstallArgs(agentInstallArgs(claimsOf(1, "u-a"), "agent-a", marketAgentItem(), agentPayload{}))
	a.Visibility = "private"
	b := rowFromInstallArgs(agentInstallArgs(claimsOf(2, "u-b"), "agent-b", marketAgentItem(), agentPayload{}))
	b.Visibility = "private"

	if !agentRowVisible(1, "u-a", a) {
		t.Fatalf("租户 1 的用户看不到自己装的 Agent")
	}
	if !agentRowVisible(2, "u-b", b) {
		t.Fatalf("租户 2 的用户看不到自己装的 Agent")
	}
	if agentRowVisible(1, "u-a", b) {
		t.Errorf("租户 1 看到了租户 2 的 Agent —— 跨租户泄漏")
	}
	if agentRowVisible(2, "u-b", a) {
		t.Errorf("租户 2 看到了租户 1 的 Agent —— 跨租户泄漏")
	}

	// 同租户不同用户：private 的看不到，团队共享（visibility='tenant'）的看得到。
	b.Visibility = "tenant"
	if agentRowVisible(1, "u-a", b) {
		t.Errorf("跨租户的 tenant 共享行仍然不可见，租户隔离优先于共享")
	}
	shared := &Agent{ID: "agent-shared", TenantID: 1, UserID: "u-other", Visibility: "tenant"}
	if !agentRowVisible(1, "u-a", shared) {
		t.Errorf("同租户的团队共享 Agent 应对所有人可见")
	}
	private := &Agent{ID: "agent-private", TenantID: 1, UserID: "u-other", Visibility: "private"}
	if agentRowVisible(1, "u-a", private) {
		t.Errorf("同租户别人的私有 Agent 不该出现在列表里")
	}
}

// 已停用的 Agent 仍必须出现在列表里（前端"全部"分组据此渲染并标注"已停用"）。
// 可见性判定若把 enabled 也算进去，用户会失去启用入口 —— 空状态只在列表真的为空时出现。
func TestAgentRowVisible_DisabledStillListed(t *testing.T) {
	disabled := &Agent{ID: "a-off", TenantID: 1, UserID: "u-a", Visibility: "private", Enabled: false}
	if !agentRowVisible(1, "u-a", disabled) {
		t.Errorf("已停用的 Agent 必须仍然出现在列表里（否则用户没有启用入口）")
	}
	// visibility 为 NULL 的历史行按 private 处理：只有属主可见。
	legacy := &Agent{ID: "a-legacy", TenantID: 1, UserID: "u-a", Visibility: ""}
	if !agentRowVisible(1, "u-a", legacy) {
		t.Errorf("visibility 为空的历史行应视为 private，属主必须能看到")
	}
	if agentRowVisible(1, "u-b", legacy) {
		t.Errorf("visibility 为空的历史行不该被同租户其他人看到")
	}
}

// 读取侧必须兜住 NULL 时间戳 —— 这是本轮 bug 的直接原因：
// NULL 扫进 time.Time 报错 → List 跳过该行 → 页面空状态。
func TestAgentReadQueries_TolerateNullTimestamps(t *testing.T) {
	for name, q := range map[string]string{"list": agentListQuery, "get": agentGetQuery} {
		if !strings.Contains(q, "COALESCE(created_at") {
			t.Errorf("%s 查询没有兜 created_at 的 NULL，历史行会被整行跳过:\n%s", name, q)
		}
		if !strings.Contains(q, "COALESCE(updated_at") {
			t.Errorf("%s 查询没有兜 updated_at 的 NULL:\n%s", name, q)
		}
	}
}
