package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/athenavi/chiron/internal/market"
)

// newTestMarketHandler 构造一个只吃内置目录的 handler。
func newTestMarketHandler() *MarketHandler {
	return NewMarketHandler(market.NewService(nil))
}

// decodeOK 读出标准成功响应里的 data 段。
func decodeOK(t *testing.T, rec *httptest.ResponseRecorder, out any) {
	t.Helper()
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", rec.Code, rec.Body.String())
	}
	var resp struct {
		Success bool            `json:"success"`
		Data    json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("解析响应失败: %v; body=%s", err, rec.Body.String())
	}
	if !resp.Success {
		t.Fatalf("success = false; body=%s", rec.Body.String())
	}
	if err := json.Unmarshal(resp.Data, out); err != nil {
		t.Fatalf("解析 data 失败: %v", err)
	}
}

func TestMarketCatalogReturnsBuiltinItems(t *testing.T) {
	h := newTestMarketHandler()
	rec := httptest.NewRecorder()
	h.Catalog(rec, httptest.NewRequest(http.MethodGet, "/v1/market/catalog", nil))

	var got catalogResponse
	decodeOK(t, rec, &got)

	if got.Total == 0 || len(got.Items) == 0 {
		t.Fatal("目录为空：内置条目必须始终可用")
	}
	if len(got.Sources) == 0 || got.Sources[0].ID != market.SourceBuiltin {
		t.Fatalf("sources = %+v, want builtin 在首位", got.Sources)
	}
	if got.Sources[0].Count != got.Total {
		t.Errorf("builtin count = %d, total = %d（无外部源时应相等）", got.Sources[0].Count, got.Total)
	}
}

// kind 非法必须 400，而不是返回空列表 —— 空列表会让前端把拼错的类型
// 显示成"这个分类没有内容"。
func TestMarketCatalogRejectsUnknownKind(t *testing.T) {
	h := newTestMarketHandler()
	rec := httptest.NewRecorder()
	h.Catalog(rec, httptest.NewRequest(http.MethodGet, "/v1/market/catalog?kind=bogus", nil))

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400; body=%s", rec.Code, rec.Body.String())
	}
}

func TestMarketCatalogFiltersByKind(t *testing.T) {
	h := newTestMarketHandler()
	rec := httptest.NewRecorder()
	h.Catalog(rec, httptest.NewRequest(http.MethodGet, "/v1/market/catalog?kind=skill", nil))

	var got catalogResponse
	decodeOK(t, rec, &got)

	if len(got.Items) == 0 {
		t.Fatal("skill 分类为空")
	}
	for _, it := range got.Items {
		if it.Kind != market.KindSkill {
			t.Errorf("item %q kind = %q, want skill", it.ID, it.Kind)
		}
	}
}

// 分页边界：超出末页时返回空 items，但 total 仍是筛选后的总数
// （前端分页器据 total 判断"共几页"，不能用当前页长度代替）。
func TestMarketCatalogPagination(t *testing.T) {
	h := newTestMarketHandler()

	rec := httptest.NewRecorder()
	h.Catalog(rec, httptest.NewRequest(http.MethodGet, "/v1/market/catalog?page_size=1", nil))
	var first catalogResponse
	decodeOK(t, rec, &first)
	if len(first.Items) != 1 {
		t.Fatalf("page_size=1 应返回 1 条，得到 %d", len(first.Items))
	}
	if first.Total < 2 {
		t.Skip("内置条目不足 2 条，无法验证分页")
	}

	rec = httptest.NewRecorder()
	h.Catalog(rec, httptest.NewRequest(http.MethodGet, "/v1/market/catalog?page_size=1&page=9999", nil))
	var beyond catalogResponse
	decodeOK(t, rec, &beyond)
	if len(beyond.Items) != 0 {
		t.Errorf("超出末页应返回空列表，得到 %d 条", len(beyond.Items))
	}
	if beyond.Total != first.Total {
		t.Errorf("total = %d, want %d（total 与页无关）", beyond.Total, first.Total)
	}
}

// page_size 有上限，否则一次请求可以把整个目录（含外部源）拉下来。
func TestMarketCatalogCapsPageSize(t *testing.T) {
	h := newTestMarketHandler()
	rec := httptest.NewRecorder()
	h.Catalog(rec, httptest.NewRequest(http.MethodGet, "/v1/market/catalog?page_size=100000", nil))

	var got catalogResponse
	decodeOK(t, rec, &got)
	if got.PageSize > 200 {
		t.Errorf("page_size = %d, want <= 200", got.PageSize)
	}
}

// source 是必填的：同名条目在不同源是不同东西，服务端不能替调用方猜。
func TestMarketItemRequiresSource(t *testing.T) {
	h := newTestMarketHandler()
	req := httptest.NewRequest(http.MethodGet, "/v1/market/items//skill:brainstorming", nil)
	req.SetPathValue("source", "")
	req.SetPathValue("id", "skill:brainstorming")

	rec := httptest.NewRecorder()
	h.Item(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

func TestMarketItemFound(t *testing.T) {
	h := newTestMarketHandler()
	items := market.BuiltinItems()
	if len(items) == 0 {
		t.Skip("内置目录为空")
	}

	req := httptest.NewRequest(http.MethodGet, "/v1/market/items/builtin/"+items[0].ID, nil)
	req.SetPathValue("source", market.SourceBuiltin)
	req.SetPathValue("id", items[0].ID)

	rec := httptest.NewRecorder()
	h.Item(rec, req)

	var got struct {
		Item     market.Item     `json:"item"`
		Warnings []market.Warning `json:"warnings"`
	}
	decodeOK(t, rec, &got)
	if got.Item.ID != items[0].ID {
		t.Errorf("item id = %q, want %q", got.Item.ID, items[0].ID)
	}
}

func TestMarketItemNotFound(t *testing.T) {
	h := newTestMarketHandler()
	req := httptest.NewRequest(http.MethodGet, "/v1/market/items/builtin/nope", nil)
	req.SetPathValue("source", market.SourceBuiltin)
	req.SetPathValue("id", "nope")

	rec := httptest.NewRecorder()
	h.Item(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404; body=%s", rec.Code, rec.Body.String())
	}
}

// 外部源失败时，列表仍 200，问题只出现在 warnings 里 —— 单个源不可达
// 不能让整个市场页变成错误页。
func TestMarketCatalogSurvivesBrokenExternalSource(t *testing.T) {
	svc := market.NewService(nil) // fetcher 为空 = 外部抓取未启用
	svc.SetExternals([]market.Source{{
		ID: "team", Name: "team", Type: market.SourceTypeOperator, Enabled: true,
	}})
	h := NewMarketHandler(svc)

	rec := httptest.NewRecorder()
	h.Catalog(rec, httptest.NewRequest(http.MethodGet, "/v1/market/catalog", nil))

	var got catalogResponse
	decodeOK(t, rec, &got)
	if len(got.Items) == 0 {
		t.Error("内置条目必须仍然返回")
	}
	if len(got.Warnings) == 0 || got.Warnings[0].Reason != "remote_disabled" {
		t.Errorf("warnings = %+v, want remote_disabled", got.Warnings)
	}
}
