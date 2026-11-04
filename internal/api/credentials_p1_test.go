package api

import (
	"strings"
	"testing"
)

// ── P1 凭据层回归：API key ──
//
// `apiKeyAuthQuery` 是每个 API 请求都走的语句。它必须自己带 revoked / expires
// 谓词，并且带回**签发时刻** —— 少了后者，`revokeUserSessions`（改密 / 重置密码 /
// 管理员踢人）只对 JWT 生效，此前签发的长期 API key 依然能用，而 API key 恰恰是
// 最容易落在 CI 与第三方系统里的那类凭据。
//
// webhook secret 的 KDF 用例已随企业 webhook 域一并删除（ent_webhook_handler.go）。

func TestAPIKeyAuthQueryCarriesIssuedAtAndRevocation(t *testing.T) {
	for _, want := range []string{
		"COALESCE(ak.revoked, false) = false",            // 吊销
		"ak.expires_at IS NULL OR ak.expires_at > NOW()", // 过期
		"AS issued_at", // 签发时刻（会话纪元比对用）
	} {
		if !strings.Contains(apiKeyAuthQuery, want) {
			t.Errorf("apiKeyAuthQuery 缺少 %q —— 长期凭据的撤销/到期不再生效：\n%s", want, apiKeyAuthQuery)
		}
	}

	// 第 5 列必须替换成 Unix 秒；直接扫 `created_at` 会在非 UTC 会话时区上整体偏移
	// 一个时区（`timestamp without time zone` + `now()` 的隐式转换）。
	if !strings.Contains(apiKeyAuthQuery, "EXTRACT(EPOCH FROM") {
		t.Errorf("apiKeyAuthQuery 的签发时刻未按会话时区还原成 epoch，时区偏移会让旧 key 逃过撤销：\n%s",
			apiKeyAuthQuery)
	}
	// 历史行可能是 NULL：必须兜成 0（1970），否则一次升级把全部旧 key 打死。
	if !strings.Contains(apiKeyAuthQuery, "COALESCE(") {
		t.Errorf("apiKeyAuthQuery 未对 NULL 签发时刻兜底：\n%s", apiKeyAuthQuery)
	}
}
