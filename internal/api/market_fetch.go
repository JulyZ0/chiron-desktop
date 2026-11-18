package api

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"

	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/market"
)

// engineDocumentFetcher 通过 Python 引擎抓取外部市场文档。
//
// **Go 侧不发外部 HTTP**：所有出网都收敛到 Python 的
// `/v1/internal/market/fetch`（走 `app.tools.ssrf.fetch_url_safe`）。
// 在 Go 里另写一套 scheme/端口/IP/重定向校验必然与 Python 漂移，而绕过只需
// 挑更宽松的那一边 —— SSRF 面因此只剩一处。
type engineDocumentFetcher struct {
	py *engine.PythonClient
}

// fetchResponse 是 Python 端点的响应体。
type fetchResponse struct {
	OK          bool   `json:"ok"`
	Body        string `json:"body"`
	SHA256      string `json:"sha256"`
	ContentType string `json:"content_type"`
	Bytes       int    `json:"bytes"`
	Error       string `json:"error"`
	Detail      string `json:"detail"`
	MaxBytes    int    `json:"max_bytes"`
}

// FetchDocument 抓取一份市场文档并返回原始字节。
//
// 错误一律转成 `*market.SourceError`（带稳定原因码）：上层据此做 warnings 与
// 状态码映射，而不是把引擎的错误串直接透给前端。
func (f engineDocumentFetcher) FetchDocument(ctx context.Context, url string, maxBytes int) ([]byte, error) {
	if f.py == nil {
		return nil, market.NewSourceError("source_unreachable", "engine client unavailable")
	}
	if maxBytes <= 0 || maxBytes > market.MaxDocumentBytes {
		maxBytes = market.MaxDocumentBytes
	}

	var resp fetchResponse
	err := f.py.PostJSON(ctx, "/v1/internal/market/fetch", map[string]any{
		"url":       url,
		"max_bytes": maxBytes,
	}, &resp)
	if err != nil {
		// 传输层失败（引擎不可达/超时）。这里**不**重试：抓取层已有单飞与 TTL 缓存，
		// 再叠一层重试只会让一个坏源占用更久的并发名额。
		return nil, classifyEngineFetchError(err)
	}
	if !resp.OK {
		if resp.Error == "" {
			return nil, market.NewSourceError("source_unreachable", resp.Detail)
		}
		return nil, market.NewSourceError(resp.Error, resp.Detail)
	}

	raw, decErr := base64.StdEncoding.DecodeString(resp.Body)
	if decErr != nil {
		return nil, market.NewSourceError("source_invalid", "engine returned undecodable body")
	}
	return raw, nil
}

// classifyEngineFetchError 把引擎调用失败映射成稳定原因码。
func classifyEngineFetchError(err error) error {
	msg := strings.ToLower(err.Error())
	switch {
	case errors.Is(err, context.DeadlineExceeded),
		strings.Contains(msg, "deadline exceeded"),
		strings.Contains(msg, "timeout"):
		return market.NewSourceError("source_timeout", err.Error())
	case strings.Contains(msg, "400"), strings.Contains(msg, "422"):
		return market.NewSourceError("source_invalid", err.Error())
	}
	return market.NewSourceError("source_unreachable", fmt.Sprintf("engine fetch failed: %.200s", err.Error()))
}
