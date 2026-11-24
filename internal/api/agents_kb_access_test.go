package api

import (
	"context"
	"testing"
)

// ── H77：Agent 的知识库绑定必须按调用者授权 ─────────────────────────────────
//
// 修复前：`POST/PATCH /v1/agents` 把 `kb_id` 原样写库，派发时引擎按 ID 查库、用
// **库主**身份检索 —— 于是"知道别人的知识库 UUID"就能把对方私有库正文注进自己的
// 对话。授权被"以资源所有者身份调用"绕过去了。
//
// 这里钉住 Go 侧的三条判据（与引擎 `kb_access_allowed` 同构）：
//  1. 自己的库 → 通过；
//  2. `visibility='public'` → 通过；
//  3. `visibility='tenant'` 且库租户 == 调用者租户 → 通过；
//  4. 其余（别人的私有库 / 跨租户共享 / 不存在的库）→ 拒绝，且**错误文案不区分**
//     "不存在"与"无权"，避免 kb_id 变成存在性探针。

type fakeKBStore struct {
	row   map[string]interface{}
	err   error
	calls int
	lastQ string
	lastA []interface{}
}

func (f *fakeKBStore) FetchOne(_ context.Context, sql string, args ...interface{}) (map[string]interface{}, error) {
	f.calls++
	f.lastQ, f.lastA = sql, args
	if f.err != nil {
		return nil, f.err
	}
	return f.row, nil
}

func kbRow(owner, visibility string, tenant int) map[string]interface{} {
	return map[string]interface{}{
		"owner_id":   owner,
		"visibility": visibility,
		"kb_tenant":  tenant,
	}
}

func TestValidateAgentKBIDAllowsOwnPublicTenantShared(t *testing.T) {
	cases := []struct {
		name string
		row  map[string]interface{}
		user string
		ten  int16
	}{
		{"own", kbRow("u-alice", "private", 1), "u-alice", 1},
		{"public", kbRow("u-bob", "public", 2), "u-alice", 1},
		{"tenant-shared", kbRow("u-bob", "tenant", 1), "u-alice", 1},
	}
	for _, c := range cases {
		store := &fakeKBStore{row: c.row}
		if err := validateAgentKBID(context.Background(), store, "kb-1", c.user, c.ten); err != nil {
			t.Fatalf("%s: 应放行，得到 %v", c.name, err)
		}
	}
}

func TestValidateAgentKBIDRejectsForeignPrivate(t *testing.T) {
	store := &fakeKBStore{row: kbRow("u-bob", "private", 1)}
	err := validateAgentKBID(context.Background(), store, "kb-bob", "u-alice", 1)
	if err == nil {
		t.Fatal("别人的私有库必须拒绝（同租户也不行）")
	}
}

func TestValidateAgentKBIDRejectsCrossTenantShared(t *testing.T) {
	store := &fakeKBStore{row: kbRow("u-bob", "tenant", 2)}
	if err := validateAgentKBID(context.Background(), store, "kb-2", "u-alice", 1); err == nil {
		t.Fatal("跨租户的 tenant 共享库必须拒绝")
	}
}

func TestValidateAgentKBIDRejectsNullTenantRow(t *testing.T) {
	// 与 SQL `tenant_id = $n` 同口径：NULL/0 租户匹配不上任何调用者。
	store := &fakeKBStore{row: kbRow("u-bob", "tenant", 0)}
	if err := validateAgentKBID(context.Background(), store, "kb-3", "u-alice", 1); err == nil {
		t.Fatal("库租户为空时不得按共享放行")
	}
}

func TestValidateAgentKBIDMissingRowIsIndistinguishable(t *testing.T) {
	missing := &fakeKBStore{row: nil}
	foreign := &fakeKBStore{row: kbRow("u-bob", "private", 1)}

	errMissing := validateAgentKBID(context.Background(), missing, "kb-x", "u-alice", 1)
	errForeign := validateAgentKBID(context.Background(), foreign, "kb-x", "u-alice", 1)
	if errMissing == nil || errForeign == nil {
		t.Fatal("两种情形都必须拒绝")
	}
	if errMissing.Error() != errForeign.Error() {
		t.Fatalf("「不存在」与「无权」必须同一句话（避免存在性探针）：\n%q\n%q",
			errMissing.Error(), errForeign.Error())
	}
}

func TestValidateAgentKBIDEmptyIsAllowedWithoutQuery(t *testing.T) {
	store := &fakeKBStore{row: nil}
	if err := validateAgentKBID(context.Background(), store, "  ", "u-alice", 1); err != nil {
		t.Fatalf("空 kb_id 表示不绑库，应直接通过：%v", err)
	}
	if store.calls != 0 {
		t.Fatalf("空 kb_id 不该查库，实际查询 %d 次", store.calls)
	}
}

func TestValidateAgentKBIDFailsClosedWhenStoreUnavailable(t *testing.T) {
	if err := validateAgentKBID(context.Background(), nil, "kb-1", "u-alice", 1); err == nil {
		t.Fatal("存储不可用时不得放行（否则等于回到「随便绑」）")
	}
}

func TestValidateAgentKBIDPropagatesStoreError(t *testing.T) {
	store := &fakeKBStore{err: context.DeadlineExceeded}
	if err := validateAgentKBID(context.Background(), store, "kb-1", "u-alice", 1); err == nil {
		t.Fatal("查询失败必须拒绝，不能当成「通过」")
	}
}

func TestValidateAgentKBIDQueriesByID(t *testing.T) {
	store := &fakeKBStore{row: kbRow("u-alice", "private", 1)}
	_ = validateAgentKBID(context.Background(), store, "kb-abc", "u-alice", 1)
	if store.lastA == nil || len(store.lastA) != 1 || store.lastA[0] != "kb-abc" {
		t.Fatalf("应按 kb_id 查询，实际参数 %v", store.lastA)
	}
}
