package api

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

// 历史 provider 回填（P0-4）的规则用例。
//
// 要钉住五件事：
//  1. **默认 dry_run**：不传 body / 不传字段时绝不动库（批量改写历史行）；
//  2. **dry_run 返回将影响行数与 ≤10 条示例**；
//  3. **映射只来自模型目录**：同名多厂商、目录缺失、目标键已被占用都要跳过；
//  4. 预览与执行**用同一份计划**（预览说 3 行，执行也只提交这 3 行）；
//  5. 失败（读/写）必须 503，而不是回一个"改了 0 行"的假成功。

// fakeBackfillStore 是 providerBackfillStore 的假 DB：直接喂价格行与目录行，
// 让 planProviderBackfill 的映射规则在**没有 PG**的情况下也能被验证。
type fakeBackfillStore struct {
	prices  []modelProviderRow
	catalog []modelProviderRow

	loadErr  error
	applyErr error

	loadCalls  int
	applyCalls int
	applied    int64
	lastPlan   []providerBackfillSample
}

func (f *fakeBackfillStore) LoadRows(_ context.Context) ([]modelProviderRow, []modelProviderRow, error) {
	f.loadCalls++
	if f.loadErr != nil {
		return nil, nil, f.loadErr
	}
	return f.prices, f.catalog, nil
}

func (f *fakeBackfillStore) ApplyAssignments(_ context.Context, plan []providerBackfillSample) (int64, error) {
	f.applyCalls++
	f.lastPlan = plan
	if f.applyErr != nil {
		return 0, f.applyErr
	}
	if f.applied > 0 {
		return f.applied, nil
	}
	return int64(len(plan)), nil
}

func backfillRequest(t *testing.T, body string) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/admin/billing/backfill-provider", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	return req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "owner-1"}))
}

func runBackfill(t *testing.T, store providerBackfillStore, body string) (int, map[string]any) {
	t.Helper()
	rec := httptest.NewRecorder()
	BackfillModelPriceProviders(store)(rec, backfillRequest(t, body))
	var parsed struct {
		Data map[string]any `json:"data"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &parsed)
	return rec.Code, parsed.Data
}

func TestBackfillProviderDefaultsToDryRun(t *testing.T) {
	store := &fakeBackfillStore{
		prices:  []modelProviderRow{{Model: "deepseek-v4.1-flash"}},
		catalog: []modelProviderRow{{Model: "deepseek-v4.1-flash", Provider: "deepseek"}},
	}

	// 空 body：安全默认 —— 只预览，不写库
	code, data := runBackfill(t, store, "")
	if code != http.StatusOK {
		t.Fatalf("空 body 应按 dry_run 处理并回 200，实际 %d（%v）", code, data)
	}
	if data["dry_run"] != true || data["will_update"] != float64(1) || data["applied"] != float64(0) {
		t.Fatalf("dry_run 响应形状不对：%#v", data)
	}
	if store.applyCalls != 0 {
		t.Fatal("dry_run 时绝不能调用 ApplyAssignments")
	}
	// 规则必须随响应回传：界面不必各写一份说明
	if !strings.Contains(data["rule"].(string), "llm_models") {
		t.Fatalf("响应应说明映射规则：%v", data["rule"])
	}
}

func TestBackfillProviderExplicitDryRunFalseApplies(t *testing.T) {
	store := &fakeBackfillStore{
		prices:  []modelProviderRow{{Model: "m"}},
		catalog: []modelProviderRow{{Model: "m", Provider: "openai"}},
	}

	code, data := runBackfill(t, store, `{"dry_run":false}`)

	if code != http.StatusOK {
		t.Fatalf("应 200，实际 %d", code)
	}
	if data["dry_run"] != false || data["applied"] != float64(1) || data["will_update"] != float64(1) {
		t.Fatalf("实际回填数应回传：%#v", data)
	}
	if store.applyCalls != 1 {
		t.Fatalf("显式 dry_run:false 必须调用 ApplyAssignments 一次，实际 %d", store.applyCalls)
	}
	// 预览的计划与提交的计划必须是同一份
	if len(store.lastPlan) != 1 || store.lastPlan[0] != (providerBackfillSample{Model: "m", Provider: "openai"}) {
		t.Fatalf("提交的计划不对：%#v", store.lastPlan)
	}
}

// fakeCatalogPrices 覆盖四类"不该映射/不该改"的形态 + 两类该映射的形态。
func fakeCatalogPrices() ([]modelProviderRow, []modelProviderRow) {
	prices := []modelProviderRow{
		{Model: "mapped-by-name"},                         // 目录 name 命中
		{Model: "550e8400-e29b-41d4-a716-446655440000"},   // 目录 id 命中
		{Model: "ambiguous"},                              // 目录里两个厂商 ⇒ 不映射
		{Model: "unknown-model"},                          // 目录没有 ⇒ 留空
		{Model: "already-has-target"},                     // (model, 目标 provider) 已存在 ⇒ 冲突跳过
		{Model: "already-has-target", Provider: "openai"}, // 冲突来源行
		{Model: "has-provider", Provider: "anthropic"},    // 本来就有 provider ⇒ 不动
	}
	catalog := []modelProviderRow{
		{Model: "mapped-by-name", Provider: "deepseek"},
		{Model: "550e8400-e29b-41d4-a716-446655440000", Provider: "openai"},
		{Model: "ambiguous", Provider: "openai"},
		{Model: "ambiguous", Provider: "azure"},
		{Model: "already-has-target", Provider: "openai"},
		{Model: "has-provider", Provider: "openai"}, // 即使目录说 openai，也不覆盖已有值
	}
	return prices, catalog
}

func TestPlanProviderBackfillRules(t *testing.T) {
	prices, catalog := fakeCatalogPrices()

	plan := planProviderBackfill(prices, catalog)

	got := make(map[string]string, len(plan))
	for _, item := range plan {
		got[item.Model] = item.Provider
	}
	if len(plan) != 2 {
		t.Fatalf("只该回填 2 行，实际 %#v", plan)
	}
	if got["mapped-by-name"] != "deepseek" {
		t.Fatalf("name 命中的行应映射：%#v", plan)
	}
	if got["550e8400-e29b-41d4-a716-446655440000"] != "openai" {
		t.Fatalf("id 命中的行应映射：%#v", plan)
	}
	// 不该出现：歧义、目录缺失、目标键冲突、已有 provider
	for _, model := range []string{"ambiguous", "unknown-model", "already-has-target", "has-provider"} {
		if _, bad := got[model]; bad {
			t.Fatalf("%s 不该出现在计划里：%#v", model, plan)
		}
	}
}

func TestPlanProviderBackfillIsDeterministic(t *testing.T) {
	prices := []modelProviderRow{{Model: "b"}, {Model: "a"}, {Model: "c"}}
	catalog := []modelProviderRow{
		{Model: "c", Provider: "x"}, {Model: "b", Provider: "y"}, {Model: "a", Provider: "z"},
	}

	plan := planProviderBackfill(prices, catalog)

	if len(plan) != 3 || plan[0].Model != "a" || plan[1].Model != "b" || plan[2].Model != "c" {
		t.Fatalf("计划顺序应稳定按模型名：%#v", plan)
	}
}

func TestCollapseProviderMapRejectsAmbiguity(t *testing.T) {
	got := collapseProviderMap([]modelProviderRow{
		{Model: "solo", Provider: "openai"},
		{Model: "dup-same", Provider: "openai"},
		{Model: "dup-same", Provider: "openai"},
		{Model: "conflict", Provider: "openai"},
		{Model: "conflict", Provider: "azure"},
		{Model: "empty-provider", Provider: "  "},
		{Model: "  ", Provider: "openai"},
	})

	if len(got) != 2 || got["solo"] != "openai" || got["dup-same"] != "openai" {
		t.Fatalf("只该保留唯一厂商的映射：%#v", got)
	}
	if _, bad := got["conflict"]; bad {
		t.Fatalf("歧义模型必须留空：%#v", got)
	}
}

func TestBackfillProviderSampleLimit(t *testing.T) {
	prices := make([]modelProviderRow, 0, 12)
	catalog := make([]modelProviderRow, 0, 12)
	for i := 0; i < 12; i++ {
		model := "model-" + string(rune('a'+i))
		prices = append(prices, modelProviderRow{Model: model})
		catalog = append(catalog, modelProviderRow{Model: model, Provider: "openai"})
	}
	store := &fakeBackfillStore{prices: prices, catalog: catalog}

	code, data := runBackfill(t, store, `{"dry_run":true}`)

	if code != http.StatusOK {
		t.Fatalf("应 200，实际 %d", code)
	}
	if data["will_update"] != float64(12) {
		t.Fatalf("will_update 应是全量 12，实际 %v", data["will_update"])
	}
	samples := data["samples"].([]any)
	if len(samples) != providerBackfillSampleLimit {
		t.Fatalf("示例应截到 %d 条，实际 %d", providerBackfillSampleLimit, len(samples))
	}
}

func TestBackfillProviderRejectsMalformedBody(t *testing.T) {
	// 拼错的布尔值不能静默当成 false —— 那会直接改库。
	store := &fakeBackfillStore{}
	code, _ := runBackfill(t, store, `{"dry_run":"yes"}`)

	if code != http.StatusBadRequest {
		t.Fatalf("非法 body 应 400，实际 %d", code)
	}
	if store.loadCalls != 0 || store.applyCalls != 0 {
		t.Fatal("非法请求不该碰存储")
	}
}

func TestBackfillProviderLoadFailureIs503(t *testing.T) {
	store := &fakeBackfillStore{loadErr: errors.New(`pq: relation "model_prices" does not exist`)}

	code, _ := runBackfill(t, store, `{"dry_run":true}`)

	if code != http.StatusServiceUnavailable {
		t.Fatalf("读失败应 503（不是回 0 行），实际 %d", code)
	}
}

func TestBackfillProviderApplyFailureIs503(t *testing.T) {
	store := &fakeBackfillStore{
		prices:   []modelProviderRow{{Model: "m"}},
		catalog:  []modelProviderRow{{Model: "m", Provider: "openai"}},
		applyErr: errors.New("deadlock detected"),
	}

	code, data := runBackfill(t, store, `{"dry_run":false}`)

	if code != http.StatusServiceUnavailable {
		t.Fatalf("回填失败应 503，实际 %d（%v）", code, data)
	}
	if data["applied"] != nil {
		t.Fatalf("失败时不该回成功体：%#v", data)
	}
}

func TestBackfillProviderRequiresAuth(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/admin/billing/backfill-provider",
		strings.NewReader(`{"dry_run":true}`))
	BackfillModelPriceProviders(&fakeBackfillStore{})(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("未认证应 401，实际 %d", rec.Code)
	}
}
