package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/id"
	"github.com/athenavi/chiron/internal/market"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// ── 市场安装流水线（写路径）──
//
// 与 `market_handler.go`（只读目录）分开成一个**独立 handler**：源管理分支正在
// 改那个文件的注册逻辑，安装流水线并行落地时只新增本文件 + gateway_router 的一行
// 注册，两边改动不重叠，合并时不需要互相解冲突。
//
// 分层：
//   - 编排（幂等、内外源默认启用状态、外部 MCP 门禁）在 `internal/market/install.go`，
//     不依赖 HTTP / DB / Python，可纯单测；
//   - 落点在**本文件**：四类资源分别写到 agents 表、用户 plugins.json、
//     Python 技能目录、Python 知识库表 —— 这些依赖都在 api 层，注入给编排层。

// 安装链路上的两个上游错误类别，决定 HTTP 状态码：
//
//   - `errInstallNotImplemented` → 501：本部署形态缺这一类落点（例如没有 DB 时不能装
//     agent、没接引擎时不能装 skill/knowledge）。**绝不伪造成功** —— 假装装好了会让
//     用户在"已安装"列表里看到一条什么都不会发生的东西。
//   - `errInstallUpstream` → 502：落点存在但这次调用失败（引擎不可达、写文件失败…）。
var (
	errInstallNotImplemented = errors.New("market install not supported in this deployment")
	errInstallUpstream       = errors.New("market install upstream failed")
)

// installResponse 是 POST /v1/market/install 的响应体（与前端分支的契约一一对应）。
type installResponse struct {
	Installed        bool           `json:"installed"`
	Source           string         `json:"source"`
	Kind             market.Kind    `json:"kind"`
	ID               string         `json:"id"`
	Ref              map[string]any `json:"ref"`
	AlreadyInstalled bool           `json:"already_installed,omitempty"`
	Enabled          bool           `json:"enabled"`
	RequiresApproval bool           `json:"requires_approval"`
}

// MarketInstallHandler 暴露安装/卸载/已安装列表三条路由。
type MarketInstallHandler struct {
	svc *market.Service
	// install 是编排服务（幂等与策略都在里面）。
	install *market.InstallService
}

// NewMarketInstallHandler 构造安装 handler。
//
// `py` 可为 nil（未接引擎）：skill / knowledge 两类随之返回 501，其余照常。
// `plugins` 为 MCP 落点（复用插件页的 plugins.json 读写，不新开一份配置文件）。
// `cfg` 为 nil 时按最保守的默认值走（外部 MCP 禁止）。
func NewMarketInstallHandler(
	svc *market.Service,
	py *engine.PythonClient,
	plugins *PluginHandler,
	cfg *config.Config,
) *MarketInstallHandler {
	store := newInstallStore()
	installers := map[market.Kind]market.Installer{
		market.KindAgent:     agentMarketInstaller{},
		market.KindMCP:       mcpMarketInstaller{plugins: plugins},
		market.KindSkill:     skillMarketInstaller{py: py},
		market.KindKnowledge: knowledgeMarketInstaller{py: py},
	}
	opts := market.InstallOptions{}
	if cfg != nil {
		opts.AllowRemoteMCP = cfg.MarketAllowRemoteMCP
	}
	return &MarketInstallHandler{svc: svc, install: market.NewInstallService(store, installers, opts)}
}

// RegisterRoutes 注册安装流水线的三条路由（auth + rate limited）。
func (h *MarketInstallHandler) RegisterRoutes(mux *http.ServeMux, authMW, rlMW routeMiddleware) {
	mux.Handle("POST /v1/market/install", authMW(rlMW(http.HandlerFunc(h.Install))))
	mux.Handle("DELETE /v1/market/install/{source}/{kind}/{id}", authMW(rlMW(http.HandlerFunc(h.Uninstall))))
	mux.Handle("GET /v1/market/install", authMW(rlMW(http.HandlerFunc(h.List))))
}

// Install POST /v1/market/install
//
// body: { source, kind, id, env?: {KEY: value} }
func (h *MarketInstallHandler) Install(w http.ResponseWriter, r *http.Request) {
	tenantID, userID, ok := installIdentity(w, r)
	if !ok {
		return
	}
	var body struct {
		Source string            `json:"source"`
		Kind   string            `json:"kind"`
		ID     string            `json:"id"`
		Env    map[string]string `json:"env"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	body.Source = strings.TrimSpace(body.Source)
	body.ID = strings.TrimSpace(body.ID)
	if body.Source == "" || body.ID == "" {
		BadRequest(w, "source, kind and id are required")
		return
	}
	kind, err := market.ParseKind(strings.TrimSpace(body.Kind))
	if err != nil {
		BadRequest(w, err.Error())
		return
	}

	// 目录里找不到 → 404。**不能**用请求体自称的 payload 落库：那是调用方给的
	// 内容，等于让任何人往自己的 agents 表里写任意配置。
	item, found := market.Find(h.svc.Load(r.Context()).Items, body.Source, body.ID)
	if !found {
		NotFound(w, "market item not found: "+body.Source+"/"+body.ID)
		return
	}
	if item.Kind != kind {
		BadRequest(w, fmt.Sprintf("kind mismatch: item %q is %q", body.ID, item.Kind))
		return
	}

	res, err := h.install.Install(r.Context(), tenantID, userID, item, body.Env)
	if err != nil {
		writeInstallError(w, err)
		return
	}
	OK(w, installResponse{
		Installed:        true,
		Source:           res.Source,
		Kind:             res.Kind,
		ID:               res.ID,
		Ref:              res.Ref,
		AlreadyInstalled: res.AlreadyInstalled,
		Enabled:          res.Enabled,
		RequiresApproval: res.RequiresApproval,
	})
}

// Uninstall DELETE /v1/market/install/{source}/{kind}/{id}
//
// **幂等**：目录里已找不到该条目、或本机根本没装过，都返回成功。外部源下线后
// 用户仍有权卸载曾经装过的东西 —— 卸载只依赖记录里的 ref，不依赖 payload。
func (h *MarketInstallHandler) Uninstall(w http.ResponseWriter, r *http.Request) {
	tenantID, userID, ok := installIdentity(w, r)
	if !ok {
		return
	}
	source := strings.TrimSpace(r.PathValue("source"))
	id := strings.TrimSpace(r.PathValue("id"))
	kindRaw := strings.TrimSpace(r.PathValue("kind"))
	if source == "" || id == "" {
		BadRequest(w, "source, kind and id are required")
		return
	}
	kind, err := market.ParseKind(kindRaw)
	if err != nil {
		BadRequest(w, err.Error())
		return
	}

	var item *market.Item
	if it, found := market.Find(h.svc.Load(r.Context()).Items, source, id); found {
		// 只在 kind 一致时才把 payload 交给落点：目录里同名不同类型的条目
		// 会把卸载引到错误的落点上。
		if it.Kind == kind {
			item = &it
		}
	}

	if err := h.install.Uninstall(r.Context(), tenantID, userID, source, kind, id, item); err != nil {
		writeInstallError(w, err)
		return
	}
	OK(w, map[string]bool{"uninstalled": true})
}

// List GET /v1/market/install?kind=
func (h *MarketInstallHandler) List(w http.ResponseWriter, r *http.Request) {
	tenantID, userID, ok := installIdentity(w, r)
	if !ok {
		return
	}
	var kind market.Kind
	if raw := strings.TrimSpace(r.URL.Query().Get("kind")); raw != "" {
		parsed, err := market.ParseKind(raw)
		if err != nil {
			BadRequest(w, err.Error())
			return
		}
		kind = parsed
	}
	recs, err := h.install.List(r.Context(), tenantID, userID, kind)
	if err != nil {
		writeInstallError(w, err)
		return
	}
	if recs == nil {
		recs = []market.InstalledRecord{}
	}
	OK(w, map[string]any{"items": recs})
}

// installIdentity 取当前登录身份（字符串形态，与 market.InstallStore 的口径一致）。
func installIdentity(w http.ResponseWriter, r *http.Request) (tenantID, userID string, ok bool) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.UserID == "" {
		Unauthorized(w, ErrAuthRequired)
		return "", "", false
	}
	return tenantStr(claims.TenantID), claims.UserID, true
}

// writeInstallError 把编排/落点错误映射成契约里的状态码。
//
// 映射口径：
//   - 参数类（kind 非法、缺字段、kind 与条目不符）→ 400
//   - 外部 MCP 默认禁止                          → 403（原因写清楚，不是 500）
//   - 本部署缺该 kind 的落点                     → 501
//   - 上游明确拒绝（引擎 4xx）                   → 对应 4xx（如知识库未选 provider 是 400）
//   - 其余（引擎不可达、写库/写文件失败）        → 502
func writeInstallError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, market.ErrBadRequest):
		BadRequest(w, strings.TrimPrefix(err.Error(), market.ErrBadRequest.Error()+": "))
	case errors.Is(err, market.ErrRemoteMCPForbidden):
		Forbidden(w, err.Error())
	case errors.Is(err, market.ErrInstallerUnavailable), errors.Is(err, errInstallNotImplemented):
		NotImplemented(w, err.Error())
	default:
		var up *upstreamError
		if errors.As(err, &up) {
			switch up.status {
			case http.StatusBadRequest, http.StatusUnprocessableEntity:
				BadRequest(w, up.detail)
			case http.StatusNotFound:
				NotFound(w, up.detail)
			case http.StatusForbidden:
				Forbidden(w, up.detail)
			default:
				ServiceUnavailable(w, up.detail)
			}
			return
		}
		slog.Error("market install failed", "error", err)
		// 安装失败 → 502：网关自己没坏，是它依赖的落点没成功。
		JSON(w, http.StatusBadGateway, APIResponse{Success: false, Error: "market install failed: " + err.Error()})
	}
}

// upstreamError 携带上游明确给出的状态码与消息（引擎 4xx 原样透出，其余归 502）。
type upstreamError struct {
	status int
	detail string
}

func (e *upstreamError) Error() string { return e.detail }

// classifyUpstream 把 PythonClient 的错误串（`python POST /path returned 400: {...}`）
// 还原成状态码 + 可读消息。
//
// 为什么要还原：引擎的 400 往往带着**可操作的原因**（"请先在知识库设置中选择 provider"）。
// 一律折成 502 会让用户看到"安装失败"却不知道下一步做什么。
func classifyUpstream(prefix string, err error) error {
	msg := err.Error()
	for _, code := range []int{400, 403, 404, 409, 422} {
		if strings.Contains(msg, " returned "+strconv.Itoa(code)) {
			detail := msg
			if i := strings.Index(msg, ": "); i >= 0 {
				detail = msg[i+2:]
			}
			return &upstreamError{status: code, detail: prefix + ": " + strings.TrimSpace(detail)}
		}
	}
	return fmt.Errorf("%w: %s: %v", errInstallUpstream, prefix, err)
}

// ── 落点存储 ────────────────────────────────────────────────

// newInstallStore 选落点：有 DB 用 PostgreSQL，否则退化为进程内 map。
//
// 桌面版可能没有 DB（单机、免安装形态）。此时退化为进程内 map 并**明确告警**：
// 静默丢弃安装记录会让市场页的「已安装」徽标与卸载入口一起失效，而重启后
// 记录全丢这件事必须能在日志里查到。
func newInstallStore() market.InstallStore {
	if db.Pool != nil {
		return &postgresInstallStore{pool: db.Pool}
	}
	slog.Warn("market install: database pool unavailable, using in-process install records " +
		"(records are lost on restart)")
	return newMemoryInstallStore()
}

// postgresInstallStore 用 pgx 直接读写 market_installations（见迁移 0006）。
type postgresInstallStore struct{ pool *pgxpool.Pool }

// installTenantID 把字符串租户 ID 转成 smallint（表里 tenant_id 是 smallint）。
// 非法值回退 0：与 parseTenantID 的口径一致（0 = 未绑定租户）。
func installTenantID(s string) int16 {
	v, err := strconv.Atoi(s)
	if err != nil || v < 0 || v > 32767 {
		return 0
	}
	return int16(v)
}

func (s *postgresInstallStore) List(ctx context.Context, tenantID, userID string, kind market.Kind) ([]market.InstalledRecord, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT source, kind, market_id, ref, enabled, created_at
		   FROM market_installations
		  WHERE tenant_id = $1 AND user_id = $2 AND ($3 = '' OR kind = $3)
		  ORDER BY created_at DESC`,
		installTenantID(tenantID), userID, string(kind))
	if err != nil {
		return nil, fmt.Errorf("list market installs: %w", err)
	}
	defer rows.Close()

	out := make([]market.InstalledRecord, 0, 8)
	for rows.Next() {
		var rec market.InstalledRecord
		var raw []byte
		var kindStr string
		if err := rows.Scan(&rec.Source, &kindStr, &rec.MarketID, &raw, &rec.Enabled, &rec.CreatedAt); err != nil {
			return nil, fmt.Errorf("scan market install: %w", err)
		}
		rec.Kind, rec.Ref = market.Kind(kindStr), decodeRef(raw)
		out = append(out, rec)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate market installs: %w", err)
	}
	return out, nil
}

func (s *postgresInstallStore) Get(ctx context.Context, tenantID, userID string, rec market.InstalledRecord) (market.InstalledRecord, bool, error) {
	var got market.InstalledRecord
	var raw []byte
	var kindStr string
	err := s.pool.QueryRow(ctx,
		`SELECT source, kind, market_id, ref, enabled, created_at
		   FROM market_installations
		  WHERE tenant_id = $1 AND user_id = $2 AND source = $3 AND kind = $4 AND market_id = $5`,
		installTenantID(tenantID), userID, rec.Source, string(rec.Kind), rec.MarketID).
		Scan(&got.Source, &got.Kind, &got.MarketID, &raw, &got.Enabled, &got.CreatedAt)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return market.InstalledRecord{}, false, nil
		}
		return market.InstalledRecord{}, false, fmt.Errorf("get market install: %w", err)
	}
	got.Kind, got.Ref = market.Kind(kindStr), decodeRef(raw)
	return got, true, nil
}

// Upsert 是幂等的**数据**落点：唯一索引 (tenant, user, source, kind, market_id)
// 让并发重复安装收敛成一行，而不是两行。
func (s *postgresInstallStore) Upsert(ctx context.Context, tenantID, userID string, rec market.InstalledRecord) error {
	newID, err := id.UUID()
	if err != nil {
		return fmt.Errorf("generate install id: %w", err)
	}
	refJSON, err := json.Marshal(nonNilMap(rec.Ref))
	if err != nil {
		return fmt.Errorf("marshal install ref: %w", err)
	}
	_, err = s.pool.Exec(ctx,
		`INSERT INTO market_installations
		     (id, tenant_id, user_id, source, kind, market_id, ref, enabled, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now(), now())
		 ON CONFLICT (tenant_id, user_id, source, kind, market_id)
		 DO UPDATE SET ref = EXCLUDED.ref, enabled = EXCLUDED.enabled, updated_at = now()`,
		newID, installTenantID(tenantID), userID, rec.Source, string(rec.Kind), rec.MarketID,
		string(refJSON), rec.Enabled)
	if err != nil {
		return fmt.Errorf("upsert market install: %w", err)
	}
	return nil
}

func (s *postgresInstallStore) Delete(ctx context.Context, tenantID, userID, source string, kind market.Kind, marketID string) error {
	if _, err := s.pool.Exec(ctx,
		`DELETE FROM market_installations
		  WHERE tenant_id = $1 AND user_id = $2 AND source = $3 AND kind = $4 AND market_id = $5`,
		installTenantID(tenantID), userID, source, string(kind), marketID); err != nil {
		return fmt.Errorf("delete market install: %w", err)
	}
	return nil
}

// decodeRef 把 json 列还原成 map；坏数据按空对象处理（一条坏记录不该让整个列表 500）。
func decodeRef(raw []byte) map[string]any {
	out := map[string]any{}
	if len(raw) == 0 {
		return out
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		slog.Warn("market install: bad ref json, using empty object", "error", err)
		return map[string]any{}
	}
	return out
}

func nonNilMap(m map[string]any) map[string]any {
	if m == nil {
		return map[string]any{}
	}
	return m
}

// memoryInstallStore 是 db.Pool == nil 时的退化实现（桌面版免 DB 形态）。
//
// 键里**必须**带 tenant/user：安装记录是用户级数据，进程内实现同样要按
// (tenant_id, user_id) 隔离，否则同一进程内两个用户会互相看到对方装的东西。
type memoryInstallStore struct {
	mu   sync.Mutex
	rows map[installKey]market.InstalledRecord
}

// installKey 对应 Postgres 侧的唯一约束 (tenant_id, user_id, source, kind, market_id)。
type installKey struct {
	tenantID string
	userID   string
	source   string
	kind     market.Kind
	marketID string
}

func newMemoryInstallStore() *memoryInstallStore {
	return &memoryInstallStore{rows: map[installKey]market.InstalledRecord{}}
}

func memoryInstallKey(tenantID, userID string, source string, kind market.Kind, id string) installKey {
	return installKey{tenantID: tenantID, userID: userID, source: source, kind: kind, marketID: id}
}

func (s *memoryInstallStore) List(_ context.Context, tenantID, userID string, kind market.Kind) ([]market.InstalledRecord, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]market.InstalledRecord, 0, len(s.rows))
	for k, r := range s.rows {
		if k.tenantID != tenantID || k.userID != userID {
			continue
		}
		if kind != "" && r.Kind != kind {
			continue
		}
		out = append(out, r)
	}
	// 与 Postgres 侧口径对齐：新的在前（created_at DESC），同一时刻再按身份字段排序，
	// 让 map 遍历顺序不影响响应（前端与测试都不该吃顺序的亏）。
	sort.Slice(out, func(i, j int) bool {
		if !out[i].CreatedAt.Equal(out[j].CreatedAt) {
			return out[i].CreatedAt.After(out[j].CreatedAt)
		}
		if out[i].Source != out[j].Source {
			return out[i].Source < out[j].Source
		}
		if out[i].Kind != out[j].Kind {
			return out[i].Kind < out[j].Kind
		}
		return out[i].MarketID < out[j].MarketID
	})
	return out, nil
}

func (s *memoryInstallStore) Get(_ context.Context, tenantID, userID string, rec market.InstalledRecord) (market.InstalledRecord, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	got, ok := s.rows[memoryInstallKey(tenantID, userID, rec.Source, rec.Kind, rec.MarketID)]
	return got, ok, nil
}

func (s *memoryInstallStore) Upsert(_ context.Context, tenantID, userID string, rec market.InstalledRecord) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.rows[memoryInstallKey(tenantID, userID, rec.Source, rec.Kind, rec.MarketID)] = rec
	return nil
}

func (s *memoryInstallStore) Delete(_ context.Context, tenantID, userID, source string, kind market.Kind, marketID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.rows, memoryInstallKey(tenantID, userID, source, kind, marketID))
	return nil
}

// ── 落点 1/4：agent → agents 表 ────────────────────────────

// agentMarketInstaller 把市场 agent 条目落成一条 agents 行。
//
// 复用 `agents.go` 的创建口径（json 列类型、id 生成、默认 max_turns/timeout），
// 但**不新增公开路由** —— 安装是市场自己的写路径。
type agentMarketInstaller struct{}

// agentPayload 是市场 agent 条目的 payload 形状（见 internal/market/catalog/agents.json）。
type agentPayload struct {
	SystemPrompt   string          `json:"system_prompt"`
	Description    string          `json:"description"`
	Tools          json.RawMessage `json:"tools"`
	LLMConfig      json.RawMessage `json:"llm_config"`
	MaxTurns       int             `json:"max_turns"`
	TimeoutSeconds int             `json:"timeout_seconds"`
}

func (agentMarketInstaller) Install(ctx context.Context, item market.Item, env map[string]string) (map[string]any, error) {
	claims := auth.GetClaims(ctx)
	if claims == nil || claims.UserID == "" {
		return nil, fmt.Errorf("%w: missing identity", market.ErrBadRequest)
	}
	if db.Pool == nil {
		// 没有 DB 就没有 agents 表：明确 501，而不是假装装好了
		//（否则"已安装"列表里会出现一个永远不存在的 agent）。
		return nil, fmt.Errorf("%w: agents require a database", errInstallNotImplemented)
	}

	var p agentPayload
	if len(item.Payload) > 0 {
		if err := json.Unmarshal(item.Payload, &p); err != nil {
			return nil, fmt.Errorf("%w: invalid agent payload: %v", market.ErrBadRequest, err)
		}
	}
	toolsJSON := jsonOrEmptyArray(p.Tools)
	llmJSON := string(p.LLMConfig)
	if len(p.LLMConfig) == 0 || string(p.LLMConfig) == "null" {
		llmJSON = "{}"
	}
	if p.MaxTurns <= 0 {
		p.MaxTurns = 5
	}
	if p.TimeoutSeconds <= 0 {
		p.TimeoutSeconds = 120
	}
	description := p.Description
	if description == "" {
		description = item.Description
	}

	agentID, err := id.UUID()
	if err != nil {
		return nil, fmt.Errorf("generate agent id: %w", err)
	}
	if _, err := db.GlobalDBManager.Exec(ctx,
		`INSERT INTO agents (id, tenant_id, user_id, name, description, system_prompt, tools, llm_config,
		                     max_turns, timeout_seconds, enabled, kb_id, skills, plugins)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, '', '[]', '[]')`,
		agentID, claims.TenantID, claims.UserID, item.Name, description, p.SystemPrompt,
		toolsJSON, llmJSON, p.MaxTurns, p.TimeoutSeconds, true); err != nil {
		return nil, fmt.Errorf("%w: create agent: %v", errInstallUpstream, err)
	}

	// **不落 env 的值**：market env 里是凭据（如 GITHUB_TOKEN），落库等于把密钥写进
	// agents 配置。只记键名，便于用户回看"这条需要哪些凭据"（值由工具侧配置持有）。
	envKeys := make([]string, 0, len(env))
	for k := range env {
		envKeys = append(envKeys, k)
	}
	return map[string]any{
		"agent_id":      agentID,
		"name":          item.Name,
		"market_id":     item.ID,
		"env_keys":      envKeys,
		"approval_mode": "workbench", // agent 首次运行由引擎侧审批，不受插件闸影响
	}, nil
}

func (agentMarketInstaller) Uninstall(ctx context.Context, item market.Item, ref map[string]any) error {
	claims := auth.GetClaims(ctx)
	if claims == nil || claims.UserID == "" {
		return fmt.Errorf("%w: missing identity", market.ErrBadRequest)
	}
	agentID := refStringOf(ref, "agent_id")
	if agentID == "" || db.Pool == nil {
		// 没有落点引用或没有 DB：没有可删的东西（记录本身由编排层清掉）。
		return nil
	}
	// 归属条件必须带 user_id：只按 id 删等于让拿到别人 agent_id 的人删掉它。
	if _, err := db.GlobalDBManager.Exec(ctx,
		`DELETE FROM agents WHERE id = $1 AND tenant_id = $2 AND user_id = $3`,
		agentID, claims.TenantID, claims.UserID); err != nil {
		return fmt.Errorf("%w: delete agent: %v", errInstallUpstream, err)
	}
	return nil
}

// ── 落点 2/4：mcp → 用户 plugins.json ──────────────────────

// mcpMarketInstaller 把市场 mcp 条目写进用户 plugins.json。
//
// 复用插件页的读写函数（同一份配置文件、同一套命令校验），**Approved 恒为 false**：
// 市场装完的是"配置"，能不能执行由用户级总开关 + 逐条授权决定（fail-closed）。
type mcpMarketInstaller struct{ plugins *PluginHandler }

// mcpPayload 是市场 mcp 条目的 payload 形状（见 internal/market/catalog/mcp.json）。
type mcpPayload struct {
	Command     string   `json:"command"`
	Args        []string `json:"args"`
	Description string   `json:"description"`
	Version     string   `json:"version"`
	ReadOnly    bool     `json:"read_only"`
}

func (m mcpMarketInstaller) Install(ctx context.Context, item market.Item, env map[string]string) (map[string]any, error) {
	claims := auth.GetClaims(ctx)
	if claims == nil || claims.UserID == "" {
		return nil, fmt.Errorf("%w: missing identity", market.ErrBadRequest)
	}
	if m.plugins == nil {
		return nil, fmt.Errorf("%w: plugin store unavailable", errInstallNotImplemented)
	}
	name := strings.TrimSpace(item.Name)
	if name == "" {
		name = item.ID
	}
	if !validPluginName(name) {
		return nil, fmt.Errorf("%w: invalid plugin name %q", market.ErrBadRequest, name)
	}

	var p mcpPayload
	if len(item.Payload) > 0 {
		if err := json.Unmarshal(item.Payload, &p); err != nil {
			return nil, fmt.Errorf("%w: invalid mcp payload: %v", market.ErrBadRequest, err)
		}
	}
	if p.Command == "" {
		return nil, fmt.Errorf("%w: mcp payload missing command", market.ErrBadRequest)
	}
	// 与插件页 Install 同一套校验：命令形态 + 运维可选上界。
	if err := validatePluginCommandShape(p.Command); err != nil {
		return nil, fmt.Errorf("%w: %v", market.ErrBadRequest, err)
	}
	if err := checkOperatorAllowlist(p.Command, m.plugins.cfg); err != nil {
		return nil, fmt.Errorf("%w: %v", market.ErrBadRequest, err)
	}
	if err := checkPluginArgsAllowed(p.Args); err != nil {
		return nil, fmt.Errorf("%w: %v", market.ErrBadRequest, err)
	}

	m.plugins.mu.Lock()
	defer m.plugins.mu.Unlock()

	pf, err := m.plugins.readPluginsFile(claims.UserID)
	if err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("%w: read plugins.json: %v", errInstallUpstream, err)
	}
	for _, existing := range pf.MCPServers {
		if existing.Name == name {
			// 插件页已手工配过同名 server：**不覆盖**（用户手改的配置优先），
			// 也不重复追加 —— 市场这一侧只补一条安装记录。
			slog.Info("market install: plugin already configured, keeping existing entry",
				"user", claims.UserID, "name", name)
			return map[string]any{"name": name, "plugin": "existing", "market_id": item.ID}, nil
		}
	}

	pf.MCPServers = append(pf.MCPServers, MCPPlugin{
		Name:        name,
		Command:     p.Command,
		Args:        p.Args,
		Env:         env,
		Description: firstNonEmpty(p.Description, item.Description),
		Version:     firstNonEmpty(p.Version, item.Version),
		Status:      "active",
		Source:      "market",
		// 市场装的是配置，不是授权：用户必须在插件页逐条确认后才可执行。
		Approved: false,
		ReadOnly: p.ReadOnly,
	})
	if err := m.plugins.savePluginsFile(claims.UserID, pf); err != nil {
		return nil, fmt.Errorf("%w: save plugins.json: %v", errInstallUpstream, err)
	}
	return map[string]any{"name": name, "command": p.Command, "market_id": item.ID}, nil
}

func (m mcpMarketInstaller) Uninstall(ctx context.Context, item market.Item, ref map[string]any) error {
	claims := auth.GetClaims(ctx)
	if claims == nil || claims.UserID == "" {
		return fmt.Errorf("%w: missing identity", market.ErrBadRequest)
	}
	if m.plugins == nil {
		return nil
	}
	name := refStringOf(ref, "name")
	if name == "" {
		name = item.Name
	}
	if name == "" {
		return nil
	}

	m.plugins.mu.Lock()
	defer m.plugins.mu.Unlock()

	pf, err := m.plugins.readPluginsFile(claims.UserID)
	if err != nil {
		if os.IsNotExist(err) {
			// 文件都没有 = 没装过，幂等成功。
			return nil
		}
		return fmt.Errorf("%w: read plugins.json: %v", errInstallUpstream, err)
	}
	kept := make([]MCPPlugin, 0, len(pf.MCPServers))
	for _, p := range pf.MCPServers {
		if p.Name == name {
			continue
		}
		kept = append(kept, p)
	}
	if len(kept) == len(pf.MCPServers) {
		// 不在文件里（用户手工删过）→ 幂等成功。
		return nil
	}
	pf.MCPServers = kept
	if err := m.plugins.savePluginsFile(claims.UserID, pf); err != nil {
		return fmt.Errorf("%w: save plugins.json: %v", errInstallUpstream, err)
	}
	return nil
}

// ── 落点 3/4：skill → 代理 Python ──────────────────────────

// skillMarketInstaller 把市场 skill 条目交给引擎的技能安装接口
// （`POST /v1/skills/install` 的 `inline` 分支，见 python-engine/app/api/skills.py）。
//
// 为什么走 inline 而不是 url：条目内容是**目录里已有的 JSON**（payload），
// 再让引擎出网抓一次既多一跳又要复述 SSRF 策略；inline 直接把这份字节交给它解析。
type skillMarketInstaller struct{ py *engine.PythonClient }

func (s skillMarketInstaller) Install(ctx context.Context, item market.Item, _ map[string]string) (map[string]any, error) {
	claims := auth.GetClaims(ctx)
	if claims == nil || claims.UserID == "" {
		return nil, fmt.Errorf("%w: missing identity", market.ErrBadRequest)
	}
	if s.py == nil {
		return nil, fmt.Errorf("%w: python engine unavailable for skill install", errInstallNotImplemented)
	}
	if len(item.Payload) == 0 {
		return nil, fmt.Errorf("%w: skill payload is empty", market.ErrBadRequest)
	}

	target := withQuery("/v1/skills/install", "scope=private&"+identityQuery(claims))
	var resp struct {
		Skill map[string]any `json:"skill"`
		Scope string         `json:"scope"`
	}
	// P1-2：把**市场来源**写进技能元数据（`market:<source>/<id>`），否则装完之后
	// 技能列表分不清"市场装的 / 本地传的"，升级端点也找不到上游（它只对市场来源生效）。
	origin := "market:" + item.Source + "/" + item.ID
	body := map[string]any{"inline": string(item.Payload), "origin": origin}
	if err := s.py.PostJSON(ctx, target, body, &resp); err != nil {
		return nil, classifyUpstream("install skill", err)
	}
	name := item.Name
	if v, ok := resp.Skill["name"].(string); ok && v != "" {
		name = v
	}
	return map[string]any{
		"name":      name,
		"scope":     firstNonEmpty(resp.Scope, "user"),
		"market_id": item.ID,
		"origin":    origin,
	}, nil
}

func (s skillMarketInstaller) Uninstall(ctx context.Context, item market.Item, ref map[string]any) error {
	claims := auth.GetClaims(ctx)
	if claims == nil || claims.UserID == "" {
		return fmt.Errorf("%w: missing identity", market.ErrBadRequest)
	}
	if s.py == nil {
		// 没有引擎 = 没有技能落点，记录清掉即可（编排层会做）。
		return nil
	}
	name := firstNonEmpty(refStringOf(ref, "name"), item.Name)
	if name == "" {
		return nil
	}
	target := withQuery("/v1/skills/"+url.PathEscape(name), identityQuery(claims))
	if err := s.py.DeleteJSON(ctx, target, nil); err != nil {
		// 引擎侧已经没有这个技能（用户手工删过）→ 幂等成功；
		// 其它失败如实上抛，避免"记录删了技能还在"。
		if isUpstreamNotFound(err) {
			return nil
		}
		return classifyUpstream("uninstall skill", err)
	}
	return nil
}

// ── 落点 4/4：knowledge → 代理 Python ──────────────────────

// knowledgeMarketInstaller 安装一个**文档合集**：建知识库 + 把清单里的文档导进去
// （`POST /v1/kb` 与 `POST /v1/kb/{id}/documents`，见 python-engine/app/api/knowledge.py）。
//
// **只导入条目自己声明的文档**：市场不抓取、不复制安装者没有显式授权的第三方内容。
// 清单为空是合法形态（「空白文档合集」），此时行为与旧模板完全一致 —— 建一个空库。
//
// **不自动建索引**：索引由用户在知识库页显式触发（`POST /v1/kb/{id}/build`）。
// 建索引会消耗 provider 额度且耗时，装一个合集不该在用户没要求时就开始烧。
type knowledgeMarketInstaller struct{ py *engine.PythonClient }

// knowledgePayload 是市场 knowledge 条目的 payload 形状。
type knowledgePayload struct {
	Description string `json:"description"`
	Visibility  string `json:"visibility"`
	// Documents 是合集里的文档清单。省略或为空 = 空合集。
	Documents []knowledgeDocument `json:"documents"`
}

// knowledgeDocument 是合集里的一份文档。
//
// **只有 `url` 一种来源**：知识库 provider 的白名单是 PDF / Office 二进制
// （`app/knowledge/remote.py` 的 `_LOCAL_SUFFIXES` / `_CLOUD_SUFFIXES`），md/txt
// 传上去会在上传阶段被 400 拒绝。所以"内联正文"在这个落点上没有归宿 —— 提供它
// 只会让发布方写下一份注定装不上的条目。
//
// `url` 由引擎侧抓取并直接入库（`/v1/internal/market/kb-import`），不经网关搬运
// 字节：合集文档是几十 MB 的 PDF，走网关要多做一次大字节编解码，还会让"多大算
// 太大"的判断出现第二份。
type knowledgeDocument struct {
	Name string `json:"name"`
	URL  string `json:"url"`
	// FileType 是可选的类型提示；name 自带扩展名时以 name 为准。
	FileType string `json:"file_type"`
}

// maxCollectionDocuments 是单个合集允许声明的文档数上限。
//
// 上限存在的理由不是引擎扛不住，而是**安装是一个同步请求**：抓取 + 上传几百份
// 文档会把 HTTP 请求拖到分钟级，前端只能看到一直转圈。超出上限的合集应当拆成
// 多个条目，或由用户自己上传。
const maxCollectionDocuments = 50

// normalizeCollectionDocuments 校验并规整文档清单（**纯函数，无 I/O**）。
//
// 之所以在建库**之前**做：清单写错是发布方的错误，应该在产生任何副作用之前
// 暴露。否则会留下一个"建好了但只有一半文档"的知识库，用户既看不出问题，
// 卸载重装又会多出一个空库。
func normalizeCollectionDocuments(in []knowledgeDocument) ([]knowledgeDocument, error) {
	if len(in) > maxCollectionDocuments {
		return nil, fmt.Errorf("too many documents in collection (%d > %d)",
			len(in), maxCollectionDocuments)
	}
	out := make([]knowledgeDocument, 0, len(in))
	for i, d := range in {
		name := strings.TrimSpace(d.Name)
		if name == "" {
			return nil, fmt.Errorf("documents[%d]: name is required", i)
		}
		rawURL := strings.TrimSpace(d.URL)
		if rawURL == "" {
			return nil, fmt.Errorf("documents[%d] (%s): url is required", i, name)
		}
		u, err := url.Parse(rawURL)
		if err != nil || u.Scheme != "https" || u.Host == "" {
			// Python 侧 `_require_https` 才是权威；这里提前拦是为了让错误
			// 指向"哪个文档的地址不合法"，而不是一句笼统的抓取失败。
			return nil, fmt.Errorf("documents[%d] (%s): url must be an absolute https URL", i, name)
		}
		ft := strings.ToLower(strings.TrimSpace(d.FileType))
		// name 带扩展名时以它为准（`file_type` 只作 content-type 提示），
		// 因为引擎按 name 的扩展判断 provider 是否支持 —— 两处不一致时
		// 让"看得见的文件名"赢，比让一个提示字段赢更好理解。
		if !hasExtension(name) {
			if ft == "" {
				return nil, fmt.Errorf("documents[%d] (%s): name needs a file extension or file_type", i, name)
			}
			name += "." + ft
		}
		out = append(out, knowledgeDocument{
			Name:     name,
			URL:      rawURL,
			FileType: ft,
		})
	}
	return out, nil
}

// hasExtension 判断文件名是否带扩展名（`.md` / `.pdf` 之类）。
//
// 只认"点后至少一个字符"，且不看点在最前的情形（`.env` 这种 dotfile 按无扩展名处理）：
// 市场合集里出现 dotfile 名字本身就是错的。
func hasExtension(name string) bool {
	i := strings.LastIndexByte(name, '.')
	return i > 0 && i < len(name)-1
}

func (k knowledgeMarketInstaller) Install(ctx context.Context, item market.Item, _ map[string]string) (map[string]any, error) {
	claims := auth.GetClaims(ctx)
	if claims == nil || claims.UserID == "" {
		return nil, fmt.Errorf("%w: missing identity", market.ErrBadRequest)
	}
	if k.py == nil {
		return nil, fmt.Errorf("%w: python engine unavailable for knowledge install", errInstallNotImplemented)
	}

	var p knowledgePayload
	if len(item.Payload) > 0 {
		if err := json.Unmarshal(item.Payload, &p); err != nil {
			return nil, fmt.Errorf("%w: invalid knowledge payload: %v", market.ErrBadRequest, err)
		}
	}
	docs, err := normalizeCollectionDocuments(p.Documents)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", market.ErrBadRequest, err)
	}
	visibility := p.Visibility
	if visibility == "" {
		visibility = "private"
	}
	body := map[string]any{
		"name":        item.Name,
		"description": firstNonEmpty(p.Description, item.Description),
		"visibility":  visibility,
	}
	var resp map[string]any
	if err := k.py.PostJSON(ctx, withQuery("/v1/kb", identityQuery(claims)), body, &resp); err != nil {
		// 引擎的 400 常带可操作原因（"请先在知识库设置中选择 provider"），
		// classifyUpstream 会把它原样透成 400 而不是含糊的 502。
		return nil, classifyUpstream("create knowledge base", err)
	}
	kbID, _ := resp["id"].(string)
	if kbID == "" {
		return nil, fmt.Errorf("%w: engine returned no knowledge base id", errInstallUpstream)
	}

	// 逐份导入。任何一份失败都**回滚刚建的库**：留着半成品库的话，安装记录不会
	// 写入（Installer 返回错误即中止），用户重试就会再建一个 —— 空库越积越多，
	// 而真正的失败原因早被刷过去了。
	for _, doc := range docs {
		if err := k.importDocument(ctx, claims, kbID, doc); err != nil {
			k.rollback(ctx, claims, kbID, item.ID, err)
			return nil, err
		}
	}

	return map[string]any{
		"kb_id":     kbID,
		"name":      item.Name,
		"market_id": item.ID,
		// documents 记录**实际导入**的份数：卸载时无从复查，装上多少个字面上
		// 就是多少个。空合集为 0。
		"documents": len(docs),
	}, nil
}

// kbImportResponse 是引擎 `/v1/internal/market/kb-import` 的响应体。
//
// 该端点恒回 200，失败以 `ok=false` + 稳定原因码表达 —— 所以错误分类在这里
// 做，而不是靠 `classifyUpstream` 解析状态码。
type kbImportResponse struct {
	OK     bool   `json:"ok"`
	Error  string `json:"error"`
	Detail string `json:"detail"`
	Bytes  int    `json:"bytes"`
}

// importDocument 让引擎抓取并直接入库一份合集文档。
//
// **字节不经网关**：走内部端点 `/v1/internal/market/kb-import`，由引擎出网抓取
// （`fetch_url_safe`）后写入对象存储。网关只传 `url` 与知识库身份 —— 否则几十 MB
// 的 PDF 要在两侧各做一遍 base64 编解码，而"多大算太大"也会出现第二份判断。
func (k knowledgeMarketInstaller) importDocument(
	ctx context.Context, claims *auth.Claims, kbID string, doc knowledgeDocument,
) error {
	body := map[string]any{
		"kb_id":     kbID,
		"user_id":   claims.UserID,
		"name":      doc.Name,
		"file_type": doc.FileType,
		"url":       doc.URL,
	}
	var resp kbImportResponse
	if err := k.py.PostJSON(ctx, "/v1/internal/market/kb-import", body, &resp); err != nil {
		// 传输层失败（引擎不可达/超时）不是调用方的问题。
		return fmt.Errorf("%w: import %q: %v", errInstallUpstream, doc.Name, err)
	}
	if resp.OK {
		return nil
	}

	// 原因码分两类，对用户的含义不同：
	//   - `source_*`：发布方给的地址抓不下来（不可达/超时/被 SSRF 策略拦/非 https），
	//     安装者无从修正，归上游故障；
	//   - `kb_*` / `unsupported_document` / `payload_too_large`：入库阶段被拒，
	//     原因往往能指向"这份文档本身有问题"，但仍不是安装者能自己改的。
	// 两类都回 502，差别只体现在 detail 里 —— 让用户看到是哪一份、为什么。
	code := firstNonEmpty(resp.Error, "kb_upload_failed")
	detail := strings.TrimSpace(resp.Detail)
	if detail == "" {
		detail = "engine rejected the document"
	}
	return fmt.Errorf("%w: import %q from %s: %s (%s)",
		errInstallUpstream, doc.Name, doc.URL, code, detail)
}

// rollback 尽力删除刚建的知识库。
//
// 失败只记日志、不改变对外错误：用户需要看到的是**原始失败原因**（哪份文档、
// 为什么），而不是"回滚也失败了"。留下的空库仍可在知识库页手动删掉。
func (k knowledgeMarketInstaller) rollback(ctx context.Context, claims *auth.Claims, kbID, marketID string, cause error) {
	if delErr := k.py.DeleteJSON(ctx,
		withQuery("/v1/kb/"+url.PathEscape(kbID), identityQuery(claims)), nil); delErr != nil {
		slog.Warn("market install: rollback knowledge base failed",
			"kb_id", kbID, "market_id", marketID, "error", delErr, "cause", cause)
		return
	}
	slog.Info("market install: rolled back knowledge base after failed document import",
		"kb_id", kbID, "market_id", marketID)
}

func (k knowledgeMarketInstaller) Uninstall(ctx context.Context, item market.Item, ref map[string]any) error {
	claims := auth.GetClaims(ctx)
	if claims == nil || claims.UserID == "" {
		return fmt.Errorf("%w: missing identity", market.ErrBadRequest)
	}
	if k.py == nil {
		return nil
	}
	kbID := refStringOf(ref, "kb_id")
	if kbID == "" {
		// 没有落点引用时不能瞎删（知识库里有用户上传的文档，误删代价极高）。
		slog.Warn("market uninstall: knowledge record has no kb_id, skipping upstream delete",
			"market_id", item.ID)
		return nil
	}
	if err := k.py.DeleteJSON(ctx, withQuery("/v1/kb/"+url.PathEscape(kbID), identityQuery(claims)), nil); err != nil {
		if isUpstreamNotFound(err) {
			return nil
		}
		return classifyUpstream("delete knowledge base", err)
	}
	return nil
}

// ── 小工具 ──────────────────────────────────────────────────

// identityQuery 构造 `user_id=&tenant_id=`（引擎无鉴权，身份只能由网关注入；
// 引擎侧从 query 读这两个字段，见 python-engine/app/api/skills.py 与 knowledge.py）。
//
// **不带前导 `?`**，调用方一律经 `withQuery` 拼接。
//
// 历史缺陷：这里曾返回 `?user_id=…`，而 skill 安装的路径本身已带 `?scope=private`，
// 于是拼出 `/v1/skills/install?scope=private?user_id=…&tenant_id=…` —— Python 把
// `user_id` 当成了 `scope` 的一部分，只剩 `tenant_id` 一个身份字段，引擎中间件于是
// 回 401 `Incomplete gateway identity: tenant_id and user_id are both required`。
// 市场里"安装技能"因此必然失败，而错误文案指向的是鉴权，离真正的拼接缺陷很远。
func identityQuery(claims *auth.Claims) string {
	return "user_id=" + url.QueryEscape(claims.UserID) +
		"&tenant_id=" + url.QueryEscape(tenantStr(claims.TenantID))
}

// refStringOf 从落点引用里取字符串字段。
func refStringOf(ref map[string]any, key string) string {
	if ref == nil {
		return ""
	}
	if v, ok := ref[key].(string); ok {
		return v
	}
	return ""
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}

// isUpstreamNotFound 判断引擎是否明确回了 404（幂等卸载用）。
func isUpstreamNotFound(err error) bool {
	return err != nil && strings.Contains(err.Error(), " returned 404")
}
