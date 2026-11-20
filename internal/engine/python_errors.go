package engine

// python_errors.go —— 引擎调用的**错误分类**。
//
// 为什么需要它：上游失败此前只有两种可辨识形态（错误串里写死的
// `python GET <path> returned <code>: <body>` 与 `call python engine: <err>`），
// 而"5xx 重试耗尽"这条路径返回的是 `request failed after 4 retries: server error 502`
// —— 它既不含状态码也不含路径，调用方只能把它归到"未知"，于是网关把**引擎明确报出的
// 业务失败**（如 pip list 超时，引擎回 502）显示成 "python engine unreachable"。
// 运维据此去查网络，实际引擎活着、网络也没问题。
//
// 这里把结论固化成类型，调用方用 `errors.As` 取状态码/原因，不再靠正则猜文案。

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strings"
)

// UpstreamError 表示**请求已经到达引擎**并收到非 2xx 响应（或 5xx/429 重试耗尽）。
//
// 语义上它是"引擎在这一层给出的结论"（参数非法 / 未授权 / 内部错误 / 忙），
// 与"根本没连上"是两件完全不同的事，处置也不同。
type UpstreamError struct {
	Method string
	Path   string
	Status int
	// Body 是上游响应体（已截断，见 maxUpstreamErrBody）。可能是空串：
	// 429/5xx 重试耗尽时若上游没给体，这里就是空。
	Body string
}

func (e *UpstreamError) Error() string {
	return fmt.Sprintf("python %s %s returned %d: %s", e.Method, e.Path, e.Status, e.Body)
}

// StatusCode 便于调用方在不关心类型断言细节时取值（未分类错误返回 0）。
func (e *UpstreamError) StatusCode() int { return e.Status }

// TransportError 表示请求**没有拿到响应**（拨号失败、连接被拒、超时、连接中断）。
//
// 只有这一类才配叫 "unreachable" —— 但也必须再分成"超时"与"连不上"：
// 超时说明引擎在跑只是慢（pip 冷启动），连不上说明地址/进程有问题。
type TransportError struct {
	Method string
	Path   string
	Err    error
}

func (e *TransportError) Error() string {
	return fmt.Sprintf("call python engine: %v", e.Err)
}

func (e *TransportError) Unwrap() error { return e.Err }

// FailureKind 是给调用方看的失败分类（只分处置方式不同的三类，不细分系统错误码）。
type FailureKind int

const (
	// FailureUnknown：无法分类（含调用方主动取消）。调用方应回笼统文案，不要断言原因。
	FailureUnknown FailureKind = iota
	// FailureTimeout：拿到了"等太久"的结论（客户端整体超时 / 调用方 ctx 截止）。
	FailureTimeout
	// FailureUnreachable：连接建立失败或中途断掉（引擎没起来 / 地址错 / 端口不对）。
	FailureUnreachable
	// FailureUpstream：引擎回了非 2xx（见 UpstreamError.Status）。
	FailureUpstream
)

// FailureKindOf 把任意错误归类。对非本包产生的错误一律返回 FailureUnknown ——
// 猜错原因比说"不知道"更糟。
func FailureKindOf(err error) FailureKind {
	if err == nil {
		return FailureUnknown
	}
	var up *UpstreamError
	if errors.As(err, &up) {
		return FailureUpstream
	}
	var te *TransportError
	if !errors.As(err, &te) {
		return FailureUnknown
	}
	if IsTimeout(err) {
		return FailureTimeout
	}
	if isConnectFailure(te.Err) {
		return FailureUnreachable
	}
	return FailureUnknown
}

// IsTimeout 判断错误链里是否有"超时"结论。
//
// 三种来源都要覆盖：http.Client.Timeout（url.Error → net.Error）、
// 调用方 ctx 截止（context.DeadlineExceeded）、以及自定义 DeadlineExceeded。
func IsTimeout(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	var ne net.Error
	if errors.As(err, &ne) && ne.Timeout() {
		return true
	}
	// http.Client 超时的错误串在部分平台不带 Timeout()=true 的 net.Error（历史行为），
	// 兜一层文案判断：漏判会让"引擎慢"被报成"连不上"，正是本次要修的错。
	msg := strings.ToLower(err.Error())
	return strings.Contains(msg, "timeout") || strings.Contains(msg, "timed out") ||
		strings.Contains(msg, "deadline exceeded") || strings.Contains(msg, "awaiting headers")
}

// isConnectFailure 判断是否"连不上"（拨号失败/连接被拒/连接被重置）。
//
// 用 net.OpError.Op 判定而不是平台错误码：Windows 的 connectex 拒绝在
// syscall.ECONNREFUSED 上并不稳定命中，而 Op="dial" 是跨平台的稳定事实。
func isConnectFailure(err error) bool {
	var op *net.OpError
	if errors.As(err, &op) {
		switch op.Op {
		case "dial", "read", "write":
			return true
		}
	}
	msg := strings.ToLower(err.Error())
	for _, s := range []string{
		"connection refused", "actively refused", "connectex",
		"no route to host", "connection reset", "broken pipe",
		"network is unreachable", "no such host",
	} {
		if strings.Contains(msg, s) {
			return true
		}
	}
	var dnsErr *net.DNSError
	return errors.As(err, &dnsErr)
}
