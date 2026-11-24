package api

// ── 设备项目 WebDAV facade（R2 spike）──
//
// 目标：让服务端把 client 项目挂成普通目录（rclone mount / WinFsp），Python 工具零改动。
// 这里用标准库之外的 `golang.org/x/net/webdav` 实现 FileSystem，所有操作经设备桥：
//
//	WebDAV 客户端 (rclone) ──► /v1/device-webdav/<projectID>/ ──► deviceHub ──► 用户设备
//
// 仅内部令牌可访问（与 engine-config 同口径），且只接受 provider=client 的项目。
// 写入采用整文件缓冲后回写；spike 上限 32MiB/文件，超限显式报错（避免打爆网关内存）。

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"golang.org/x/net/webdav"
)

const webdavMaxWriteBytes = 32 << 20

// H55 失败语义：
//   - 设备端列表被截断时**不能**把半份列表当成完整目录返回 —— 上层会据此认为
//     "文件不存在"并可能删除/覆盖真实内容，所以直接让遍历失败。
//   - 写入必须带 ExpectedHash（OpenFile 时看到的版本），设备端原子校验；
//     冲突返回 412，而不是静默 last-write-wins。
var (
	errDeviceListTruncated   = errors.New("device directory listing is truncated by the device bridge; refusing to serve a partial listing")
	errDeviceWriteConflict   = errors.New("device write conflict: file changed since this upload started")
	errDeviceHashUnsupported = errors.New("device bridge does not support the hash op")
)

// deviceWebDAVWriteConflicts 统计 hash 冲突次数（诊断与回归测试用）。
var deviceWebDAVWriteConflicts atomic.Int64

func deviceWebDAVWriteConflictCount() int64 {
	return deviceWebDAVWriteConflicts.Load()
}

type deviceProject struct {
	ID       string
	Tenant   string
	User     string
	Provider string
	DeviceID string
	Root     string
}

func deviceProjectByID(ctx context.Context, id string) (deviceProject, bool) {
	if db.GlobalDBManager == nil || strings.TrimSpace(id) == "" {
		return deviceProject{}, false
	}
	row, err := db.GlobalDBManager.FetchOne(ctx,
		`SELECT id, tenant_id, COALESCE(user_id,'') AS user_id,
		        COALESCE(provider,'server') AS provider, COALESCE(device_id,'') AS device_id,
		        COALESCE(root_path,'') AS root_path
		   FROM session_map_workspaces WHERE id = $1`, id)
	if err != nil || row == nil {
		return deviceProject{}, false
	}
	p := deviceProject{
		ID:       stringOf(row["id"]),
		Tenant:   tenantStr(int16(intOf(row["tenant_id"]))),
		User:     stringOf(row["user_id"]),
		Provider: stringOf(row["provider"]),
		DeviceID: stringOf(row["device_id"]),
		Root:     stringOf(row["root_path"]),
	}
	return p, p.ID != ""
}

type deviceWebDAVFS struct {
	tenant string
	user   string
	device string
	root   string
}

func (fs *deviceWebDAVFS) call(ctx context.Context, req deviceOpRequest) (deviceOpResponse, error) {
	req.Root = fs.root
	return deviceHubSingleton.Call(ctx, fs.tenant, fs.user, fs.device, req)
}

func (fs *deviceWebDAVFS) abs(name string) (string, bool) {
	rel := strings.TrimPrefix(strings.ReplaceAll(name, `\`, "/"), "/")
	return joinClientPath(fs.root, rel)
}

// hash 读取设备端文件的 sha256（用作 ETag 与 ExpectedHash）。
// exists=false 表示设备上没有这个文件；这两种情况都不算错误。
func (fs *deviceWebDAVFS) hash(ctx context.Context, abs string) (hash string, exists bool, err error) {
	resp, err := fs.call(ctx, deviceOpRequest{Op: "hash", Path: abs})
	if err != nil {
		if isDeviceNotFound(err) {
			return "", false, nil
		}
		if strings.Contains(strings.ToLower(err.Error()), "unsupported") {
			return "", false, errDeviceHashUnsupported
		}
		return "", false, err
	}
	hash = strings.ToLower(strings.TrimSpace(resp.Hash))
	return hash, hash != "", nil
}

// ── 单次 PUT 的共享上下文 ──
//
// webdav.Handler 把 status 映射写死在源码里（OpenFile 失败 → 404、Close 失败 → 405），
// 而 H55 要求 hash 冲突必须是 412。这里不改 handler：FS 检出冲突后把原因记进 state，
// 由 deviceWebDAVStatusWriter 在写 header 时改写状态码。这样 LOCK/UNLOCK、PROPFIND、
// MOVE 等既有语义都不受影响。
type deviceWebDAVWriteStateKey struct{}

type deviceWebDAVWriteState struct {
	mu          sync.Mutex
	ifMatch     string
	ifNoneMatch string
	// newHash 是写入成功后设备返回的新 hash，用作响应 ETag。
	newHash string
	// conflict 非空表示本次写入应返回 412。
	conflict string
	// replayed 表示命中"内容已一致"的重试，未真正写入。
	replayed bool
}

func deviceWebDAVWriteStateFrom(ctx context.Context) *deviceWebDAVWriteState {
	if ctx == nil {
		return nil
	}
	state, _ := ctx.Value(deviceWebDAVWriteStateKey{}).(*deviceWebDAVWriteState)
	return state
}

func (s *deviceWebDAVWriteState) markConflict(reason string) {
	if s == nil {
		return
	}
	s.mu.Lock()
	if s.conflict == "" {
		s.conflict = reason
	}
	s.mu.Unlock()
}

func (s *deviceWebDAVWriteState) markNewHash(hash string) {
	if s == nil {
		return
	}
	s.mu.Lock()
	s.newHash = hash
	s.mu.Unlock()
}

func (s *deviceWebDAVWriteState) markReplayed() {
	if s == nil {
		return
	}
	s.mu.Lock()
	s.replayed = true
	s.mu.Unlock()
}

func (s *deviceWebDAVWriteState) snapshot() (conflict, newHash string, replayed bool) {
	if s == nil {
		return "", "", false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.conflict, s.newHash, s.replayed
}

// deviceWebDAVStatusWriter 把 FS 检出的 hash 冲突改写成 412，并在成功时用
// 内容 hash 覆盖 handler 的启发式 ETag（handler 用 ModTime+Size 算，设备侧拿不到
// 稳定的 mtime，那个 ETag 每次都不一样，客户端无法拿来做 If-Match）。
type deviceWebDAVStatusWriter struct {
	http.ResponseWriter
	state       *deviceWebDAVWriteState
	wroteHeader bool
}

func (w *deviceWebDAVStatusWriter) WriteHeader(code int) {
	if w.wroteHeader {
		return
	}
	w.wroteHeader = true
	conflict, newHash, replayed := w.state.snapshot()
	switch {
	case conflict != "":
		// 冲突时 handler 可能已经写过 ETag，必须清掉，避免客户端缓存一个假版本。
		w.Header().Del("ETag")
		w.Header().Set("X-Chiron-Device-Conflict", conflict)
		code = http.StatusPreconditionFailed
	case replayed:
		// 幂等重放：告诉客户端"内容已经是你要的"，便于区分真实写入与重试。
		w.Header().Set("X-Chiron-Device-Write", "replayed")
	case newHash != "":
		w.Header().Set("X-Chiron-Device-Write", "written")
		w.Header().Set("ETag", `"`+newHash+`"`)
	}
	w.ResponseWriter.WriteHeader(code)
}

func (w *deviceWebDAVStatusWriter) Write(p []byte) (int, error) {
	if !w.wroteHeader {
		w.WriteHeader(http.StatusOK)
	}
	if conflict, _, _ := w.state.snapshot(); conflict != "" {
		// 错误体已经无意义（状态被改写成 412），丢弃以免污染响应。
		return len(p), nil
	}
	return w.ResponseWriter.Write(p)
}

func (w *deviceWebDAVStatusWriter) Flush() {
	if flusher, ok := w.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

func (w *deviceWebDAVStatusWriter) Unwrap() http.ResponseWriter {
	return w.ResponseWriter
}

// ── 读侧：目录遍历必须整体失败 ──
//
// PROPFIND 的响应是**边遍历边写**：handler 先写 multistatus 头，再逐目录写 entry。
// 列表被截断要等到 Readdir 才知道，那时 header 已经发出，改成 500 也追不回来
// （客户端只看到"207 + 空目录"，比报错更危险）。所以 PROPFIND 先缓冲，handler
// 结束后再决定是回放还是整体 500。
const deviceWebDAVDebufferedResponseLimit = 16 << 20

type deviceWebDAVReadStateKey struct{}

type deviceWebDAVReadState struct {
	mu        sync.Mutex
	truncated string
}

func deviceWebDAVReadStateFrom(ctx context.Context) *deviceWebDAVReadState {
	if ctx == nil {
		return nil
	}
	state, _ := ctx.Value(deviceWebDAVReadStateKey{}).(*deviceWebDAVReadState)
	return state
}

func (s *deviceWebDAVReadState) markTruncated(path string) {
	if s == nil {
		return
	}
	s.mu.Lock()
	if s.truncated == "" {
		s.truncated = path
	}
	s.mu.Unlock()
}

func (s *deviceWebDAVReadState) truncatedPath() string {
	if s == nil {
		return ""
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.truncated
}

type deviceWebDAVDeferredWriter struct {
	target   http.ResponseWriter
	header   http.Header
	body     bytes.Buffer
	code     int
	streamed bool
}

func newDeviceWebDAVDeferredWriter(target http.ResponseWriter) *deviceWebDAVDeferredWriter {
	return &deviceWebDAVDeferredWriter{target: target, header: make(http.Header)}
}

func (w *deviceWebDAVDeferredWriter) Header() http.Header { return w.header }

func (w *deviceWebDAVDeferredWriter) WriteHeader(code int) {
	if w.code == 0 {
		w.code = code
	}
}

func (w *deviceWebDAVDeferredWriter) Write(p []byte) (int, error) {
	if w.code == 0 {
		w.code = http.StatusOK
	}
	if !w.streamed {
		if w.body.Len()+len(p) <= deviceWebDAVDebufferedResponseLimit {
			return w.body.Write(p)
		}
		slog.Warn("device webdav: PROPFIND response exceeds buffer limit, falling back to streaming",
			"limit", deviceWebDAVDebufferedResponseLimit)
		w.startStream()
	}
	return w.target.Write(p)
}

func (w *deviceWebDAVDeferredWriter) Flush() {
	w.startStream()
	if flusher, ok := w.target.(http.Flusher); ok {
		flusher.Flush()
	}
}

func (w *deviceWebDAVDeferredWriter) Unwrap() http.ResponseWriter {
	return w.target
}

func (w *deviceWebDAVDeferredWriter) startStream() {
	if w.streamed {
		return
	}
	w.streamed = true
	for k, values := range w.header {
		w.target.Header()[k] = values
	}
	if w.code == 0 {
		w.code = http.StatusOK
	}
	w.target.WriteHeader(w.code)
	if w.body.Len() > 0 {
		_, _ = w.target.Write(w.body.Bytes())
		w.body.Reset()
	}
}

// commit 在 handler 返回后落盘：截断 ⇒ 500（丢弃半份列表），否则原样回放。
func (w *deviceWebDAVDeferredWriter) commit(truncatedPath string) {
	if w.streamed {
		return
	}
	if truncatedPath != "" {
		w.header.Del("Content-Length")
		w.header.Set("Content-Type", "text/plain; charset=utf-8")
		w.header.Set("X-Chiron-Device-Truncated", "1")
		w.target.WriteHeader(http.StatusInternalServerError)
		_, _ = fmt.Fprintf(w.target,
			"device bridge: directory listing truncated at %q; refusing partial listing\n", truncatedPath)
		return
	}
	for k, values := range w.header {
		w.target.Header()[k] = values
	}
	if w.code == 0 {
		w.code = http.StatusOK
	}
	w.target.WriteHeader(w.code)
	if w.body.Len() > 0 {
		_, _ = w.target.Write(w.body.Bytes())
	}
}


func (fs *deviceWebDAVFS) Mkdir(ctx context.Context, name string, _ os.FileMode) error {
	abs, ok := fs.abs(name)
	if !ok {
		return os.ErrPermission
	}
	_, err := fs.call(ctx, deviceOpRequest{Op: "mkdir", Path: abs})
	return err
}

func (fs *deviceWebDAVFS) RemoveAll(ctx context.Context, name string) error {
	abs, ok := fs.abs(name)
	if !ok {
		return os.ErrPermission
	}
	_, err := fs.call(ctx, deviceOpRequest{Op: "remove", Path: abs})
	return err
}

func (fs *deviceWebDAVFS) Rename(ctx context.Context, oldName, newName string) error {
	from, ok := fs.abs(oldName)
	if !ok {
		return os.ErrPermission
	}
	to, ok := fs.abs(newName)
	if !ok {
		return os.ErrPermission
	}
	_, err := fs.call(ctx, deviceOpRequest{Op: "rename", Path: from, To: to})
	return err
}

func (fs *deviceWebDAVFS) Stat(ctx context.Context, name string) (os.FileInfo, error) {
	abs, ok := fs.abs(name)
	if !ok {
		return nil, os.ErrPermission
	}
	resp, err := fs.call(ctx, deviceOpRequest{Op: "stat", Path: abs})
	if err != nil {
		if isDeviceNotFound(err) {
			return nil, os.ErrNotExist
		}
		return nil, err
	}
	if resp.Stat == nil {
		return nil, os.ErrNotExist
	}
	return deviceFileInfo{
		name: path.Base("/" + strings.TrimPrefix(strings.ReplaceAll(name, `\`, "/"), "/")),
		size: resp.Stat.Size,
		dir:  resp.Stat.IsDir,
	}, nil
}

func (fs *deviceWebDAVFS) OpenFile(ctx context.Context, name string, flag int, perm os.FileMode) (webdav.File, error) {
	abs, ok := fs.abs(name)
	if !ok {
		return nil, os.ErrPermission
	}
	writable := flag&(os.O_WRONLY|os.O_RDWR|os.O_CREATE|os.O_TRUNC) != 0
	f := &deviceWebDAVFile{fs: fs, ctx: ctx, name: name, abs: abs, flag: flag, writable: writable}
	if flag&os.O_TRUNC != 0 && writable {
		f.dirty = true
	}
	if writable {
		// 先固定"我要覆盖的版本"：Close 时作为 ExpectedHash 交给设备端原子校验。
		if err := f.loadBaseVersion(ctx); err != nil {
			return nil, err
		}
	}
	if !writable || flag&(os.O_CREATE|os.O_TRUNC) == 0 {
		info, err := fs.Stat(ctx, name)
		if err != nil {
			if !(flag&os.O_CREATE != 0 && errors.Is(err, os.ErrNotExist)) {
				return nil, err
			}
		} else {
			f.info = info
		}
	}
	return f, nil
}

// loadBaseVersion 读取设备端当前内容 hash，作为本次写入的并发基线。
//
// 读不到就不写：写侧唯一的保护就是这个基线，拿不到基线时放行等于回到
// "并发编辑静默覆盖"。仅当设备端明确不支持 hash op（老版本桌面端）时降级，
// 并留一条告警，避免整条写入通路直接不可用。
func (f *deviceWebDAVFile) loadBaseVersion(ctx context.Context) error {
	hash, exists, err := f.fs.hash(ctx, f.abs)
	if errors.Is(err, errDeviceHashUnsupported) {
		slog.Warn("device webdav: device bridge lacks hash op, conditional write disabled", "path", f.abs)
		return nil
	}
	if err != nil {
		return err
	}
	f.baseHash, f.baseExists = hash, exists
	return nil
}

type deviceWebDAVFile struct {
	fs       *deviceWebDAVFS
	ctx      context.Context
	name     string
	abs      string
	flag     int
	writable bool
	dirty    bool
	pos      int64
	buf      []byte
	info     os.FileInfo
	dirs     []os.FileInfo
	dirPos   int
	// baseHash/baseExists 是 OpenFile 时设备上的版本，写入时作为 ExpectedHash。
	baseHash   string
	baseExists bool
}

func (f *deviceWebDAVFile) Stat() (os.FileInfo, error) {
	if f.info != nil {
		return f.info, nil
	}
	info, err := f.fs.Stat(f.ctx, f.name)
	if err != nil {
		return nil, err
	}
	f.info = info
	return info, nil
}

func (f *deviceWebDAVFile) Readdir(count int) ([]os.FileInfo, error) {
	if f.dirs == nil {
		abs, ok := f.fs.abs(f.name)
		if !ok {
			return nil, os.ErrPermission
		}
		resp, err := f.fs.call(f.ctx, deviceOpRequest{Op: "list", Path: abs})
		if err != nil {
			return nil, err
		}
		// 设备端超过列举上限时会置 truncated。此时 Entries 只是**前 N 项**：
		// 把它当完整目录返回，会让客户端以为其余文件不存在（并可能据此删除/覆盖），
		// 所以宁可直接失败，让 rclone/PROPFIND 显式报错。
		if resp.Truncated {
			slog.Warn("device webdav: directory listing truncated by device bridge, refusing partial listing",
				"path", abs, "entries", len(resp.Entries))
			deviceWebDAVReadStateFrom(f.ctx).markTruncated(abs)
			f.dirs = nil
			f.dirPos = 0
			return nil, errDeviceListTruncated
		}
		f.dirs = make([]os.FileInfo, 0, len(resp.Entries))
		for _, e := range resp.Entries {
			f.dirs = append(f.dirs, deviceFileInfo{name: e.Name, size: e.Size, dir: e.IsDir})
		}
	}
	if count <= 0 {
		out := f.dirs[f.dirPos:]
		f.dirPos = len(f.dirs)
		return out, nil
	}
	if f.dirPos >= len(f.dirs) {
		return nil, io.EOF
	}
	end := f.dirPos + count
	if end > len(f.dirs) {
		end = len(f.dirs)
	}
	out := f.dirs[f.dirPos:end]
	f.dirPos = end
	return out, nil
}

func (f *deviceWebDAVFile) Read(p []byte) (int, error) {
	if f.writable && f.flag&os.O_WRONLY != 0 && f.flag&os.O_RDWR == 0 {
		return 0, os.ErrInvalid
	}
	if len(p) == 0 {
		return 0, nil
	}
	if len(p) > 8<<20 {
		p = p[:8<<20]
	}
	resp, err := f.fs.call(f.ctx, deviceOpRequest{
		Op: "read", Path: f.abs, Offset: f.pos, MaxBytes: int64(len(p)),
	})
	if err != nil {
		return 0, err
	}
	data, decErr := base64.StdEncoding.DecodeString(resp.DataB64)
	if decErr != nil {
		return 0, decErr
	}
	n := copy(p, data)
	f.pos += int64(n)
	if n == 0 {
		return 0, io.EOF
	}
	return n, nil
}

func (f *deviceWebDAVFile) Write(p []byte) (int, error) {
	if !f.writable {
		return 0, os.ErrPermission
	}
	if len(f.buf)+len(p) > webdavMaxWriteBytes {
		return 0, errors.New("file too large for device bridge (32MiB cap)")
	}
	f.buf = append(f.buf, p...)
	f.dirty = true
	return len(p), nil
}

func (f *deviceWebDAVFile) Seek(offset int64, whence int) (int64, error) {
	switch whence {
	case io.SeekStart:
		f.pos = offset
	case io.SeekCurrent:
		f.pos += offset
	case io.SeekEnd:
		if f.info != nil {
			f.pos = f.info.Size() + offset
		} else {
			f.pos = offset
		}
	}
	if f.pos < 0 {
		return 0, os.ErrInvalid
	}
	return f.pos, nil
}

func (f *deviceWebDAVFile) Close() error {
	if !f.writable || !f.dirty {
		return nil
	}
	if err := f.checkWritePreconditions(); err != nil {
		return err
	}
	if !f.dirty {
		// 命中幂等重放：内容已经与本次上传一致，不再写一次。
		return nil
	}
	req := deviceOpRequest{
		Op:      "write",
		Path:    f.abs,
		DataB64: base64.StdEncoding.EncodeToString(f.buf),
		// 即使客户端没带 If-Match，也按"打开时看到的版本"做条件写：
		// 设备端在真正落盘前原子比对 expected_hash，冲突即拒绝。
		ExpectedHash: f.baseHash,
	}
	resp, err := f.fs.call(f.ctx, req)
	if err != nil {
		if isDeviceConflict(err) {
			deviceWebDAVWriteConflicts.Add(1)
			slog.Warn("device webdav: write conflict, file changed on device",
				"path", f.abs, "expected_hash", f.baseHash)
			f.markConflictOnDeviceChanged()
			return errDeviceWriteConflict
		}
		return err
	}
	f.dirty = false
	if state := deviceWebDAVWriteStateFrom(f.ctx); state != nil {
		state.markNewHash(strings.ToLower(strings.TrimSpace(resp.Hash)))
	}
	return nil
}

// checkWritePreconditions 处理客户端带来的条件头（If-Match / If-None-Match: *）。
//
// 判定口径：
//   - 冲突且内容已经与本次上传一致 ⇒ 视为幂等重放，直接成功，不重复写；
//   - 冲突且内容不同 ⇒ 412（由 status writer 改写），绝不静默覆盖；
//   - 客户端用别的 ETag 形式（非 64 位 hex，例如其它 WebDAV 实现的启发式 ETag）
//     ⇒ 不据此拒绝：那种 ETag 在本 facade 里无法验证，强行比较会把正常写全拒掉。
//     无条件头时仍由 ExpectedHash 兜住真正的并发覆盖。
func (f *deviceWebDAVFile) checkWritePreconditions() error {
	state := deviceWebDAVWriteStateFrom(f.ctx)
	if state == nil {
		return nil
	}
	state.mu.Lock()
	ifMatch, ifNoneMatch := state.ifMatch, state.ifNoneMatch
	state.mu.Unlock()

	if matchNoneStar(ifNoneMatch) {
		if !f.baseExists {
			return nil
		}
		if f.bufferHash() == f.baseHash {
			f.dirty = false
			state.markReplayed()
			return nil
		}
		return f.conflict("if-none-match: file already exists")
	}
	if ifMatch == "" {
		return nil
	}
	tokens := parseETagList(ifMatch)
	verifiable, matched := false, false
	for _, token := range tokens {
		switch {
		case token == "*":
			verifiable = true
			if f.baseExists {
				matched = true
			}
		case isContentHashToken(token):
			verifiable = true
			if f.baseExists && token == f.baseHash {
				matched = true
			}
		}
	}
	if !verifiable || matched {
		return nil
	}
	if f.baseExists && f.bufferHash() == f.baseHash {
		// 同一份内容重试：第一次其实已经写成功（或别处已写入同样内容），幂等返回成功。
		f.dirty = false
		state.markReplayed()
		return nil
	}
	return f.conflict("if-match mismatch")
}

func (f *deviceWebDAVFile) conflict(reason string) error {
	deviceWebDAVWriteConflicts.Add(1)
	slog.Warn("device webdav: write precondition failed", "path", f.abs, "reason", reason)
	if state := deviceWebDAVWriteStateFrom(f.ctx); state != nil {
		state.markConflict(reason)
	}
	return errDeviceWriteConflict
}

// markConflictOnDeviceChanged 是设备端拒绝写入（expected_hash 已过期）时的标记：
// 计数已由调用方 +1，这里只把 412 的原因交给 status writer。
func (f *deviceWebDAVFile) markConflictOnDeviceChanged() {
	if state := deviceWebDAVWriteStateFrom(f.ctx); state != nil {
		state.markConflict("expected_hash mismatch on device")
	}
}

func (f *deviceWebDAVFile) bufferHash() string {
	if !f.baseExists {
		return ""
	}
	sum := sha256.Sum256(f.buf)
	return hex.EncodeToString(sum[:])
}

// isDeviceConflict 识别设备端返回的 hash 冲突（device-fs.cjs: expected_hash 不匹配）。
func isDeviceConflict(err error) bool {
	if err == nil {
		return false
	}
	return strings.Contains(strings.ToLower(err.Error()), "conflict")
}

// deviceWebDAVETagHash 是"内容 hash 形式"的 ETag：64 位小写 hex，可带双引号或 W/ 前缀。
func isContentHashToken(token string) bool {
	if len(token) != 64 {
		return false
	}
	for _, r := range token {
		if (r < '0' || r > '9') && (r < 'a' || r > 'f') {
			return false
		}
	}
	return true
}

func matchNoneStar(header string) bool {
	for _, token := range parseETagList(header) {
		if token == "*" {
			return true
		}
	}
	return false
}

// parseETagList 把 If-Match/If-None-Match 拆成小写 token，去掉 W/ 与引号。
func parseETagList(header string) []string {
	header = strings.TrimSpace(header)
	if header == "" {
		return nil
	}
	parts := strings.Split(header, ",")
	tokens := make([]string, 0, len(parts))
	for _, part := range parts {
		token := strings.ToLower(strings.TrimSpace(part))
		token = strings.TrimPrefix(token, "w/")
		token = strings.Trim(token, `"`)
		token = strings.TrimSpace(token)
		if token != "" {
			tokens = append(tokens, token)
		}
	}
	return tokens
}

type deviceFileInfo struct {
	name string
	size int64
	dir  bool
}

func (i deviceFileInfo) Name() string { return i.name }
func (i deviceFileInfo) Size() int64  { return i.size }
func (i deviceFileInfo) IsDir() bool  { return i.dir }
func (i deviceFileInfo) Sys() any     { return nil }
func (i deviceFileInfo) ModTime() time.Time {
	return time.Now()
}
func (i deviceFileInfo) Mode() os.FileMode {
	if i.dir {
		return os.ModeDir | 0o755
	}
	return 0o644
}

func isDeviceNotFound(err error) bool {
	if err == nil {
		return false
	}
	msg := strings.ToLower(err.Error())
	return strings.Contains(msg, "not found") || strings.Contains(msg, "no such file") ||
		strings.Contains(msg, "enoent")
}

// 共享锁表：rclone 对同一项目会并发读写；锁路径里已带 projectID，不会跨项目串。
var deviceWebDAVLockSystem = webdav.NewMemLS()

// DeviceWebDAVHandler 暴露 `/v1/device-webdav/{projectID}/...`，由 internalTokenMW 包裹。
func DeviceWebDAVHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		rest := strings.TrimPrefix(r.URL.Path, "/v1/device-webdav/")
		if rest == r.URL.Path {
			http.NotFound(w, r)
			return
		}
		id := rest
		if i := strings.Index(rest, "/"); i >= 0 {
			id = rest[:i]
		}
		proj, ok := deviceProjectByID(r.Context(), id)
		if !ok || proj.Provider != smProviderClient || proj.DeviceID == "" {
			http.NotFound(w, r)
			return
		}
		fs := &deviceWebDAVFS{
			tenant: proj.Tenant, user: proj.User, device: proj.DeviceID, root: proj.Root,
		}
		serveDeviceWebDAV(w, r, "/v1/device-webdav/"+id, fs)
	}
}

// serveDeviceWebDAV 是 facade 的实际装配点（拆出来便于测试直接注入 fs）。
//
// PUT 额外挂一层 state：FS 检出 hash 冲突后要把状态码改写成 412。
func serveDeviceWebDAV(w http.ResponseWriter, r *http.Request, prefix string, fs *deviceWebDAVFS) {
	h := &webdav.Handler{
		Prefix:     prefix,
		FileSystem: fs,
		LockSystem: deviceWebDAVLockSystem,
	}
	switch r.Method {
	case http.MethodPut:
		state := &deviceWebDAVWriteState{
			ifMatch:     r.Header.Get("If-Match"),
			ifNoneMatch: r.Header.Get("If-None-Match"),
		}
		ctx := context.WithValue(r.Context(), deviceWebDAVWriteStateKey{}, state)
		h.ServeHTTP(&deviceWebDAVStatusWriter{ResponseWriter: w, state: state}, r.WithContext(ctx))
	case "PROPFIND":
		readState := &deviceWebDAVReadState{}
		ctx := context.WithValue(r.Context(), deviceWebDAVReadStateKey{}, readState)
		deferred := newDeviceWebDAVDeferredWriter(w)
		h.ServeHTTP(deferred, r.WithContext(ctx))
		deferred.commit(readState.truncatedPath())
	default:
		h.ServeHTTP(w, r)
	}
}
