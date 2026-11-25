package contract

// ── v2（H16b）：把 v1 里 deferred 的四族纳入契约 ──────────────────────────────
//
// 纳入口径只有一条：**跨语言或跨进程，且消费方不止一个**。每族排除掉的字段写在
// `contracts/versioned.json` 的 `excluded` 段里（带理由），这里只实现判据。
//
// 判据与那份文件的 `rules` **一一对应**：rules 里写了什么，这里就必须真校验什么。
// 反过来，rules 里明确说"不判罚 / 不设枚举"的（例如 queue.task_type 的未知类型、
// approval.reason 的长度），这里也真的不管 —— 契约过严与过松一样会让两端漂移。

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"
)

// maxUntrackedEntries 与网关的硬上限一致（internal/api/project_checkpoints.go 的
// ingestUntrackedMax，超了直接 400）。
const maxUntrackedEntries = 5000

// queueDeadlineLayout 是队列消息 deadline 的格式；消费方按同一布局解析
// （python-engine/app/queue/worker.py 的 strptime("%Y-%m-%dT%H:%M:%SZ")）。
const queueDeadlineLayout = "2006-01-02T15:04:05Z"

var (
	// 审批三态（C5）：approve / reject / edit。注意**不是** approved/denied/edited ——
	// 网关（submit_handler.go）与引擎（parse_approval_payload）用的就是这三个字面值。
	approvalDecisions = map[string]bool{"approve": true, "reject": true, "edit": true}
	// 检查点结果态：缺省 ok（老引擎不带它）。
	checkpointStates = map[string]bool{"ok": true, "failed": true}
	// 检查点错误码：与 checkpoint_service._classify_error 的三类一致。
	checkpointErrorCodes = map[string]bool{"git": true, "io": true, "unknown": true}
	// MCP server 状态：网关更新时就按这两个值校验（plugin_handler.go）。
	mcpStatuses = map[string]bool{"active": true, "inactive": true}
)

// ── 小工具：两端的类型判定必须逐字一致 ───────────────────────────────────────

func asString(v any) (string, bool) {
	s, ok := v.(string)
	return s, ok
}

func asObject(v any) (map[string]any, bool) {
	o, ok := v.(map[string]any)
	return o, ok
}

// asInt 收整数与"整值浮点"（`encoding/json` 把 JSON 数字解成 float64）；bool 不算整数。
func asInt(v any) (int64, bool) {
	switch n := v.(type) {
	case float64:
		if n != float64(int64(n)) {
			return 0, false
		}
		return int64(n), true
	case float32:
		if float64(n) != float64(int64(n)) {
			return 0, false
		}
		return int64(n), true
	case int:
		return int64(n), true
	case int32:
		return int64(n), true
	case int64:
		return n, true
	case json.Number:
		i, err := n.Int64()
		if err != nil {
			return 0, false
		}
		return i, true
	default:
		return 0, false
	}
}

// asLooseInt 额外收十进制数字串：Redis Stream 的字段**一律是字符串**，消费方也是按
// `_as_int`（int(value)）解析的。这里放宽的不是判据，而是传输形态。
func asLooseInt(v any) (int64, bool) {
	if i, ok := asInt(v); ok {
		return i, true
	}
	s, ok := v.(string)
	if !ok {
		return 0, false
	}
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, false
	}
	i, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return 0, false
	}
	return i, true
}

// queueDeadlineOK 判定 deadline 是否可被消费方解析：先钉住形状（Go 与 Python 的
// 宽松解析不一致，形状必须先一致），再真解析（挡住 2026-13-45 这类）。
func queueDeadlineOK(raw string) bool {
	if len(raw) != len("2006-01-02T15:04:05Z") {
		return false
	}
	for i, r := range raw {
		switch i {
		case 4, 7:
			if r != '-' {
				return false
			}
		case 10:
			if r != 'T' {
				return false
			}
		case 13, 16:
			if r != ':' {
				return false
			}
		case 19:
			if r != 'Z' {
				return false
			}
		default:
			if r < '0' || r > '9' {
				return false
			}
		}
	}
	_, err := time.Parse(queueDeadlineLayout, raw)
	return err == nil
}

// ── tool_approval ───────────────────────────────────────────────────────────

// ValidateToolApproval 校验审批决定载荷。
//
// 三条硬规则：session_id/tool_call_id 必填；decision 与 approved 至少给一个（缺字段
// 不得等同放行）；decision 只能是 approve/reject/edit，且 edit 必须带能解析成 JSON
// 对象的 arguments。`approved` 只为已发布的前端保留 —— 显式 false 与"没发"是两回事。
func ValidateToolApproval(payload map[string]any) []Violation {
	var out []Violation

	for _, field := range []string{"session_id", "tool_call_id"} {
		raw, exists := payload[field]
		if !exists {
			out = append(out, Violation{FamilyToolApproval, field, "必填"})
			continue
		}
		s, isStr := asString(raw)
		if !isStr {
			out = append(out, Violation{FamilyToolApproval, field, "必须是字符串"})
			continue
		}
		if strings.TrimSpace(s) == "" {
			out = append(out, Violation{FamilyToolApproval, field, "不得为空：缺了决定无法路由、票据也对不上"})
		}
	}

	decision := ""
	hasDecision := false
	if raw, exists := payload["decision"]; exists {
		hasDecision = true
		kind, isStr := asString(raw)
		if !isStr {
			out = append(out, Violation{FamilyToolApproval, "decision", "必须是字符串"})
			hasDecision = false
		} else if trimmed := strings.ToLower(strings.TrimSpace(kind)); !approvalDecisions[trimmed] {
			out = append(out, Violation{FamilyToolApproval, "decision", fmt.Sprintf("未知取值 %q（只允许 approve/reject/edit）", kind)})
			hasDecision = false
		} else {
			decision = trimmed
		}
	}

	approvedRaw, hasApproved := payload["approved"]
	if hasApproved {
		if _, isBool := asBoolValue(approvedRaw); !isBool {
			out = append(out, Violation{FamilyToolApproval, "approved", "必须是布尔"})
		}
	}
	if !hasDecision && !hasApproved {
		// 默认拒绝，而不是默认批准：漏发字段变成放行是安全事故，不是兼容性。
		out = append(out, Violation{FamilyToolApproval, "decision", "decision 与 approved 至少要有一个"})
	}

	if decision == "edit" {
		argsRaw, exists := payload["arguments"]
		if !exists {
			out = append(out, Violation{FamilyToolApproval, "arguments", "decision=edit 时必须提供"})
		} else if _, ok := editedArguments(argsRaw); !ok {
			out = append(out, Violation{FamilyToolApproval, "arguments", "必须是 JSON 对象的字符串编码"})
		}
	}
	// 非 edit 时 arguments 被引擎忽略（parse_approval_payload 不看它），这里也不判罚。
	return out
}

// asBoolValue 用类型断言取布尔；单独包装是为了和 asInt 的命名对称，避免误用 int 判定。
func asBoolValue(v any) (bool, bool) {
	b, ok := v.(bool)
	return b, ok
}

// editedArguments 归一 edit 的参数：既接受 JSON 字符串，也接受已解析的对象。
func editedArguments(raw any) (map[string]any, bool) {
	if obj, ok := asObject(raw); ok {
		return obj, true
	}
	s, ok := asString(raw)
	if !ok {
		return nil, false
	}
	if strings.TrimSpace(s) == "" {
		return nil, false
	}
	var parsed map[string]any
	if err := json.Unmarshal([]byte(s), &parsed); err != nil {
		return nil, false
	}
	return parsed, true
}

// ── checkpoint ──────────────────────────────────────────────────────────────

// ValidateCheckpoint 校验检查点**上报**载荷（引擎 → 网关 /v1/internal/checkpoints）。
//
// 关键判据：state=ok 时 commit/tree/ref 必须齐全（缺了就没有可回滚的目标）；
// state=failed 时不要求它们（为空正是失败原因，而失败记录同样必须能上报）。
func ValidateCheckpoint(payload map[string]any) []Violation {
	var out []Violation

	sessionID := ""
	if raw, exists := payload["session_id"]; !exists {
		out = append(out, Violation{FamilyCheckpoint, "session_id", "必填"})
	} else if s, isStr := asString(raw); !isStr {
		out = append(out, Violation{FamilyCheckpoint, "session_id", "必须是字符串"})
	} else {
		sessionID = strings.TrimSpace(s)
		if sessionID == "" {
			out = append(out, Violation{FamilyCheckpoint, "session_id", "不得为空：网关按它反查会话所属项目"})
		}
	}

	if raw, exists := payload["seq"]; !exists {
		out = append(out, Violation{FamilyCheckpoint, "seq", "必填"})
	} else if seq, ok := asInt(raw); !ok {
		out = append(out, Violation{FamilyCheckpoint, "seq", "必须是整数"})
	} else if seq <= 0 {
		out = append(out, Violation{FamilyCheckpoint, "seq", "必须是正整数：主键由 (session_id, seq, kind) 派生"})
	}

	if raw, exists := payload["kind"]; !exists {
		out = append(out, Violation{FamilyCheckpoint, "kind", "必填"})
	} else if s, isStr := asString(raw); !isStr {
		out = append(out, Violation{FamilyCheckpoint, "kind", "必须是字符串"})
	} else if strings.TrimSpace(s) == "" {
		out = append(out, Violation{FamilyCheckpoint, "kind", "不得为空"})
	}

	state := "ok"
	if raw, exists := payload["state"]; exists {
		s, isStr := asString(raw)
		if !isStr {
			out = append(out, Violation{FamilyCheckpoint, "state", "必须是字符串"})
			state = ""
		} else if trimmed := strings.ToLower(strings.TrimSpace(s)); !checkpointStates[trimmed] {
			out = append(out, Violation{FamilyCheckpoint, "state", fmt.Sprintf("未知取值 %q（只允许 ok/failed）", s)})
			state = ""
		} else {
			state = trimmed
		}
	}

	// 成功态必须有可回滚的目标；失败态不要求（那正是失败的表现）。
	for _, field := range []string{"commit_sha", "tree_sha", "ref_name"} {
		raw, exists := payload[field]
		if !exists {
			if state == "ok" {
				out = append(out, Violation{FamilyCheckpoint, field, "state=ok 时必填（否则这份快照没有可回滚的目标）"})
			}
			continue
		}
		s, isStr := asString(raw)
		if !isStr {
			out = append(out, Violation{FamilyCheckpoint, field, "必须是字符串"})
			continue
		}
		if state == "ok" && strings.TrimSpace(s) == "" {
			out = append(out, Violation{FamilyCheckpoint, field, "state=ok 时不得为空"})
		}
	}

	for _, field := range []string{"parent_sha", "head_sha", "error"} {
		if raw, exists := payload[field]; exists {
			if _, isStr := asString(raw); !isStr {
				out = append(out, Violation{FamilyCheckpoint, field, "必须是字符串"})
			}
		}
	}

	if raw, exists := payload["error_code"]; exists {
		s, isStr := asString(raw)
		if !isStr {
			out = append(out, Violation{FamilyCheckpoint, "error_code", "必须是字符串"})
		} else if trimmed := strings.ToLower(strings.TrimSpace(s)); trimmed != "" && !checkpointErrorCodes[trimmed] {
			out = append(out, Violation{FamilyCheckpoint, "error_code", fmt.Sprintf("未知取值 %q（只允许 git/io/unknown）", s)})
		}
	}

	for _, field := range []string{"file_count", "skipped_count", "byte_size"} {
		raw, exists := payload[field]
		if !exists {
			continue
		}
		n, ok := asInt(raw)
		if !ok {
			out = append(out, Violation{FamilyCheckpoint, field, "必须是整数"})
			continue
		}
		if n < 0 {
			out = append(out, Violation{FamilyCheckpoint, field, "必须是非负整数"})
		}
	}

	if raw, exists := payload["untracked"]; exists {
		out = append(out, validateUntracked(FamilyCheckpoint, raw)...)
	}
	return out
}

// validateUntracked 校验未跟踪文件清单：网关的硬上限之外，每条都必须带非空
// path 与 sha256 —— 清单不全时回滚会"成功"但文件回不来。
func validateUntracked(family Family, raw any) []Violation {
	var out []Violation
	items, isSlice := raw.([]any)
	if !isSlice {
		return []Violation{{family, "untracked", "必须是数组"}}
	}
	if len(items) > maxUntrackedEntries {
		out = append(out, Violation{family, "untracked", fmt.Sprintf("条目数 %d 超过上限 %d", len(items), maxUntrackedEntries)})
	}
	for i, item := range items {
		entry, isObj := asObject(item)
		if !isObj {
			out = append(out, Violation{family, fmt.Sprintf("untracked[%d]", i), "必须是对象"})
			continue
		}
		for _, field := range []string{"path", "sha256"} {
			value, exists := entry[field]
			if !exists {
				out = append(out, Violation{family, fmt.Sprintf("untracked[%d].%s", i, field), "必填"})
				continue
			}
			s, isStr := asString(value)
			if !isStr {
				out = append(out, Violation{family, fmt.Sprintf("untracked[%d].%s", i, field), "必须是字符串"})
				continue
			}
			if strings.TrimSpace(s) == "" {
				out = append(out, Violation{family, fmt.Sprintf("untracked[%d].%s", i, field), "不得为空：回滚按 sha256 从 CAS 取内容"})
			}
		}
		if sizeRaw, exists := entry["size"]; exists {
			if n, ok := asInt(sizeRaw); !ok {
				out = append(out, Violation{family, fmt.Sprintf("untracked[%d].size", i), "必须是整数"})
			} else if n < 0 {
				out = append(out, Violation{family, fmt.Sprintf("untracked[%d].size", i), "必须是非负整数"})
			}
		}
	}
	return out
}

// ── queue ───────────────────────────────────────────────────────────────────

// ValidateQueue 校验队列任务消息（Redis Stream 载荷）。
//
// 注意两件事：(1) Redis 里字段一律是字符串，所以 tenant_id / retry_count 收数字串 ——
// 放宽的是传输形态，不是判据；(2) task_type **不设枚举**：新增任务类型是 additive 的，
// 未知类型由消费方转发/送 DLQ，不在这里判违约。
func ValidateQueue(payload map[string]any) []Violation {
	var out []Violation

	for _, field := range []string{"task_id", "task_type"} {
		raw, exists := payload[field]
		if !exists {
			out = append(out, Violation{FamilyQueue, field, "必填"})
			continue
		}
		s, isStr := asString(raw)
		if !isStr {
			out = append(out, Violation{FamilyQueue, field, "必须是字符串"})
			continue
		}
		if strings.TrimSpace(s) == "" {
			out = append(out, Violation{FamilyQueue, field, "不得为空"})
		}
	}

	if raw, exists := payload["tenant_id"]; !exists {
		out = append(out, Violation{FamilyQueue, "tenant_id", "必填"})
	} else if n, ok := asLooseInt(raw); !ok {
		out = append(out, Violation{FamilyQueue, "tenant_id", "必须是整数或十进制数字串（消费方会静默回落 0 = 无租户任务）"})
	} else if n < 0 {
		out = append(out, Violation{FamilyQueue, "tenant_id", "必须是非负整数"})
	}

	if raw, exists := payload["payload"]; !exists {
		out = append(out, Violation{FamilyQueue, "payload", "必填"})
	} else if s, isStr := asString(raw); !isStr {
		out = append(out, Violation{FamilyQueue, "payload", "必须是 JSON 对象的字符串编码"})
	} else {
		var body map[string]any
		if err := json.Unmarshal([]byte(s), &body); err != nil {
			out = append(out, Violation{FamilyQueue, "payload", "必须是 JSON 对象的字符串编码"})
		}
	}

	if raw, exists := payload["retry_count"]; exists {
		if n, ok := asLooseInt(raw); !ok {
			out = append(out, Violation{FamilyQueue, "retry_count", "必须是整数或十进制数字串"})
		} else if n < 0 {
			out = append(out, Violation{FamilyQueue, "retry_count", "必须是非负整数：它是进 DLQ 的唯一依据"})
		}
	}

	if raw, exists := payload["idempotency_key"]; exists {
		if _, isStr := asString(raw); !isStr {
			out = append(out, Violation{FamilyQueue, "idempotency_key", "必须是字符串"})
		}
		// 空串等价于未提供（消费方回落 {task_type}:{task_id}），不判罚。
	}

	if raw, exists := payload["deadline"]; exists {
		s, isStr := asString(raw)
		if !isStr {
			out = append(out, Violation{FamilyQueue, "deadline", "必须是字符串"})
		} else if !queueDeadlineOK(strings.TrimSpace(s)) {
			out = append(out, Violation{FamilyQueue, "deadline", "必须是 YYYY-MM-DDTHH:MM:SSZ（解析失败会被当作没有截止时间直接执行）"})
		}
	}
	return out
}

// ── mcp ─────────────────────────────────────────────────────────────────────

// ValidateMCP 校验 MCP server 配置条目（plugins.json 的 mcp_servers[i]）。
//
// 关键判据是**授权两件套**：approved=true 而 approval_digest 为空，等于"不知道批了
// 什么"，两端都按未授权处理（plugin_approval.go / store.py 同口径）。
func ValidateMCP(payload map[string]any) []Violation {
	var out []Violation

	for _, field := range []string{"name", "command"} {
		raw, exists := payload[field]
		if !exists {
			out = append(out, Violation{FamilyMCP, field, "必填"})
			continue
		}
		s, isStr := asString(raw)
		if !isStr {
			out = append(out, Violation{FamilyMCP, field, "必须是字符串"})
			continue
		}
		if strings.TrimSpace(s) == "" {
			out = append(out, Violation{FamilyMCP, field, "不得为空：引擎按 name 分流、按 command 起子进程"})
		}
	}

	if raw, exists := payload["args"]; exists {
		items, isSlice := raw.([]any)
		if !isSlice {
			out = append(out, Violation{FamilyMCP, "args", "必须是字符串数组"})
		} else {
			for i, item := range items {
				if _, isStr := asString(item); !isStr {
					out = append(out, Violation{FamilyMCP, fmt.Sprintf("args[%d]", i), "必须是字符串"})
				}
			}
		}
	}

	if raw, exists := payload["env"]; exists {
		env, isObj := asObject(raw)
		if !isObj {
			out = append(out, Violation{FamilyMCP, "env", "必须是 {字符串: 字符串}"})
		} else {
			for key, value := range env {
				if _, isStr := asString(value); !isStr {
					out = append(out, Violation{FamilyMCP, "env." + key, "必须是字符串（env 里装的是凭据/端点）"})
				}
			}
		}
	}

	if raw, exists := payload["status"]; exists {
		s, isStr := asString(raw)
		if !isStr {
			out = append(out, Violation{FamilyMCP, "status", "必须是字符串"})
		} else if trimmed := strings.ToLower(strings.TrimSpace(s)); !mcpStatuses[trimmed] {
			out = append(out, Violation{FamilyMCP, "status", fmt.Sprintf("未知取值 %q（只允许 active/inactive）", s)})
		}
	}

	approved := false
	if raw, exists := payload["approved"]; exists {
		b, isBool := asBoolValue(raw)
		if !isBool {
			out = append(out, Violation{FamilyMCP, "approved", "必须是布尔"})
		} else {
			approved = b
		}
	}

	digest := ""
	if raw, exists := payload["approval_digest"]; exists {
		s, isStr := asString(raw)
		if !isStr {
			out = append(out, Violation{FamilyMCP, "approval_digest", "必须是字符串"})
		} else {
			digest = strings.TrimSpace(s)
		}
	}
	if approved && digest == "" {
		out = append(out, Violation{FamilyMCP, "approval_digest", "approved=true 时必须非空（没有摘要的批准按未授权处理）"})
	}

	if raw, exists := payload["read_only"]; exists {
		if _, isBool := asBoolValue(raw); !isBool {
			out = append(out, Violation{FamilyMCP, "read_only", "必须是布尔"})
		}
	}
	return out
}
