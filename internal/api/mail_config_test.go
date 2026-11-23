package api

// 邮件配置：校验口径、env 兜底、邀请邮件内容。真实发送由 auth/mail_test.go 覆盖。

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
)

type fakeMailSender struct {
	cfg *auth.MailConfig
	msg *auth.MailMessage
	err error
}

func (f *fakeMailSender) Send(_ context.Context, cfg *auth.MailConfig, msg *auth.MailMessage) error {
	f.cfg = cfg
	f.msg = msg
	return f.err
}

func smtpCfg() mailRuntimeConfig {
	return mailRuntimeConfig{
		Provider: auth.MailProviderSMTP, Host: "smtp.example.com", Port: 587,
		Username: "u", Password: "p", Security: auth.MailSecurityStartTLS,
		From: "noreply@example.com", FromName: "Chiron", TimeoutSeconds: 10,
	}
}

func TestMailConfigValidate(t *testing.T) {
	ok := smtpCfg()
	if err := ok.validate(); err != nil {
		t.Fatalf("valid smtp rejected: %v", err)
	}

	cases := []struct {
		name string
		edit func(*mailRuntimeConfig)
		want string
	}{
		{"provider", func(c *mailRuntimeConfig) { c.Provider = "carrier-pigeon" }, "provider"},
		{"from", func(c *mailRuntimeConfig) { c.From = "not-an-email" }, "from address"},
		{"host", func(c *mailRuntimeConfig) { c.Host = "" }, "smtp host"},
		{"port", func(c *mailRuntimeConfig) { c.Port = 70000 }, "smtp port"},
		{"security", func(c *mailRuntimeConfig) { c.Security = "tls-ish" }, "security"},
		{"password", func(c *mailRuntimeConfig) { c.Password = "" }, "password"},
		{"timeout", func(c *mailRuntimeConfig) { c.TimeoutSeconds = 0 }, "timeout_seconds"},
	}
	for _, tc := range cases {
		cfg := smtpCfg()
		tc.edit(&cfg)
		if err := cfg.validate(); err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Fatalf("%s: want error containing %q, got %v", tc.name, tc.want, err)
		}
	}

	qc := mailRuntimeConfig{
		Provider: auth.MailProviderQingchen, BaseURL: "https://mail.example.com",
		APIKey: "k", From: "noreply@example.com", TimeoutSeconds: 10,
	}
	if err := qc.validate(); err != nil {
		t.Fatalf("valid qingchen rejected: %v", err)
	}
	qc.BaseURL = "ftp://mail.example.com"
	if err := qc.validate(); err == nil {
		t.Fatal("non-http base_url must be rejected")
	}
}

func TestReadMailConfigEnvFallback(t *testing.T) {
	// 隔离宿主环境里可能存在的其它 MAIL_*（空串在 get() 里等同未设置）。
	for _, k := range []string{
		"MAIL_SMTP_USERNAME", "MAIL_SMTP_PASSWORD", "MAIL_BASE_URL", "MAIL_API_KEY",
		"MAIL_FROM_NAME", "MAIL_REPLY_TO", "MAIL_TIMEOUT_SECONDS",
	} {
		t.Setenv(k, "")
	}
	t.Setenv("MAIL_PROVIDER", "smtp")
	t.Setenv("MAIL_SMTP_HOST", "smtp.env.example")
	t.Setenv("MAIL_SMTP_PORT", "2525")
	t.Setenv("MAIL_SMTP_SECURITY", "none")
	t.Setenv("MAIL_FROM", "bot@env.example")

	cfg := readMailRuntimeConfig(context.Background(), nil)
	if cfg.Provider != auth.MailProviderSMTP || cfg.Host != "smtp.env.example" || cfg.Port != 2525 {
		t.Fatalf("unexpected env config: %+v", cfg)
	}
	if cfg.Security != auth.MailSecurityNone || cfg.From != "bot@env.example" {
		t.Fatalf("unexpected env config: %+v", cfg)
	}
	if cfg.TimeoutSeconds != 30 || cfg.FromName != "Chiron" {
		t.Fatalf("defaults not applied: %+v", cfg)
	}
	if cfg.validate() != nil {
		// 没有 username 时不要求 password；其余字段齐全应通过。
		t.Fatalf("env config should be valid: %v", cfg.validate())
	}
}

func TestSendInviteEmailContent(t *testing.T) {
	sender := &fakeMailSender{}
	expires := time.Date(2026, 11, 24, 8, 30, 0, 0, time.UTC)
	url := "https://chiron.example.com/invite/tok-123"
	if err := sendInviteEmail(context.Background(), sender, smtpCfg(), "user@example.com", url, expires); err != nil {
		t.Fatalf("send: %v", err)
	}
	if sender.msg == nil || len(sender.msg.To) != 1 || sender.msg.To[0] != "user@example.com" {
		t.Fatalf("unexpected message: %+v", sender.msg)
	}
	if !strings.Contains(sender.msg.Text, url) || !strings.Contains(sender.msg.HTML, url) {
		t.Fatalf("invite url missing from message: %+v", sender.msg)
	}
	if !strings.Contains(sender.msg.Text, "2026-11-24T08:30:00Z") {
		t.Fatalf("expiry missing: %s", sender.msg.Text)
	}
	if sender.cfg == nil || sender.cfg.From != "noreply@example.com" || sender.cfg.TimeoutSeconds != 10 {
		t.Fatalf("config not passed through: %+v", sender.cfg)
	}
}

func TestMailConfigViewMasksSecrets(t *testing.T) {
	cfg := smtpCfg()
	view := mailConfigView(cfg)
	if !view.HasPassword || !view.Ready || view.Problem != "" {
		t.Fatalf("unexpected view: %+v", view)
	}
	if view.Provider != "smtp" || view.Host != "smtp.example.com" {
		t.Fatalf("visible fields lost: %+v", view)
	}
}
