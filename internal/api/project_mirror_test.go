package api

// S2 项目镜像的纯逻辑与端到端回归：相对路径安全、清单读写、设备↔镜像同步。

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/gorilla/websocket"
)

func TestSafeMirrorRel(t *testing.T) {
	ok := []string{"a.go", "src/main.go", `win\dir\file.txt`}
	for _, rel := range ok {
		if got, valid := safeMirrorRel(rel); !valid || got == "" {
			t.Fatalf("safeMirrorRel(%q) 应通过", rel)
		}
	}
	bad := []string{"", "..", "../etc/passwd", "a/../../b", "./a", "a//b"}
	for _, rel := range bad {
		if _, valid := safeMirrorRel(rel); valid {
			t.Fatalf("safeMirrorRel(%q) 应拒绝", rel)
		}
	}
}

func TestMirrorManifestRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "m.json")
	want := map[string]string{"a.go": "abc", "src/b.go": "def"}
	if err := saveMirrorManifest(path, want); err != nil {
		t.Fatalf("save: %v", err)
	}
	got, err := loadMirrorManifest(path)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if len(got) != len(want) || got["a.go"] != "abc" || got["src/b.go"] != "def" {
		t.Fatalf("manifest roundtrip mismatch: %+v", got)
	}
	if err := os.WriteFile(path, []byte("{bad"), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err = loadMirrorManifest(path)
	if err != nil || len(got) != 0 {
		t.Fatalf("坏清单应返回空 map,nil；得到 %+v,%v", got, err)
	}
}

// deviceRel 把设备侧绝对路径还原成项目相对路径（与 fakeProjectDevice 的根一致）。
func deviceRel(p string) string {
	return strings.TrimPrefix(strings.ReplaceAll(p, `\`, "/"), "D:/proj/")
}

// fakeProjectDevice 启动一个最小设备：walk/read/write 三个 op，内容存内存。
func fakeProjectDevice(t *testing.T, tenant, user, device string, files map[string]string) {
	t.Helper()
	fakeProjectDeviceHooks(t, tenant, user, device, files, nil)
}

// fakeProjectDeviceHooks 同 fakeProjectDevice，额外允许对写入注入设备侧错误
// （`onWrite` 返回非空字符串即当成失败），用于验证"回写失败必须可见"。
func fakeProjectDeviceHooks(
	t *testing.T, tenant, user, device string, files map[string]string, onWrite func(rel string) string,
) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		up := websocket.Upgrader{CheckOrigin: func(*http.Request) bool { return true }}
		conn, err := up.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		dc := &deviceConn{
			tenant: tenant, user: user, device: device, ws: conn,
			pending: map[string]chan deviceOpResponse{}, closed: make(chan struct{}),
		}
		deviceHubSingleton.register(dc)
		t.Cleanup(func() { deviceHubSingleton.unregister(dc) })
		// 服务端读回包并分发给 pending（与 DeviceFSHandler 同构）
		go func() {
			for {
				var resp deviceOpResponse
				if err := conn.ReadJSON(&resp); err != nil {
					return
				}
				dc.pendMu.Lock()
				ch := dc.pending[resp.ID]
				delete(dc.pending, resp.ID)
				dc.pendMu.Unlock()
				if ch != nil {
					ch <- resp
					close(ch)
				}
			}
		}()
	}))
	t.Cleanup(srv.Close)

	wsURL := "ws" + strings.TrimPrefix(srv.URL, "http")
	client, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
	if err != nil {
		t.Fatalf("dial fake device: %v", err)
	}
	t.Cleanup(func() { client.Close() })
	// 客户端扮演设备：读 op、执行、回包
	go func() {
		for {
			var req deviceOpRequest
			if err := client.ReadJSON(&req); err != nil {
				return
			}
			resp := deviceOpResponse{ID: req.ID}
			rel := deviceRel(req.Path)
			switch req.Op {
			case "walk":
				resp.OK = true
				for name := range files {
					resp.Entries = append(resp.Entries, deviceEntry{Name: name})
				}
			case "read":
				if content, ok := files[rel]; ok {
					resp.OK = true
					resp.DataB64 = base64.StdEncoding.EncodeToString([]byte(content))
				} else {
					resp.Error = "not found"
				}
			case "write":
				if onWrite != nil {
					if msg := onWrite(rel); msg != "" {
						resp.Error = msg
						break
					}
				}
				data, _ := base64.StdEncoding.DecodeString(req.DataB64)
				if req.ExpectedHash != "" {
					sum := sha256.Sum256([]byte(files[rel]))
					if hex.EncodeToString(sum[:]) != req.ExpectedHash {
						resp.Error = "conflict"
						break
					}
				}
				files[rel] = string(data)
				sum := sha256.Sum256(data)
				resp.OK = true
				resp.Hash = hex.EncodeToString(sum[:])
			case "hash":
				content, ok := files[rel]
				if !ok {
					resp.Error = "not found"
					break
				}
				sum := sha256.Sum256([]byte(content))
				resp.OK, resp.Hash = true, hex.EncodeToString(sum[:])
			case "remove":
				delete(files, rel)
				resp.OK = true
			default:
				resp.Error = "unsupported"
			}
			_ = client.WriteJSON(resp)
		}
	}()
	deadline := time.Now().Add(2 * time.Second)
	for !deviceHubSingleton.Online(tenant, user, device) && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if !deviceHubSingleton.Online(tenant, user, device) {
		t.Fatal("fake device 未注册")
	}
}

func TestProjectMirrorEnsureAndSyncBack(t *testing.T) {
	t.Setenv("CHIRON_MIRROR_ROOT", t.TempDir())
	files := map[string]string{"a.txt": "hello"}
	fakeProjectDevice(t, "ten", "usr", "dev-mirror", files)

	loc := projectLocator{
		ID: "proj-mirror-test", Provider: smProviderClient, DeviceID: "dev-mirror",
		RootPath: `D:\proj`,
	}
	root, err := ensureProjectMirror(context.Background(), "ten", "usr", loc)
	if err != nil {
		t.Fatalf("ensure mirror: %v", err)
	}
	if got, _ := os.ReadFile(filepath.Join(root, "a.txt")); string(got) != "hello" {
		t.Fatalf("镜像内容 = %q", got)
	}

	// agent 在镜像里改文件 → 回写设备
	if err := os.WriteFile(filepath.Join(root, "a.txt"), []byte("changed"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := syncProjectMirrorBack(context.Background(), "ten", "usr", loc); err != nil {
		t.Fatalf("sync back: %v", err)
	}
	if files["a.txt"] != "changed" {
		t.Fatalf("设备文件未回写: %q", files["a.txt"])
	}

	// 设备侧被并发修改 → 再回写必须冲突（不覆盖）
	files["a.txt"] = "device-edit"
	if err := os.WriteFile(filepath.Join(root, "a.txt"), []byte("agent-edit"), 0o600); err != nil {
		t.Fatal(err)
	}
	err = syncProjectMirrorBack(context.Background(), "ten", "usr", loc)
	if err == nil || !strings.Contains(err.Error(), "conflict") {
		t.Fatalf("应报 conflict，得到 %v", err)
	}
	if files["a.txt"] != "device-edit" {
		t.Fatalf("冲突时不得覆盖设备文件，得到 %q", files["a.txt"])
	}

	// 设备侧改回基线内容后，删除镜像文件 → 设备也应删除
	files["a.txt"] = "changed"
	if err := os.Remove(filepath.Join(root, "a.txt")); err != nil {
		t.Fatal(err)
	}
	if err := syncProjectMirrorBack(context.Background(), "ten", "usr", loc); err != nil {
		t.Fatalf("delete sync: %v", err)
	}
	if _, ok := files["a.txt"]; ok {
		t.Fatal("镜像删除后设备文件应被删除")
	}
}

func TestProjectMirrorCheckpointRestoreSync(t *testing.T) {
	t.Setenv("CHIRON_MIRROR_ROOT", t.TempDir())
	files := map[string]string{
		"a.txt":                        "checkpoint-before",
		".gitignore":                   "cache/\n",
		".github/workflows/ci.yml":     "name: ci\n",
		".git/config":                  "local-only metadata",
		".env":                         "SECRET=not-for-server",
		"nested/.git/objects/pack/obj": "must-not-cross-device-bridge",
	}
	fakeProjectDevice(t, "1", "usr", "dev-checkpoint", files)
	loc := projectLocator{
		ID: "proj-checkpoint", Provider: smProviderClient, DeviceID: "dev-checkpoint",
		RootPath: `D:\proj`, RootKey: "d:/proj",
	}
	claims := &auth.Claims{TenantID: 1, UserID: "usr"}
	root, err := checkpointRootForLocator(context.Background(), claims, loc)
	if err != nil {
		t.Fatalf("checkpoint root: %v", err)
	}
	if root == loc.RootPath || filepath.Dir(root) == loc.RootPath {
		t.Fatalf("检查点 root 必须是服务端镜像而不是设备路径: %q", root)
	}
	for _, rel := range []string{".gitignore", ".github/workflows/ci.yml"} {
		if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(rel))); err != nil {
			t.Fatalf("普通点文件应镜像 %s: %v", rel, err)
		}
	}
	for _, rel := range []string{".git", ".env", filepath.Join("nested", ".git")} {
		if _, err := os.Stat(filepath.Join(root, rel)); !os.IsNotExist(err) {
			t.Fatalf(".git 元数据不得镜像 %s: %v", rel, err)
		}
	}

	// 模拟检查点之后 agent 的修改，并先按正常写回语义同步到设备。
	if err := os.WriteFile(filepath.Join(root, "a.txt"), []byte("agent-change"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "new.txt"), []byte("new"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := syncProjectMirrorBack(context.Background(), smTenant(claims), "usr", loc); err != nil {
		t.Fatalf("agent write back: %v", err)
	}
	if files["a.txt"] != "agent-change" || files["new.txt"] != "new" {
		t.Fatalf("agent 修改未到设备: %+v", files)
	}

	// 模拟引擎把旧检查点恢复到镜像，再回写：旧内容覆盖 agent 修改、新增文件删除。
	if err := os.WriteFile(filepath.Join(root, "a.txt"), []byte("checkpoint-before"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(root, "new.txt")); err != nil {
		t.Fatal(err)
	}
	if err := syncProjectMirrorBack(context.Background(), smTenant(claims), "usr", loc); err != nil {
		t.Fatalf("restore write back: %v", err)
	}
	if files["a.txt"] != "checkpoint-before" {
		t.Fatalf("恢复内容未回写设备: %q", files["a.txt"])
	}
	if _, ok := files["new.txt"]; ok {
		t.Fatal("恢复后新增文件应从设备删除")
	}

	// 恢复也遵守 ExpectedHash：设备在恢复期间被用户修改时不得静默覆盖。
	files["a.txt"] = "user-edit"
	if err := os.WriteFile(filepath.Join(root, "a.txt"), []byte("older-checkpoint"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := syncProjectMirrorBack(context.Background(), smTenant(claims), "usr", loc); err == nil || !strings.Contains(err.Error(), "conflict") {
		t.Fatalf("恢复回写冲突必须显式返回，得到 %v", err)
	}
	if files["a.txt"] != "user-edit" {
		t.Fatalf("恢复冲突覆盖了用户设备修改: %q", files["a.txt"])
	}
}

// ── 检查点 E2E：镜像里绝不能有 .git / 密钥；恢复回写必须尊重设备侧并发修改 ──

// TestProjectMirrorNeverCarriesGitOrSecrets 钉住"镜像只装源码"。
//
// 设备桥是跨机器的：一旦 `.git`（含本机绝对路径、远端地址）或 `.env` / 私钥
// 进了镜像，它们就同时进了服务端磁盘与后续的检查点快照。旧版设备实现可能把
// 这些一起 walk 上来，因此服务端必须自己再筛一遍（不依赖设备端自律）。
func TestProjectMirrorNeverCarriesGitOrSecrets(t *testing.T) {
	t.Setenv("CHIRON_MIRROR_ROOT", t.TempDir())
	files := map[string]string{
		"src/main.go":              "package main\n",
		".gitignore":               "dist/\n",
		".github/workflows/ci.yml": "name: ci\n",
		".git/config":              `[remote "origin"] url = https://user:token@example.com/x`,
		"vendor/dep/.git/HEAD":     "ref: refs/heads/main",
		".env":                     "API_KEY=leak",
		".env.local":               "API_KEY=leak2",
		".npmrc":                   "//registry/:_authToken=leak",
		".ssh/id_rsa":              "PRIVATE-RSA",
		".ssh/config":              "Host *",
		".aws/credentials":         "PRIVATE-AWS",
		"deploy/server.pem":        "PRIVATE-PEM",
		"deploy/client.p12":        "PRIVATE-P12",
		"id_ed25519":               "PRIVATE-ED25519",
		"nested/.ssh/id_ed25519":   "PRIVATE-NESTED",
	}
	fakeProjectDevice(t, "1", "usr", "dev-secrets", files)
	loc := projectLocator{
		ID: "proj-secrets", Provider: smProviderClient, DeviceID: "dev-secrets",
		RootPath: `D:\proj`, RootKey: "d:/proj",
	}
	root, err := ensureProjectMirror(context.Background(), "1", "usr", loc)
	if err != nil {
		t.Fatalf("ensure mirror: %v", err)
	}

	// 普通点文件属于源码内容，必须保留。
	for _, rel := range []string{".gitignore", ".github/workflows/ci.yml", "src/main.go"} {
		if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(rel))); err != nil {
			t.Fatalf("普通文件应镜像 %s: %v", rel, err)
		}
	}
	// 元数据与凭据一个都不许进镜像。
	var leaked []string
	_ = filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		rel, _ := filepath.Rel(root, path)
		if mirrorHasGitMetadata(rel) || mirrorHasSensitiveFile(rel) {
			leaked = append(leaked, filepath.ToSlash(rel))
		}
		return nil
	})
	if len(leaked) > 0 {
		t.Fatalf(".git / 密钥不得进入镜像，实际存在: %v", leaked)
	}
	for _, rel := range []string{".git/config", ".env", ".ssh/id_rsa", ".aws/credentials", "deploy/server.pem"} {
		if _, err := os.Stat(filepath.Join(root, filepath.FromSlash(rel))); !os.IsNotExist(err) {
			t.Fatalf("%s 不得镜像（err=%v）", rel, err)
		}
	}
}

// TestMirrorWriteBackNewFileConflictsWithDeviceCreation 覆盖"新文件"的并发窗口。
//
// 镜像里新出现的文件在基线里没有条目，设备 `write` 的 expected_hash 也就为空 ——
// 而空 expected_hash 在设备侧等于"不做任何校验、直接覆盖"。用户若在回合期间
// 在设备上新建了同名文件，旧实现会把它静默覆盖掉。回写必须先探一次 hash。
func TestMirrorWriteBackNewFileConflictsWithDeviceCreation(t *testing.T) {
	t.Setenv("CHIRON_MIRROR_ROOT", t.TempDir())
	files := map[string]string{"a.txt": "hello"}
	fakeProjectDevice(t, "1", "usr", "dev-newfile", files)
	loc := projectLocator{
		ID: "proj-newfile", Provider: smProviderClient, DeviceID: "dev-newfile",
		RootPath: `D:\proj`, RootKey: "d:/proj",
	}
	root, err := ensureProjectMirror(context.Background(), "1", "usr", loc)
	if err != nil {
		t.Fatalf("ensure mirror: %v", err)
	}

	// agent 新建 notes.md；用户同时在设备上新建了同名文件。
	if err := os.WriteFile(filepath.Join(root, "notes.md"), []byte("agent notes"), 0o600); err != nil {
		t.Fatal(err)
	}
	files["notes.md"] = "user notes"

	err = syncProjectMirrorBack(context.Background(), "1", "usr", loc)
	if err == nil || !strings.Contains(err.Error(), "conflict") {
		t.Fatalf("设备已存在同名新文件时必须报 conflict，得到 %v", err)
	}
	if files["notes.md"] != "user notes" {
		t.Fatalf("冲突时不得覆盖用户在设备上新建的文件: %q", files["notes.md"])
	}
	// 冲突不该影响同批次的其它文件（a.txt 未被改动，因此这里仍是原值）。
	if files["a.txt"] != "hello" {
		t.Fatalf("无关文件被改动: %q", files["a.txt"])
	}

	// 用户删掉自己那份后，下一次回写应当成功。
	delete(files, "notes.md")
	if err := syncProjectMirrorBack(context.Background(), "1", "usr", loc); err != nil {
		t.Fatalf("设备文件消失后应能回写: %v", err)
	}
	if files["notes.md"] != "agent notes" {
		t.Fatalf("回写内容不对: %q", files["notes.md"])
	}
}

// TestMirrorWriteBackFailureIsVisible 钉住"回写失败必须报出来"。
//
// 引擎侧的恢复可以成功（它只改服务端镜像），设备却没变。如果把设备侧的失败
// 只写进日志，用户会看到"回滚成功"而本机文件一个都没动。
func TestMirrorWriteBackFailureIsVisible(t *testing.T) {
	t.Setenv("CHIRON_MIRROR_ROOT", t.TempDir())
	files := map[string]string{"a.txt": "v1", "b.txt": "v1"}
	fakeProjectDeviceHooks(t, "1", "usr", "dev-fail", files, func(rel string) string {
		if rel == "a.txt" {
			return "permission denied"
		}
		return ""
	})
	loc := projectLocator{
		ID: "proj-fail", Provider: smProviderClient, DeviceID: "dev-fail",
		RootPath: `D:\proj`, RootKey: "d:/proj",
	}
	root, err := ensureProjectMirror(context.Background(), "1", "usr", loc)
	if err != nil {
		t.Fatalf("ensure mirror: %v", err)
	}
	if err := os.WriteFile(filepath.Join(root, "a.txt"), []byte("v2"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "b.txt"), []byte("v2"), 0o600); err != nil {
		t.Fatal(err)
	}

	err = syncProjectMirrorBack(context.Background(), "1", "usr", loc)
	if err == nil || !strings.Contains(err.Error(), "project_mirror_sync_failed") {
		t.Fatalf("设备写失败必须返回错误（否则等于假成功），得到 %v", err)
	}
	if !strings.Contains(err.Error(), "a.txt") {
		t.Fatalf("错误里必须带上失败的路径，得到 %v", err)
	}
	if files["a.txt"] != "v1" {
		t.Fatalf("写失败的文件不应被当成已回写: %q", files["a.txt"])
	}
	// 同一批次的其它文件仍然要正常回写（失败不阻断）。
	if files["b.txt"] != "v2" {
		t.Fatalf("其它文件应正常回写: %q", files["b.txt"])
	}
}
