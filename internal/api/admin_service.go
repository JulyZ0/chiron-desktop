package api

// admin_service.go —— 后台「服务」页：局域网/广域网访问范围与对外域名。
//
//	GET /v1/admin/service   当前生效的监听事实 + 已持久化配置 + 本机可用地址
//	PUT /v1/admin/service   保存（system_settings 的 `service` 分类）
//
// ## 为什么需要这一页
//
// 「别的设备能不能打开这个界面」取决于两件互不相干的事：
//
//  1. **监听地址**（`BIND_HOST`）：只绑 `127.0.0.1` 时，局域网里的手机连 TCP 都连不上。
//     它写在进程启动参数里，与用户对"我开了什么"的心智模型完全脱节 —— 现象是
//     "在另一台机器上打不开"，而不是任何一句报错。
//  2. **对外地址**（`PUBLIC_BASE_URL`）：真正消费它的只有 **Cookie 的 Secure 推导**
//     （https ⇒ Secure）与自诊断里的一致性检查。分享链接**不走它** —— 前端按
//     访问者当前的 `window.location.origin` 拼。这里必须写准：说成"分享链接按它拼"
//     会让人改了域名去等分享链接变化，而那永远不会发生。
//     它此前挂在 `payment` 分类下（支付域整体删除后已无人维护），于是这一页既是
//     补入口，也是把它挪到语义正确的位置。
//
// ## 为什么监听地址要重启才生效
//
// 监听套接字在 `http.Server.ListenAndServe` 里就绑定了，进程内换不掉。所以本页把
// 「当前生效值」与「已保存待生效值」分开显示：保存成功后明确告诉用户要重启网关，
// 而不是让页面看起来生效了、实际仍然是旧值 —— 那正是这类页面最常见的骗人方式。

import (
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/settings"
)

// settingsCategoryService 是「服务」页配置所在的 system_settings 分组。
const settingsCategoryService = "service"

// bindAllInterfaces 代表"监听全部网卡"。
//
// 空串与 `0.0.0.0` 同义（`net.Listen("tcp", ":8080")` 就是全网卡），两者都归一成
// 这个常量再判定，避免"页面上选了局域网、实际存的是空串"这类看起来不一致的状态。
const bindAllInterfaces = "0.0.0.0"

// bindLoopback 是"仅本机"的取值。
const bindLoopback = "127.0.0.1"

// AdminServiceInfo 是「服务」页的响应体。
type AdminServiceInfo struct {
	// ── 当前生效（进程启动时读定的值）──

	// ListenAddr 是 http.Server 实际绑定的 `host:port`。
	ListenAddr string `json:"listen_addr"`
	// Port 是监听端口。
	Port string `json:"port"`
	// BindHost 是生效中的绑定地址（归一后；`0.0.0.0` 表示全部网卡）。
	BindHost string `json:"bind_host"`
	// PublicBaseURL 是生效中的对外地址（空 = 未配置）。
	PublicBaseURL string `json:"public_base_url"`
	// CookieSecure 是当前 Cookie 是否带 Secure —— 由对外地址协议推导，
	// 页面据此解释"登录了又像没登录"这类现象。
	CookieSecure bool `json:"cookie_secure"`
	// RestartRequired 表示已保存的配置与进程持有的生效值不同（必须重启网关）。
	//
	// 两项都纳入：`bind_host` 绑在套接字上，进程内换不掉；`public_base_url` 由启动时
	// 的 loadConfig + applyDBSettingsAfterConnect 读入并据此推导 `CookieSecure` ——
	// 保存后进程里仍是旧值，只有重启才一致。
	RestartRequired bool `json:"restart_required"`

	// ── 已持久化（下次启动会用的值）──

	SavedBindHost      string `json:"saved_bind_host"`
	SavedPublicBaseURL string `json:"saved_public_base_url"`

	// LocalAddresses 是本机的非回环 IPv4 地址 —— 用户要告诉同事
	// "用这个地址访问"时总得有个来源。只列 IPv4：局域网里手输 IPv6
	// 字面量几乎不会发生，列出来反而淹没有用的那几条。
	LocalAddresses []string `json:"local_addresses"`

	// Warning 是"这组配置会带来什么后果"的提示（空 = 没有需要提醒的组合）。
	Warning string `json:"warning"`
}

// GetServiceInfo GET /v1/admin/service —— 读服务配置与运行时事实。
func (h *AdminHandler) GetServiceInfo(w http.ResponseWriter, r *http.Request) {
	OK(w, h.buildServiceInfo(r, ""))
}

// buildServiceInfo 组装响应。warning 由保存路径传入（验证后的组合结论），
// 读取路径传空 —— 读不该弹出警示，那会让用户以为配置刚刚出了问题。
func (h *AdminHandler) buildServiceInfo(r *http.Request, warning string) AdminServiceInfo {
	info := AdminServiceInfo{
		ListenAddr:     h.cfg.ListenAddr(),
		Port:           h.cfg.Port,
		BindHost:       normalizeBindHost(h.cfg.BindHost),
		PublicBaseURL:  strings.TrimSpace(h.cfg.PublicBaseURL),
		CookieSecure:   h.cfg.CookieSecure,
		LocalAddresses: localIPv4Addrs(),
		Warning:        warning,
	}

	// 已持久化值：读不到（无数据库 / 未配置）时回落成"与生效值相同"，
	// 页面就不会凭空报"需要重启"。
	info.SavedBindHost, info.SavedPublicBaseURL = savedServiceConfig(
		info.BindHost, info.PublicBaseURL, h.loadSavedServiceConfig(r))
	info.RestartRequired = serviceRestartRequired(info)

	return info
}

// loadSavedServiceConfig 读 `service` 分类里已持久化的键（读不到返回 nil）。
func (h *AdminHandler) loadSavedServiceConfig(r *http.Request) map[string]interface{} {
	s := h.ensureSettingsStore()
	if s == nil {
		return nil
	}
	m, err := s.LoadConfig(r.Context(), settingsCategoryService)
	if err != nil {
		return nil
	}
	return m
}

// savedServiceConfig 决定"已保存值"该怎么展示。
//
// 这是本页最容易骗人的一处：**键存在就采用，包括空串**。若按"非空才采用"回落成
// 生效值，用户显式清空对外地址后页面会显示"没改过"，而库里其实已经是空 ——
// 于是他会反复保存、反复看起来没生效。bind_host 的归一也在这里做，
// 让"存的是空串、生效的是 0.0.0.0"不会显示成一次待生效的改动。
func savedServiceConfig(
	effectiveBindHost, effectivePublicBaseURL string,
	saved map[string]interface{},
) (bindHost, publicBaseURL string) {
	bindHost = effectiveBindHost
	publicBaseURL = effectivePublicBaseURL
	if v, ok := saved["bind_host"].(string); ok {
		bindHost = normalizeBindHost(v)
	}
	if v, ok := saved["public_base_url"].(string); ok {
		publicBaseURL = strings.TrimSpace(v)
	}
	return bindHost, publicBaseURL
}

// serviceRestartRequired 判断"已保存"与"生效中"是否已经分叉。
//
// 两项都只被进程读一次：`bind_host` 绑在套接字上，`public_base_url` 决定启动时推导的
// `CookieSecure`。任一项不同都必须重启网关。端口不在本页可改范围，不参与比较。
func serviceRestartRequired(info AdminServiceInfo) bool {
	return info.SavedBindHost != info.BindHost ||
		info.SavedPublicBaseURL != info.PublicBaseURL
}

// SaveServiceInfo PUT /v1/admin/service —— 保存服务配置。
//
// 请求体：`{"bind_host": "127.0.0.1", "public_base_url": "https://chat.example.com"}`。
// 两个字段都用指针接收：空串是**合法值**（bind_host 空 = 全部网卡；
// public_base_url 空 = 未配置），不能与"字段缺席"混为一谈。
func (h *AdminHandler) SaveServiceInfo(w http.ResponseWriter, r *http.Request) {
	var body struct {
		BindHost      *string `json:"bind_host"`
		PublicBaseURL *string `json:"public_base_url"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	cfgMap := map[string]interface{}{}
	if body.BindHost != nil {
		host, err := normalizeBindHostInput(*body.BindHost)
		if err != nil {
			BadRequest(w, err.Error())
			return
		}
		cfgMap["bind_host"] = host
	}
	if body.PublicBaseURL != nil {
		u, err := normalizePublicBaseURL(*body.PublicBaseURL)
		if err != nil {
			BadRequest(w, err.Error())
			return
		}
		cfgMap["public_base_url"] = u
	}
	if len(cfgMap) == 0 {
		BadRequest(w, "nothing to save: provide bind_host and/or public_base_url")
		return
	}

	// 保存前把"会变成什么样"算清楚：对外地址是 https 还是 http，直接决定 Cookie
	// 带不带 Secure，也就决定局域网里能不能保持登录；而只听回环却配了对外地址，
	// 等于对外发一个连不上的地址。这两件事都该在保存时就摆明。
	effectiveHost := h.cfg.BindHost
	if v, ok := cfgMap["bind_host"].(string); ok {
		effectiveHost = v
	}
	effectiveURL := h.cfg.PublicBaseURL
	if v, ok := cfgMap["public_base_url"].(string); ok {
		effectiveURL = v
	}
	warning := serviceConfigWarning(effectiveHost, effectiveURL)

	s := h.ensureSettingsStore()
	if s == nil {
		InternalError(w, "settings store unavailable")
		return
	}
	userID := ""
	if claims := auth.GetClaims(r.Context()); claims != nil {
		userID = claims.ID
	}
	if err := s.SaveConfig(r.Context(), settingsCategoryService, cfgMap, userID); err != nil {
		if err == settings.ErrEncryptedKeyNotFound {
			InternalError(w, "APP_SECRET not configured; cannot encrypt sensitive settings")
			return
		}
		InternalError(w, "failed to save service settings")
		return
	}

	// 跨副本广播：监听地址不可能热换（见文件头），但保存仍需广播 ——
	// 否则多个副本各自展示不同的"已保存值"，页面上的"需要重启"会自相矛盾。
	if err := PublishSettingsChanged(r.Context(), settingsCategoryService, cfgMap); err != nil {
		// 与其它分类同口径：广播失败不影响保存结果。
		slog.Warn("publish service settings changed failed", "error", err)
	}
	slog.Info("service settings saved", "keys", len(cfgMap))

	// 返回保存后的事实，让页面不必再猜是否需要重启。
	OK(w, h.buildServiceInfo(r, warning))
}

// serviceConfigWarning 描述"这组配置会带来什么后果"。
//
// 只在**确定会咬人**的组合上给出：一条总是出现的提示会被用户学会忽略，
// 而这里两条各自对应一次真实的"配了之后打不开 / 登不上"。
func serviceConfigWarning(bindHost, publicBaseURL string) string {
	host := normalizeBindHost(bindHost)
	urlLower := strings.ToLower(strings.TrimSpace(publicBaseURL))

	// ① 只监听本机却配了对外域名 ⇒ 域名指向的机器上没有任何东西在听那个端口。
	if host == bindLoopback && urlLower != "" {
		return "bind_host 为 127.0.0.1：其它设备无法访问该对外地址。" +
			"要开放局域网/公网访问，请把监听地址改为 0.0.0.0 并重启网关。"
	}
	// ② https 对外地址会打开 Cookie Secure：此时仍用 http 访问的设备
	//    发不出 cookie，表现为"登录了但立刻又变成未登录"。
	if strings.HasPrefix(urlLower, "https://") {
		return "对外地址为 https，Cookie 将带 Secure：若仍有设备用 http 访问，登录态不会被携带。"
	}
	return ""
}

// normalizeBindHost 把各种"全网卡"写法归一成 0.0.0.0，便于比较与展示。
func normalizeBindHost(host string) string {
	h := strings.TrimSpace(host)
	h = strings.TrimPrefix(h, "[")
	h = strings.TrimSuffix(h, "]")
	if h == "" || h == "::" || h == "*" {
		return bindAllInterfaces
	}
	return h
}

// normalizeBindHostInput 校验页面提交的绑定地址。
//
// 只接受 IP 字面量或空串：主机名要靠 DNS 解析，而解析结果可能在启动时并不存在 →
// 网关起不来，且错误发生在用户改完这一页并重启之后。空串与 0.0.0.0 同义。
func normalizeBindHostInput(raw string) (string, error) {
	h := strings.TrimSpace(raw)
	h = strings.TrimPrefix(h, "[")
	h = strings.TrimSuffix(h, "]")
	if h == "" || h == "*" {
		return bindAllInterfaces, nil
	}
	ip := net.ParseIP(h)
	if ip == nil {
		return "", invalidInput("bind_host must be an IP address such as 127.0.0.1 or 0.0.0.0")
	}
	if ip4 := ip.To4(); ip4 != nil {
		return ip4.String(), nil
	}
	return ip.String(), nil
}

// normalizePublicBaseURL 校验对外地址。
//
// 必须是 http/https 且带主机名：它决定 Cookie 的 Secure 推导，裸 `example.com`
// 会被 Parse 成没有 host 的相对引用，`ftp://` 则推不出任何有意义的判定。
// 末尾 `/` 去掉，避免与后续路径拼出 `//`。
func normalizePublicBaseURL(raw string) (string, error) {
	v := strings.TrimSpace(raw)
	if v == "" {
		return "", nil // 清空 = 未配置，合法
	}
	u, err := url.Parse(v)
	if err != nil {
		return "", invalidInput("public_base_url is not a valid URL")
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", invalidInput("public_base_url must start with http:// or https://")
	}
	if u.Host == "" {
		return "", invalidInput("public_base_url must include a host, e.g. https://chat.example.com")
	}
	return strings.TrimRight(u.String(), "/"), nil
}

// invalidInput 让校验函数返回可读错误（handler 直接把它当 400 的文案）。
type invalidInput string

func (e invalidInput) Error() string { return string(e) }

// localIPv4Addrs 列出本机非回环 IPv4 地址（供页面提示"用这个地址访问"）。
func localIPv4Addrs() []string {
	out := []string{}
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return out
	}
	for _, a := range addrs {
		ipnet, ok := a.(*net.IPNet)
		if !ok || ipnet.IP.IsLoopback() {
			continue
		}
		ip4 := ipnet.IP.To4()
		if ip4 == nil {
			continue
		}
		out = append(out, ip4.String())
	}
	sort.Strings(out)
	return out
}
