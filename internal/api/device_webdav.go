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
	"context"
	"encoding/base64"
	"errors"
	"io"
	"net/http"
	"os"
	"path"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"golang.org/x/net/webdav"
)

const webdavMaxWriteBytes = 32 << 20

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
	_, err := f.fs.call(f.ctx, deviceOpRequest{
		Op: "write", Path: f.abs, DataB64: base64.StdEncoding.EncodeToString(f.buf),
	})
	if err == nil {
		f.dirty = false
	}
	return err
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
		h := &webdav.Handler{
			Prefix:     "/v1/device-webdav/" + id,
			FileSystem: fs,
			LockSystem: deviceWebDAVLockSystem,
		}
		h.ServeHTTP(w, r)
	}
}
