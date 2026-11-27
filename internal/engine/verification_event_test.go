package engine

import (
	"encoding/json"
	"strings"
	"testing"
)

// N6：`done.verification` 必须在**网关重新序列化**后仍然存在。
//
// 这条链路是"引擎 → PythonEvent → broadcast.Event → SSE 帧"三段序列化：
// PythonEvent 没声明的字段会在中间那段静默消失，前端只能继续看模型自述
// "改完了" —— Budget/Context/Cache 都踩过同一个坑。
func TestPythonEventCarriesVerification(t *testing.T) {
	raw := `{"type":"done","verification":{"status":"failed","command":"go test ./...","exit_code":2}}`
	var ev PythonEvent
	if err := json.Unmarshal([]byte(raw), &ev); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if ev.Verification == nil {
		t.Fatal("verification 没有被解析：字段未声明")
	}
	if got := ev.Verification["status"]; got != "failed" {
		t.Fatalf("status = %v, want failed", got)
	}
	if got := ev.Verification["command"]; got != "go test ./..." {
		t.Fatalf("command = %v", got)
	}

	out, err := json.Marshal(ev)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if !strings.Contains(string(out), `"verification"`) {
		t.Fatalf("重新序列化后 verification 丢了: %s", out)
	}
}
