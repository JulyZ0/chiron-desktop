package api

// share_link.go —— 分享链接的**绝对 URL** 计算（唯一的拼装点）。
//
// 为什么必须由后端产出：前端只有 `window.location.origin`，而桌面端页面跑在 `file://`
// 上 —— origin 是 `file://`，任何相对路径拼出来的都是 `file:///share/<token>`（某些
// 打包形态里表现为 `file:///s/<token>`），换一台机器就是「找不到文件」。链接的可达性
// 是后端事实（它知道自己的对外地址），不该让每个客户端各猜一次。
//
// 优先级：
//  1. `PUBLIC_BASE_URL`（部署方在「管理后台 → 服务 → 对外地址」配置的权威值）；
//  2. 请求的 scheme + Host —— 直连网关时（桌面端 127.0.0.1:<port>）就是本机地址；
//     反向代理后面只有**可信代理**送来的 `X-Forwarded-Proto` / `X-Forwarded-Host` 才算数
//     （与 `realIPHeader` 同一套 TRUSTED_PROXY_CIDRS 判定，否则任何客户端都能伪造链接主机）。

import (
	"net/http"
	"strings"

	"github.com/athenavi/chiron/config"
)

// sharePathPrefix 是 SPA 里公开分享页的路由前缀。
//
// 统一为 `/s/`（短链形态，与前端 `utils/shareLink.ts` 的 SHARE_PATH_PREFIX 一致）。
// 前端同时保留 `/share/:id` 作为**兼容别名** —— 链接发出去就收不回来，旧链接必须
// 继续能打开，但那只是读侧兜底，不再是对外产出的形态。
//
// 与 `GET /v1/share/{id}`（JSON 接口）刻意分开：前者是给人打开的页面，后者是数据。
const sharePathPrefix = "/s/"

// publicBaseURL 返回网关的对外基址（不带结尾 "/"）；无法确定时返回空串。
func publicBaseURL(cfg *config.Config, r *http.Request) string {
	if cfg != nil {
		if base := strings.TrimSpace(cfg.PublicBaseURL); base != "" {
			return strings.TrimRight(base, "/")
		}
	}
	if r == nil {
		return ""
	}

	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	host := strings.TrimSpace(r.Host)

	// 只有可信代理才有权改写协议与主机：这两个头能决定「链接指向谁」，无条件采信
	// 等于把分享链接变成可被请求方指定的任意 URL。
	if cfg != nil && isTrustedProxyPeer(r.RemoteAddr, cfg.TrustedProxyCIDRs) {
		if proto := firstHeaderValue(r.Header.Get("X-Forwarded-Proto")); proto != "" {
			scheme = strings.ToLower(proto)
		} else if proto := firstHeaderValue(r.Header.Get("X-Forwarded-Protocol")); proto != "" {
			scheme = strings.ToLower(proto)
		}
		if forwardedHost := firstHeaderValue(r.Header.Get("X-Forwarded-Host")); forwardedHost != "" {
			host = forwardedHost
		}
	}
	if host == "" {
		return ""
	}
	return scheme + "://" + host
}

// sharePublicURL 组装一条分享页的绝对 URL；token 为空或无法确定基址时返回空串
// （前端据此报"请先配置对外地址"，而不是拼一个打不开的相对路径）。
func sharePublicURL(cfg *config.Config, r *http.Request, token string) string {
	token = strings.TrimSpace(token)
	if token == "" {
		return ""
	}
	base := publicBaseURL(cfg, r)
	if base == "" {
		return ""
	}
	return base + sharePathPrefix + token
}

// firstHeaderValue 取逗号分隔头的第一个值（代理链会追加，只认最靠近客户端的那一跳口径
// 与 X-Forwarded-For 一致 —— 取**最左**）。
func firstHeaderValue(raw string) string {
	if raw == "" {
		return ""
	}
	if i := strings.IndexByte(raw, ','); i >= 0 {
		raw = raw[:i]
	}
	return strings.TrimSpace(raw)
}
