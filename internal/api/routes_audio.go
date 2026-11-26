package api

// routes_audio.go —— 本地语音转写的网关代理（`/v1/audio/**`）。
//
// 为什么不让渲染进程直连引擎：与 `desktop_infer.go` 完全同因 —— 引擎的
// `AuthMiddleware` 只在 `ENGINE_ALLOW_DIRECT_JWT` 打开时才接受用户 JWT，生产上默认
// 关闭；网关是唯一稳定能把用户身份换成 `(user_id, tenant_id)` 的地方，也是唯一
// 挂了限流与审计的入口。音频是**用户的声音**，更不该多开一条旁路。
//
// 职责边界：本文件只做字节透传。
//   - **不解析音频**，不落盘、不记 body，日志里只有长度与租户/用户；
//   - 不缓存转写结果：同一段音频重发就该重新算（用户改口重说很常见），
//     缓存反而会把"我改了话"变成"还是上次那句"；
//   - 引擎不可用时如实 503，而不是回一个空字符串假装转写成功。

import (
	"log/slog"
	"net/http"
	"net/url"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

const (
	// audioTranscribePath 引擎侧转写路径（见 python-engine/app/api/audio.py）。
	audioTranscribePath = "/v1/audio/transcribe"
	// audioAvailabilityPath 引擎侧能力探测路径（只查依赖与开关，不加载模型）。
	audioAvailabilityPath = "/v1/audio/availability"

	// audioMaxBodyBytes 请求体上限（26 MiB）。
	//
	// 比引擎侧的 `MAX_AUDIO_BYTES`（25 MiB）**略大**：多出来的余量留给 multipart 的
	// boundary 与字段头。这里若与引擎设成同一个数，一段刚好 25 MiB 的录音会在这里
	// 被拒，而错误信息只说"request body too large"—— 用户以为麦克风坏了。
	// 真正的业务上限仍在引擎侧（单一真相，避免两处阈值各说各话）。
	audioMaxBodyBytes = 26 << 20
)

// transcribeTarget 用**已验证的 claims** 重建 query 交给引擎。
//
// 客户端传的身份参数一律不作数：引擎的网关代理路径认 `?user_id=&tenant_id=`，
// 而 `ForwardRequest` 会剥掉客户端自带的同名头/参数由这里覆盖 —— 不重建就等于
// 允许任何登录用户以别人的身份转写（转写本身不落库，但日志与用量归属会错）。
func audioIdentityQuery(claims *auth.Claims) string {
	q := url.Values{}
	q.Set("user_id", claims.UserID)
	q.Set("tenant_id", tenantStr(claims.TenantID))
	return q.Encode()
}

// AudioTranscribeHandler 把一段音频代理给引擎做本地转写。
//
// 上游状态码原样透传，语义分工明确：
//   - 413 音频过大（客户端该缩短录音）；
//   - 503 本机没装 faster-whisper 或开关关掉（可操作：装依赖/改配置）；
//   - 500 解码失败（录音格式不被支持）；
//   - 422 请求形状不对。
//
// 这些处置方式彼此不同，合成一个 500 会让前端只能笼统地说"转写失败"。
func AudioTranscribeHandler(pythonClient *engine.PythonClient) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		claims := auth.GetClaims(r.Context())
		// tenant 必须存在：引擎对 tenant 0 不做隔离（与 desktop_infer 同一条防御）。
		if claims == nil || claims.UserID == "" || claims.TenantID == 0 {
			Unauthorized(w, "missing tenant context")
			return
		}
		if pythonClient == nil {
			ServiceUnavailable(w, "python engine not available")
			return
		}
		// 26MB 录音在慢网/反代下会超过全局 ReadTimeout(10s)：它量的是
		// "从 accept 起读取整个请求体"，与处理时长无关。
		extendUploadReadDeadline(w)
		if r.ContentLength > audioMaxBodyBytes {
			JSON(w, http.StatusRequestEntityTooLarge, APIResponse{Success: false, Error: "audio too large"})
			return
		}
		// Content-Length 缺失（chunked）时同样在读层封顶。
		r.Body = http.MaxBytesReader(w, r.Body, audioMaxBodyBytes)

		// 审计只留元信息：租户、用户、长度。**不记音频内容**，也不记文件名 ——
		// 文件名常是"会议录音-客户名-日期"这类含个人信息的串。
		slog.Info("audio transcribe proxy",
			"tenant_id", claims.TenantID,
			"user_id", claims.UserID,
			"content_length", r.ContentLength,
		)

		pythonClient.ForwardRequest(w, r, audioTranscribePath+"?"+audioIdentityQuery(claims))
	}
}

// AudioAvailabilityHandler 代理"本机能否转写"的探测。
//
// 单独一条路径而不是让前端乐观地直接录音：装没装 faster-whisper 决定按钮该不该
// 可点、以及点了之后说什么。让用户录完 30 秒再被告知"本机不支持"，是最差的一种。
func AudioAvailabilityHandler(pythonClient *engine.PythonClient) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		claims := auth.GetClaims(r.Context())
		if claims == nil || claims.UserID == "" || claims.TenantID == 0 {
			Unauthorized(w, "missing tenant context")
			return
		}
		if pythonClient == nil {
			ServiceUnavailable(w, "python engine not available")
			return
		}
		pythonClient.ForwardRequest(w, r, audioAvailabilityPath+"?"+audioIdentityQuery(claims))
	}
}
