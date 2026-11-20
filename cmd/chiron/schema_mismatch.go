package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/athenavi/chiron/config"
)

// schemaMismatchFix 是写给用户/运维的修复命令。前端「schema 不一致」页与错误页
// 都直接展示这一句，所以它必须是**能照抄执行**的形态，而不是一句"请检查迁移"。
const schemaMismatchFix = "run alembic upgrade head"

// schemaMismatch 描述一次启动期的 schema 门禁失败。
//
// 为什么要把它做成结构体而不是直接 os.Exit(1)：旧行为下进程立刻死掉、端口上没有
// 任何监听者，Electron 壳的 `/health` 探测只会得到"连接被拒"，前端于是停在白屏/
// 转圈 —— 用户看不到 expected/actual，也不知道该跑什么命令。把失败原因变成一个
// **可查询的 HTTP 响应**，白屏就变成了可诊断的页面。
type schemaMismatch struct {
	// Expected 是代码期望的迁移 head（migrations/versions 解析结果）。
	Expected string `json:"expected"`
	// Actual 是数据库 alembic_version 里的当前 revision（可能为空：从没迁移过）。
	Actual string `json:"actual"`
	// Reason 区分两类漂移：revision（版本不一致）与 missing_tables（版本对但表缺）。
	Reason string `json:"reason,omitempty"`
	// Missing 在 Reason=missing_tables 时列出缺失的表名。
	Missing []string `json:"missing_tables,omitempty"`
}

// Reason 取值。前端只按 `status` 判定，这两个值只用于日志与页面副标题。
const (
	schemaMismatchRevision      = "revision"
	schemaMismatchMissingTables = "missing_tables"
)

// schemaMismatchBody 是 `/healthz` 与其它路径共用的响应体。
//
// `status` 恒为 `schema_mismatch`：前端探测据此决定跳转，**不依赖 HTTP 状态码**——
// `/healthz` 故意回 200，否则 Electron 壳的就绪探测会把它当成"网关没起来"，
// 于是用户连这个诊断页都看不到，又回到白屏。
type schemaMismatchBody struct {
	Status   string   `json:"status"`
	Expected string   `json:"expected"`
	Actual   string   `json:"actual"`
	Reason   string   `json:"reason,omitempty"`
	Missing  []string `json:"missing_tables,omitempty"`
	Fix      string   `json:"fix"`
	Error    string   `json:"error,omitempty"`
}

func (m schemaMismatch) body() schemaMismatchBody {
	return schemaMismatchBody{
		Status:   "schema_mismatch",
		Expected: m.Expected,
		Actual:   m.Actual,
		Reason:   m.Reason,
		Missing:  m.Missing,
		Fix:      schemaMismatchFix,
	}
}

// newSchemaMismatchHandler 构造"最小可诊断服务"。
//
// 只做两件事：
//   - `GET /healthz` → 200 + 漂移详情，让壳与前端**能**探到这个进程并读到原因；
//   - 其它一切路径 → 503 + 同样的 JSON（带 `error`），明确"网关起来了，但不可用"。
//
// 为什么不复用正常路由表：正常路由需要 PG/Redis/引擎全部就绪，而这条路径的存在
// 前提正是"依赖的 schema 不对"。挂半套业务路由只会把 500 散落到各处，反而掩盖
// 真正的根因。
func newSchemaMismatchHandler(m schemaMismatch) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			writeSchemaMismatchJSON(w, http.StatusMethodNotAllowed, m.body())
			return
		}
		writeSchemaMismatchJSON(w, http.StatusOK, m.body())
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		body := m.body()
		body.Error = "gateway is unavailable: database schema does not match this build"
		writeSchemaMismatchJSON(w, http.StatusServiceUnavailable, body)
	})
	return mux
}

func writeSchemaMismatchJSON(w http.ResponseWriter, status int, body schemaMismatchBody) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	// 这是"配置修好之前别缓存"的语义：用户跑完 alembic upgrade head 后刷新页面，
	// 必须立刻看到新结果，而不是代理层缓存的旧 503。
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(body); err != nil {
		slog.Warn("schema mismatch response write failed", "error", err)
	}
}

// serveSchemaMismatchAndWait 在漂移时提供最小 HTTP 服务并阻塞到进程被信号终止。
//
// 阻塞是刻意的：调用方（main）在此之后**不能**继续初始化业务组件，但退出进程又会
// 让端口消失、前端重新掉回白屏。于是保持监听、等运维修好迁移后重启。
func serveSchemaMismatchAndWait(cfg *config.Config, m schemaMismatch) {
	srv := &http.Server{
		Addr:              cfg.ListenAddr(),
		Handler:           newSchemaMismatchHandler(m),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       cfg.ReadTimeout,
		WriteTimeout:      cfg.WriteTimeout,
		IdleTimeout:       cfg.IdleTimeout,
	}

	done := make(chan os.Signal, 1)
	signal.Notify(done, os.Interrupt, syscall.SIGTERM)
	defer signal.Stop(done)

	go func() {
		slog.Warn("serving schema-mismatch diagnostics only (no business routes)",
			"addr", srv.Addr, "expected", m.Expected, "actual", m.Actual, "reason", m.Reason)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("schema-mismatch server error", "error", err)
			done <- syscall.SIGQUIT
		}
	}()

	<-done
	slog.Info("shutting down schema-mismatch diagnostics")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		slog.Warn("schema-mismatch shutdown error", "error", err)
	}
}
