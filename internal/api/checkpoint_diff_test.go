package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

// ── 预览端点的归属校验 ──
//
// 引擎的 `/v1/internal/checkpoints/diff` 拿到 root 就去读工作区，它自己无法判断
// "这个 checkpoint 是不是调用者的"。所以归属必须在网关查，且必须**同时**按
// tenant 与 user 过滤。这里钉住三件事：
//
//  1. 没有鉴权上下文一律 401（不进入归属查询）；
//  2. 缺 checkpoint id 一律 400（不拿空 id 去查库）；
//  3. 归属查询 SQL 必须同时包含 tenant_id 与 user_id 条件 —— 这是"猜到的 id
//     能不能预览别人的工作区"的唯一防线，删掉任何一颗都会静默放行。

func TestCheckpointDiffOwnershipSQLRequiresTenantAndUser(t *testing.T) {
	// 归一化空白后按子串判定，避免因换行/缩进让断言变脆。
	sql := strings.Join(strings.Fields(checkpointDiffOwnershipSQL), " ")
	if !strings.Contains(sql, "c.tenant_id = $2") {
		t.Fatalf("归属查询缺少 tenant 条件：%s", sql)
	}
	if !strings.Contains(sql, "COALESCE(c.user_id,'') = $3") {
		t.Fatalf("归属查询缺少 user 条件（可预览他人检查点）：%s", sql)
	}
	if !strings.Contains(sql, "root_path") {
		t.Fatalf("归属查询必须一并取回工作区根：%s", sql)
	}
}

func TestCheckpointDiffRejectsUnauthenticated(t *testing.T) {
	// 没有 claims：必须在碰数据库/引擎之前就 401。
	req := httptest.NewRequest(http.MethodPost, "/v1/checkpoints/cp-1/diff", strings.NewReader("{}"))
	req.SetPathValue("cid", "cp-1")
	rec := httptest.NewRecorder()

	CheckpointDiffHandler(&engine.PythonClient{})(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("未鉴权应当 401，实际 %d：%s", rec.Code, rec.Body.String())
	}
}

func TestCheckpointDiffRejectsEmptyID(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/v1/checkpoints//diff", strings.NewReader("{}"))
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 1}))
	rec := httptest.NewRecorder()

	CheckpointDiffHandler(&engine.PythonClient{})(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("空 checkpoint id 应当 400，实际 %d：%s", rec.Code, rec.Body.String())
	}
}

func TestCheckpointDiffDoesNotForwardWithoutOwnershipLookup(t *testing.T) {
	// 数据库不可用 ⇒ 无法校验归属 ⇒ 必须 503，绝不能"校验不了就放行"转发给引擎。
	req := httptest.NewRequest(http.MethodPost, "/v1/checkpoints/cp-1/diff", strings.NewReader("{}"))
	req.SetPathValue("cid", "cp-1")
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 1}))
	rec := httptest.NewRecorder()

	// 传一个非 nil 的 python client 也不行：DB 为 nil 时应当在转发之前就返回。
	CheckpointDiffHandler(&engine.PythonClient{})(rec, req)

	// 具体码可能是 503（DB manager 为 nil）或 500（池未初始化 / 查询失败）——
	// 两者都表示"归属没校验成功"。断言的是**它没被放行**：任何 2xx 都意味着
	// 请求带着用户的 root 转发到了引擎。
	if rec.Code < 500 {
		t.Fatalf("归属校验失败必须 5xx 且不转发，实际 %d：%s", rec.Code, rec.Body.String())
	}
}

// ── GC 的安全默认 ──

func TestResolveGcDryRunDefaultsToTrue(t *testing.T) {
	if !resolveGcDryRun(nil) {
		t.Fatal("缺省 dry_run 必须是 true —— 一次手滑的 {} 请求不该删掉本机所有缓存")
	}
	no := false
	if resolveGcDryRun(&no) {
		t.Fatal("显式 dry_run=false 必须被尊重")
	}
	yes := true
	if !resolveGcDryRun(&yes) {
		t.Fatal("显式 dry_run=true 必须被尊重")
	}
}

func TestResolveGcRetention(t *testing.T) {
	if got := resolveGcRetention(0); got != checkpointGcDefaultRetentionDays {
		t.Fatalf("0 应当回落到默认 %d 天，实际 %d", checkpointGcDefaultRetentionDays, got)
	}
	if got := resolveGcRetention(-5); got != checkpointGcDefaultRetentionDays {
		t.Fatalf("负数应当回落到默认 %d 天，实际 %d", checkpointGcDefaultRetentionDays, got)
	}
	if got := resolveGcRetention(7); got != 7 {
		t.Fatalf("显式保留期应当保留，实际 %d", got)
	}
	if got := resolveGcRetention(99999); got != 3650 {
		t.Fatalf("超上限应当被夹到 3650，实际 %d", got)
	}
}
