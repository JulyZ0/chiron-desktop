package api

import (
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/market"
)

// MarketHandler 暴露资源市场（agents / skills / knowledge / mcp 四类）。
//
// 分层：本文件只做 HTTP 适配（参数解析、分页、错误映射），目录聚合与筛选在
// `internal/market`。这样聚合逻辑可以脱离 HTTP 单测，外部源抓取也能单独替换。
type MarketHandler struct {
	svc *market.Service
}

// NewMarketHandler 构造市场 handler。svc 不可为 nil —— 内置目录随包发布，
// 不依赖任何外部服务，没有"市场功能不可用"的合法状态。
func NewMarketHandler(svc *market.Service) *MarketHandler {
	return &MarketHandler{svc: svc}
}

// RegisterRoutes 注册市场只读路由。写操作（安装/卸载/源管理）在后续阶段注册。
func (h *MarketHandler) RegisterRoutes(mux *http.ServeMux, authMW, rlMW routeMiddleware) {
	mux.Handle("GET /v1/market/catalog", authMW(rlMW(http.HandlerFunc(h.Catalog))))
	mux.Handle("GET /v1/market/items/{source}/{id}", authMW(rlMW(http.HandlerFunc(h.Item))))
}

// catalogResponse 是目录列表的响应体。
//
// 四段缺一不可：
//   - items   —— 当前页条目
//   - sources —— **全部**来源（含本轮失败/被关闭的），页面据此渲染 Tab 与告警
//   - warnings—— 非致命问题；单源失败不能让整个列表 500
//   - total   —— 筛选后的总数，分页器用
type catalogResponse struct {
	Items    []market.Item    `json:"items"`
	Total    int              `json:"total"`
	Page     int              `json:"page"`
	PageSize int              `json:"page_size"`
	Sources  []market.Source  `json:"sources"`
	Warnings []market.Warning `json:"warnings,omitempty"`
}

// Catalog GET /v1/market/catalog
//
// 查询参数：kind / q / tag / source / page / page_size。
// kind 非法时返回 400 而不是静默返回空列表 —— 空列表会让前端把拼错的类型
// 显示成"这个分类没有内容"。
func (h *MarketHandler) Catalog(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()

	var kind market.Kind
	if raw := strings.TrimSpace(q.Get("kind")); raw != "" {
		parsed, err := market.ParseKind(raw)
		if err != nil {
			BadRequest(w, err.Error())
			return
		}
		kind = parsed
	}

	snap := h.svc.Load(r.Context())
	filtered := market.Filter(snap.Items, market.Query{
		Kind:   kind,
		Q:      q.Get("q"),
		Tag:    q.Get("tag"),
		Source: strings.TrimSpace(q.Get("source")),
	})

	page, pageSize := parsePagination(q)
	start := (page - 1) * pageSize
	items := []market.Item{}
	if start < len(filtered) {
		end := start + pageSize
		if end > len(filtered) {
			end = len(filtered)
		}
		items = filtered[start:end]
	}

	OK(w, catalogResponse{
		Items:    items,
		Total:    len(filtered),
		Page:     page,
		PageSize: pageSize,
		Sources:  snap.Sources,
		Warnings: snap.Warnings,
	})
}

// Item GET /v1/market/items/{source}/{id}
//
// `source` 必填。内置条目用固定值 `builtin` —— 让调用方显式说出"我要哪个源的
// 这条"，而不是让服务端猜。同名条目在不同源是不同东西，安装时必须能区分。
func (h *MarketHandler) Item(w http.ResponseWriter, r *http.Request) {
	source := strings.TrimSpace(r.PathValue("source"))
	id := r.PathValue("id")
	if source == "" || id == "" {
		BadRequest(w, "source and id are required")
		return
	}

	snap := h.svc.Load(r.Context())
	it, ok := market.Find(snap.Items, source, id)
	if !ok {
		NotFound(w, "market item not found: "+source+"/"+id)
		return
	}
	OK(w, map[string]interface{}{
		"item":     it,
		"warnings": snap.Warnings,
	})
}
