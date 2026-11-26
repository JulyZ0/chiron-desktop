package api

// openai_compat.go —— OpenAI 兼容对话端点（`POST /v1/chat/completions`）。
//
// ## 为什么需要它
//
// 用户在「设置 → API Keys」里能创建 `sk-` 开头的凭据，但在此之前**没有任何端点
// 能真正用它跑一次推理**：`GET /v1/models` 只列清单，`POST /v1/desktop/infer`
// 是本系统私有的片段推理形状，`POST /v1/chat/submit` 是 Agent 运行时的异步 SSE
// 协议。三者都不是标准大模型 API 的形状，于是"发了 key，却没有任何接口能用它"。
//
// 本端点做**形状适配**：推理仍走 Python 引擎的 `GatewayRouter`（同一套 provider
// 选择、缓存、预算），网关不新写推理逻辑 —— 多一条路径就多一处口径漂移。
//
// ## 计费口径
//
// 与 `/v1/chat/submit` **同一张表、同一套定价**：`usage_events` +
// `usageCostCents`（`model_prices` 单价 × token）。引擎侧不记账（它会再记一份，
// 重复计费），用量随响应体回到这里落地。
//
// `DeductTokens` / `RecordTokenUsage` 与 `billing_records` / `credit_transactions`
// 已随计费域整体删除，`usage_events` 是仓库里唯一活着的用量事实源
// （`GET /v1/billing/usage` 也读它）。要改成"扣 credits"必须先恢复那些表并定口径。
//
// ## 形状
//
// 请求/响应体**原样透传**，不包 `{success, data}`：任何 OpenAI SDK 都不认那层
// 外壳，包了就等于"这个兼容层只对本仓库前端有效"。流式帧同理逐帧转发，只在旁路
// 读一份 `usage` 用于记账。

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/id"
)

const (
	// openAICompletionsPath 是引擎侧路径（见 python-engine/app/api/openai_compat.py）。
	openAICompletionsPath = "/v1/chat/completions"

	// openAIMaxBodyBytes 请求体上限（16 MiB）。与 desktop_infer 同口径：业务上限
	// 是引擎侧的 2M 字符，按 UTF-8 最坏 4 字节算约 8 MB，留一倍余量。
	openAIMaxBodyBytes = 16 << 20

	// openAIMaxResponseBytes 非流式响应体的读取上限。回答本身受 max_tokens 约束，
	// 正常远小于此；设上限只是防止上游异常时把整段内容读进内存。
	openAIMaxResponseBytes = 32 << 20

	// openAIStreamWriteTimeout 单帧写的期限。
	//
	// 客户端不读（接收窗口长期为满）或连接半开时，无期限的 Write 会永久阻塞并占住
	// goroutine 与 fd。与 events.go 的 sseWriteTimeout 同因同治。
	openAIStreamWriteTimeout = 30 * time.Second
)

// openAIChatMessage 是一条输入消息（只解析会校验的字段）。
type openAIChatMessage struct {
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content"`
}

// openAIChatRequest 是请求体里网关**需要看懂**的部分。
//
// 其余字段（`top_p` / `stop` / `presence_penalty` …）不声明也就不消费：它们会被
// 原样透传给引擎，引擎再按自己的白名单决定用不用。兼容层的价值在于"现有客户端
// 能直接接上"，在这里逐字段建白名单只会把本来能用的请求挡在门外。
type openAIChatRequest struct {
	Model               string              `json:"model"`
	Messages            []openAIChatMessage `json:"messages"`
	Stream              bool                `json:"stream"`
	Tools               []json.RawMessage   `json:"tools"`
	ToolChoice          json.RawMessage     `json:"tool_choice"`
	N                   *int                `json:"n"`
	MaxTokens           *int                `json:"max_tokens"`
	MaxCompletionTokens *int                `json:"max_completion_tokens"`
}

// openAIUsage 是响应体里的用量块（非流式 `usage` / 流式最后那个 chunk 的 `usage`）。
type openAIUsage struct {
	PromptTokens        int `json:"prompt_tokens"`
	CompletionTokens    int `json:"completion_tokens"`
	PromptTokensDetails struct {
		CachedTokens int `json:"cached_tokens"`
	} `json:"prompt_tokens_details"`
}

// openAIResponseProbe 只挑出记账需要的字段。
//
// 用独立结构体而不是把整个响应建模：响应形状由 OpenAI 契约决定并会演进（各家网关
// 还会塞私有字段），网关只负责"别弄丢它"，不该跟着建模。
type openAIResponseProbe struct {
	Model   string            `json:"model"`
	Usage   *openAIUsage      `json:"usage"`
	Error   *openAIError      `json:"error"`
	Choices []json.RawMessage `json:"choices"`
}

// openAIError 是 OpenAI 形状的错误体（`{"error": {...}}`）。
type openAIError struct {
	Message string `json:"message"`
	Type    string `json:"type"`
	Code    string `json:"code"`
}

// writeOpenAIError 按 OpenAI 契约写一个错误响应。
//
// 不能复用 `BadRequest`/`Unauthorized`：它们会包 `{success,error}` 外壳并顺带写入
// i18n `code`。OpenAI SDK 只读 `error.message`，别的形状会让它把整段 JSON 塞进
// 异常文本 —— 用户看到一串 payload，却看不到"为什么失败"。
func writeOpenAIError(w http.ResponseWriter, status int, message, errType, code string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"error": openAIError{Message: message, Type: errType, Code: code},
	})
}

// OpenAICompletionsHandler 把 OpenAI 兼容的对话请求代理给 Python 引擎。
//
// 鉴权与限流由外层 `authMW(rlMW(...))` 负责：API Key（`X-API-Key` 或
// `Authorization: Bearer sk-...`）已经在那里换成 (user_id, tenant_id)。
func OpenAICompletionsHandler(pythonClient *engine.PythonClient) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// 身份先判：未认证的请求不该看到"引擎可用性"这类内部状态。
		claims := auth.GetClaims(r.Context())
		if claims == nil || claims.UserID == "" || claims.TenantID == 0 {
			writeOpenAIError(w, http.StatusUnauthorized,
				"missing tenant context", "invalid_request_error", "unauthenticated")
			return
		}
		// 日费用熔断（R7）：API Key 直连同样受租户/用户日预算约束。
		if scope, used, limit := costBreakerExceeded(r.Context(), claims.TenantID, claims.UserID); scope != "" {
			writeOpenAIError(w, http.StatusTooManyRequests,
				fmt.Sprintf("daily cost budget exceeded for this %s (today: %d / %d cents)", scope, used, limit),
				"rate_limit_error", "daily_cost_limit_exceeded")
			return
		}
		if pythonClient == nil {
			writeOpenAIError(w, http.StatusServiceUnavailable,
				"inference engine is not available", "server_error", "engine_unavailable")
			return
		}
		if r.ContentLength > openAIMaxBodyBytes {
			writeOpenAIError(w, http.StatusRequestEntityTooLarge,
				"request body too large", "invalid_request_error", "body_too_large")
			return
		}

		raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, openAIMaxBodyBytes))
		if err != nil {
			writeOpenAIError(w, http.StatusBadRequest,
				"failed to read request body", "invalid_request_error", "invalid_body")
			return
		}

		var req openAIChatRequest
		if err := json.Unmarshal(raw, &req); err != nil {
			writeOpenAIError(w, http.StatusBadRequest,
				"request body is not valid JSON: "+err.Error(), "invalid_request_error", "invalid_body")
			return
		}
		if denied, msg, code := validateOpenAIRequest(&req); denied {
			writeOpenAIError(w, http.StatusBadRequest, msg, "invalid_request_error", code)
			return
		}

		// 身份经 query 透传给引擎（引擎 AuthMiddleware 的网关代理路径要求
		// `?user_id=&tenant_id=` 与网关注入的 X-Internal-Token 同时存在）。
		// 由已验证的 claims **重建**，客户端传什么都不作数。
		target := openAICompletionsPath +
			"?user_id=" + url.QueryEscape(claims.UserID) +
			"&tenant_id=" + tenantStr(claims.TenantID)

		// 审计只记元信息。**不记 messages** —— 那是用户的对话内容。
		slog.Info("openai completions",
			"tenant_id", claims.TenantID,
			"user_id", claims.UserID,
			"model", req.Model,
			"messages", len(req.Messages),
			"stream", req.Stream,
		)

		if req.Stream {
			proxyOpenAIStream(w, r, pythonClient, target, raw, claims)
			return
		}
		proxyOpenAIOnce(w, r, pythonClient, target, raw, claims)
	}
}

// validateOpenAIRequest 拒绝**无法正确满足**的请求。
//
// 静默忽略这些参数会让调用方在它自己的解析分支里出错（"我要求了工具调用，却收到
// 一段闲聊"），比这里直接说清楚难查得多。
func validateOpenAIRequest(req *openAIChatRequest) (denied bool, msg, code string) {
	if req.Model == "" {
		return true, "model is required", "model_required"
	}
	if len(req.Messages) == 0 {
		return true, "messages must not be empty", "messages_required"
	}
	if len(req.Tools) > 0 || len(bytes.TrimSpace(req.ToolChoice)) > 0 {
		return true, "tools/tool_choice are not supported by this endpoint yet; " +
			"send a plain chat request", "tools_not_supported"
	}
	if req.N != nil && *req.N > 1 {
		return true, "n > 1 is not supported; send one choice per request", "n_not_supported"
	}
	return false, "", ""
}

// proxyOpenAIOnce 处理非流式请求：读完整响应 → 记账 → 原样写回。
func proxyOpenAIOnce(
	w http.ResponseWriter,
	r *http.Request,
	pythonClient *engine.PythonClient,
	target string,
	body []byte,
	claims *auth.Claims,
) {
	resp, err := pythonClient.PostResponse(r.Context(), target, body)
	if err != nil {
		slog.Error("openai completions: engine call failed", "error", err)
		writeOpenAIError(w, http.StatusBadGateway,
			"upstream inference failed", "upstream_error", "upstream_failed")
		return
	}
	defer resp.Body.Close()

	payload, readErr := io.ReadAll(io.LimitReader(resp.Body, openAIMaxResponseBytes))
	if readErr != nil {
		slog.Error("openai completions: read engine response failed", "error", readErr)
		writeOpenAIError(w, http.StatusBadGateway,
			"upstream inference failed", "upstream_error", "upstream_failed")
		return
	}

	// 只重整 Content-Type：上游的 Set-Cookie 之类不该经这条路径流回客户端。
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(resp.StatusCode)
	if _, err := w.Write(payload); err != nil {
		slog.Debug("openai completions: write response failed", "error", err)
	}

	// 记账放在**写完响应之后**：写库慢不该让用户多等，写失败也只告警
	// （RecordUsageEvent 自身保证不返回错误）。
	if resp.StatusCode == http.StatusOK {
		var probe openAIResponseProbe
		if err := json.Unmarshal(payload, &probe); err == nil {
			recordOpenAIUsage(r, claims, probe.Model, probe.Usage)
		} else {
			slog.Warn("openai completions: unparsable response, usage not recorded",
				"error", err)
		}
	}
}

// proxyOpenAIStream 处理流式请求：逐帧转发，旁路取最后一帧的 usage 记账。
func proxyOpenAIStream(
	w http.ResponseWriter,
	r *http.Request,
	pythonClient *engine.PythonClient,
	target string,
	body []byte,
	claims *auth.Claims,
) {
	// 流式请求不能带整体超时（会硬截断长回答），由 request ctx 控制生命周期。
	resp, err := pythonClient.PostStreamResponse(r.Context(), target, body)
	if err != nil {
		slog.Error("openai completions: engine stream call failed", "error", err)
		writeOpenAIError(w, http.StatusBadGateway,
			"upstream inference failed", "upstream_error", "upstream_failed")
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		// 流还没开始，状态码还改得动：把上游的错误体原样交给客户端
		// （引擎对参数错误会返回 OpenAI 形状的 400/422，透传比重新包装更准）。
		payload, _ := io.ReadAll(io.LimitReader(resp.Body, openAIMaxResponseBytes))
		if ct := resp.Header.Get("Content-Type"); ct != "" {
			w.Header().Set("Content-Type", ct)
		} else {
			w.Header().Set("Content-Type", "application/json; charset=utf-8")
		}
		w.WriteHeader(resp.StatusCode)
		_, _ = w.Write(payload)
		return
	}

	rc := http.NewResponseController(w)
	// SSE 长连接豁免服务器 WriteTimeout（默认会切断流）；豁免是逐帧完成的，
	// 见下方每一帧前的 SetWriteDeadline。
	if err := rc.SetWriteDeadline(time.Time{}); err != nil {
		slog.Warn("openai stream: cannot exempt write timeout (long streams will be cut)", "error", err)
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)

	reader := bufio.NewReaderSize(resp.Body, 64*1024)
	var probe openAIStreamProbe

	for {
		line, readErr := reader.ReadBytes('\n')
		if len(line) > 0 {
			// 旁路解析：只在 `data: {...}` 且能解析时更新用量。解析失败是正常的
			// （`[DONE]`、注释行、半截帧），不该影响转发。
			probeStreamFrame(line, &probe)

			_ = rc.SetWriteDeadline(time.Now().Add(openAIStreamWriteTimeout))
			if _, werr := w.Write(line); werr != nil {
				slog.Debug("openai completions: stream write failed, closing", "error", werr)
				break
			}
			if len(bytes.TrimSpace(line)) == 0 {
				// 空行 = SSE 帧结束：只有这时才 flush，否则每个换行都推一次。
				if ferr := rc.Flush(); ferr != nil {
					slog.Debug("openai completions: stream flush failed, closing", "error", ferr)
					break
				}
			}
		}
		if readErr != nil {
			if readErr != io.EOF {
				slog.Warn("openai completions: stream read stopped", "error", readErr)
			}
			break
		}
	}

	recordOpenAIUsage(r, claims, probe.model(), probe.usage())
}

// openAIStreamProbe 累积流式响应里的记账信息。
//
// 为什么要跨帧累积：OpenAI 的流式响应**默认不返回 usage**，要显式请求
// `stream_options.include_usage` 才有，而那一帧是**独立的一帧**（`choices` 为空）；
// 引擎侧已经补发它（见 openai_compat.py 的 `_stream`），但帧序不保证 usage 与
// model 在同一帧里 —— 分别记住最后看到的值才是稳的。
type openAIStreamProbe struct {
	modelName string
	usageData *openAIUsage
}

func (p *openAIStreamProbe) model() string {
	if p == nil {
		return ""
	}
	return p.modelName
}

func (p *openAIStreamProbe) usage() *openAIUsage {
	if p == nil {
		return nil
	}
	return p.usageData
}

// probeStreamFrame 从一帧 SSE 里取出 model 与 usage。
//
// 只认 `data: {...}` 行，解析失败**静默跳过**：`data: [DONE]`、注释行、心跳、
// 半截帧都是正常现象，为此告警只会把日志刷满。这个函数纯旁路，任何情况下都不该
// 影响转发。
func probeStreamFrame(line []byte, probe *openAIStreamProbe) {
	trimmed := bytes.TrimSpace(line)
	if !bytes.HasPrefix(trimmed, []byte("data:")) {
		return
	}
	payload := bytes.TrimSpace(trimmed[len("data:"):])
	if len(payload) == 0 || payload[0] != '{' {
		// `[DONE]` 或空帧
		return
	}
	var frame struct {
		Model string       `json:"model"`
		Usage *openAIUsage `json:"usage"`
	}
	if err := json.Unmarshal(payload, &frame); err != nil {
		return
	}
	if frame.Model != "" {
		probe.modelName = frame.Model
	}
	if frame.Usage != nil {
		probe.usageData = frame.Usage
	}
}

// recordOpenAIUsage 把一次调用的用量写进 `usage_events`。
//
// 口径与 `/v1/chat/submit` 完全一致（见 submit_handler.go 的结算段）：
//
//   - 一行一次调用，`cost_cents` 由 `usageCostCents` 按 `model_prices` 单价算；
//   - 取不到单价时写 NULL（"不知道"），而不是 0（"免费"）；
//   - 零用量也落一行：调用次数要真实，token 为 0 只表示"这次没拿到用量"。
//
// `session_id` 取自可选的 `X-Session-Id` 请求头：外部客户端往往有自己的会话概念，
// 带上它就能和 `usage_events` 里别的行对上；不带就是空串（本端点的常态）。
func recordOpenAIUsage(
	r *http.Request,
	claims *auth.Claims,
	model string,
	usage *openAIUsage,
) {
	input, output, cached := 0, 0, 0
	if usage != nil {
		input = usage.PromptTokens
		output = usage.CompletionTokens
		cached = usage.PromptTokensDetails.CachedTokens
	}

	// 独立 ctx：请求 ctx 在响应写完后可能已取消，而记账必须落地。
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	sessionID := r.Header.Get("X-Session-Id")
	RecordUsageEvent(ctx, UsageEvent{
		TenantID:     claims.TenantID,
		UserID:       claims.UserID,
		SessionID:    sessionID,
		TurnID:       id.NextID(),
		Model:        model,
		InputTokens:  input,
		OutputTokens: output,
		CachedTokens: cached,
		CostCents:    usageCostCents(ctx, model, input, output, cached),
	})
}

// registerOpenAICompatRoutes 注册 OpenAI 兼容端点。
//
// 鉴权口径与 `/v1/chat/submit` 一致：**authMW + rlMW**。这道推理会真的产生上游
// 费用，少一层限流就等于给每个持 key 的客户端开了一条不限速的打上游通道。
func registerOpenAICompatRoutes(
	mux *http.ServeMux,
	authMW, rlMW routeMiddleware,
	pythonClient *engine.PythonClient,
) {
	mux.Handle("POST /v1/chat/completions", authMW(rlMW(OpenAICompletionsHandler(pythonClient))))
}
