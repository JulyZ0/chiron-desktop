// Package contract 是 Go 与 Python 之间的**版本化线上契约**（PRD 9.2 / H16）。
//
// 为什么需要它：`error` 与 `terminal` 两族载荷由网关产出、引擎也产出，前端与 SDK
// 消费。任何一侧悄悄加/改字段，另一侧都只会在运行时才发现（典型症状："前端显示
// 未知错误"、"流结束了但界面还在转"）。契约把字段与判据写成**数据**，两端各有一份
// 校验实现，并跑**同一组** conformance 夹具 —— 漂移会在测试里失败，而不是在用户那里。
package contract

import (
	"encoding/json"
	"fmt"
	"strings"
)

// Version 是契约版本。改动**删/改**字段必须同时升版本并改两端；只加可选字段不用升。
const Version = 1

// Family 是一族载荷（error / terminal）。
type Family string

const (
	FamilyError    Family = "error"
	FamilyTerminal Family = "terminal"
)

// Violation 描述一处违约（带字段路径，便于定位）。
type Violation struct {
	Family Family
	Field  string
	Reason string
}

func (v Violation) Error() string {
	if v.Field == "" {
		return fmt.Sprintf("%s: %s", v.Family, v.Reason)
	}
	return fmt.Sprintf("%s.%s: %s", v.Family, v.Field, v.Reason)
}

// maxMessageBytes 与契约文件里的 max_bytes 一致（4096）。
const maxMessageBytes = 4096

var terminalTypes = map[string]bool{"done": true, "error": true, "cancelled": true}

// ValidateError 校验 error 族载荷。
//
// 规则来自 `contracts/versioned.json`：message 必填且 ≤4096 字节；code/retryable/
// params 可选但有类型要求；未知字段**允许**（additive-only 兼容）。
func ValidateError(payload map[string]any) []Violation {
	var out []Violation
	msg, ok := payload["message"]
	if !ok {
		out = append(out, Violation{FamilyError, "message", "必填"})
	} else if s, isStr := msg.(string); !isStr {
		out = append(out, Violation{FamilyError, "message", "必须是字符串"})
	} else if len([]byte(s)) > maxMessageBytes {
		out = append(out, Violation{FamilyError, "message", "超过 4096 字节"})
	}
	if raw, exists := payload["code"]; exists {
		if _, isStr := raw.(string); !isStr {
			out = append(out, Violation{FamilyError, "code", "必须是字符串"})
		}
	}
	if raw, exists := payload["retryable"]; exists {
		if _, isBool := raw.(bool); !isBool {
			out = append(out, Violation{FamilyError, "retryable", "必须是布尔"})
		}
	}
	if raw, exists := payload["params"]; exists {
		if _, isObj := raw.(map[string]any); !isObj {
			out = append(out, Violation{FamilyError, "params", "必须是对象"})
		}
	}
	if raw, exists := payload["trace_id"]; exists {
		if _, isStr := raw.(string); !isStr {
			out = append(out, Violation{FamilyError, "trace_id", "必须是字符串"})
		}
	}
	return out
}

// ValidateTerminal 校验 terminal 族载荷。
//
// 三条硬规则：type 必填且只能是 done/error/cancelled；type=error 必须带 error 对象
// 且它本身通过 error 族校验；type=done 不得带 error。
func ValidateTerminal(payload map[string]any) []Violation {
	var out []Violation
	rawType, exists := payload["type"]
	if !exists {
		return append(out, Violation{FamilyTerminal, "type", "必填"})
	}
	kind, isStr := rawType.(string)
	if !isStr {
		return append(out, Violation{FamilyTerminal, "type", "必须是字符串"})
	}
	if !terminalTypes[kind] {
		return append(out, Violation{FamilyTerminal, "type", fmt.Sprintf("未知取值 %q", kind)})
	}
	errRaw, hasErr := payload["error"]
	switch kind {
	case "error":
		if !hasErr {
			out = append(out, Violation{FamilyTerminal, "error", "type=error 时必须提供"})
			break
		}
		body, isObj := errRaw.(map[string]any)
		if !isObj {
			out = append(out, Violation{FamilyTerminal, "error", "必须是对象"})
			break
		}
		for _, v := range ValidateError(body) {
			out = append(out, Violation{FamilyTerminal, "error." + v.Field, v.Reason})
		}
	default:
		if hasErr {
			out = append(out, Violation{FamilyTerminal, "error", "type=" + kind + " 时不得提供"})
		}
	}
	if raw, exists := payload["finish_reason"]; exists {
		if _, isStr := raw.(string); !isStr {
			out = append(out, Violation{FamilyTerminal, "finish_reason", "必须是字符串"})
		}
	}
	return out
}

// Validate 按族名分派。
func Validate(family string, payload map[string]any) ([]Violation, error) {
	switch Family(strings.ToLower(family)) {
	case FamilyError:
		return ValidateError(payload), nil
	case FamilyTerminal:
		return ValidateTerminal(payload), nil
	default:
		return nil, fmt.Errorf("未知契约族：%q", family)
	}
}

// Decode 把 JSON 字节解成载荷；解不出就返回违约（契约校验不接受"大概是"）。
func Decode(family string, raw []byte) (map[string]any, []Violation, error) {
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, nil, fmt.Errorf("%s: 不是合法 JSON 对象: %w", family, err)
	}
	violations, err := Validate(family, payload)
	return payload, violations, err
}

// TerminalSequence 检查一个流里出现的 terminal 事件序列是否合法：
// **最多一个**，且必须通过载荷校验。
func TerminalSequence(events []map[string]any) []Violation {
	var out []Violation
	seen := 0
	for i, ev := range events {
		if _, isTerminal := ev["type"]; !isTerminal {
			continue
		}
		kind, _ := ev["type"].(string)
		if !terminalTypes[kind] {
			continue // 非终态事件（delta 等）不受此函数约束
		}
		seen++
		if seen > 1 {
			out = append(out, Violation{FamilyTerminal, "", fmt.Sprintf("第 %d 个事件：终态事件重复出现", i)})
		}
		for _, v := range ValidateTerminal(ev) {
			out = append(out, Violation{FamilyTerminal, fmt.Sprintf("[%d] %s", i, v.Field), v.Reason})
		}
	}
	return out
}
