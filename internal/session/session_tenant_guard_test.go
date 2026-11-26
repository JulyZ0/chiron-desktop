package session

import (
	"os"
	"strings"
	"testing"
)

// H33 的回归网：会话的租户归属必须来自调用方 claims。
//
// 这条约束在运行时表现为"新会话落到默认租户"——不会报错、不会 panic，只会让
// 多租户用户看不见自己的会话。没有 DB 就没法从行为上断言，所以这里只钉住
// 「INSERT 的 tenant 参数不再是常量」这一处结构事实（与 api/user_insert_id_test.go
// 用源码守卫同一套路）。
func TestCreateSessionTenantComesFromCaller(t *testing.T) {
	src, err := os.ReadFile("manager_crud.go")
	if err != nil {
		t.Fatalf("读取 manager_crud.go 失败: %v", err)
	}
	text := string(src)

	if !strings.Contains(text, "func (m *Manager) CreateSession(ctx context.Context, id, userID, title string, tenantID int16)") {
		t.Fatal("CreateSession 必须接收调用方的 tenantID（H33：会话写入使用 claims tenant）")
	}

	// 定位 sessions INSERT 的参数行：必须是 tenantID，不能是 DefaultTenantID。
	idx := strings.Index(text, "INSERT INTO sessions (id, tenant_id, user_id, title, created_at, updated_at)")
	if idx < 0 {
		t.Fatal("没有找到 sessions 的 INSERT —— 本守卫已失效，请更新匹配串")
	}
	tail := text[idx:]
	if end := strings.Index(tail, ")"); end > 0 {
		tail = tail[:end]
	}
	args := text[idx+len("INSERT INTO sessions (id, tenant_id, user_id, title, created_at, updated_at)"):]
	if stop := strings.Index(args, "\n\t\tif err != nil"); stop > 0 {
		args = args[:stop]
	}
	if strings.Contains(args, "DefaultTenantID") {
		t.Fatalf("sessions INSERT 不得再写常量租户：\n%s", args)
	}
	if !strings.Contains(args, "tenantID") {
		t.Fatalf("sessions INSERT 必须使用调用方的 tenantID：\n%s", args)
	}
}

// A3：**隐式**建会话（消息先落库、会话后建）同样不能再写死默认租户。
//
// 显式 CreateSession 早已要求调用方传 tenantID（上一条），但 SaveUserMessage /
// SaveMessages / CreateTurn / SaveMessage 这条"按 session_id 写入"的路径仍硬编码
// DefaultTenantID —— 多租户/邀请码用户经它产生的会话落进默认租户：本人列表看不见，
// 项目侧按 claims tenant 过滤也找不到。没有 DB 时这里只钉结构事实：
// 整个文件不得再出现常量租户，两处 INSERT 都必须读 tenantIDFromContext(ctx)。
func TestImplicitSessionInsertsUseCallerTenant(t *testing.T) {
	src, err := os.ReadFile("manager_messages.go")
	if err != nil {
		t.Fatalf("读取 manager_messages.go 失败: %v", err)
	}
	text := string(src)

	if strings.Contains(text, "DefaultTenantID") {
		t.Fatal("manager_messages.go 又出现了 DefaultTenantID：隐式建会话必须用 tenantIDFromContext(ctx)")
	}
	inserts := strings.Count(text, "INSERT INTO sessions (id, tenant_id, user_id, title, created_at, updated_at)")
	if inserts < 2 {
		t.Fatalf("预期至少两处隐式建会话 INSERT，找到 %d 处 —— 守卫的匹配串可能已失效", inserts)
	}
	if got := strings.Count(text, "tenantIDFromContext(ctx)"); got < inserts {
		t.Fatalf("每处 sessions INSERT 都要读 claims 租户：INSERT=%d, tenantIDFromContext=%d", inserts, got)
	}
}
