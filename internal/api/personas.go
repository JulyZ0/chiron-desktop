package api

// personas.go —— 人格（persona）管理。
//
// ## 为什么不再用「行为塑形」
//
// 原 harness_profiles 是**按模型前缀自动追加提示词**的机制：用户看不到自己"被塑形"
// 成了什么，也无法在对话里换个说法。产品把它收敛成「人格」：
//
//   - 后台维护一份租户级人格库（名称 / 描述 / 系统提示词 / 启用 / 默认）；
//   - 普通用户在对话里**显式选择**人格（未选 = 用默认人格，仍没有 = 不注入）；
//   - 内置预置「猫娘」，管理员可以自由增删改。
//
// 提示词本身由**服务端**在提交时按 id 解析并塞进 `context.persona`：
// 客户端只传 id，既避免前端持有过期文案，也不把"任意系统提示"变成一个新的注入面。

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5"
)

const (
	personaMaxName        = 100
	personaMaxDescription = 500
	personaMaxPrompt      = 20000
)

// Persona 是一个人格。`Preset` 非空表示内置来源（如 catgirl），仅用于展示。
type Persona struct {
	ID           string    `json:"id"`
	Name         string    `json:"name"`
	Description  string    `json:"description"`
	SystemPrompt string    `json:"system_prompt"`
	Enabled      bool      `json:"enabled"`
	IsDefault    bool      `json:"is_default"`
	Preset       string    `json:"preset,omitempty"`
	SortOrder    int       `json:"sort_order"`
	CreatedAt    time.Time `json:"created_at"`
	UpdatedAt    time.Time `json:"updated_at"`
}

type personaWriteBody struct {
	Name         *string `json:"name"`
	Description  *string `json:"description"`
	SystemPrompt *string `json:"system_prompt"`
	Enabled      *bool   `json:"enabled"`
	IsDefault    *bool   `json:"is_default"`
	SortOrder    *int    `json:"sort_order"`
}

// PersonaHandler 没有状态：身份取自 claims，存储是全局 DB 管理器。
type PersonaHandler struct{}

func NewPersonaHandler() *PersonaHandler { return &PersonaHandler{} }

const personaColumns = `id, name, COALESCE(description, ''), COALESCE(system_prompt, ''),
	enabled, is_default, COALESCE(preset_key, ''), sort_order, created_at, updated_at`

func scanPersona(row interface{ Scan(...any) error }) (Persona, error) {
	var p Persona
	err := row.Scan(&p.ID, &p.Name, &p.Description, &p.SystemPrompt, &p.Enabled,
		&p.IsDefault, &p.Preset, &p.SortOrder, &p.CreatedAt, &p.UpdatedAt)
	return p, err
}

// ListForUser GET /v1/personas —— 对话里的人格选择器只看到**启用**的条目。
func (h *PersonaHandler) ListForUser(w http.ResponseWriter, r *http.Request) {
	userID, tenantID, ok := notificationIdentity(w, r)
	if !ok {
		return
	}
	_ = userID // 人格是租户级资源；身份校验只为拿 tenant + 阻止匿名访问
	items, err := listPersonas(r, tenantID, true)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list personas failed")
		return
	}
	OK(w, map[string]interface{}{"personas": items})
}

// ListAdmin GET /v1/admin/personas
func (h *PersonaHandler) ListAdmin(w http.ResponseWriter, r *http.Request) {
	_, tenantID, ok := notificationIdentity(w, r)
	if !ok {
		return
	}
	items, err := listPersonas(r, tenantID, false)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list personas failed")
		return
	}
	OK(w, map[string]interface{}{"personas": items})
}

func listPersonas(r *http.Request, tenantID int16, enabledOnly bool) ([]Persona, error) {
	if db.GlobalDBManager == nil {
		return nil, errors.New("database unavailable")
	}
	query := `SELECT ` + personaColumns + ` FROM personas WHERE tenant_id = $1`
	if enabledOnly {
		query += ` AND enabled`
	}
	query += ` ORDER BY is_default DESC, sort_order ASC, created_at ASC`
	rows, err := db.GlobalDBManager.Query(r.Context(), query, tenantID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make([]Persona, 0, 8)
	for rows.Next() {
		p, err := scanPersona(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

// Create POST /v1/admin/personas
func (h *PersonaHandler) Create(w http.ResponseWriter, r *http.Request) {
	_, tenantID, ok := notificationIdentity(w, r)
	if !ok {
		return
	}
	var body personaWriteBody
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	name := strings.TrimSpace(strPtr(body.Name))
	prompt := strings.TrimSpace(strPtr(body.SystemPrompt))
	if msg := validatePersona(name, prompt); msg != "" {
		BadRequest(w, msg)
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, ErrDBUnavailable)
		return
	}
	id := newUUID()
	_, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO personas (id, tenant_id, name, description, system_prompt, enabled, is_default, sort_order)
		 VALUES ($1, $2, $3, $4, $5, $6, false, $7)`,
		id, tenantID, name, strings.TrimSpace(strPtr(body.Description)), prompt,
		boolValue(body.Enabled, true), intValue(body.SortOrder, 0))
	if err != nil {
		if isUniqueViolation(err) {
			JSON(w, http.StatusConflict, APIResponse{Success: false, Error: "a persona with this name already exists"})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "create persona failed")
		return
	}
	if boolValue(body.IsDefault, false) {
		if err := setDefaultPersona(r, tenantID, id); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "set default persona failed")
			return
		}
	}
	persona, err := getPersona(r, tenantID, id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "load persona failed")
		return
	}
	Created(w, persona)
}

// Update PUT /v1/admin/personas/{id}
func (h *PersonaHandler) Update(w http.ResponseWriter, r *http.Request) {
	_, tenantID, ok := notificationIdentity(w, r)
	if !ok {
		return
	}
	id := strings.TrimSpace(r.PathValue("id"))
	if id == "" {
		BadRequest(w, "id is required")
		return
	}
	var body personaWriteBody
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, ErrDBUnavailable)
		return
	}
	current, err := getPersona(r, tenantID, id)
	if err != nil {
		NotFound(w, "persona not found")
		return
	}
	name := current.Name
	if body.Name != nil {
		name = strings.TrimSpace(*body.Name)
	}
	prompt := current.SystemPrompt
	if body.SystemPrompt != nil {
		prompt = strings.TrimSpace(*body.SystemPrompt)
	}
	if msg := validatePersona(name, prompt); msg != "" {
		BadRequest(w, msg)
		return
	}
	description := current.Description
	if body.Description != nil {
		description = strings.TrimSpace(*body.Description)
	}
	enabled := current.Enabled
	if body.Enabled != nil {
		enabled = *body.Enabled
	}
	sortOrder := current.SortOrder
	if body.SortOrder != nil {
		sortOrder = *body.SortOrder
	}
	_, err = db.GlobalDBManager.Exec(r.Context(),
		`UPDATE personas
		    SET name = $3, description = $4, system_prompt = $5, enabled = $6,
		        sort_order = $7, updated_at = NOW()
		  WHERE id = $1 AND tenant_id = $2`,
		id, tenantID, name, description, prompt, enabled, sortOrder)
	if err != nil {
		if isUniqueViolation(err) {
			JSON(w, http.StatusConflict, APIResponse{Success: false, Error: "a persona with this name already exists"})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "update persona failed")
		return
	}
	// 默认位单独处理：true 走事务（清掉同租户其它默认），false 直接落。
	if body.IsDefault != nil {
		if *body.IsDefault {
			err = setDefaultPersona(r, tenantID, id)
		} else {
			_, err = db.GlobalDBManager.Exec(r.Context(),
				`UPDATE personas SET is_default = false, updated_at = NOW()
				  WHERE id = $1 AND tenant_id = $2`, id, tenantID)
		}
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "set default persona failed")
			return
		}
	}
	updated, err := getPersona(r, tenantID, id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "load persona failed")
		return
	}
	OK(w, updated)
}

// Delete DELETE /v1/admin/personas/{id}
func (h *PersonaHandler) Delete(w http.ResponseWriter, r *http.Request) {
	_, tenantID, ok := notificationIdentity(w, r)
	if !ok {
		return
	}
	id := strings.TrimSpace(r.PathValue("id"))
	if id == "" {
		BadRequest(w, "id is required")
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, ErrDBUnavailable)
		return
	}
	tag, err := db.GlobalDBManager.Exec(r.Context(),
		`DELETE FROM personas WHERE id = $1 AND tenant_id = $2`, id, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete persona failed")
		return
	}
	OK(w, map[string]interface{}{"deleted": tag.RowsAffected()})
}

// SetDefault POST /v1/admin/personas/{id}/default
func (h *PersonaHandler) SetDefault(w http.ResponseWriter, r *http.Request) {
	_, tenantID, ok := notificationIdentity(w, r)
	if !ok {
		return
	}
	id := strings.TrimSpace(r.PathValue("id"))
	if id == "" {
		BadRequest(w, "id is required")
		return
	}
	if err := setDefaultPersona(r, tenantID, id); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			NotFound(w, "persona not found")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "set default persona failed")
		return
	}
	persona, err := getPersona(r, tenantID, id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "load persona failed")
		return
	}
	OK(w, persona)
}

func getPersona(r *http.Request, tenantID int16, id string) (Persona, error) {
	if db.GlobalDBManager == nil {
		return Persona{}, errors.New("database unavailable")
	}
	return scanPersona(db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT `+personaColumns+` FROM personas WHERE id = $1 AND tenant_id = $2`, id, tenantID))
}

// setDefaultPersona 在事务里先清后置：不可能出现双默认（部分唯一索引是最后一道闸）。
func setDefaultPersona(r *http.Request, tenantID int16, id string) error {
	if db.GlobalDBManager == nil {
		return errors.New("database unavailable")
	}
	tx, err := db.GlobalDBManager.Begin(r.Context())
	if err != nil {
		return err
	}
	defer tx.Rollback(r.Context())
	if _, err := tx.Exec(r.Context(),
		`UPDATE personas SET is_default = false, updated_at = NOW()
		  WHERE tenant_id = $1 AND is_default AND id <> $2`, tenantID, id); err != nil {
		return err
	}
	tag, err := tx.Exec(r.Context(),
		`UPDATE personas SET is_default = true, enabled = true, updated_at = NOW()
		  WHERE id = $1 AND tenant_id = $2`, id, tenantID)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return pgx.ErrNoRows
	}
	return tx.Commit(r.Context())
}

// personaForContext 是提交路径的唯一解析点：显式 id 优先，否则取租户默认。
//
// 返回的 map 直接塞进 `context.persona`（引擎按 `{name, prompt}` 消费）。
// 找不到 / 已停用 => ok=false，调用方应删除该键（不注入任何人设）。
func personaForContext(ctx context.Context, tenantID int16, personaID string) (map[string]interface{}, bool) {
	if db.GlobalDBManager == nil || tenantID == 0 {
		return nil, false
	}
	query := `SELECT name, system_prompt FROM personas WHERE tenant_id = $1 AND enabled`
	args := []interface{}{tenantID}
	if strings.TrimSpace(personaID) != "" {
		query += ` AND id = $2`
		args = append(args, strings.TrimSpace(personaID))
	} else {
		query += ` AND is_default`
	}
	var name, prompt string
	err := db.GlobalDBManager.QueryRow(ctx, query, args...).Scan(&name, &prompt)
	if err != nil || strings.TrimSpace(prompt) == "" {
		return nil, false
	}
	return map[string]interface{}{"name": name, "prompt": prompt}, true
}

func validatePersona(name, prompt string) string {
	if name == "" {
		return "name is required"
	}
	if utf8.RuneCountInString(name) > personaMaxName {
		return "name is too long"
	}
	if prompt == "" {
		return "system_prompt is required"
	}
	if utf8.RuneCountInString(prompt) > personaMaxPrompt {
		return "system_prompt is too long"
	}
	return ""
}

func strPtr(v *string) string {
	if v == nil {
		return ""
	}
	return *v
}

func boolValue(v *bool, fallback bool) bool {
	if v == nil {
		return fallback
	}
	return *v
}

func intValue(v *int, fallback int) int {
	if v == nil {
		return fallback
	}
	return *v
}
