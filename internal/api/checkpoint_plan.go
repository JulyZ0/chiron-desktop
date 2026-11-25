package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"strconv"

	"github.com/athenavi/chiron/internal/engine"
)

// ── H73：破坏性操作必须绑定不可变计划（PRD 9.2）────────────────────────────
//
// 缺陷形态（前端已修、服务端未修）：checkpoint 回收是「dry-run 出计划 → 弹确认 →
// 再执行」两步。前端已经把目标 workspace 与计划摘要一起钉住，但**服务端不校验**：
// `POST /v1/admin/checkpoints/gc` 仍只认 `retention_days`/`workspace_id`，
// 于是任何调用方（含被改过的前端、或直接 curl）都能在"确认过"的名义下删掉
// 一份**没预览过**的范围。
//
// 治法：摘要由**服务端**签发与校验。
//   - dry-run：服务端按本次计划内容算出 digest 并下发；
//   - 执行：请求必须带 `plan_digest`；服务端在新一轮只读 dry-run 上**重算**，
//     不一致即 409 并拒绝删除（"确认的是 A、删的是 B"这类漂移在此终结）。
//
// 摘要只用于"是不是同一份计划"的判等，不是密码学承诺 —— 但它由服务端算，
// 且覆盖目标与全部计划字段，所以调用方无法用"报一个旧 digest"绕过比对。

// gcPlanDigestVersion 在摘要口径变化时递增：老客户端的 digest 会被判为不一致，
// 而不是"恰好相等"。
const gcPlanDigestVersion = 1

// canonicalStr 把任意 JSON 值规范化成稳定字符串。
//
// 为什么不能直接 `fmt.Sprint`：map 的迭代顺序随机，同一个计划会算出两个摘要 ——
// 那会让"重算比对"变成随机失败。这里按 key 排序后递归展开。
func canonicalStr(v interface{}) string {
	switch t := v.(type) {
	case nil:
		return "null"
	case bool:
		if t {
			return "true"
		}
		return "false"
	case string:
		return strconv.Quote(t)
	case float64:
		// JSON 数字统一经 float64 落地；用最小表示避免 1 与 1.0 产生两个摘要。
		if t == float64(int64(t)) {
			return strconv.FormatInt(int64(t), 10)
		}
		return strconv.FormatFloat(t, 'g', -1, 64)
	case int:
		return strconv.Itoa(t)
	case int64:
		return strconv.FormatInt(t, 10)
	case []interface{}:
		out := "["
		for i, item := range t {
			if i > 0 {
				out += ","
			}
			out += canonicalStr(item)
		}
		return out + "]"
	case []string:
		out := "["
		for i, item := range t {
			if i > 0 {
				out += ","
			}
			out += strconv.Quote(item)
		}
		return out + "]"
	case map[string]interface{}:
		keys := make([]string, 0, len(t))
		for k := range t {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		out := "{"
		for i, k := range keys {
			if i > 0 {
				out += ","
			}
			out += strconv.Quote(k) + ":" + canonicalStr(t[k])
		}
		return out + "}"
	default:
		return strconv.Quote(fmt.Sprint(t))
	}
}

// gcPlanDigestSource 挑出参与摘要的字段。
//
// 与前端 `computeGcPlanDigest` 覆盖同一组语义（目标 + 保留期 + 计划内容），
// 但**由服务端独立计算**：前端那份只用于本地"计划是否漂移"的自检。
func gcPlanDigestSource(workspaceID string, retentionDays int, plan map[string]interface{}) map[string]interface{} {
	planned := []string{}
	if raw, ok := plan["planned"].([]interface{}); ok {
		for _, item := range raw {
			planned = append(planned, fmt.Sprint(item))
		}
	}
	if raw, ok := plan["planned"].([]string); ok {
		planned = append(planned, raw...)
	}
	sort.Strings(planned)

	dangling := []string{}
	if raw, ok := plan["dangling_refs"].([]interface{}); ok {
		for _, item := range raw {
			dangling = append(dangling, fmt.Sprint(item))
		}
	}
	sort.Strings(dangling)

	return map[string]interface{}{
		"v":              gcPlanDigestVersion,
		"workspace":      workspaceID,
		"retention_days": retentionDays,
		"scanned":        plan["scanned"],
		"referenced":     plan["referenced"],
		"expired":        plan["expired"],
		"tree_paths":     plan["tree_paths"],
		"deleted":        plan["deleted"],
		"freed_bytes":    plan["freed_bytes"],
		"planned":        planned,
		"dangling_refs":  dangling,
	}
}

// gcPlanDigest 计算计划摘要（sha256 前 16 字节，够区分且便于日志阅读）。
func gcPlanDigest(workspaceID string, retentionDays int, plan map[string]interface{}) string {
	canonical := canonicalStr(gcPlanDigestSource(workspaceID, retentionDays, plan))
	sum := sha256.Sum256([]byte(canonical))
	return hex.EncodeToString(sum[:16])
}

// verifyGcPlanAgainstSnapshot 在**执行前**重算并比对计划摘要（H73）。
//
// 抽成独立函数的原因：它在 handler 里需要 PG（取项目根），而比对逻辑本身与存储无关。
// 分开后可以用桩引擎直接验证三件事 —— 一致放行、漂移拒绝、并回传最新摘要。
//
// 返回：最新摘要、是否一致、错误。
func verifyGcPlanAgainstSnapshot(
	ctx context.Context,
	python *engine.PythonClient,
	workspaceID string,
	roots []string,
	retentionDays int,
	provided string,
) (string, bool, error) {
	latest, err := gcPlanSnapshot(ctx, python, workspaceID, roots, retentionDays)
	if err != nil {
		return "", false, err
	}
	return latest, latest == provided, nil
}
