package contract

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ── H16：两端跑**同一组** conformance 夹具 ──────────────────────────────────
//
// 夹具住在 `contracts/fixtures/conformance.json`，Python 侧
// （`python-engine/tests/test_contract_h16.py`）跑的是同一份文件。任何一端改了判据
// 都会在这里或那里失败 —— 这就是"契约"和"文档"的区别。

type fixtureCase struct {
	Family  string         `json:"family"`
	Name    string         `json:"name"`
	Valid   bool           `json:"valid"`
	Reason  string         `json:"reason"`
	Payload map[string]any `json:"payload"`
}

type fixtureFile struct {
	Version int           `json:"version"`
	Cases   []fixtureCase `json:"cases"`
}

func loadFixtures(t *testing.T) fixtureFile {
	t.Helper()
	path := filepath.Join("..", "..", "contracts", "fixtures", "conformance.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读取夹具失败: %v", err)
	}
	var file fixtureFile
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatalf("夹具不是合法 JSON: %v", err)
	}
	return file
}

func TestContractVersionMatchesFixtures(t *testing.T) {
	file := loadFixtures(t)
	if file.Version != Version {
		t.Fatalf("夹具版本 %d 与契约版本 %d 不一致", file.Version, Version)
	}
}

func TestConformanceFixtures(t *testing.T) {
	file := loadFixtures(t)
	if len(file.Cases) == 0 {
		t.Fatal("夹具为空")
	}
	for _, tc := range file.Cases {
		tc := tc
		t.Run(tc.Family+"/"+tc.Name, func(t *testing.T) {
			if tc.Name == "message-too-long" {
				// 夹具用占位串避免文件里出现 4KB 空白；这里把它换成真实超长消息。
				tc.Payload["message"] = strings.Repeat("x", maxMessageBytes+1)
			}
			violations, err := Validate(tc.Family, tc.Payload)
			if err != nil {
				t.Fatalf("族名无效: %v", err)
			}
			if tc.Valid && len(violations) > 0 {
				t.Fatalf("应通过却被判违约: %v", violations)
			}
			if !tc.Valid && len(violations) == 0 {
				t.Fatalf("应违约却通过了（%s）", tc.Reason)
			}
		})
	}
}

func TestUnknownFamilyIsRejected(t *testing.T) {
	if _, err := Validate("nope", map[string]any{}); err == nil {
		t.Fatal("未知族名必须报错，不能静默通过")
	}
}

func TestAdditiveFieldsDoNotBreakValidation(t *testing.T) {
	// additive-only：同版本内新增字段不得让旧校验失败。
	payload := map[string]any{"message": "ok", "brand_new_field": []any{1, 2, 3}}
	if v := ValidateError(payload); len(v) > 0 {
		t.Fatalf("新增字段不该被判违约: %v", v)
	}
	errBody := map[string]any{"message": "boom", "extra": true}
	terminal := map[string]any{"type": "error", "error": errBody, "future": "x"}
	if v := ValidateTerminal(terminal); len(v) > 0 {
		t.Fatalf("新增字段不该被判违约: %v", v)
	}
}

func TestTerminalSequenceRejectsDuplicateTerminal(t *testing.T) {
	events := []map[string]any{
		{"type": "delta", "text": "a"},
		{"type": "done"},
		{"type": "done"},
	}
	violations := TerminalSequence(events)
	if len(violations) == 0 {
		t.Fatal("流里出现两个终态事件必须判违约（前端会一直等下去）")
	}
}

func TestTerminalSequenceAcceptsSingleTerminal(t *testing.T) {
	events := []map[string]any{
		{"type": "delta"},
		{"type": "error", "error": map[string]any{"message": "上游挂了"}},
	}
	if v := TerminalSequence(events); len(v) > 0 {
		t.Fatalf("单个终态事件应通过: %v", v)
	}
}

func TestDecodeRejectsInvalidJSON(t *testing.T) {
	if _, _, err := Decode("error", []byte("not json")); err == nil {
		t.Fatal("非 JSON 必须报错")
	}
	_, violations, err := Decode("error", []byte(`{"message":"x"}`))
	if err != nil || len(violations) != 0 {
		t.Fatalf("合法载荷不该有问题: %v %v", err, violations)
	}
}
