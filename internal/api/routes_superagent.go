package api

// ── Super-agent 接管：配置 CRUD（网关侧）+ 运行时端点代理 ──
//
// ## 为什么配置在这里而不在引擎
//
// 配置落在 PG（`superagent_takeovers` / `superagent_settings`），而前端的每个配置
// 请求都带 JWT。让网关处理，租户/用户归属直接来自已验证的 claims，不需要把身份
// 再透传给引擎、也不需要在引擎侧重建一套鉴权。引擎只管**只有它才知道的运行时事实**
// （presence、接管段、打断），见 `python-engine/app/api/superagent.py`。
//
// ## 两层校验为什么分开
//
// 保存期校验分两类，处置完全不同：
//
//   - **非法**（422）：数量超限、间隔太短、once 超出 24 小时、预算非正。这些是
//     "这条配置根本不成立"，存进去只会让巡检循环每一拍都白跑。
//   - **可疑**（200 + warnings[]）：yolo 会被降级、多个 once 撞在同一时刻、各会话
//     预算之和超过日预算。这些是"用户可能是这么想的"，拦住他不合理 ——
//     只提示，让他自己决定。
//
// 混成一类的后果：要么用户被一堆他确实想要的配置反复弹窗拦住，要么真错误被
// 提示淹没。所以 `validateTakeover` 只返回 []string（硬错误），
// `takeoverWarnings` 单独返回软提示。

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/id"
	"github.com/jackc/pgx/v5/pgconn"
)

// 与 `python-engine/app/agent/takeover.py` 的常量必须保持一致。
// 改一处不改另一处会导致"网关放行、引擎拒绝"，而这种不一致只会在运行时暴露。
const (
	superagentMaxSessions    = 5
	superagentMinInterval    = 15 * 60 // 秒
	superagentMaxOnceHorizon = 24 * time.Hour
	superagentKindOnce       = "once"
	superagentKindInterval   = "interval"
	superagentUnsafeMode     = "yolo"
	superagentSafeMode       = "auto"
)

// SuperagentTakeover 是一条接管配置。
//
// `session_id` 在更新时**不允许修改**：换会话等于换一条策略（定时、预算、授权档位
// 都该重设），而"原地改 session_id"会让历史审计指向一个从未被它接管过的会话。
type SuperagentTakeover struct {
	ID                  string     `json:"id"`
	SessionID           string     `json:"session_id"`
	Enabled             bool       `json:"enabled"`
	ScheduleKind        string     `json:"schedule_kind"`
	OnceAt              *time.Time `json:"once_at,omitempty"`
	IntervalSeconds     int        `json:"interval_seconds"`
	ToolsMode           string     `json:"tools_mode"`
	DailyTokenBudget    int        `json:"daily_token_budget"`
	TokensUsedToday     int        `json:"tokens_used_today"`
	BudgetDay           string     `json:"budget_day,omitempty"`
	LastRunAt           *time.Time `json:"last_run_at,omitempty"`
	LastVerdict         string     `json:"last_verdict,omitempty"`
	LastReason          string     `json:"last_reason,omitempty"`
	MeltState           string     `json:"melt_state"`
	MeltReason          string     `json:"melt_reason,omitempty"`
	CooldownUntil       *time.Time `json:"cooldown_until,omitempty"`
	ConsecutiveFailures int        `json:"consecutive_failures"`
	CreatedAt           *time.Time `json:"created_at,omitempty"`
	UpdatedAt           *time.Time `json:"updated_at,omitempty"`
}

// SuperagentSettings 是用户级的接管总开关。
type SuperagentSettings struct {
	Enabled        bool       `json:"enabled"`
	MaxSessions    int        `json:"max_sessions"`
	DayTokenBudget int        `json:"day_token_budget"`
	IdleMinutes    int        `json:"idle_minutes"`
	UpdatedAt      *time.Time `json:"updated_at,omitempty"`
}

// SuperagentHandler 处理配置 CRUD，并把运行时端点转给引擎。
type SuperagentHandler struct {
	pythonClient *engine.PythonClient
}

func NewSuperagentHandler(pc *engine.PythonClient) *SuperagentHandler {
	return &SuperagentHandler{pythonClient: pc}
}

// 列顺序必须与 `scanTakeover` 一致。抽成常量是因为两处漂移不会有编译错误，
// 只会在 Scan 时静默错位（列类型相近时甚至不报错）。
const superagentTakeoverColumns = `
       id::text, session_id, enabled, schedule_kind, once_at, interval_seconds,
       tools_mode, daily_token_budget, tokens_used_today,
       COALESCE(to_char(budget_day, 'YYYY-MM-DD'), ''),
       last_run_at, COALESCE(last_verdict, ''), COALESCE(last_reason, ''),
       melt_state, COALESCE(melt_reason, ''), cooldown_until,
       consecutive_failures, created_at, updated_at`

const superagentListQuery = `SELECT ` + superagentTakeoverColumns + `
  FROM superagent_takeovers
 WHERE tenant_id = $1 AND user_id = $2
 ORDER BY created_at DESC`

const superagentOneQuery = `SELECT ` + superagentTakeoverColumns + `
  FROM superagent_takeovers
 WHERE tenant_id = $1 AND user_id = $2 AND id::text = $3`

func scanTakeover(sc interface{ Scan(...any) error }) (SuperagentTakeover, error) {
	var t SuperagentTakeover
	err := sc.Scan(
		&t.ID, &t.SessionID, &t.Enabled, &t.ScheduleKind, &t.OnceAt, &t.IntervalSeconds,
		&t.ToolsMode, &t.DailyTokenBudget, &t.TokensUsedToday, &t.BudgetDay,
		&t.LastRunAt, &t.LastVerdict, &t.LastReason,
		&t.MeltState, &t.MeltReason, &t.CooldownUntil,
		&t.ConsecutiveFailures, &t.CreatedAt, &t.UpdatedAt,
	)
	return t, err
}

// ── 校验（纯函数，便于单测）────────────────────────────────────────

// validateTakeover 返回**硬错误**（422）。空切片表示可以保存。
//
// now 由调用方注入而不是内部取：单测要对固定时刻断言"once 是否超出 24 小时窗口"，
// 内部调 time.Now() 会让这类用例只能在某些时刻通过。
func validateTakeover(t SuperagentTakeover, now time.Time) []string {
	var errs []string

	if trimSpace(t.SessionID) == "" {
		errs = append(errs, "session_id is required")
	}

	switch t.ScheduleKind {
	case superagentKindOnce:
		if t.OnceAt == nil {
			errs = append(errs, "once_at is required when schedule_kind is 'once'")
		} else {
			at := *t.OnceAt
			if !at.After(now) {
				errs = append(errs, "once_at must be in the future")
			} else if at.Sub(now) > superagentMaxOnceHorizon {
				errs = append(errs, "once_at must be within 24 hours from now")
			}
		}
	case superagentKindInterval:
		if t.IntervalSeconds < superagentMinInterval {
			errs = append(errs, "interval_seconds must be at least 900 (15 minutes)")
		}
	default:
		errs = append(errs, "schedule_kind must be 'once' or 'interval'")
	}

	if t.DailyTokenBudget <= 0 {
		errs = append(errs, "daily_token_budget must be greater than 0")
	}

	return errs
}

// takeoverWarnings 返回**软提示**（保存成功但值得提醒）。
//
// 每一条都要说清"会发生什么"，不能只说"这样可能不好" —— 用户面对一条
// 说不清后果的警告，只能选择忽略。
func takeoverWarnings(t SuperagentTakeover, siblings []SuperagentTakeover) []string {
	var warns []string

	// yolo 在无人值守下永远被降级。用户在聊天里开了 yolo 会以为接管也如此，
	// 必须明确告知，否则他会以为"它应该自己删掉了那个文件"。
	if trimSpace(t.ToolsMode) == superagentUnsafeMode {
		warns = append(warns, "'yolo' is downgraded to 'auto' during unattended takeover: "+"tools requiring confirmation will suspend for your input instead of running")
	}

	if t.ScheduleKind == superagentKindOnce && t.OnceAt != nil {
		// 同一时刻（5 分钟窗口内）触发的多个 once 会争抢引擎并发槽，
		// 结果是"排队的那个比预期晚很久才开始"，而用户看到的是"没跑"。
		for _, s := range siblings {
			if s.ID == t.ID || !s.Enabled || s.ScheduleKind != superagentKindOnce || s.OnceAt == nil {
				continue
			}
			diff := s.OnceAt.Sub(*t.OnceAt)
			if diff < 0 {
				diff = -diff
			}
			if diff <= 5*time.Minute {
				warns = append(warns, "another takeover ('"+s.SessionID+"') is scheduled within 5 minutes of this one; they may contend for engine capacity")
				break
			}
		}
		// 距现在不到 2 分钟的 once 几乎立刻触发，用户来不及反悔。
		if until := t.OnceAt.Sub(time.Now()); until > 0 && until < 2*time.Minute {
			warns = append(warns, "once_at is less than 2 minutes away; the takeover will start almost immediately")
		}
	}

	// 单会话预算过高时提醒核对账户级日预算。
	// 这里**不查** settings 表：那要多一次往返，而这条只是软提示。
	// 准确的"各会话预算之和 vs 日预算"冲突判断留给前端（它手里有 settings）。
	if t.Enabled && t.DailyTokenBudget > 200_000 {
		warns = append(warns, "per-session daily budget is high; verify the account-level daily budget can cover it")
	}

	return warns
}

// ── CRUD ────────────────────────────────────────────────────────────

func (h *SuperagentHandler) List(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	rows, err := db.GlobalDBManager.Query(r.Context(), superagentListQuery, claims.TenantID, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list takeovers failed")
		return
	}
	defer rows.Close()

	items := make([]SuperagentTakeover, 0, 5)
	for rows.Next() {
		t, err := scanTakeover(rows)
		if err != nil {
			slog.Warn("scan takeover", "error", err)
			continue
		}
		items = append(items, t)
	}
	OK(w, items)
}

func (h *SuperagentHandler) Create(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())

	var body SuperagentTakeover
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	body.SessionID = trimSpace(body.SessionID)
	body = withTakeoverDefaults(body)

	if errs := validateTakeover(body, time.Now()); len(errs) > 0 {
		validationFailed(w, errs)
		return
	}

	siblings, err := h.listAll(r.Context(), claims.TenantID, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "load takeovers failed")
		return
	}

	// 数量上限按**启用中的**条数算，不是总条数。留着 5 条关掉的草稿不该
	// 阻止用户启用一条新的 —— 那只会逼他先删再建，丢掉草稿。
	if body.Enabled {
		if n := countEnabled(siblings, ""); n >= superagentMaxSessions {
			validationFailed(w, []string{
				"at most " + itoa(superagentMaxSessions) + " enabled takeovers are allowed; disable or delete one first",
			})
			return
		}
	}

	newID, err := id.UUID()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate id failed")
		return
	}

	_, err = db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO superagent_takeovers
		   (id, tenant_id, user_id, session_id, enabled, schedule_kind, once_at,
		    interval_seconds, tools_mode, daily_token_budget)
		 VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
		newID, claims.TenantID, claims.UserID, body.SessionID, body.Enabled,
		body.ScheduleKind, body.OnceAt, body.IntervalSeconds, body.ToolsMode,
		body.DailyTokenBudget)
	if err != nil {
		if isTakeoverUniqueViolation(err) {
			// 同会话只能有一条：重复行会让"这一拍该不该跑"没有唯一答案。
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "a takeover for this session already exists",
			})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "create takeover failed")
		return
	}

	body.ID = newID
	warns := takeoverWarnings(body, siblings)

	saved, err := h.get(r.Context(), claims.TenantID, claims.UserID, newID)
	if err != nil {
		// 写成功但读不回来：如实返回提交的内容，而不是报 500 让用户以为没存上。
		slog.Warn("created takeover but read-back failed", "id", newID, "error", err)
		JSON(w, http.StatusCreated, APIResponse{
			Success: true,
			Data:    map[string]any{"takeover": body, "warnings": warns},
		})
		return
	}
	JSON(w, http.StatusCreated, APIResponse{
		Success: true,
		Data:    map[string]any{"takeover": saved, "warnings": warns},
	})
}

// Update 按需更新可编辑字段。
//
// `session_id` 刻意不在可编辑集合内 —— 换会话等于换一条策略，见类型注释。
// 未提交的字段保持原值（用指针区分"没传"与"传了零值"）。
func (h *SuperagentHandler) Update(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	takeoverID := r.PathValue("id")
	if trimSpace(takeoverID) == "" {
		BadRequest(w, "id is required")
		return
	}

	var body struct {
		Enabled          *bool           `json:"enabled"`
		ScheduleKind     *string         `json:"schedule_kind"`
		OnceAt           json.RawMessage `json:"once_at"`
		IntervalSeconds  *int            `json:"interval_seconds"`
		ToolsMode        *string         `json:"tools_mode"`
		DailyTokenBudget *int            `json:"daily_token_budget"`
		// 恢复熔断：`melt_state='errors'` 必须人工清除，这是唯一入口。
		ClearMelt *bool `json:"clear_melt"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	cur, err := h.get(r.Context(), claims.TenantID, claims.UserID, takeoverID)
	if err != nil {
		NotFound(w, "takeover not found")
		return
	}

	// 先在内存里合并出"更新后的完整状态"，再对它校验。
	// 只校验提交的字段是不够的：把 schedule_kind 改成 once 但没给 once_at 时，
	// 单字段校验看不出问题，合并后才知道缺东西。
	merged := cur
	if body.ScheduleKind != nil {
		merged.ScheduleKind = trimSpace(*body.ScheduleKind)
	}
	if len(body.OnceAt) > 0 {
		if string(body.OnceAt) == "null" {
			merged.OnceAt = nil
		} else {
			var at time.Time
			if err := json.Unmarshal(body.OnceAt, &at); err != nil {
				BadRequest(w, "once_at must be an RFC3339 timestamp or null")
				return
			}
			merged.OnceAt = &at
		}
	}
	if body.IntervalSeconds != nil {
		merged.IntervalSeconds = *body.IntervalSeconds
	}
	if body.ToolsMode != nil {
		merged.ToolsMode = trimSpace(*body.ToolsMode)
	}
	if body.DailyTokenBudget != nil {
		merged.DailyTokenBudget = *body.DailyTokenBudget
	}
	if body.Enabled != nil {
		merged.Enabled = *body.Enabled
	}
	merged = withTakeoverDefaults(merged)

	if errs := validateTakeover(merged, time.Now()); len(errs) > 0 {
		validationFailed(w, errs)
		return
	}

	siblings, err := h.listAll(r.Context(), claims.TenantID, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "load takeovers failed")
		return
	}
	// 启用一条新的时，把"除自己以外"的启用数拿来比上限。
	if merged.Enabled {
		if n := countEnabled(siblings, takeoverID); n >= superagentMaxSessions {
			validationFailed(w, []string{
				"at most " + itoa(superagentMaxSessions) + " enabled takeovers are allowed; disable or delete one first",
			})
			return
		}
	}

	sets := []string{
		"enabled = $1", "schedule_kind = $2", "once_at = $3",
		"interval_seconds = $4", "tools_mode = $5", "daily_token_budget = $6",
	}
	args := []any{
		merged.Enabled, merged.ScheduleKind, merged.OnceAt,
		merged.IntervalSeconds, merged.ToolsMode, merged.DailyTokenBudget,
	}
	if body.ClearMelt != nil && *body.ClearMelt {
		// 用户手动恢复：连失败计数一起清零，否则下一拍立刻又熔断。
		sets = append(sets, "melt_state = ''", "melt_reason = NULL", "consecutive_failures = 0")
	}
	args = append(args, claims.TenantID, claims.UserID, takeoverID)

	q := "UPDATE superagent_takeovers SET " + joinComma(sets) + ", updated_at = now()" +
		" WHERE tenant_id = $7 AND user_id = $8 AND id::text = $9"
	if _, err := db.GlobalDBManager.Exec(r.Context(), q, args...); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update takeover failed")
		return
	}

	saved, err := h.get(r.Context(), claims.TenantID, claims.UserID, takeoverID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "read takeover failed")
		return
	}
	OK(w, map[string]any{"takeover": saved, "warnings": takeoverWarnings(merged, siblings)})
}

func (h *SuperagentHandler) Delete(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	takeoverID := r.PathValue("id")
	if trimSpace(takeoverID) == "" {
		BadRequest(w, "id is required")
		return
	}
	// 三重条件防跨租户/跨用户：只靠 id 是不够的（id 可被猜到或被日志泄露）。
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`DELETE FROM superagent_takeovers WHERE tenant_id = $1 AND user_id = $2 AND id::text = $3`,
		claims.TenantID, claims.UserID, takeoverID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete takeover failed")
		return
	}
	OK(w, map[string]string{"status": "deleted"})
}

// ── settings ────────────────────────────────────────────────────────

func (h *SuperagentHandler) GetSettings(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	s, err := h.loadSettings(r.Context(), claims.TenantID, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "load settings failed")
		return
	}
	OK(w, s)
}

func (h *SuperagentHandler) PutSettings(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())

	var body SuperagentSettings
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	var errs []string
	if body.MaxSessions <= 0 {
		body.MaxSessions = superagentMaxSessions
	}
	if body.MaxSessions > superagentMaxSessions {
		errs = append(errs, "max_sessions must not exceed "+itoa(superagentMaxSessions))
	}
	if body.DayTokenBudget <= 0 {
		errs = append(errs, "day_token_budget must be greater than 0")
	}
	if body.IdleMinutes <= 0 {
		body.IdleMinutes = 10
	}
	if body.IdleMinutes > 120 {
		errs = append(errs, "idle_minutes must not exceed 120")
	}
	if len(errs) > 0 {
		validationFailed(w, errs)
		return
	}

	_, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO superagent_settings
		   (tenant_id, user_id, enabled, max_sessions, day_token_budget, idle_minutes, updated_at)
		 VALUES ($1,$2,$3,$4,$5,$6, now())
		 ON CONFLICT (tenant_id, user_id) DO UPDATE
		    SET enabled = EXCLUDED.enabled,
		        max_sessions = EXCLUDED.max_sessions,
		        day_token_budget = EXCLUDED.day_token_budget,
		        idle_minutes = EXCLUDED.idle_minutes,
		        updated_at = now()`,
		claims.TenantID, claims.UserID, body.Enabled, body.MaxSessions,
		body.DayTokenBudget, body.IdleMinutes)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "save settings failed")
		return
	}
	OK(w, body)
}

// ── 运行时端点：纯代理到引擎 ───────────────────────────────────────
//
// 网关在这里**不解释**响应体：presence 的 TTL、接管段的 `known` 语义、打断的
// 冷却时长都由引擎定义。网关只负责鉴权（JWT）、限流、覆盖身份头，然后把响应
// 原样交回前端。在网关再包一层 `{success, data}` 会让前端面对两套解包逻辑。

func (h *SuperagentHandler) proxy(w http.ResponseWriter, r *http.Request, path string) {
	if h.pythonClient == nil {
		InternalError(w, "python engine not available")
		return
	}
	h.pythonClient.ForwardRequest(w, r, path)
}

func (h *SuperagentHandler) RegisterRoutes(
	mux *http.ServeMux,
	authMW, rlMW func(http.Handler) http.Handler,
) {
	mux.Handle("GET /v1/superagent/takeovers", authMW(rlMW(http.HandlerFunc(h.List))))
	mux.Handle("POST /v1/superagent/takeovers", authMW(rlMW(http.HandlerFunc(h.Create))))
	mux.Handle("PUT /v1/superagent/takeovers/{id}", authMW(rlMW(http.HandlerFunc(h.Update))))
	mux.Handle("DELETE /v1/superagent/takeovers/{id}", authMW(rlMW(http.HandlerFunc(h.Delete))))
	mux.Handle("GET /v1/superagent/settings", authMW(rlMW(http.HandlerFunc(h.GetSettings))))
	mux.Handle("PUT /v1/superagent/settings", authMW(rlMW(http.HandlerFunc(h.PutSettings))))

	mux.Handle("GET /v1/superagent/status", authMW(rlMW(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h.proxy(w, r, "/v1/superagent/status")
	}))))
	mux.Handle("POST /v1/superagent/presence", authMW(rlMW(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h.proxy(w, r, "/v1/superagent/presence")
	}))))
	mux.Handle("GET /v1/superagent/segment/{session_id}", authMW(rlMW(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h.proxy(w, r, "/v1/superagent/segment/"+r.PathValue("session_id"))
	}))))
	// 打断：引擎侧做双拍（停 run + 写冷却），网关只转发。
	// 这里**不做**二次确认弹窗之类的交互 —— 打断必须是"点一下就生效"，
	// 任何多余往返都意味着 run 多改一个文件。
	mux.Handle("POST /v1/superagent/interrupt", authMW(rlMW(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h.proxy(w, r, "/v1/superagent/interrupt")
	}))))
}

// ── 内部辅助 ────────────────────────────────────────────────────────

func (h *SuperagentHandler) listAll(ctx context.Context, tenantID int16, userID string) ([]SuperagentTakeover, error) {
	rows, err := db.GlobalDBManager.Query(ctx, superagentListQuery, tenantID, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	items := make([]SuperagentTakeover, 0, 5)
	for rows.Next() {
		t, err := scanTakeover(rows)
		if err != nil {
			slog.Warn("scan takeover", "error", err)
			continue
		}
		items = append(items, t)
	}
	return items, rows.Err()
}

func (h *SuperagentHandler) get(ctx context.Context, tenantID int16, userID, takeoverID string) (SuperagentTakeover, error) {
	rows, err := db.GlobalDBManager.Query(ctx, superagentOneQuery, tenantID, userID, takeoverID)
	if err != nil {
		return SuperagentTakeover{}, err
	}
	defer rows.Close()
	if !rows.Next() {
		return SuperagentTakeover{}, errors.New("not found")
	}
	return scanTakeover(rows)
}

func (h *SuperagentHandler) loadSettings(ctx context.Context, tenantID int16, userID string) (SuperagentSettings, error) {
	// 没有行时返回**默认值**而不是写库：用户只是打开设置页，
	// 不该产生一条"他什么都没改"的记录（那会让审计里的首次变更时刻失真）。
	s := SuperagentSettings{
		Enabled:        false,
		MaxSessions:    superagentMaxSessions,
		DayTokenBudget: 600_000,
		IdleMinutes:    10,
	}
	rows, err := db.GlobalDBManager.Query(ctx,
		`SELECT enabled, max_sessions, day_token_budget, idle_minutes, updated_at
		   FROM superagent_settings WHERE tenant_id = $1 AND user_id = $2`,
		tenantID, userID)
	if err != nil {
		return s, err
	}
	defer rows.Close()
	if rows.Next() {
		if err := rows.Scan(&s.Enabled, &s.MaxSessions, &s.DayTokenBudget, &s.IdleMinutes, &s.UpdatedAt); err != nil {
			return s, err
		}
	}
	return s, rows.Err()
}

func withTakeoverDefaults(t SuperagentTakeover) SuperagentTakeover {
	if trimSpace(t.ScheduleKind) == "" {
		t.ScheduleKind = superagentKindInterval
	}
	if t.IntervalSeconds <= 0 && t.ScheduleKind == superagentKindInterval {
		t.IntervalSeconds = 30 * 60
	}
	if trimSpace(t.ToolsMode) == "" {
		t.ToolsMode = superagentSafeMode
	}
	if t.DailyTokenBudget <= 0 {
		t.DailyTokenBudget = 120_000
	}
	return t
}

func countEnabled(items []SuperagentTakeover, excludeID string) int {
	n := 0
	for _, t := range items {
		if t.Enabled && t.ID != excludeID {
			n++
		}
	}
	return n
}

func isTakeoverUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}

// validationFailed 返回 422 并带全部错误。
//
// 只回第一条会让用户改一条重试一次 —— 五条错误的配置要提交五遍。
func validationFailed(w http.ResponseWriter, errs []string) {
	JSON(w, http.StatusUnprocessableEntity, APIResponse{
		Success: false,
		Error:   errs[0],
		Data:    map[string]any{"errors": errs},
	})
}
