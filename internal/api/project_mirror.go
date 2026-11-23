package api

// ── client 项目的按需镜像（S2）──
//
// 引擎工具（fs/git/run_code）只认本地路径，无法直接对设备文件做 syscall。这里把
// client 项目**按需镜像**到服务端临时目录：
//
//   回合前：device walk/read → <mirror>/<project_id>，记下每个文件的设备侧 sha256
//   回合后：本地扫描差异 → device write(expected_hash=基线) 回写，冲突保留原样
//
// 为什么不是"整仓长期同步"：镜像只在有 client 项目回合时建立/使用，且带文件数与
// 字节上限（设备侧 walk 执行）；超限直接报错让用户改用附件，而不是给 agent 半个仓库。
// 这是 S2 的过渡实现；S3 之后再按 PRD §4 换成逐操作 ProjectFS RPC。

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/auth"
)

const (
	mirrorManifestName = "manifest.json"
	mirrorMaxWriteFile = 4 << 20 // 回写单文件上限 4MiB
)

var (
	mirrorLocksMu sync.Mutex
	mirrorLocks   = map[string]*sync.Mutex{}
)

func mirrorLock(projectID string) *sync.Mutex {
	mirrorLocksMu.Lock()
	defer mirrorLocksMu.Unlock()
	l := mirrorLocks[projectID]
	if l == nil {
		l = &sync.Mutex{}
		mirrorLocks[projectID] = l
	}
	return l
}

func mirrorBaseDir() string {
	if v := strings.TrimSpace(os.Getenv("CHIRON_MIRROR_ROOT")); v != "" {
		return v
	}
	return filepath.Join(os.TempDir(), "chiron-mirror")
}

func mirrorPaths(projectID string) (root, manifest string) {
	base := mirrorBaseDir()
	root = filepath.Join(base, projectID)
	manifest = filepath.Join(base, projectID+".manifest.json")
	return root, manifest
}

func fileSHA256(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func loadMirrorManifest(path string) (map[string]string, error) {
	out := map[string]string{}
	raw, err := os.ReadFile(path)
	if err != nil {
		return out, err
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return map[string]string{}, nil // 坏清单按空处理，触发重建
	}
	return out, nil
}

func saveMirrorManifest(path string, m map[string]string) error {
	raw, err := json.Marshal(m)
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func safeMirrorRel(rel string) (string, bool) {
	rel = strings.TrimSpace(strings.ReplaceAll(rel, `\`, "/"))
	if rel == "" {
		return "", false
	}
	for _, seg := range strings.Split(rel, "/") {
		if seg == "" || seg == "." || seg == ".." {
			return "", false
		}
	}
	return rel, true
}

// ensureProjectMirror 建立（或复用）项目镜像，返回服务端本地根目录。
func ensureProjectMirror(ctx context.Context, tenant, user string, loc projectLocator) (string, error) {
	if loc.Provider != smProviderClient {
		return "", errors.New("not a client project")
	}
	if loc.DeviceID == "" {
		return "", errors.New("client project has no device")
	}
	lock := mirrorLock(loc.ID)
	lock.Lock()
	defer lock.Unlock()

	root, manifestPath := mirrorPaths(loc.ID)
	if st, err := os.Stat(root); err == nil && st.IsDir() {
		if _, err := os.Stat(manifestPath); err == nil {
			return root, nil
		}
	}

	walkResp, err := deviceHubSingleton.Call(ctx, tenant, user, loc.DeviceID, deviceOpRequest{
		Op: "walk", Root: loc.RootPath, Path: loc.RootPath,
	})
	if err != nil {
		return "", fmt.Errorf("device walk failed: %w", err)
	}
	if walkResp.Truncated {
		return "", errors.New("project is too large to mirror; upload the needed files as attachments")
	}
	// 重建：镜像目录是我们自己的缓存，可安全删除（不是用户数据）。
	_ = os.RemoveAll(root)
	if err := os.MkdirAll(root, 0o700); err != nil {
		return "", err
	}
	manifest := make(map[string]string, len(walkResp.Entries))
	skipped := 0
	for _, ent := range walkResp.Entries {
		rel, ok := safeMirrorRel(ent.Name)
		if !ok || ent.IsDir {
			continue
		}
		abs := filepath.Join(loc.RootPath, filepath.FromSlash(rel))
		readResp, err := deviceHubSingleton.Call(ctx, tenant, user, loc.DeviceID, deviceOpRequest{
			Op: "read", Root: loc.RootPath, Path: abs, MaxBytes: 1 << 20,
		})
		if err != nil {
			return "", fmt.Errorf("device read %s: %w", rel, err)
		}
		if readResp.Truncated {
			skipped++ // >1MiB：不入镜像，避免把大二进制塞进工具上下文
			continue
		}
		data, err := base64.StdEncoding.DecodeString(readResp.DataB64)
		if err != nil {
			return "", fmt.Errorf("decode %s: %w", rel, err)
		}
		target := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
			return "", err
		}
		if err := os.WriteFile(target, data, 0o600); err != nil {
			return "", err
		}
		sum := sha256.Sum256(data)
		manifest[rel] = hex.EncodeToString(sum[:])
	}
	if err := saveMirrorManifest(manifestPath, manifest); err != nil {
		return "", err
	}
	slog.Info("project mirror ready", "project", loc.ID, "files", len(manifest), "skipped_large", skipped)
	return root, nil
}

// syncProjectMirrorBack 把镜像里被 agent 改过/新建的文件回写到设备。
//
// 期望 hash 用基线（设备侧旧内容）：若用户在设备上同时改了同一文件，设备返回
// conflict，本轮不回写该文件并汇总报错 —— 宁可让用户看到冲突，也不静默覆盖。
func syncProjectMirrorBack(ctx context.Context, tenant, user string, loc projectLocator) error {
	if loc.Provider != smProviderClient || loc.DeviceID == "" {
		return nil
	}
	lock := mirrorLock(loc.ID)
	lock.Lock()
	defer lock.Unlock()

	root, manifestPath := mirrorPaths(loc.ID)
	manifest, err := loadMirrorManifest(manifestPath)
	if err != nil {
		return nil // 没镜像过 / 清单缺失：没有可回写的内容
	}
	var conflicts []string
	changed := 0
	walkErr := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			name := d.Name()
			if name == ".git" || name == "node_modules" || name == "target" || name == "__pycache__" {
				return filepath.SkipDir
			}
			return nil
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return nil
		}
		rel = filepath.ToSlash(rel)
		if !safeMirrorRelOk(rel) {
			return nil
		}
		sum, err := fileSHA256(path)
		if err != nil {
			return nil
		}
		base, existed := manifest[rel]
		if existed && base == sum {
			return nil
		}
		st, err := os.Stat(path)
		if err != nil || st.Size() > mirrorMaxWriteFile {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return nil
		}
		abs := filepath.Join(loc.RootPath, filepath.FromSlash(rel))
		resp, err := deviceHubSingleton.Call(ctx, tenant, user, loc.DeviceID, deviceOpRequest{
			Op: "write", Root: loc.RootPath, Path: abs,
			DataB64: base64.StdEncoding.EncodeToString(data), ExpectedHash: base,
		})
		if err != nil {
			if strings.Contains(err.Error(), "conflict") {
				conflicts = append(conflicts, rel)
			} else {
				slog.Warn("mirror write back failed", "project", loc.ID, "path", rel, "error", err)
			}
			return nil
		}
		manifest[rel] = resp.Hash
		changed++
		return nil
	})
	_ = saveMirrorManifest(manifestPath, manifest)
	if walkErr != nil {
		slog.Warn("mirror scan failed", "project", loc.ID, "error", walkErr)
	}
	if changed > 0 {
		slog.Info("project mirror synced", "project", loc.ID, "files", changed)
	}
	if len(conflicts) > 0 {
		return fmt.Errorf("project_mirror_conflict: %s", strings.Join(conflicts, ", "))
	}
	return nil
}

func safeMirrorRelOk(rel string) bool {
	_, ok := safeMirrorRel(rel)
	return ok
}

// syncProjectMirrorForSession 供代理 afterResponse 调用：按会话解析 client 项目并回写。
func syncProjectMirrorForSession(ctx context.Context, sessionID string) {
	claims := auth.GetClaims(ctx)
	if claims == nil {
		return
	}
	loc, ok := projectLocatorForSession(ctx, sessionID)
	if !ok || loc.Provider != smProviderClient {
		return
	}
	// 请求 ctx 可能已随响应结束；回写用独立超时，不阻塞收尾太久。
	tctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	if err := syncProjectMirrorBack(tctx, smTenant(claims), claims.UserID, loc); err != nil {
		slog.Warn("mirror sync failed", "session", sessionID, "error", err)
	}
}
