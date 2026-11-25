package contract

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ── H16b：契约 v2（tool_approval / checkpoint / queue / mcp）──────────────────
//
// 与 `contract_conformance_h16_test.go` 同一套路：夹具住在
// `contracts/fixtures/conformance.json`，Python 侧跑的是**同一份文件**。这里额外补三
// 类断言，防止"契约文件写了、实现没跟上"：
//
//  1. `versioned.json` 里出现的每个族都必须有实现（分派不报错），且 deferred 已清空；
//  2. 每族夹具数量 / 反例 / forward-compat 覆盖面达标（否则"纳入契约"只是写了个字）；
//  3. 几族**有条目形状语义**的判据各自有独立用例（枚举、必填联动、传输形态）。

type v2FixtureCase struct {
	Family        string         `json:"family"`
	Name          string         `json:"name"`
	Valid         bool           `json:"valid"`
	Reason        string         `json:"reason"`
	ForwardCompat bool           `json:"forward_compat"`
	Payload       map[string]any `json:"payload"`
}

type v2FixtureFile struct {
	Version int             `json:"version"`
	Cases   []v2FixtureCase `json:"cases"`
}

type versionedFamily struct {
	Description string                     `json:"description"`
	Fields      map[string]json.RawMessage `json:"fields"`
	Rules       []string                   `json:"rules"`
	Excluded    []struct {
		Field  string `json:"field"`
		Reason string `json:"reason"`
	} `json:"excluded"`
}

type versionedFile struct {
	Contract string                    `json:"contract"`
	Version  int                       `json:"version"`
	Families map[string]versionedFamily `json:"families"`
	Deferred struct {
		Families []string `json:"families"`
		Note     string   `json:"note"`
	} `json:"deferred"`
}

func loadVersioned(t *testing.T) versionedFile {
	t.Helper()
	path := filepath.Join("..", "..", "contracts", "versioned.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读取契约文件失败: %v", err)
	}
	var file versionedFile
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatalf("契约文件不是合法 JSON: %v", err)
	}
	return file
}

func loadV2Fixtures(t *testing.T) v2FixtureFile {
	t.Helper()
	path := filepath.Join("..", "..", "contracts", "fixtures", "conformance.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读取夹具失败: %v", err)
	}
	var file v2FixtureFile
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatalf("夹具不是合法 JSON: %v", err)
	}
	return file
}

// TestVersionedFileIsInSyncWithImplementation 把"契约文件"与"实现"钉在一起。
//
// 契约文件是**数据**，实现是**代码**：两边不一致时（新增族只改了文件、或实现漏注册）
// 这里必须红 —— 否则契约会重新退化成"文档"。
func TestVersionedFileIsInSyncWithImplementation(t *testing.T) {
	contractFile := loadVersioned(t)
	if contractFile.Version != Version {
		t.Fatalf("契约文件版本 %d 与实现版本 %d 不一致", contractFile.Version, Version)
	}
	if len(contractFile.Deferred.Families) != 0 {
		t.Fatalf("v2 应已把 deferred 清空，仍有: %v", contractFile.Deferred.Families)
	}
	if len(contractFile.Families) == 0 {
		t.Fatal("契约文件里没有任何族")
	}
	for name, family := range contractFile.Families {
		if _, err := Validate(name, map[string]any{}); err != nil {
			t.Fatalf("族 %q 在契约文件里出现但实现未注册: %v", name, err)
		}
		if strings.TrimSpace(family.Description) == "" {
			t.Fatalf("族 %q 缺 description", name)
		}
		if len(family.Fields) == 0 {
			t.Fatalf("族 %q 缺 fields", name)
		}
		if len(family.Rules) == 0 {
			t.Fatalf("族 %q 缺 rules", name)
		}
	}
	// v2 必须把 v1 的四个 deferred 族全部纳入。
	for _, name := range []string{"tool_approval", "checkpoint", "queue", "mcp"} {
		if _, exists := contractFile.Families[name]; !exists {
			t.Fatalf("v2 缺族 %q", name)
		}
	}
}

// TestFixtureCoveragePerFamily 保证每族都有"能区分对错"的最小用例集。
func TestFixtureCoveragePerFamily(t *testing.T) {
	fixtures := loadV2Fixtures(t)
	if fixtures.Version != Version {
		t.Fatalf("夹具版本 %d 与契约版本 %d 不一致", fixtures.Version, Version)
	}
	type coverage struct {
		total     int
		valid     int
		invalid   int
		forwardOK int
	}
	stats := map[string]*coverage{}
	for _, tc := range fixtures.Cases {
		entry := stats[tc.Family]
		if entry == nil {
			entry = &coverage{}
			stats[tc.Family] = entry
		}
		entry.total++
		if tc.Valid {
			entry.valid++
		} else {
			entry.invalid++
		}
		if tc.ForwardCompat {
			if !tc.Valid {
				t.Fatalf("%s/%s 标了 forward_compat 却是非法例（未知字段必须被忽略）", tc.Family, tc.Name)
			}
			entry.forwardOK++
		}
	}
	for family := range loadVersioned(t).Families {
		entry := stats[family]
		if entry == nil {
			t.Fatalf("族 %q 一条夹具都没有", family)
		}
		if entry.total < 4 {
			t.Fatalf("族 %q 只有 %d 条夹具（要求 ≥4）", family, entry.total)
		}
		if entry.invalid < 1 {
			t.Fatalf("族 %q 缺非法例：改坏实现也不会有夹具红", family)
		}
		// forward-compat 只有 v2 新族强制要求：v1 三族的既有用例保持原样不动。
		switch family {
		case string(FamilyToolApproval), string(FamilyCheckpoint), string(FamilyQueue), string(FamilyMCP):
			if entry.forwardOK < 1 {
				t.Fatalf("族 %q 缺 forward-compat 例：未知字段必须被忽略", family)
			}
		}
	}
}

// TestUnknownFamilyNamesAreRejected 覆盖"像但不对"的族名：拼错、驼峰、连字符。
//
// 静默通过会让写错的族名变成"没有任何校验"的假绿灯 —— 这比校验本身写错更难发现。
func TestUnknownFamilyNamesAreRejected(t *testing.T) {
	for _, name := range []string{"", "nope", "tool-approval", "ToolApproval", "approval", "queues", "mcp_server"} {
		if _, err := Validate(name, map[string]any{}); err == nil {
			t.Fatalf("未知族名 %q 必须报错", name)
		}
	}
	// 大小写不敏感是契约刻意允许的（网关日志里出现过大写），别把它当未知族。
	if _, err := Validate("TOOL_APPROVAL", map[string]any{}); err != nil {
		t.Fatalf("族名大小写不敏感: %v", err)
	}
}

func validateOK(t *testing.T, family string, payload map[string]any) {
	t.Helper()
	violations, err := Validate(family, payload)
	if err != nil {
		t.Fatalf("族名无效: %v", err)
	}
	if len(violations) > 0 {
		t.Fatalf("应通过却被判违约: %v", violations)
	}
}

func validateFail(t *testing.T, family, field string, payload map[string]any) {
	t.Helper()
	violations, err := Validate(family, payload)
	if err != nil {
		t.Fatalf("族名无效: %v", err)
	}
	if len(violations) == 0 {
		t.Fatalf("应违约却通过了（期望字段 %s）", field)
	}
	if field == "" {
		return
	}
	for _, v := range violations {
		if v.Field == field {
			return
		}
	}
	t.Fatalf("违约字段不是 %s: %v", field, violations)
}

// ── tool_approval ───────────────────────────────────────────────────────────

func TestToolApprovalTrinaryDecision(t *testing.T) {
	base := func(decision string) map[string]any {
		payload := map[string]any{"session_id": "s", "tool_call_id": "c", "decision": decision}
		if decision == "edit" {
			payload["arguments"] = `{"command":"ls"}`
		}
		return payload
	}
	for _, decision := range []string{"approve", "reject", "edit"} {
		validateOK(t, "tool_approval", base(decision))
	}
	// edit 的 arguments 是必填：缺了就无法重跑分级。
	validateFail(t, "tool_approval", "arguments", map[string]any{
		"session_id": "s", "tool_call_id": "c", "decision": "edit",
	})
	// 大小写/空白归一后仍然合法（网关 trim 过再透传）。
	validateOK(t, "tool_approval", base(" Approve "))
	// approved/denied/edited 不是本契约的取值 —— 别被"看起来像"骗过去。
	for _, wrong := range []string{"approved", "denied", "edited", "allow"} {
		validateFail(t, "tool_approval", "decision", base(wrong))
	}
}

func TestToolApprovalLegacyApprovedFlag(t *testing.T) {
	// 旧前端只发 approved：显式 false 是"拒绝"，不是"没发"。
	validateOK(t, "tool_approval", map[string]any{
		"session_id": "s", "tool_call_id": "c", "approved": false,
	})
	// "false" 这种字符串不得被当真 —— 它决定放行与否。
	validateFail(t, "tool_approval", "approved", map[string]any{
		"session_id": "s", "tool_call_id": "c", "approved": "false",
	})
}

func TestToolApprovalEditArgumentsMustBeObject(t *testing.T) {
	validateOK(t, "tool_approval", map[string]any{
		"session_id": "s", "tool_call_id": "c", "decision": "edit",
		"arguments": map[string]any{"command": "ls"},
	})
	for _, bad := range []any{"[1,2]", "\"x\"", "", "{not json", 42} {
		validateFail(t, "tool_approval", "arguments", map[string]any{
			"session_id": "s", "tool_call_id": "c", "decision": "edit", "arguments": bad,
		})
	}
}

// ── checkpoint ──────────────────────────────────────────────────────────────

func TestCheckpointOkRequiresRollbackTarget(t *testing.T) {
	full := map[string]any{
		"session_id": "s", "seq": 1, "kind": "pre_turn",
		"commit_sha": "c1", "tree_sha": "t1", "ref_name": "r1",
	}
	validateOK(t, "checkpoint", full)
	for _, field := range []string{"commit_sha", "tree_sha", "ref_name"} {
		payload := map[string]any{}
		for k, v := range full {
			payload[k] = v
		}
		delete(payload, field)
		validateFail(t, "checkpoint", field, payload)
	}
	// 失败态不要求它们：为空正是失败原因，失败记录同样必须能上报。
	validateOK(t, "checkpoint", map[string]any{
		"session_id": "s", "seq": 2, "kind": "post_turn",
		"state": "failed", "error": "写 ref 失败", "error_code": "git",
	})
}

func TestCheckpointStateAndSeqBounds(t *testing.T) {
	base := map[string]any{
		"session_id": "s", "seq": 1, "kind": "pre_turn",
		"commit_sha": "c", "tree_sha": "t", "ref_name": "r",
	}
	withState := func(state any) map[string]any {
		payload := map[string]any{"state": state}
		for k, v := range base {
			payload[k] = v
		}
		return payload
	}
	// 大小写与前后空白归一后仍是合法值（读取侧本来就这么比）。
	for _, okState := range []any{"ok", "OK", " failed ", "Failed"} {
		validateOK(t, "checkpoint", withState(okState))
	}
	for _, badState := range []any{"partial", "failed_hard", 1} {
		validateFail(t, "checkpoint", "state", withState(badState))
	}
	for _, badSeq := range []any{0, -1, 1.5, "1"} {
		payload := map[string]any{"seq": badSeq}
		for k, v := range base {
			if k == "seq" {
				continue
			}
			payload[k] = v
		}
		validateFail(t, "checkpoint", "seq", payload)
	}
}

func TestCheckpointUntrackedManifest(t *testing.T) {
	base := map[string]any{
		"session_id": "s", "seq": 1, "kind": "post_turn",
		"commit_sha": "c", "tree_sha": "t", "ref_name": "r",
	}
	withUntracked := func(items any) map[string]any {
		payload := map[string]any{"untracked": items}
		for k, v := range base {
			payload[k] = v
		}
		return payload
	}
	validateOK(t, "checkpoint", withUntracked([]any{
		map[string]any{"path": "a.txt", "sha256": "aa", "size": 3},
	}))
	validateFail(t, "checkpoint", "untracked", withUntracked("a.txt"))
	validateFail(t, "checkpoint", "untracked[0]", withUntracked([]any{"a.txt"}))
	validateFail(t, "checkpoint", "untracked[0].path", withUntracked([]any{
		map[string]any{"sha256": "aa"},
	}))
	validateFail(t, "checkpoint", "untracked[0].sha256", withUntracked([]any{
		map[string]any{"path": "a.txt", "sha256": ""},
	}))
	validateFail(t, "checkpoint", "untracked[0].size", withUntracked([]any{
		map[string]any{"path": "a.txt", "sha256": "aa", "size": -1},
	}))
	// 超过网关硬上限必须判违约（而不是"安静截断"）。
	many := make([]any, maxUntrackedEntries+1)
	for i := range many {
		many[i] = map[string]any{"path": "a.txt", "sha256": "aa"}
	}
	validateFail(t, "checkpoint", "untracked", withUntracked(many))
}

// ── queue ───────────────────────────────────────────────────────────────────

func TestQueueAcceptsRedisStringEncodings(t *testing.T) {
	// Redis 字段一律是字符串：数字串必须收，否则真实消息全被判违约。
	validateOK(t, "queue", map[string]any{
		"task_id": "t1", "task_type": "memory_save",
		"tenant_id": "7", "payload": `{"a":1}`,
		"retry_count": "1", "deadline": "2026-11-26T00:00:00Z",
	})
	validateOK(t, "queue", map[string]any{
		"task_id": "t1", "task_type": "memory_save", "tenant_id": 7, "payload": `{}`,
	})
	// 空 idempotency_key 等价于未提供（消费方回落默认键），不判罚。
	validateOK(t, "queue", map[string]any{
		"task_id": "t1", "task_type": "memory_save", "tenant_id": 7, "payload": `{}`,
		"idempotency_key": "",
	})
}

func TestQueueRejectsDirtyFields(t *testing.T) {
	base := func() map[string]any {
		return map[string]any{"task_id": "t1", "task_type": "memory_save", "tenant_id": 7, "payload": `{}`}
	}
	validateFail(t, "queue", "tenant_id", map[string]any{
		"task_id": "t1", "task_type": "memory_save", "tenant_id": "acme", "payload": `{}`,
	})
	validateFail(t, "queue", "payload", map[string]any{
		"task_id": "t1", "task_type": "memory_save", "tenant_id": 7, "payload": `[1,2]`,
	})
	validateFail(t, "queue", "retry_count", map[string]any{
		"task_id": "t1", "task_type": "memory_save", "tenant_id": 7, "payload": `{}`, "retry_count": -1,
	})
	for _, bad := range []string{"2026-11-26 00:00:00", "2026-13-45T00:00:00Z", "2026-11-26T00:00:00"} {
		payload := base()
		payload["deadline"] = bad
		validateFail(t, "queue", "deadline", payload)
	}
	// task_type 不设枚举：新增任务类型是 additive 的，未知类型由消费方送 DLQ。
	validateOK(t, "queue", map[string]any{
		"task_id": "t1", "task_type": "brand_new_task", "tenant_id": 7, "payload": `{}`,
	})
}

// ── mcp ─────────────────────────────────────────────────────────────────────

func TestMCPApprovalRequiresDigest(t *testing.T) {
	validateOK(t, "mcp", map[string]any{
		"name": "xerj", "command": "npx", "approved": true,
		"approval_digest": "0123456789abcdef0123456789abcdef",
	})
	validateFail(t, "mcp", "approval_digest", map[string]any{
		"name": "xerj", "command": "npx", "approved": true,
	})
	// 未授权（默认 false）不要求摘要：新装的插件就是"没批过"。
	validateOK(t, "mcp", map[string]any{"name": "xerj", "command": "npx"})
}

func TestMCPFieldShapes(t *testing.T) {
	validateOK(t, "mcp", map[string]any{
		"name": "xerj", "command": "npx", "args": []any{"-y", "xerj-mcp"},
		"env": map[string]any{"TOKEN": "t"}, "status": "active", "read_only": false,
	})
	validateFail(t, "mcp", "command", map[string]any{"name": "xerj"})
	validateFail(t, "mcp", "status", map[string]any{"name": "x", "command": "npx", "status": "enabled"})
	validateFail(t, "mcp", "args", map[string]any{"name": "x", "command": "npx", "args": "npx -y"})
	validateFail(t, "mcp", "args[0]", map[string]any{"name": "x", "command": "npx", "args": []any{1}})
	validateFail(t, "mcp", "env", map[string]any{"name": "x", "command": "npx", "env": []any{}})
	validateFail(t, "mcp", "env.TOKEN", map[string]any{
		"name": "x", "command": "npx", "env": map[string]any{"TOKEN": 1},
	})
	validateFail(t, "mcp", "read_only", map[string]any{"name": "x", "command": "npx", "read_only": "yes"})
}

// TestV2FamiliesIgnoreUnknownFields 是契约的**兼容承诺**：同版本内新增字段不得让旧
// 校验失败（夹具里每族也各有一条这样的用例）。
func TestV2FamiliesIgnoreUnknownFields(t *testing.T) {
	validateOK(t, "tool_approval", map[string]any{
		"session_id": "s", "tool_call_id": "c", "decision": "approve",
		"question": "执行吗", "level": "delete", "risk": "high", "future": 1,
	})
	validateOK(t, "checkpoint", map[string]any{
		"session_id": "s", "seq": 1, "kind": "pre_turn",
		"commit_sha": "c", "tree_sha": "t", "ref_name": "r",
		"label": "", "future": map[string]any{"x": 1},
	})
	validateOK(t, "queue", map[string]any{
		"task_id": "t", "task_type": "memory_save", "tenant_id": 7, "payload": `{}`,
		"created_at": "2026-11-25T00:00:00Z", "priority": "1", "trace_id": "tr",
	})
	validateOK(t, "mcp", map[string]any{
		"name": "x", "command": "npx", "description": "d", "version": "1.0.0",
		"source": "market", "future": true,
	})
}
