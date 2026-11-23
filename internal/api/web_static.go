package api

// web_static.go —— 可选的 SPA 静态托管（CHIRON_WEB_ROOT）。
//
// ## 为什么需要
//
// 桌面端以 `loadFile` 打开 `dist/index.html`（file://），分享链接因此只能在**这台机器
// 的这个窗口里**被打开：链接形如 `file:///.../index.html#/share/<token>`，换成别人的浏览器
// 就是「找不到文件」。Go 网关此前完全不托管前端产物，所以任何 `http(s)://<gateway>/share/<token>`
// 都不存在 —— 分享功能对外等于不可用。
//
// 配上 `CHIRON_WEB_ROOT=<含 index.html 的目录>` 后，网关在**自己的端口上**托管同一套前端，
// 分享链接就有了一个真正可达的宿主。不配置时一行都不注册，行为与引入前完全一致。
//
// ## 边界（安全）
//
//   - 只处理 GET/HEAD；其它方法 404（不接管 API 的写路径）。
//   - API 前缀（/v1、/docs、/health、/ready、/metrics、/search …）一律不回落 index.html，
//     未注册就是 404 —— 不能让 `/v1/随便什么` 返回一页 HTML 把排障带偏。
//   - 不列目录：目录请求与不存在的文件一样回落 index.html（SPA 的客户端路由）。
//   - 路径穿越由 `fs.ValidPath` + `os.DirFS` 双重拦下（前者拒绝 `..`/绝对路径，后者
//     在文件系统层再拒一次）。隐藏文件（`.` 开头）不对外提供。
//   - 分享页匿名可访问：这里托管的是公开的前端壳，接口鉴权仍在各自的 handler 上。

import (
	"bytes"
	"io/fs"
	"log/slog"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
	"time"
)

// spaReservedPrefixes 是**不能被 SPA 兜底吞掉**的顶层前缀。
//
// 都是已有（或曾经有过）的 API/运维端点：命中它们而没注册对应路由时，正确的回答是
// 404，而不是一页前端 HTML —— 后者会让「接口 404」看起来像「接口返回了奇怪的内容」。
var spaReservedPrefixes = []string{
	"/v1",
	"/docs",
	"/health",
	"/healthz",
	"/ready",
	"/readyz",
	"/metrics",
	"/search",
	"/submit",
	"/cancel",
	"/events",
	"/media",
	"/ws",
}

// isReservedAPIPath 判断请求路径是否应交给 API 处理（不参与静态托管）。
func isReservedAPIPath(p string) bool {
	for _, prefix := range spaReservedPrefixes {
		if p == prefix || strings.HasPrefix(p, prefix+"/") {
			return true
		}
	}
	return false
}

// newSPAHandler 构造 SPA 静态托管处理器。
//
// root 必须是**已存在且含 index.html** 的目录；否则返回错误，由调用方记一条 warn 并
// 不挂载 —— 配错路径不应该让网关起不来，但也不该静默"托管了一个空壳"。
func newSPAHandler(root string) (http.Handler, error) {
	root = strings.TrimSpace(root)
	if root == "" {
		return nil, os.ErrInvalid
	}
	abs, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	index, err := os.ReadFile(filepath.Join(abs, "index.html"))
	if err != nil {
		return nil, err
	}
	var indexMod time.Time
	if st, statErr := os.Stat(filepath.Join(abs, "index.html")); statErr == nil {
		indexMod = st.ModTime()
	}
	// 桌面构建（`vite --mode desktop`）把 base 设为 `./`，index.html 里全是
	// `./assets/...` 这类相对引用 —— 那是给 file:// 用的。但同一份 dist 也被网关在
	// `/s/<token>` 下托管：浏览器会把 `./assets/x.js` 解析成 `/s/assets/x.js`，
	// 资源 404 ⇒ 分享落地页白屏。注入 `<base href="/">` 后，相对引用一律回到站点根，
	// file:// 侧不受影响（它不经过本 handler）。
	index = withRootBase(index)

	fsys := os.DirFS(abs)
	// http.FileServerFS 负责 Content-Type / Range / If-Modified-Since 等细节；
	// 是否允许它出手由下面逐请求判定（目录一律不走它，避免列目录与 301 到 ./）。
	fileServer := http.FileServerFS(fsys)

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			// 挂载在 "GET /" 上，正常不会走到这里；留作 backstop（例如未来改成 "/"）。
			w.Header().Set("Allow", "GET, HEAD")
			NotFound(w, "not found")
			return
		}

		reqPath := r.URL.Path
		if isReservedAPIPath(reqPath) {
			NotFound(w, "not found")
			return
		}

		// path.Clean 会消掉 ".."；再补前导 "/" 才和 fs.ValidPath 的口径一致（"a/b"）。
		clean := strings.TrimPrefix(path.Clean("/"+reqPath), "/")
		if clean != "" && fs.ValidPath(clean) && !isHiddenPath(clean) {
			if st, statErr := fs.Stat(fsys, clean); statErr == nil && !st.IsDir() {
				fileServer.ServeHTTP(w, r)
				return
			}
		}

		// SPA 兜底：客户端路由（/share/<token> 等）与不存在的静态资源都回 index.html。
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-cache")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		http.ServeContent(w, r, "index.html", indexMod, bytes.NewReader(index))
	}), nil
}

// withRootBase 给 index.html 注入 `<base href="/">`（已有 base 时原样返回）。
//
// 只影响经 HTTP 托管的这份副本：桌面窗口用 file:// 读 asar 里的 dist，不经过这里。
func withRootBase(index []byte) []byte {
	lower := bytes.ToLower(index)
	if bytes.Contains(lower, []byte("<base ")) {
		return index
	}
	head := bytes.Index(lower, []byte("<head>"))
	if head < 0 {
		return index
	}
	insertAt := head + len("<head>")
	out := make([]byte, 0, len(index)+len(`<base href="/">`))
	out = append(out, index[:insertAt]...)
	out = append(out, []byte(`<base href="/">`)...)
	out = append(out, index[insertAt:]...)
	return out
}

// isHiddenPath 拒绝任何一段以 "." 开头的路径（.env / .git 之类不该经静态托管外泄）。
func isHiddenPath(p string) bool {
	for _, seg := range strings.Split(p, "/") {
		if strings.HasPrefix(seg, ".") {
			return true
		}
	}
	return false
}

// mountSPA 在 mux 上注册 SPA 兜底（仅在配置了可用目录时）。
//
// 单独成函数是为了让 gateway_router.go 的装配处保持一行，也方便单测直接调用。
func mountSPA(mux *http.ServeMux, root string) bool {
	if strings.TrimSpace(root) == "" {
		return false
	}
	h, err := newSPAHandler(root)
	if err != nil {
		slog.Warn("web root configured but unusable; SPA hosting disabled",
			"web_root", root, "error", err)
		return false
	}
	// 注册为**无方法限定**的 "/"，而不是 "GET /"。
	//
	// Go 1.22 的 ServeMux 会判定模式冲突：`GET /`（方法更窄、路径更宽）与
	// `/v1/device-webdav/`（方法不限定、路径更窄）互不包含，注册时直接 panic。
	// 改成 "/" 后两者在所有方法上可比，更具体的 WebDAV 路由照常优先；
	// 非 GET/HEAD 由 handler 首行的 backstop 返回 404，不会接管 API 写路径。
	mux.Handle("/", h)
	slog.Info("SPA hosting enabled", "web_root", root)
	return true
}
