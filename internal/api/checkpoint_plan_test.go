package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/engine"
)

// ── H73：破坏性操作必须绑定不可变计划（PRD 9.2）────────────────────────────
//
// 前端早已把目标 workspace 与计划摘要钉在确认框上，但**服务端不校验**：
// `POST /v1/admin/checkpoints/gc` 只认 `retention_days`/`workspace_id`。
// 于是"确认的是 A、删的是 B"这类漂移在服务端完全不可见 —— 一个被改过的前端、
// 或一句 curl，就能在"用户已确认"的名义下删掉没预览过的范围。
//
// 现在：dry-run 由服务端签发 `plan_digest`；执行前服务端**再跑一次只读 dry-run 重算**，
// 不一致即 409 且什么都不删。
//
// 这一组覆盖纯函数层（可确定性断言）与 handler 层（带桩 engine）。

func gcPlanPayload(overrides map[string]interface{}) map[string]interface{} {
	plan := map[string]interface{}{
		"scanned":       float64(10),
		"referenced":    float64(6),
		"expired":       float64(3),
		"tree_paths":    float64(2),
		"deleted":       float64(3),
		"freed_bytes":   float64(4096),
		"planned":       []interface{}{"refs/cp-b", "refs/cp-a"},
		"dangling_refs": []interface{}{},
	}
	for k, v := range overrides {
		plan[k] = v
	}
	return plan
}

func TestGcPlanDigestIsStableAcrossKeyOrder(t *testing.T) {
	a := gcPlanDigest("ws-1", 30, gcPlanPayload(nil))
	b := gcPlanDigest("ws-1", 30, gcPlanPayload(nil))
	if a != b {
		t.Fatalf("同一份计划必须得到同一摘要：%s vs %s", a, b)
	}
	if len(a) != 32 {
		t.Fatalf("摘要长度应为 32 位十六进制，得到 %d（%s）", len(a), a)
	}
}

func TestGcPlanDigestChangesWithEachDimension(t *testing.T) {
	base := gcPlanDigest("ws-1", 30, gcPlanPayload(nil))

	cases := []struct {
		name      string
		workspace string
		retention int
		payload   map[string]interface{}
	}{
		{"换 workspace（最危险的那类漂移）", "ws-2", 30, gcPlanPayload(nil)},
		{"换保留期", "ws-1", 7, gcPlanPayload(nil)},
		{"计划条目变化", "ws-1", 30, gcPlanPayload(map[string]interface{}{
			"planned": []interface{}{"refs/cp-c"},
		})},
		{"待删数量变化", "ws-1", 30, gcPlanPayload(map[string]interface{}{"deleted": float64(4)})},
		{"悬空引用变化", "ws-1", 30, gcPlanPayload(map[string]interface{}{
			"dangling_refs": []interface{}{"refs/missing"},
		})},
	}
	for _, c := range cases {
		got := gcPlanDigest(c.workspace, c.retention, c.payload)
		if got == base {
			t.Fatalf("%s 后摘要不变（等于没有绑定）", c.name)
		}
	}
}

func TestGcPlanDigestIgnoresPlannedOrder(t *testing.T) {
	a := gcPlanDigest("ws-1", 30, gcPlanPayload(map[string]interface{}{
		"planned": []interface{}{"refs/cp-a", "refs/cp-b"},
	}))
	b := gcPlanDigest("ws-1", 30, gcPlanPayload(map[string]interface{}{
		"planned": []interface{}{"refs/cp-b", "refs/cp-a"},
	}))
	if a != b {
		t.Fatal("计划条目顺序不同不该改变摘要（否则重算比对会随机失败）")
	}
}

// ── handler 层：用桩 engine 验证"执行前必须比对" ───────────────────────────

// stubEngine 起一个最小的引擎桩，按调用次数返回不同计划。
func stubEngine(t *testing.T, plans []map[string]interface{}) (*engine.PythonClient, *int) {
	t.Helper()
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/v1/internal/checkpoints/gc") {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		var body map[string]interface{}
		_ = json.NewDecoder(r.Body).Decode(&body)
		idx := calls
		if idx >= len(plans) {
			idx = len(plans) - 1
		}
		calls++
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(plans[idx])
	}))
	t.Cleanup(srv.Close)
	return engine.NewPythonClient(srv.URL), &calls
}

func gcRequest(t *testing.T, h http.HandlerFunc, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/admin/checkpoints/gc", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	h(rec, req)
	return rec
}

func TestCheckpointGcRequiresPlanDigestForDestructiveRun(t *testing.T) {
	python, _ := stubEngine(t, []map[string]interface{}{gcPlanPayload(nil)})
	h := CheckpointGcHandler(python)

	rec := gcRequest(t, h, `{"dry_run": false, "retention_days": 30}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("缺 plan_digest 的执行请求必须在查库之前就被拒（得到 %d: %s）", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "plan_digest") {
		t.Fatalf("错误信息要指出缺的是 plan_digest: %s", rec.Body.String())
	}
	if strings.Contains(rec.Body.String(), `"deleted":3`) {
		t.Fatal("被拒的请求不该真的删东西")
	}
}

// 比对逻辑单独验证：handler 需要 PG 才能走到这一步，而这一层与存储无关。
func TestVerifyGcPlanRejectsDrift(t *testing.T) {
	// 预览时是 A，执行前重算是 B ⇒ 必须判为"不一致"，并回传最新摘要。
	changed := gcPlanPayload(map[string]interface{}{
		"deleted": float64(9),
		"planned": []interface{}{"refs/other"},
	})
	python, _ := stubEngine(t, []map[string]interface{}{changed})

	previewDigest := gcPlanDigest("ws-1", 30, gcPlanPayload(nil))
	latest, same, err := verifyGcPlanAgainstSnapshot(
		context.Background(), python, "ws-1", []string{"/tmp/repo"}, 30, previewDigest)
	if err != nil {
		t.Fatalf("重算失败: %v", err)
	}
	if same {
		t.Fatal("计划变了却判为一致 —— 这正是要消灭的漂移")
	}
	if latest == previewDigest {
		t.Fatal("应回传**最新**摘要（供用户重新预览），而不是旧的")
	}
}

func TestVerifyGcPlanAcceptsMatch(t *testing.T) {
	plan := gcPlanPayload(nil)
	python, _ := stubEngine(t, []map[string]interface{}{plan})
	digest := gcPlanDigest("ws-1", 30, plan)

	latest, same, err := verifyGcPlanAgainstSnapshot(
		context.Background(), python, "ws-1", []string{"/tmp/repo"}, 30, digest)
	if err != nil {
		t.Fatalf("重算失败: %v", err)
	}
	if !same {
		t.Fatalf("同一份计划应判为一致（latest=%s, provided=%s）", latest, digest)
	}
}

func TestVerifyGcPlanRejectsWorkspaceSwitch(t *testing.T) {
	// 最危险的一类漂移：摘要内容相同，但目标是另一个 workspace。
	plan := gcPlanPayload(nil)
	python, _ := stubEngine(t, []map[string]interface{}{plan})
	digestForA := gcPlanDigest("ws-a", 30, plan)

	_, same, err := verifyGcPlanAgainstSnapshot(
		context.Background(), python, "ws-b", []string{"/tmp/repo"}, 30, digestForA)
	if err != nil {
		t.Fatalf("重算失败: %v", err)
	}
	if same {
		t.Fatal("换 workspace 后摘要必须不同（否则切项目不会被发现）")
	}
}

func TestGcPlanSnapshotUsesDryRun(t *testing.T) {
	python, _ := stubEngine(t, []map[string]interface{}{gcPlanPayload(nil)})
	// 桩不校验 dry_run 字段，但返回体必须是"计划"——这里确认函数不会 panic 且给出摘要。
	got, err := gcPlanSnapshot(context.Background(), python, "ws-1", []string{"/tmp/x"}, 30)
	if err != nil {
		t.Fatalf("快照失败: %v", err)
	}
	if want := gcPlanDigest("ws-1", 30, gcPlanPayload(nil)); got != want {
		t.Fatalf("快照摘要应与直接计算一致：%s vs %s", got, want)
	}
}
