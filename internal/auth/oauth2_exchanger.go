package auth

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// ── OAuth2 Exchanger（GitHub / 微信 / 钉钉 / 飞书 / QQ）──
//
// 实现 OIDCExchanger 同一接口：SSOHandler 按 provider.protocol 分派到
// OIDC（go-oidc 发现）或本实现（显式端点 + provider 特有身份抽取）。
// 端点可被数据库覆盖（cfg.AuthURL/TokenURL/UserinfoURL 非空优先），
// 因此单测可用 httptest 服务器注入全部端点，无需真实凭据。

// qqOpenIDURL QQ 获取 openid 的固定端点。
const qqOpenIDURL = "https://graph.qq.com/oauth2.0/me"

// OAuth2Exchanger 是纯 OAuth2 协议的授权码交换实现。
type OAuth2Exchanger struct {
	client *http.Client
}

// NewOAuth2Exchanger 构造交换器（15s 超时）。
func NewOAuth2Exchanger() *OAuth2Exchanger {
	return &OAuth2Exchanger{client: &http.Client{Timeout: 15 * time.Second}}
}

// AuthURL 构造授权页跳转 URL。
func (e *OAuth2Exchanger) AuthURL(ctx context.Context, p *OIDCProviderConfig, state, nonce string) (string, error) {
	if p.AuthURL == "" {
		return "", fmt.Errorf("oauth2: provider %q has no auth_url", p.ProviderType)
	}
	base := p.AuthURL
	scopes := p.Scopes
	extra := url.Values{}

	switch p.ProviderType {
	case ProviderWeChat:
		// 微信用 appid 而非 client_id；扫码模式追加 #wechat_redirect
		base = wechatEffectiveAuthURL(p.AuthURL, p.Extra)
		if p.Extra["mode"] == "mp" {
			scopes = []string{"snsapi_userinfo"}
		}
		extra.Set("appid", p.ClientID)
	case ProviderDingTalk:
		// 钉钉用 client_id + prompt=consent
		extra.Set("client_id", p.ClientID)
		extra.Set("prompt", "consent")
	default:
		// github/feishu/qq 用标准参数
	}
	return buildAuthURLSpecial(base, p.ClientID, p.RedirectURL, scopes, state, extra, p.ProviderType), nil
}

// buildAuthURLSpecial 在通用构造基础上处理各 provider 参数名差异。
func buildAuthURLSpecial(base, clientID, redirectURL string, scopes []string, state string, extra url.Values, providerType string) string {
	q := url.Values{}
	if extra.Has("appid") {
		q.Set("appid", clientID) // 微信
	} else if extra.Has("client_id") {
		q.Set("client_id", clientID) // 钉钉（同名，走通用键）
	} else {
		q.Set("client_id", clientID) // github/feishu/qq 标准
	}
	q.Set("redirect_uri", redirectURL)
	q.Set("response_type", "code")
	if len(scopes) > 0 {
		q.Set("scope", strings.Join(scopes, " "))
	}
	q.Set("state", state)
	for k := range extra {
		if k == "appid" || k == "client_id" {
			continue
		}
		q.Set(k, extra.Get(k))
	}
	sep := "?"
	if strings.Contains(base, "?") {
		sep = "&"
	}
	u := base + sep + q.Encode()
	if providerType == ProviderWeChat {
		u += "#wechat_redirect"
	}
	return u
}

// ExchangeAndVerify 授权码换 access_token → 拉取用户身份。
// OAuth2 协议无 id_token/nonce，expectedNonce 由 state HMAC 承担防重放。
func (e *OAuth2Exchanger) ExchangeAndVerify(ctx context.Context, p *OIDCProviderConfig, code, expectedNonce string) (*IDTokenResult, error) {
	if p.TokenURL == "" || p.UserinfoURL == "" {
		return nil, fmt.Errorf("oauth2: provider %q missing token_url/userinfo_url", p.ProviderType)
	}
	accessToken, err := e.exchangeToken(ctx, p, code)
	if err != nil {
		return nil, err
	}
	return e.fetchIdentity(ctx, p, accessToken)
}

// exchangeToken 各 provider 授权码 → access_token。
func (e *OAuth2Exchanger) exchangeToken(ctx context.Context, p *OIDCProviderConfig, code string) (string, error) {
	switch p.ProviderType {
	case ProviderGitHub:
		return e.exchangeGitHub(ctx, p, code)
	case ProviderWeChat:
		return e.exchangeWeChat(ctx, p, code)
	case ProviderDingTalk:
		return e.exchangeDingTalk(ctx, p, code)
	case ProviderFeishu:
		return e.exchangeFeishu(ctx, p, code)
	case ProviderQQ:
		return e.exchangeQQ(ctx, p, code)
	default:
		return "", fmt.Errorf("oauth2: no token exchange for provider %q", p.ProviderType)
	}
}

// exchangeGitHub: POST form（client_id/secret/code/redirect_uri）+ Accept: JSON。
func (e *OAuth2Exchanger) exchangeGitHub(ctx context.Context, p *OIDCProviderConfig, code string) (string, error) {
	form := url.Values{}
	form.Set("client_id", p.ClientID)
	form.Set("client_secret", p.ClientSecret)
	form.Set("code", code)
	form.Set("redirect_uri", p.RedirectURL)
	data, err := e.do(ctx, http.MethodPost, p.TokenURL, "application/x-www-form-urlencoded",
		strings.NewReader(form.Encode()), map[string]string{"Accept": "application/json"})
	if err != nil {
		return "", err
	}
	var resp struct {
		AccessToken      string `json:"access_token"`
		Error            string `json:"error"`
		ErrorDescription string `json:"error_description"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return "", fmt.Errorf("oauth2 github: malformed token response: %w", err)
	}
	if resp.AccessToken == "" {
		return "", fmt.Errorf("oauth2 github: token exchange failed: %s %s", resp.Error, resp.ErrorDescription)
	}
	return resp.AccessToken, nil
}

// exchangeWeChat: GET token_url?appid&secret&code&grant_type。
func (e *OAuth2Exchanger) exchangeWeChat(ctx context.Context, p *OIDCProviderConfig, code string) (string, error) {
	q := url.Values{}
	q.Set("appid", p.ClientID)
	q.Set("secret", p.ClientSecret)
	q.Set("code", code)
	q.Set("grant_type", "authorization_code")
	sep := "?"
	if strings.Contains(p.TokenURL, "?") {
		sep = "&"
	}
	data, err := e.do(ctx, http.MethodGet, p.TokenURL+sep+q.Encode(), "", nil, nil)
	if err != nil {
		return "", err
	}
	var resp struct {
		AccessToken string `json:"access_token"`
		OpenID      string `json:"openid"`
		ErrCode     int    `json:"errcode"`
		ErrMsg      string `json:"errmsg"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return "", fmt.Errorf("oauth2 wechat: malformed token response: %w", err)
	}
	if resp.AccessToken == "" {
		return "", fmt.Errorf("oauth2 wechat: token exchange failed: errcode=%d errmsg=%s", resp.ErrCode, resp.ErrMsg)
	}
	return resp.AccessToken + "|" + resp.OpenID, nil // openid 随 token 传递
}

// exchangeDingTalk: POST JSON {clientId, clientSecret, code, grantType}。
func (e *OAuth2Exchanger) exchangeDingTalk(ctx context.Context, p *OIDCProviderConfig, code string) (string, error) {
	payload, _ := json.Marshal(map[string]string{
		"clientId":     p.ClientID,
		"clientSecret": p.ClientSecret,
		"code":         code,
		"grantType":    "authorization_code",
	})
	data, err := e.do(ctx, http.MethodPost, p.TokenURL, "application/json", strings.NewReader(string(payload)), nil)
	if err != nil {
		return "", err
	}
	var resp struct {
		AccessToken string `json:"accessToken"`
		ErrCode     int    `json:"code"`
		ErrMsg      string `json:"message"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return "", fmt.Errorf("oauth2 dingtalk: malformed token response: %w", err)
	}
	if resp.AccessToken == "" {
		return "", fmt.Errorf("oauth2 dingtalk: token exchange failed: code=%d message=%s", resp.ErrCode, resp.ErrMsg)
	}
	return resp.AccessToken, nil
}

// exchangeFeishu: POST JSON {app_id, app_secret, code, grant_type}，响应兼容顶层/data 两种形态。
func (e *OAuth2Exchanger) exchangeFeishu(ctx context.Context, p *OIDCProviderConfig, code string) (string, error) {
	payload, _ := json.Marshal(map[string]string{
		"app_id":     p.ClientID,
		"app_secret": p.ClientSecret,
		"code":       code,
		"grant_type": "authorization_code",
	})
	data, err := e.do(ctx, http.MethodPost, p.TokenURL, "application/json", strings.NewReader(string(payload)), nil)
	if err != nil {
		return "", err
	}
	var resp map[string]any
	if err := json.Unmarshal(data, &resp); err != nil {
		return "", fmt.Errorf("oauth2 feishu: malformed token response: %w", err)
	}
	token := firstString(resp, "access_token", "data.access_token")
	if token == "" {
		return "", fmt.Errorf("oauth2 feishu: token exchange failed: %s", snippet(data))
	}
	return token, nil
}

// exchangeQQ: GET token_url?client_id&client_secret&code&redirect_uri&fmt=json。
func (e *OAuth2Exchanger) exchangeQQ(ctx context.Context, p *OIDCProviderConfig, code string) (string, error) {
	q := url.Values{}
	q.Set("client_id", p.ClientID)
	q.Set("client_secret", p.ClientSecret)
	q.Set("code", code)
	q.Set("redirect_uri", p.RedirectURL)
	q.Set("fmt", "json")
	sep := "?"
	if strings.Contains(p.TokenURL, "?") {
		sep = "&"
	}
	data, err := e.do(ctx, http.MethodGet, p.TokenURL+sep+q.Encode(), "", nil, nil)
	if err != nil {
		return "", err
	}
	var resp struct {
		AccessToken      string `json:"access_token"`
		Error            int    `json:"error"`
		ErrorDescription string `json:"error_description"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return "", fmt.Errorf("oauth2 qq: malformed token response: %w", err)
	}
	if resp.AccessToken == "" {
		return "", fmt.Errorf("oauth2 qq: token exchange failed: error=%d %s", resp.Error, resp.ErrorDescription)
	}
	return resp.AccessToken, nil
}

// fetchIdentity 按 provider 拉取 userinfo 并抽取身份。
func (e *OAuth2Exchanger) fetchIdentity(ctx context.Context, p *OIDCProviderConfig, accessToken string) (*IDTokenResult, error) {
	switch p.ProviderType {
	case ProviderGitHub:
		return e.identityGitHub(ctx, p, accessToken)
	case ProviderWeChat:
		return e.identityWeChat(ctx, p, accessToken)
	case ProviderDingTalk:
		return e.identityDingTalk(ctx, p, accessToken)
	case ProviderFeishu:
		return e.identityFeishu(ctx, p, accessToken)
	case ProviderQQ:
		return e.identityQQ(ctx, p, accessToken)
	default:
		return nil, fmt.Errorf("oauth2: no identity fetch for provider %q", p.ProviderType)
	}
}

// identityGitHub: GET /user（Bearer）；email 缺失时补查 /user/emails。
func (e *OAuth2Exchanger) identityGitHub(ctx context.Context, p *OIDCProviderConfig, accessToken string) (*IDTokenResult, error) {
	headers := map[string]string{
		"Authorization": "Bearer " + accessToken,
		"Accept":        "application/vnd.github+json",
	}
	data, err := e.do(ctx, http.MethodGet, p.UserinfoURL, "", nil, headers)
	if err != nil {
		return nil, err
	}
	var user struct {
		ID        int64  `json:"id"`
		Login     string `json:"login"`
		Name      string `json:"name"`
		Email     string `json:"email"`
		AvatarURL string `json:"avatar_url"`
	}
	if err := json.Unmarshal(data, &user); err != nil {
		return nil, fmt.Errorf("oauth2 github: malformed userinfo: %w", err)
	}
	if user.ID == 0 {
		return nil, errors.New("oauth2 github: userinfo missing id")
	}
	name := user.Name
	if name == "" {
		name = user.Login
	}
	email := user.Email
	if email == "" {
		// /user/emails 补查（公开邮箱常常为 null）
		emailsURL := strings.TrimSuffix(p.UserinfoURL, "/") + "/emails"
		if strings.HasSuffix(p.UserinfoURL, "/user") {
			if data2, err2 := e.do(ctx, http.MethodGet, emailsURL, "", nil, headers); err2 == nil {
				var emails []struct {
					Email   string `json:"email"`
					Primary bool   `json:"primary"`
				}
				if json.Unmarshal(data2, &emails) == nil {
					for _, em := range emails {
						if em.Primary && em.Email != "" {
							email = em.Email
							break
						}
					}
				}
			}
		}
	}
	return &IDTokenResult{
		Subject:   strconv.FormatInt(user.ID, 10),
		Email:     email,
		Name:      name,
		AvatarURL: user.AvatarURL,
	}, nil
}

// identityWeChat: GET userinfo?access_token&openid；subject = unionid 优先。
func (e *OAuth2Exchanger) identityWeChat(ctx context.Context, p *OIDCProviderConfig, accessTokenAndOpenID string) (*IDTokenResult, error) {
	parts := strings.SplitN(accessTokenAndOpenID, "|", 2)
	accessToken, openid := parts[0], ""
	if len(parts) == 2 {
		openid = parts[1]
	}
	if openid == "" {
		return nil, errors.New("oauth2 wechat: openid missing in token response")
	}
	q := url.Values{}
	q.Set("access_token", accessToken)
	q.Set("openid", openid)
	q.Set("lang", "zh_CN")
	sep := "?"
	if strings.Contains(p.UserinfoURL, "?") {
		sep = "&"
	}
	data, err := e.do(ctx, http.MethodGet, p.UserinfoURL+sep+q.Encode(), "", nil, nil)
	if err != nil {
		return nil, err
	}
	var user struct {
		OpenID   string `json:"openid"`
		UnionID  string `json:"unionid"`
		Nickname string `json:"nickname"`
		HeadImg  string `json:"headimgurl"`
		ErrCode  int    `json:"errcode"`
		ErrMsg   string `json:"errmsg"`
	}
	if err := json.Unmarshal(data, &user); err != nil {
		return nil, fmt.Errorf("oauth2 wechat: malformed userinfo: %w", err)
	}
	if user.ErrCode != 0 && user.OpenID == "" {
		return nil, fmt.Errorf("oauth2 wechat: userinfo failed: errcode=%d errmsg=%s", user.ErrCode, user.ErrMsg)
	}
	// unionid 优先：同主体多应用不产生重复身份
	subject := user.UnionID
	if subject == "" {
		subject = user.OpenID
	}
	if subject == "" {
		return nil, errors.New("oauth2 wechat: neither unionid nor openid present")
	}
	return &IDTokenResult{
		Subject:   subject,
		Name:      user.Nickname,
		AvatarURL: user.HeadImg,
	}, nil
}

// identityDingTalk: GET contact/users/me（header x-acs-dingtalk-access-token）。
func (e *OAuth2Exchanger) identityDingTalk(ctx context.Context, p *OIDCProviderConfig, accessToken string) (*IDTokenResult, error) {
	data, err := e.do(ctx, http.MethodGet, p.UserinfoURL, "", nil,
		map[string]string{"x-acs-dingtalk-access-token": accessToken})
	if err != nil {
		return nil, err
	}
	var user struct {
		UnionID   string `json:"unionId"`
		OpenID    string `json:"openId"`
		Nick      string `json:"nick"`
		Email     string `json:"email"`
		Mobile    string `json:"mobile"`
		AvatarURL string `json:"avatarUrl"`
	}
	if err := json.Unmarshal(data, &user); err != nil {
		return nil, fmt.Errorf("oauth2 dingtalk: malformed userinfo: %w", err)
	}
	subject := user.UnionID
	if subject == "" {
		subject = user.OpenID
	}
	if subject == "" {
		return nil, errors.New("oauth2 dingtalk: neither unionId nor openId present")
	}
	return &IDTokenResult{
		Subject:   subject,
		Email:     user.Email,
		Name:      user.Nick,
		AvatarURL: user.AvatarURL,
		Phone:     user.Mobile,
	}, nil
}

// identityFeishu: GET userinfo（Bearer），响应兼容顶层/data 两种形态。
func (e *OAuth2Exchanger) identityFeishu(ctx context.Context, p *OIDCProviderConfig, accessToken string) (*IDTokenResult, error) {
	data, err := e.do(ctx, http.MethodGet, p.UserinfoURL, "", nil,
		map[string]string{"Authorization": "Bearer " + accessToken})
	if err != nil {
		return nil, err
	}
	var resp map[string]any
	if err := json.Unmarshal(data, &resp); err != nil {
		return nil, fmt.Errorf("oauth2 feishu: malformed userinfo: %w", err)
	}
	src := resp
	if nested, ok := resp["data"].(map[string]any); ok {
		src = nested
	}
	subject := firstString(src, "sub", "open_id", "union_id")
	if subject == "" {
		return nil, fmt.Errorf("oauth2 feishu: no subject in userinfo: %s", snippet(data))
	}
	return &IDTokenResult{
		Subject:   subject,
		Email:     firstString(src, "email"),
		Name:      firstString(src, "name", "en_name"),
		AvatarURL: firstString(src, "avatar_url", "avatar"),
		Phone:     firstString(src, "mobile", "phone_number"),
	}, nil
}

// identityQQ: 先 GET /oauth2.0/me 拿 openid，再 GET userinfo。
func (e *OAuth2Exchanger) identityQQ(ctx context.Context, p *OIDCProviderConfig, accessToken string) (*IDTokenResult, error) {
	meURL := qqOpenIDURL + "?access_token=" + url.QueryEscape(accessToken) + "&fmt=json"
	data, err := e.do(ctx, http.MethodGet, meURL, "", nil, nil)
	if err != nil {
		return nil, err
	}
	var me struct {
		ClientID string `json:"client_id"`
		OpenID   string `json:"openid"`
	}
	if err := json.Unmarshal(data, &me); err != nil {
		return nil, fmt.Errorf("oauth2 qq: malformed /me response: %w", err)
	}
	if me.OpenID == "" {
		return nil, errors.New("oauth2 qq: openid missing in /me response")
	}

	q := url.Values{}
	q.Set("access_token", accessToken)
	q.Set("oauth_consumer_key", p.ClientID)
	q.Set("openid", me.OpenID)
	sep := "?"
	if strings.Contains(p.UserinfoURL, "?") {
		sep = "&"
	}
	data, err = e.do(ctx, http.MethodGet, p.UserinfoURL+sep+q.Encode(), "", nil, nil)
	if err != nil {
		return nil, err
	}
	var user struct {
		Ret      int    `json:"ret"`
		Msg      string `json:"msg"`
		Nickname string `json:"nickname"`
		FigureQQ string `json:"figureurl_qq_1"`
	}
	if err := json.Unmarshal(data, &user); err != nil {
		return nil, fmt.Errorf("oauth2 qq: malformed userinfo: %w", err)
	}
	if user.Ret != 0 {
		return nil, fmt.Errorf("oauth2 qq: userinfo failed: ret=%d msg=%s", user.Ret, user.Msg)
	}
	return &IDTokenResult{
		Subject:   me.OpenID, // QQ 无 unionid（除非接互联高级接口）
		Name:      user.Nickname,
		AvatarURL: user.FigureQQ,
	}, nil
}

// ── HTTP 辅助 ──────────────────────────────────────────

func (e *OAuth2Exchanger) do(ctx context.Context, method, rawURL, contentType string, body io.Reader, headers map[string]string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, method, rawURL, body)
	if err != nil {
		return nil, err
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := e.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("oauth2: request %s failed: %w", redactURL(rawURL), err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("oauth2: %s %s -> HTTP %d: %s", method, redactURL(rawURL), resp.StatusCode, snippet(data))
	}
	return data, nil
}

// redactURL 抹去 URL 中的 secret/token/code 参数，防日志泄露。
func redactURL(rawURL string) string {
	u, err := url.Parse(rawURL)
	if err != nil {
		return "<unparseable-url>"
	}
	q := u.Query()
	for key := range q {
		switch key {
		case "secret", "client_secret", "access_token", "code", "app_secret":
			q.Set(key, "***")
		}
	}
	u.RawQuery = q.Encode()
	return u.String()
}

// firstString 依次尝试多个点分路径，返回首个非空字符串。
func firstString(m map[string]any, paths ...string) string {
	for _, path := range paths {
		var cur any = m
		matched := true
		for _, seg := range strings.Split(path, ".") {
			node, isMap := cur.(map[string]any)
			if !isMap {
				matched = false
				break
			}
			v, exists := node[seg]
			if !exists {
				matched = false
				break
			}
			cur = v
		}
		if matched {
			if s, isStr := cur.(string); isStr && s != "" {
				return s
			}
		}
	}
	return ""
}

// snippet 截取响应片段用于错误信息（上限 200 字节）。
func snippet(data []byte) string {
	s := string(data)
	if len(s) > 200 {
		s = s[:200] + "..."
	}
	return s
}
