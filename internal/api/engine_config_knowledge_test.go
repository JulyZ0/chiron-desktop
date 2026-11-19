package api

import "testing"

// 知识库检索服务的地址由**启动器**按随机端口注入（Electron local-stack / run.py）。
// 后台「系统设置」里留空只代表"没配"，网关不能把空串下发 —— 否则引擎会把
// KNOWLEDGE_SERVICE_URL 覆盖成空，知识库整页变成"服务不可用"。
func TestKnowledgeEngineConfigDropsEmptyEnvFirstValues(t *testing.T) {
	in := map[string]interface{}{
		"knowledge_service_url":     "",              // 必须被丢掉（回落到 env）
		"internal_token":            "   ",           // 只有空白的也算空
		"gateway_internal_url":      "http://x:8080", // 非空：原样下发
		"default_model":             "",              // 非 env-first 键：保持既有语义
		"knowledge_service_timeout": 120.0,
	}

	out := pruneEnvFirstEmpty(in)

	if _, ok := out["knowledge_service_url"]; ok {
		t.Fatalf("knowledge_service_url 的空值必须被丢弃，实际仍在下发")
	}
	if _, ok := out["internal_token"]; ok {
		t.Fatalf("internal_token 只有空白也必须被丢弃")
	}
	if got := out["gateway_internal_url"]; got != "http://x:8080" {
		t.Fatalf("非空值不该被改动，得到 %v", got)
	}
	if _, ok := out["default_model"]; !ok {
		t.Fatalf("非 env-first 键的空值应保持原样（不属于本契约）")
	}
	if got := out["knowledge_service_timeout"]; got != 120.0 {
		t.Fatalf("数值键不该被 probe 掉，得到 %v", got)
	}
}

func TestKnowledgeEngineConfigKeepsNonEmptyServiceURL(t *testing.T) {
	in := map[string]interface{}{"knowledge_service_url": "http://127.0.0.1:8123"}
	out := pruneEnvFirstEmpty(in)
	if got := out["knowledge_service_url"]; got != "http://127.0.0.1:8123" {
		t.Fatalf("显式配置的检索服务地址必须原样下发，得到 %v", got)
	}
}

func TestKnowledgeEngineConfigNilAndEmptyAreSafe(t *testing.T) {
	if out := pruneEnvFirstEmpty(nil); out != nil {
		t.Fatalf("nil 输入应原样返回 nil，避免无谓分配")
	}
	if out := pruneEnvFirstEmpty(map[string]interface{}{}); len(out) != 0 {
		t.Fatalf("空 map 应原样返回，得到 %v", out)
	}
}
