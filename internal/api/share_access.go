package api

// share_access.go —— 分享运营的底座：访问统计 + 访问保护（密码 / 次数上限）。
//
// ## 统计什么、不统计什么
//
// 只统计**成功打开**（200）。密码错误、已撤销、已过期、达到上限的回包都不记 ——
// 它们不是"有人看过"，把它们混进来会让运营把"被扫链接"读成"有人在看"。
//
// ## 隐私
//
// 落库的三列都是**不可还原**或**已降级**的：
//   - `ip_hash`：sha256(域分隔符 + IP)，不落明文，只能做"同一来访者"的判断；
//   - `ua_class`：四分类（bot/mobile/desktop/other），完整 UA 不进库；
//   - `referer_host`：只保留主机名，路径与查询串一律丢弃（那里可能有一次性 token）。
//
// ## 不阻塞匿名首屏
//
// 写日志发生在响应发出**之后**的独立协程里，且用 `context.WithoutCancel` 派生上下文
// —— 请求上下文在 handler 返回时就被取消，直接拿它写库必然失败。写失败只 warn，
// 绝不影响访客看到的内容。

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"golang.org/x/crypto/bcrypt"
)

// shareAccessEntry 是一次成功访问的**脱敏事实**。
type shareAccessEntry struct {
	ShareID string
	At      time.Time
	// IPHash 是客户端 IP 的 sha256 摘要（十六进制），空串表示拿不到 IP。
	IPHash string
	// UAClass 是 UA 的四分类，见 classifyUserAgent。
	UAClass string
	// RefererHost 只有主机名（小写），不含路径与查询串。
	RefererHost string
}

// shareAccessWriter 落一条访问记录。实现必须**不阻塞**调用方（handler 在协程里调它），
// 抽成函数类型是为了单测能注入一个只记账的假实现。
type shareAccessWriter func(ctx context.Context, e shareAccessEntry)

// dbShareAccessWriter 是生产实现：一条 INSERT，失败只 warn。
func dbShareAccessWriter(ctx context.Context, e shareAccessEntry) {
	if db.GlobalDBManager == nil {
		return
	}
	if _, err := db.GlobalDBManager.Exec(ctx,
		`INSERT INTO share_access_log (share_id, at, ip_hash, ua_class, referer_host)
		 VALUES ($1, $2, $3, $4, $5)`,
		e.ShareID, e.At, nullIfEmpty(e.IPHash), nullIfEmpty(e.UAClass), nullIfEmpty(e.RefererHost)); err != nil {
		// 统计是旁路：写不进去不该让访客看到错误页，也不该刷屏 —— 一条 warn 足够。
		slog.Warn("share: record access failed", "err", err)
	}
}

// shareIPHashDomain 是 IP 哈希的域分隔符：避免同一台机器上别的 sha256(IP) 用途
// 与本表相互印证。注意这是"不可直接读"，不是"不可逆"：IPv4 空间有限，拿到盐的人
// 仍可穷举。这里要的只是"不落明文"，不做匿名化承诺 —— 真正的匿名化需要密钥轮换。
const shareIPHashDomain = "chiron:share-access:v1:"

// hashShareIP 返回 IP 的十六进制摘要；空 IP 返回空串（不写无意义的常量哈希）。
func hashShareIP(ip string) string {
	ip = strings.TrimSpace(ip)
	if ip == "" {
		return ""
	}
	sum := sha256.Sum256([]byte(shareIPHashDomain + ip))
	return hex.EncodeToString(sum[:])
}

// classifyUserAgent 把 UA 归成四类。
//
// 只做**粗分类**：运营要回答"是不是爬虫在刷"，不需要识别到具体浏览器版本。
// 顺序有意义 —— bot 判定必须在 mobile/desktop 之前（很多爬虫 UA 里带 Safari 字样）。
func classifyUserAgent(ua string) string {
	low := strings.ToLower(strings.TrimSpace(ua))
	if low == "" {
		return "other"
	}
	for _, kw := range []string{"bot", "crawler", "spider", "scrapy", "curl/", "wget/",
		"python-requests", "python-urllib", "go-http-client", "headlesschrome", "phantomjs"} {
		if strings.Contains(low, kw) {
			return "bot"
		}
	}
	for _, kw := range []string{"iphone", "ipod", "android", "mobile", "windows phone"} {
		if strings.Contains(low, kw) {
			return "mobile"
		}
	}
	for _, kw := range []string{"windows nt", "macintosh", "x11", "linux", "cros"} {
		if strings.Contains(low, kw) {
			return "desktop"
		}
	}
	return "other"
}

// refererHost 只保留 Referer 的主机名。
//
// 为什么丢弃路径与查询串：来源页 URL 里可能带一次性 token / 会话标识，落库等于
// 把它们复制进一张读权限更松的统计表。主机名足以回答"从哪来的"。
func refererHost(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	return strings.ToLower(strings.TrimSpace(u.Hostname()))
}

// shareAccessFromRequest 把请求整理成一条脱敏的访问记录。
func shareAccessFromRequest(r *http.Request, shareID string) shareAccessEntry {
	return shareAccessEntry{
		ShareID:     shareID,
		At:          time.Now().UTC(),
		IPHash:      hashShareIP(remoteIP(r)),
		UAClass:     classifyUserAgent(r.Header.Get("User-Agent")),
		RefererHost: refererHost(r.Header.Get("Referer")),
	}
}

// recordAccessAsync 把一次访问交给后台协程落库，立刻返回。
//
// `context.WithoutCancel`：handler 返回时请求上下文随即取消，用它写库会稳定失败
// （表现为"统计永远是 0"）。这里只取其中的值（如 trace id），不继承取消信号，
// 再用 5s 超时兜住卡住的连接。
func (h *ShareHandler) recordAccessAsync(r *http.Request, shareID string) {
	writer := h.access
	if writer == nil {
		return
	}
	entry := shareAccessFromRequest(r, shareID)
	ctx := context.WithoutCancel(r.Context())
	go func() {
		ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		writer(ctx, entry)
	}()
}

// ── 密码保护 ──────────────────────────────────────────────────────────

// sharePasswordHeader 是分享密码的传输头。
//
// 不放查询串：URL 会进访问日志、Referer、浏览器历史与截图，而分享密码往往被重复
// 用于同一条链接的多次访问，泄露一次等于长期泄露。
const sharePasswordHeader = "X-Share-Password"

// sharePasswordMinLen / MaxLen：bcrypt 只取前 72 字节，超过即报错；太短的密码
// 在分享场景没有意义（链接本身已经是持有人凭据）。
const (
	sharePasswordMinLen = 4
	sharePasswordMaxLen = 72
)

// hashSharePassword 用 bcrypt 生成密码摘要（与登录口令同一算法与成本）。
func hashSharePassword(password string) (string, error) {
	hash, err := bcrypt.GenerateFromPassword([]byte(password), auth.BcryptCost)
	if err != nil {
		return "", err
	}
	return string(hash), nil
}

// sharePasswordMatches 校验密码；hash 为空（未设密码）或密码为空时一律 false。
func sharePasswordMatches(hash, provided string) bool {
	if strings.TrimSpace(hash) == "" || provided == "" {
		return false
	}
	return bcrypt.CompareHashAndPassword([]byte(hash), []byte(provided)) == nil
}
