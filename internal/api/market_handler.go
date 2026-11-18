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
	// sources 是源管理的 HTTP 适配层（见 market_sources.go）。共用同一个 svc：
	// 源管理改的是配置，目录读取读的是同一份配置，两处必须看到同一个实例。
	sources *MarketSourceHandler
}

// NewMarketHandler 构造市场 handler。svc 不可为 nil —— 内置目录随包发布，
// 不依赖任何外部服务，没有"市场功能不可用"的合法状态。
func NewMarketHandler(svc *market.Service) *MarketHandler {
	return &MarketHandler{svc: svc, sources: NewMarketSourceHandler(svc)}
}

// RegisterRoutes 注册市场路由。
//
// 分两段：上面是**只读目录**（所有人可用），下面是**源管理**（写操作，语义独立，
// 见 market_sources.go）。分段是为了并行合并时能整段识别/挪动，不必逐行 diff。
func (h *MarketHandler) RegisterRoutes(mux *http.ServeMux, authMW, rlMW routeMiddleware) {
	mux.Handle("GET /v1/market/catalog", authMW(rlMW(http.HandlerFunc(h.Catalog))))
	mux.Handle("GET /v1/market/items/{source}/{id}", authMW(rlMW(http.HandlerFunc(h.Item))))

	// ── 源管理路由（外部源的后台配置 + 用户自建源）──
	// GET    列出全部源（内置/运维/用户）与能力上限；
	// POST   用户新增自建源（allow_user_sources=false → 403，上限 → 409，URL 非法 → 400）；
	// DELETE 删除**用户**源（内置/运维 → 403），幂等。
	mux.Handle("GET /v1/market/sources", authMW(rlMW(http.HandlerFunc(h.sources.List))))
	mux.Handle("POST /v1/market/sources", authMW(rlMW(http.HandlerFunc(h.sources.Create))))
	mux.Handle("DELETE /v1/market/sources/{id}", authMW(rlMW(http.HandlerFunc(h.sources.Delete))))
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
//
// 按**当前用户**加载（LoadFor）：用户自建源是 per-user 的，用 Load(ctx) 会让
// A 用户看到 B 加过的源。
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

	snap := h.svc.LoadFor(r.Context(), marketUserID(r))
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

	snap := h.svc.LoadFor(r.Context(), marketUserID(r))
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
