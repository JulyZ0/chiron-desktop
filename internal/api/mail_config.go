package api

// ── 邮件通道配置（system_settings 的 `mail` 分类）──
//
// auth.MailSender 早已实现（SMTP / 晴辰云邮），但没有任何配置来源与调用方，
// 邀请只能复制链接。这里补两件事：
//   1. 配置读取：后台保存到 system_settings，敏感键由 settings.Store 加密；
//      环境变量只作兜底（部署期一次性配置的部署可以用 env）。
//   2. 统一的发信入口：邀请邮件与后台「发送测试邮件」共用同一份配置与错误口径。
//
// 不发信也算正常：未配置时调用方退回"复制邀请链接"，不把邀请创建失败。

import (
	"context"
	"errors"
	"fmt"
	"html"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/settings"
)

const settingsCategoryMail = "mail"

// mailRuntimeConfig 是解析后的生效配置（不含任何日志输出）。
type mailRuntimeConfig struct {
	Provider       string
	Host           string
	Port           int
	Username       string
	Password       string
	Security       string
	From           string
	FromName       string
	ReplyTo        string
	BaseURL        string
	APIKey         string
	TimeoutSeconds int
}

// mailSenderFactory 抽出是为了测试替换；生产恒为真实 HTTP/SMTP 发送器。
var mailSenderFactory = func() auth.MailSender { return auth.NewHTTPMailSender() }

func readMailRuntimeConfig(ctx context.Context, store *settings.Store) mailRuntimeConfig {
	vals := map[string]interface{}{}
	if store != nil {
		if cfg, err := store.LoadConfig(ctx, settingsCategoryMail); err == nil {
			vals = cfg
		}
	}
	get := func(key, env, def string) string {
		if v, ok := vals[key]; ok {
			if s := strings.TrimSpace(stringOf(v)); s != "" {
				return s
			}
		}
		if env != "" {
			if s := strings.TrimSpace(os.Getenv(env)); s != "" {
				return s
			}
		}
		return def
	}

	cfg := mailRuntimeConfig{
		Provider:       strings.ToLower(get("provider", "MAIL_PROVIDER", auth.MailProviderSMTP)),
		Host:           get("host", "MAIL_SMTP_HOST", ""),
		Username:       get("username", "MAIL_SMTP_USERNAME", ""),
		Password:       get("password", "MAIL_SMTP_PASSWORD", ""),
		Security:       strings.ToLower(get("security", "MAIL_SMTP_SECURITY", auth.MailSecurityStartTLS)),
		From:           get("from", "MAIL_FROM", ""),
		FromName:       get("from_name", "MAIL_FROM_NAME", "Chiron"),
		ReplyTo:        get("reply_to", "MAIL_REPLY_TO", ""),
		BaseURL:        get("base_url", "MAIL_BASE_URL", ""),
		APIKey:         get("api_key", "MAIL_API_KEY", ""),
		TimeoutSeconds: 30,
	}
	if p := intOf(vals["port"]); p > 0 {
		cfg.Port = p
	} else if p, err := strconv.Atoi(os.Getenv("MAIL_SMTP_PORT")); err == nil && p > 0 {
		cfg.Port = p
	}
	if t := intOf(vals["timeout_seconds"]); t > 0 {
		cfg.TimeoutSeconds = t
	} else if t, err := strconv.Atoi(os.Getenv("MAIL_TIMEOUT_SECONDS")); err == nil && t > 0 {
		cfg.TimeoutSeconds = t
	}
	if cfg.Port <= 0 {
		switch cfg.Security {
		case auth.MailSecuritySSL:
			cfg.Port = 465
		case auth.MailSecurityNone:
			cfg.Port = 25
		default:
			cfg.Port = 587
		}
	}
	return cfg
}

// validate 是保存与发信前的同一套判据：能发信必须字段齐全，而不是"先存下来再报错"。
func (c mailRuntimeConfig) validate() error {
	if !auth.IsKnownMailProvider(c.Provider) {
		return fmt.Errorf("unsupported mail provider %q", c.Provider)
	}
	if c.From == "" || !auth.ValidateMailAddress(c.From) {
		return errors.New("from address is missing or invalid")
	}
	if c.ReplyTo != "" && !auth.ValidateMailAddress(c.ReplyTo) {
		return errors.New("reply_to address is invalid")
	}
	if c.TimeoutSeconds < 1 || c.TimeoutSeconds > 120 {
		return errors.New("timeout_seconds must be between 1 and 120")
	}
	switch c.Provider {
	case auth.MailProviderSMTP:
		if strings.TrimSpace(c.Host) == "" {
			return errors.New("smtp host is required")
		}
		if c.Port < 1 || c.Port > 65535 {
			return errors.New("smtp port is invalid")
		}
		if !auth.IsKnownMailSecurity(c.Security) {
			return fmt.Errorf("unsupported smtp security %q", c.Security)
		}
		if c.Username != "" && c.Password == "" {
			return errors.New("smtp password is required when username is set")
		}
	case auth.MailProviderQingchen:
		if c.BaseURL == "" || !strings.HasPrefix(strings.ToLower(c.BaseURL), "http") {
			return errors.New("base_url must be an http(s) URL")
		}
		if c.APIKey == "" {
			return errors.New("api_key is required for qingchen")
		}
	}
	return nil
}

func (c mailRuntimeConfig) toAuth() *auth.MailConfig {
	return &auth.MailConfig{
		Provider:       c.Provider,
		Host:           c.Host,
		Port:           c.Port,
		Username:       c.Username,
		Password:       c.Password,
		Security:       c.Security,
		From:           c.From,
		FromName:       c.FromName,
		ReplyTo:        c.ReplyTo,
		BaseURL:        c.BaseURL,
		APIKey:         c.APIKey,
		TimeoutSeconds: c.TimeoutSeconds,
	}
}

func mailTimeout(c mailRuntimeConfig) time.Duration {
	return time.Duration(c.TimeoutSeconds) * time.Second
}

// sendInviteEmail 组织并发送邀请邮件。调用方负责确认 cfg 已通过 validate()。
func sendInviteEmail(ctx context.Context, sender auth.MailSender, cfg mailRuntimeConfig, to, inviteURL string, expiresAt time.Time) error {
	senderCtx, cancel := context.WithTimeout(ctx, mailTimeout(cfg))
	defer cancel()
	expires := expiresAt.UTC().Format(time.RFC3339)
	subject := "邀请你加入 Chiron"
	text := fmt.Sprintf("你被邀请加入 Chiron。\n\n请在有效期内打开以下链接设置密码：\n%s\n\n链接有效期至：%s\n如果这不是你预期的邀请，请忽略本邮件。\n", inviteURL, expires)
	body := fmt.Sprintf(
		`<p>你被邀请加入 Chiron。</p><p>请在有效期内打开以下链接设置密码：</p><p><a href="%s">%s</a></p><p>链接有效期至：%s</p><p>如果这不是你预期的邀请，请忽略本邮件。</p>`,
		html.EscapeString(inviteURL), html.EscapeString(inviteURL), html.EscapeString(expires),
	)
	return sender.Send(senderCtx, cfg.toAuth(), &auth.MailMessage{
		To:      []string{to},
		Subject: subject,
		Text:    text,
		HTML:    body,
	})
}

// ── 管理端配置视图 ──

// AdminMailConfig 是 GET/PUT /v1/admin/mail 的响应体。密码/api_key 只回"是否已设置"。
type AdminMailConfig struct {
	Provider       string `json:"provider"`
	Host           string `json:"host"`
	Port           int    `json:"port"`
	Username       string `json:"username"`
	Security       string `json:"security"`
	From           string `json:"from"`
	FromName       string `json:"from_name"`
	ReplyTo        string `json:"reply_to"`
	BaseURL        string `json:"base_url"`
	TimeoutSeconds int    `json:"timeout_seconds"`
	HasPassword    bool   `json:"has_password"`
	HasAPIKey      bool   `json:"has_api_key"`
	Ready          bool   `json:"ready"`
	Problem        string `json:"problem,omitempty"`
}

func mailConfigView(cfg mailRuntimeConfig) AdminMailConfig {
	view := AdminMailConfig{
		Provider: cfg.Provider, Host: cfg.Host, Port: cfg.Port, Username: cfg.Username,
		Security: cfg.Security, From: cfg.From, FromName: cfg.FromName, ReplyTo: cfg.ReplyTo,
		BaseURL: cfg.BaseURL, TimeoutSeconds: cfg.TimeoutSeconds,
		HasPassword: cfg.Password != "", HasAPIKey: cfg.APIKey != "",
	}
	if err := cfg.validate(); err != nil {
		view.Problem = err.Error()
	} else {
		view.Ready = true
	}
	return view
}

// GetMailConfig GET /v1/admin/mail
func (h *AdminHandler) GetMailConfig(w http.ResponseWriter, r *http.Request) {
	cfg := readMailRuntimeConfig(r.Context(), h.ensureSettingsStore())
	OK(w, mailConfigView(cfg))
}

// SaveMailConfig PUT /v1/admin/mail
//
// 密码/api_key 的语义：字段缺省（JSON null）或空串 = **保持原值**；要清空请显式
// 传 `"clear_password": true` / `"clear_api_key": true`。避免前端每次保存都把
// 只回显过布尔值的密钥覆盖成空。
func (h *AdminHandler) SaveMailConfig(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Provider       *string `json:"provider"`
		Host           *string `json:"host"`
		Port           *int    `json:"port"`
		Username       *string `json:"username"`
		Password       *string `json:"password"`
		Security       *string `json:"security"`
		From           *string `json:"from"`
		FromName       *string `json:"from_name"`
		ReplyTo        *string `json:"reply_to"`
		BaseURL        *string `json:"base_url"`
		APIKey         *string `json:"api_key"`
		TimeoutSeconds *int    `json:"timeout_seconds"`
		ClearPassword  bool    `json:"clear_password"`
		ClearAPIKey    bool    `json:"clear_api_key"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	store := h.ensureSettingsStore()
	if store == nil {
		InternalError(w, "settings store unavailable")
		return
	}
	ctx := r.Context()
	cur := readMailRuntimeConfig(ctx, store)
	// 在现有配置上叠加请求里的字段，再用同一套 validate 判"保存后能不能发信"。
	patch := map[string]interface{}{}
	setStr := func(key string, v *string) {
		if v == nil {
			return
		}
		s := strings.TrimSpace(*v)
		patch[key] = s
		switch key {
		case "provider":
			cur.Provider = strings.ToLower(s)
		case "host":
			cur.Host = s
		case "username":
			cur.Username = s
		case "security":
			cur.Security = strings.ToLower(s)
		case "from":
			cur.From = s
		case "from_name":
			cur.FromName = s
		case "reply_to":
			cur.ReplyTo = s
		case "base_url":
			cur.BaseURL = s
		}
	}
	setStr("provider", body.Provider)
	setStr("host", body.Host)
	setStr("username", body.Username)
	setStr("security", body.Security)
	setStr("from", body.From)
	setStr("from_name", body.FromName)
	setStr("reply_to", body.ReplyTo)
	setStr("base_url", body.BaseURL)
	if body.Port != nil {
		patch["port"] = *body.Port
		cur.Port = *body.Port
	}
	if body.TimeoutSeconds != nil {
		patch["timeout_seconds"] = *body.TimeoutSeconds
		cur.TimeoutSeconds = *body.TimeoutSeconds
	}
	if body.ClearPassword {
		patch["password"] = ""
		cur.Password = ""
	} else if body.Password != nil && strings.TrimSpace(*body.Password) != "" {
		patch["password"] = *body.Password
		cur.Password = *body.Password
	}
	if body.ClearAPIKey {
		patch["api_key"] = ""
		cur.APIKey = ""
	} else if body.APIKey != nil && strings.TrimSpace(*body.APIKey) != "" {
		patch["api_key"] = *body.APIKey
		cur.APIKey = *body.APIKey
	}
	if err := cur.validate(); err != nil {
		BadRequest(w, err.Error())
		return
	}
	userID := ""
	if claims := auth.GetClaims(ctx); claims != nil {
		userID = claims.ID
	}
	if err := store.SaveConfig(ctx, settingsCategoryMail, patch, userID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "save mail settings failed")
		return
	}
	OK(w, mailConfigView(cur))
}

// TestMail POST /v1/admin/mail/test —— 只允许平台 owner（见路由权限），
// 收件人缺省为当前账号邮箱，避免把测试入口变成任意收件人的群发器。
func (h *AdminHandler) TestMail(w http.ResponseWriter, r *http.Request) {
	var body struct {
		To string `json:"to"`
	}
	_ = DecodeJSON(w, r, &body)
	to := strings.TrimSpace(body.To)
	if to == "" {
		if claims := auth.GetClaims(r.Context()); claims != nil {
			to = claims.Email
		}
	}
	if to == "" || !auth.ValidateMailAddress(to) {
		BadRequest(w, "valid recipient email is required")
		return
	}
	cfg := readMailRuntimeConfig(r.Context(), h.ensureSettingsStore())
	if err := cfg.validate(); err != nil {
		JSONWithCode(w, http.StatusBadRequest, "mail_not_configured", map[string]interface{}{"reason": err.Error()}, "mail is not configured")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), mailTimeout(cfg))
	defer cancel()
	err := mailSenderFactory().Send(ctx, cfg.toAuth(), &auth.MailMessage{
		To:      []string{to},
		Subject: "Chiron 邮件测试",
		Text:    "这是一封来自 Chiron 的测试邮件。收到即表示后台邮件配置可用。\n",
		HTML:    "<p>这是一封来自 Chiron 的测试邮件。收到即表示后台邮件配置可用。</p>",
	})
	if err != nil {
		// 服务商错误可能带收件人/凭据片段：只进日志（且不含密码），响应给通用错误码。
		logAndRespond(w, err, http.StatusBadGateway, "mail send failed")
		return
	}
	OK(w, map[string]interface{}{"sent": true, "to": to})
}
