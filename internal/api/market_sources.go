package api

import (
	"errors"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/market"
)

// MarketSourceHandler 是「源管理」的 HTTP 适配层（GET/POST/DELETE /v1/market/sources）。
//
// 与 MarketHandler 分开的理由：读目录（Catalog/Item）是**所有人**都用的高频路径，
// 源管理是设置页里的低频写路径，且它有一套完全不同的错误语义（403 默认关闭、
// 409 上限、400 URL 形状）。混在一个文件里会让两种关注点互相干扰。
type MarketSourceHandler struct {
	svc *market.Service
}

// NewMarketSourceHandler 构造源管理 handler。
func NewMarketSourceHandler(svc *market.Service) *MarketSourceHandler {
	return &MarketSourceHandler{svc: svc}
}

// marketUserID 从请求认证信息取当前用户 ID。
//
// 与 plugin_handler.go 的 resolveUser 同语义：没有 claims 时返回空串，
// 由调用方决定是"匿名可读"（列源）还是"必须登录"（加源/删源）。
// 这里**不**直接 401：authMW 已保证登录，空 claims 只出现在单测/内部调用里。
func marketUserID(r *http.Request) string {
	if claims := auth.GetClaims(r.Context()); claims != nil {
		return claims.UserID
	}
	return ""
}

// sourceLimits 是 GET /v1/market/sources 里的能力与上限说明。
//
// 必须一起返回：前端据它决定"要不要显示添加按钮""显示的上限是多少"。
// 只返回 sources 会让页面自己猜策略，猜错就只能等 403/409 才知道。
type sourceLimits struct {
	RemoteEnabled    bool `json:"remote_enabled"`
	AllowUserSources bool `json:"allow_user_sources"`
	AllowRemoteMCP   bool `json:"allow_remote_mcp"`
	MaxSources       int  `json:"max_sources"`
}

// sourcesResponse 是源列表的响应体。
type sourcesResponse struct {
	// Sources 含 builtin / operator / user 三类；未启用（remote_enabled=false）的
	// 外部源仍会出现，但 Enabled=false —— 管理页要能显示"配了但没启用"。
	Sources []market.Source `json:"sources"`
	Limits  sourceLimits    `json:"limits"`
}

// List GET /v1/market/sources
func (h *MarketSourceHandler) List(w http.ResponseWriter, r *http.Request) {
	userID := marketUserID(r)
	cfg := h.svc.Config()
	OK(w, sourcesResponse{
		Sources: h.svc.ResolveSources(userID),
		Limits: sourceLimits{
			RemoteEnabled:    cfg.RemoteEnabled,
			AllowUserSources: cfg.AllowUserSources,
			AllowRemoteMCP:   cfg.AllowRemoteMCP,
			MaxSources:       cfg.MaxSourcesNormalized(),
		},
	})
}

// Create POST /v1/market/sources
//
// 三条拒绝路径的语义必须分清（前端按状态码给不同文案）：
//   - 403 allow_user_sources=false：**默认就是关的**。用户级源把 SSRF 面变成
//     自助入口，不能"点一下就开"，要部署方在后台显式放行；
//   - 409 源总数已达上限（硬上限 20，不可调大）：这不是用户操作错误，
//     得先删掉一个才能加；
//   - 400 URL 形状不合法（非 https / 带凭据 / 带 fragment 之外的畸形）。
func (h *MarketSourceHandler) Create(w http.ResponseWriter, r *http.Request) {
	userID := marketUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	var body struct {
		URL  string `json:"url"`
		Name string `json:"name"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	src, err := h.svc.AddUserSource(r.Context(), userID, body.URL, body.Name)
	if err != nil {
		writeSourceError(w, err)
		return
	}
	Created(w, src)
}

// Delete DELETE /v1/market/sources/{id}
//
// 只能删 user 类型：内置源随包发布、运维源是部署方白名单。允许从这里删掉它们
// 等于把"改运维配置"的能力给了任何登录用户。
// 幂等：删不存在的 id 返回 200（前端重复点击/并发刷新不该报错）。
func (h *MarketSourceHandler) Delete(w http.ResponseWriter, r *http.Request) {
	userID := marketUserID(r)
	if userID == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	id := strings.TrimSpace(r.PathValue("id"))
	if id == "" {
		BadRequest(w, "source id is required")
		return
	}
	if err := h.svc.RemoveUserSource(r.Context(), userID, id); err != nil {
		writeSourceError(w, err)
		return
	}
	OK(w, map[string]interface{}{"deleted": id})
}

// writeSourceError 把源管理的领域错误映射成 HTTP 状态码。
//
// 集中映射的理由：三个入口（列表外的写操作）必须给出一致的语义，
// 散落 switch 迟早出现"POST 是 409、DELETE 是 500"这种不一致。
func writeSourceError(w http.ResponseWriter, err error) {
	var srcErr *market.SourceError
	switch {
	case errors.Is(err, market.ErrUserSourcesDisabled):
		Forbidden(w, "user sources are disabled by the administrator")
	case errors.Is(err, market.ErrTooManySources):
		JSONWithCode(w, http.StatusConflict, CodeQuotaExceeded,
			map[string]interface{}{"limit": market.DefaultMaxSources},
			"market source limit reached")
	case errors.Is(err, market.ErrSourceNotRemovable):
		Forbidden(w, "only user sources can be removed")
	case errors.Is(err, market.ErrSourceStoreUnavailable):
		InternalError(w, "market source store unavailable")
	case errors.As(err, &srcErr):
		// URL 形状问题：回稳定原因码，前端据此本地化（不要把 Go 的原串透给用户）。
		JSONWithCode(w, http.StatusBadRequest, CodeInvalidRequest,
			map[string]interface{}{"reason": srcErr.Code}, srcErr.Detail())
	default:
		InternalError(w, "market source operation failed")
	}
}
