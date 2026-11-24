package api

// ── 会话撤销判定（HTTP 与 WebSocket 的唯一实现）──
//
// 撤销语义有三个来源：
//   - 单次登出：`jwt:blacklist:<jti>`
//   - 批量撤销（改密、重置密码、禁用、撤销、强制下线、删除用户）：`jwt:epoch:<userID>`
//   - 首登/重置后强制改密：claims.MustChangePassword
//
// HTTP 中间件与 `/ws/rpa`、`/ws/device-fs` 必须给出**同一个结论**：只要有一条入口
// 的判定比另一条宽松，撤销就等于没做 —— "HTTP 已经 401 的凭据还能开一条长连接"，
// 攻击者只要先开 WS 就能继续读到项目文件。因此这里把判定收敛成一处，两条入口都调用，
// 避免两边各自实现后再漂移（历史上 WS 侧曾用 `epoch >= iat`、HTTP 用 `epoch > iat`，
// 同一枚 token 在改密那一秒内 WS 拒绝、HTTP 放行）。

import (
	"context"
	"net/http"

	"github.com/athenavi/chiron/internal/auth"
)

// sessionRevocation 描述"这枚凭据为什么不能继续用于 path"。
//
// status/code/message 与两边的响应口径一致：401（token 失效/存储不可用时 fail-close）、
// 403 + code=password_change_required（必须改密）。cause 非空表示判定本身失败
// （Redis 不可用等），调用方需要记日志 —— 但对外仍是拒绝，不放行。
type sessionRevocation struct {
	status  int
	code    string
	message string
	cause   error
}

func (r *sessionRevocation) Error() string {
	return r.message
}

// evaluateSessionRevocation 返回 nil 表示凭据仍然有效；否则返回拒绝原因。
//
// 判定顺序与既有 HTTP 语义一致：纪元 → 黑名单 → 强制改密。
// 纪元比较复用 tokenRevokedByEpoch（`epoch > token.iat` 才算撤销），因此
// 撤销同一秒内新签发的 token 不会被误杀 —— 改密响应里新发的 token 就落在这一秒。
func evaluateSessionRevocation(ctx context.Context, claims *auth.Claims, path string) *sessionRevocation {
	if claims == nil {
		return &sessionRevocation{status: http.StatusUnauthorized, message: "missing authorization"}
	}
	var issuedAt int64
	hasIssuedAt := claims.IssuedAt != nil
	if hasIssuedAt {
		issuedAt = claims.IssuedAt.Unix()
	}
	revoked, err := tokenRevokedByEpoch(ctx, claims.UserID, issuedAt, hasIssuedAt)
	if err != nil {
		return &sessionRevocation{
			status:  http.StatusUnauthorized,
			message: "authentication service unavailable",
			cause:   err,
		}
	}
	if revoked {
		return &sessionRevocation{status: http.StatusUnauthorized, message: "token has been revoked"}
	}
	if claims.ID != "" {
		blacklisted, err := checkJWTBlacklisted(ctx, claims.ID)
		if err != nil {
			return &sessionRevocation{
				status:  http.StatusUnauthorized,
				message: "authentication service unavailable",
				cause:   err,
			}
		}
		if blacklisted {
			return &sessionRevocation{status: http.StatusUnauthorized, message: "token has been revoked"}
		}
	}
	if claims.MustChangePassword && !allowBeforePasswordChange(path) {
		return &sessionRevocation{
			status:  http.StatusForbidden,
			code:    "password_change_required",
			message: "password change required before using this service",
		}
	}
	return nil
}
