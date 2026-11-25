package api

// ── H24：插件授权必须绑定"具体执行参数" ─────────────────────────────────────
//
// 缺陷形态（Go 侧）：更新插件时只有 `command` 变化会重置 `approved`。于是把
// `args` 从 `["--read-only"]` 改成 `["--exec", "rm -rf /"]`、或在 `env` 里换掉
// token，都**不需要重新授权** —— 用户当初同意的是那一组参数，不是"任何参数组合"。
// `read_only` 同理：它决定引擎是直连还是走 broker，改它就是改执行面。
//
// 这一层只有两个纯函数：一个把"批准了什么"折叠成摘要，一个判断两份配置的
// "执行输入"是否相同。放这里是为了让网关侧（/test、broker 入口）与引擎侧
// （pool 只看 approved）能共用同一口径。

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"
	"strings"
)

// pluginApprovalDigest 把"这组执行参数"折叠成稳定摘要。
//
// 覆盖所有会改变**实际执行内容**的字段：
//   - command（要跑什么）
//   - args（同一命令的不同参数往往是完全不同的行为）
//   - env（凭据/端点在 env 里；换 env 等于换身份）
//   - read_only（决定直连还是走 broker：执行面不同）
//
// 刻意**不**包含 description / version / status：它们不影响执行语义，改了不该
// 逼用户重新授权（否则每次改备注都要点一次同意，用户很快就会闭眼点）。
func pluginApprovalDigest(p MCPPlugin) string {
	payload := map[string]interface{}{
		"version":   1,
		"name":      p.Name,
		"command":   strings.TrimSpace(p.Command),
		"args":      append([]string{}, p.Args...),
		"env":       sortedEnvPairs(p.Env),
		"read_only": p.ReadOnly,
	}
	encoded, err := json.Marshal(payload)
	if err != nil {
		// 结构固定，Marshal 不该失败；真失败就返回"不可核对"的摘要（比 nil 更安全：
		// 空串会让调用方以为"没有绑定"，从而走 fail-closed）。
		return ""
	}
	sum := sha256.Sum256(encoded)
	return hex.EncodeToString(sum[:16])
}

// pluginApprovalInput 返回用于**比较是否变化**的规范化字符串。
func pluginApprovalInput(p MCPPlugin) string {
	return pluginApprovalDigest(p)
}

// sortedEnvPairs 把 env 展开成排序后的 "k=v" 列表（map 迭代顺序随机，必须先排序）。
func sortedEnvPairs(env map[string]string) []string {
	if len(env) == 0 {
		return []string{}
	}
	keys := make([]string, 0, len(env))
	for k := range env {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	out := make([]string, 0, len(keys))
	for _, k := range keys {
		out = append(out, k+"="+env[k])
	}
	return out
}

// pluginApprovedFor 判断"当前配置是否处于已授权且未漂移"的状态。
//
// 判据（三条全要）：
//  1. `approved` 为真；
//  2. `approval_digest` 非空；
//  3. 摘要与**当前**配置重算的结果一致（防"审批后又被改过但 approved 没清"）。
//
// 老数据（没有 digest 的 approved）判为**未授权**：宁可让用户重新点一次同意，
// 也不承认一份"不知道批准了什么"的授权。
func pluginApprovedFor(p MCPPlugin) bool {
	if !p.Approved {
		return false
	}
	if strings.TrimSpace(p.ApprovalDigest) == "" {
		return false
	}
	return p.ApprovalDigest == pluginApprovalDigest(p)
}
