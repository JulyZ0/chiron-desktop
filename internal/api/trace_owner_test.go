package api

import (
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

// ── H27：trace 的按用户收窄（fail-closed）────────────────────────────────
//
// 修复前的行为：trace stream 的键是 `chiron:traces:{tenant}`，同租户内所有用户的
// span 混在一条流里，而 GetTrace 只校验 tenant ⇒ 猜到 trace_id 就能读到别人的
// 完整调用链。这里钉住三条：
//  1. 有归属的 span 只对本人可见；
//  2. 无归属的 span（升级前老记录）只对 `admin:read` 可见 —— 不是"人人可见"；
//  3. 一条都看不见时必须回 404（由调用方按 len==0 处理），不能退化成"返回空视图"。

func traceTestClaims(userID string, perms ...string) *auth.Claims {
	return &auth.Claims{
		UserID:   userID,
		TenantID: 1,
		Perms:    perms,
	}
}

func TestTraceSpanOwnerVisibleOnlyToOwner(t *testing.T) {
	claims := traceTestClaims("u-alice")
	if !traceOwnerVisible("u-alice", claims) {
		t.Fatal("本人 span 必须可见")
	}
	if traceOwnerVisible("u-bob", claims) {
		t.Fatal("同租户其他用户的 span 不得可见（H27）")
	}
}

func TestTraceSpanWithoutOwnerRequiresAdminRead(t *testing.T) {
	plain := traceTestClaims("u-alice")
	if traceOwnerVisible("", plain) {
		t.Fatal("无归属 span 对普通用户必须 fail-closed")
	}
	admin := traceTestClaims("u-admin", auth.PermAdminRead)
	if !traceOwnerVisible("", admin) {
		t.Fatal("无归属 span 对具备 admin:read 的调用者可见（审计用途）")
	}
	if !traceOwnerVisible("u-bob", admin) {
		t.Fatal("管理员需要能看整条链路（跨用户排查）")
	}
}

func TestFilterTraceSpansDropsForeignSpans(t *testing.T) {
	spans := []TraceSpan{
		{TraceID: "t1", SpanName: "run", UserID: "u-alice"},
		{TraceID: "t1", SpanName: "turn", UserID: "u-bob"},
		{TraceID: "t1", SpanName: "legacy"}, // 升级前的老记录：无归属
	}
	got := filterTraceSpansForCaller(spans, traceTestClaims("u-alice"))
	if len(got) != 1 || got[0].SpanName != "run" {
		t.Fatalf("普通用户只应看到自己的 span，得到 %+v", got)
	}

	got = filterTraceSpansForCaller(spans, traceTestClaims("u-admin", auth.PermAdminRead))
	if len(got) != 3 {
		t.Fatalf("管理员应看到全部 span，得到 %d 条", len(got))
	}

	if got := filterTraceSpansForCaller(spans, nil); got != nil {
		t.Fatal("无 claims 时必须返回 nil（调用方据此 404）")
	}
}

func TestTraceSpanOwnerPrefersStreamField(t *testing.T) {
	values := map[string]interface{}{"user_id": "u-stream"}
	meta := map[string]any{"user_id": "u-meta"}
	if got := traceSpanOwner(values, meta); got != "u-stream" {
		t.Fatalf("stream 字段优先，得到 %q", got)
	}
	if got := traceSpanOwner(map[string]interface{}{}, meta); got != "u-meta" {
		t.Fatalf("stream 缺字段时回落到 metadata，得到 %q", got)
	}
	if got := traceSpanOwner(map[string]interface{}{"user_id": "  "}, nil); got != "" {
		t.Fatalf("空白身份视为无法归属，得到 %q", got)
	}
}
