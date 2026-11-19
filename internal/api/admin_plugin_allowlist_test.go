package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/config"
)

// admin_plugin_allowlist_test.go —— 后台白名单读写端点。
//
// 这两端点是 s10 的"自助闭环"：用户装完市场插件、测试连接被拒时，不必改部署 env
// 并重启两侧，直接在后台把命令加白即可。测试要钉住三件事：
//  1. PUT 落盘、GET 能回读（文件是唯一事实源，两侧共用）；
//  2. env 与文件是**并集**，且 env 部分只读（页面不能把它改掉）；
//  3. 非法条目（带路径分隔符）必须被拒 —— 否则这个接口就成了任意路径放行通道。

func newAllowlistAdmin(cfg *config.Config) *AdminHandler {
	return &AdminHandler{cfg: cfg}
}

func putAllowlist(t *testing.T, h *AdminHandler, body string) (*httptest.ResponseRecorder, map[string]interface{}) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPut, "/v1/admin/plugin-allowlist", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h.UpdatePluginAllowlist(rec, req)

	var resp struct {
		Success bool                   `json:"success"`
		Data    map[string]interface{} `json:"data"`
		Error   string                 `json:"error"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &resp)
	return rec, map[string]interface{}{
		"success": resp.Success,
		"data":    resp.Data,
		"error":   resp.Error,
	}
}

// 写进去什么，GET 就读回什么 —— 前端编辑框靠这个回填。
func TestAdminPluginAllowlistRoundTrip(t *testing.T) {
	t.Setenv("PLUGIN_COMMAND_ALLOWLIST", "")
	cfg := testPluginConfig(t)
	h := newAllowlistAdmin(cfg)

	rec, resp := putAllowlist(t, h, `{"commands":[" npx ","uvx","npx"]}`)
	if rec.Code != http.StatusOK || resp["success"] != true {
		t.Fatalf("写入应成功，得到 %d: %s", rec.Code, rec.Body.String())
	}
	data := resp["data"].(map[string]interface{})
	if got := strings.Join(toStrings(data["file_commands"]), ","); got != "npx,uvx" {
		t.Fatalf("应去重排序为 npx,uvx，得到 %q", got)
	}
	if data["configured"] != true {
		t.Fatal("写入后 configured 应为 true")
	}

	// 后台文件本身要真的能放开命令（引擎读同一份，判定口径一致）。
	if err := checkOperatorAllowlist("npx", cfg); err != nil {
		t.Fatalf("后台加白后应放行 npx，得到：%v", err)
	}

	getRec := httptest.NewRecorder()
	h.GetPluginAllowlist(getRec, httptest.NewRequest(http.MethodGet, "/v1/admin/plugin-allowlist", nil))
	if getRec.Code != http.StatusOK {
		t.Fatalf("GET 应成功，得到 %d", getRec.Code)
	}
}

// env 与文件是并集；env 部分只读，PUT 不能把它删掉。
func TestAdminPluginAllowlistEnvIsReadOnlyUnion(t *testing.T) {
	t.Setenv("PLUGIN_COMMAND_ALLOWLIST", "uvx")
	cfg := testPluginConfig(t)
	h := newAllowlistAdmin(cfg)

	_, resp := putAllowlist(t, h, `{"commands":["npx"]}`)
	data := resp["data"].(map[string]interface{})

	if got := strings.Join(toStrings(data["env_commands"]), ","); got != "uvx" {
		t.Fatalf("env 部分应单独回显为 uvx，得到 %q", got)
	}
	if got := strings.Join(toStrings(data["file_commands"]), ","); got != "npx" {
		t.Fatalf("文件部分应只含 npx（env 不落盘），得到 %q", got)
	}
	if got := strings.Join(toStrings(data["commands"]), ","); got != "npx,uvx" {
		t.Fatalf("生效名单应是并集 npx,uvx，得到 %q", got)
	}
	// 只写文件不该影响 env 条目的放行。
	if err := checkOperatorAllowlist("uvx", cfg); err != nil {
		t.Fatalf("env 里的命令必须仍然放行，得到：%v", err)
	}
}

// 带路径分隔符的条目必须被拒：放进去等于开一条任意路径放行通道。
func TestAdminPluginAllowlistRejectsPathEntries(t *testing.T) {
	t.Setenv("PLUGIN_COMMAND_ALLOWLIST", "")
	cfg := testPluginConfig(t)
	h := newAllowlistAdmin(cfg)

	for _, bad := range []string{"../npx", "/usr/bin/npx", `C:\\tools\\npx`} {
		body := `{"commands":["` + bad + `"]}`
		rec, resp := putAllowlist(t, h, body)
		if rec.Code != http.StatusBadRequest || resp["success"] != false {
			t.Fatalf("%q 应被拒（400），得到 %d: %s", bad, rec.Code, rec.Body.String())
		}
	}
	// 被拒之后不能留下半截文件：仍然没有任何生效条目。
	if list := loadPluginAllowlistFile(cfg); len(list) != 0 {
		t.Fatalf("非法写入不应落盘，得到 %v", list)
	}
}

func toStrings(v interface{}) []string {
	arr, ok := v.([]interface{})
	if !ok {
		return nil
	}
	out := make([]string, 0, len(arr))
	for _, item := range arr {
		if s, ok := item.(string); ok {
			out = append(out, s)
		}
	}
	return out
}
