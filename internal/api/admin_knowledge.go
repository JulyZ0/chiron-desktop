package api

// admin_knowledge.go —— 后台「知识库」页：本地模式 vs 外部 pageindex 服务。
//
//	GET  /v1/admin/knowledge        已保存配置 + 引擎当前生效值
//	PUT  /v1/admin/knowledge        保存（system_settings 的 `knowledge` 分类）
//	POST /v1/admin/knowledge/test   让**引擎**去探一个候选地址（保存前就能验）
//
// ## 为什么这一页不在网关里判断"够不够用"
//
// 网关自己探一遍索引服务，得到的结论可能与引擎不同：网关在宿主机、引擎在容器里，
// 内网出口、DNS、`INTERNAL_TOKEN` 都不一样。用户真正关心的是**引擎能不能用**，
// 所以连通性一律交给引擎探（`python-engine/app/api/internal_knowledge.py`），
// 本文件只做存储、校验与转发。
//
// ## 为什么"已保存"和"生效中"必须分开显示
//
// 保存成功只代表写进了 DB。引擎采纳还差两条路径：Redis 广播（运行期保存）或
// 下次启动的 `hydrate()`（`app/knowledge/runtime.py`）。广播丢失 / 引擎未启动时，
// 页面若按自己保存的值渲染，就会显示一个引擎从未采纳的地址 —— 那正是"改了没反应"
// 最难自查的一类现象。因此 GET 同时回引擎侧的真实持有值，由页面呈现差异。

import (
	"context"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/settings"
)

// settingsCategoryKnowledge 是「知识库」页配置所在的 system_settings 分组。
//
// ⚠️ 名字必须与引擎侧 `app/knowledge/runtime.py` 的 `KNOWLEDGE_CATEGORY` 一致：
// 引擎按这个字符串过滤广播，写错一个字符就变成"保存了、引擎永远收不到"。
const settingsCategoryKnowledge = "knowledge"

// knowledgeModeLocal / knowledgeModeRemote 与引擎侧 `runtime.MODE_LOCAL/MODE_REMOTE` 逐字相同。
const (
	knowledgeModeLocal  = "local"
	knowledgeModeRemote = "remote"
)

// knowledgeConfigKeys 是本页唯一管理的两个键。
const (
	knowledgeKeyMode = "mode"
	knowledgeKeyURL  = "service_url"
)

// adminKnowledgeInfo 是 GET/PUT 的响应体。
type adminKnowledgeInfo struct {
	// Saved 是库里已持久化的配置（下次启动 / 广播会用它）。
	Saved map[string]interface{} `json:"saved"`
	// Runtime 是**引擎当前生效**的运行时事实（未启用 / 不可达时为 nil）。
	//
	// 字段来自 `python-engine/app/knowledge/runtime.py::state()`：
	// `mode` / `service_url` / `bootstrap_url` / `using_bootstrap`。
	Runtime map[string]interface{} `json:"runtime"`
	// EngineAvailable 表示引擎是否给出了运行时事实。false 时页面不该把
	// "读不到"渲染成"配置错了" —— 那是两件事。
	EngineAvailable bool `json:"engine_available"`
	// PendingRestart 表示保存值与引擎生效值已经分叉（引擎还没采纳）。
	//
	// 不等于"需要重启"：正常情况下广播几秒内就会让它消失。页面据此提示
	// "引擎尚未采纳"，而不是断言要重启 —— 断言错了会让用户白重启一次。
	PendingRestart bool `json:"pending_restart"`
}

// GetKnowledgeInfo GET /v1/admin/knowledge —— 读配置与引擎生效值。
func (h *AdminHandler) GetKnowledgeInfo(w http.ResponseWriter, r *http.Request) {
	OK(w, h.buildKnowledgeInfo(r))
}

// buildKnowledgeInfo 组装响应。读路径不做连通性探测（`probe=false`）：
// 打开页面不该等一次最长 10 秒的网络请求。
func (h *AdminHandler) buildKnowledgeInfo(r *http.Request) adminKnowledgeInfo {
	saved := map[string]interface{}{}
	if s := h.ensureSettingsStore(); s != nil {
		if m, err := s.LoadConfig(r.Context(), settingsCategoryKnowledge); err == nil && m != nil {
			saved = m
		}
	}

	info := adminKnowledgeInfo{Saved: saved}
	if runtime, ok := h.fetchKnowledgeRuntime(r.Context(), false, ""); ok {
		info.Runtime = runtime
		info.EngineAvailable = true
		info.PendingRestart = knowledgeDiverged(saved, runtime)
	}
	return info
}

// fetchKnowledgeRuntime 向引擎要运行时事实；引擎不可用时返回 false（不伪造）。
func (h *AdminHandler) fetchKnowledgeRuntime(
	ctx context.Context, probe bool, serviceURL string,
) (map[string]interface{}, bool) {
	if h.pythonClient == nil {
		return nil, false
	}
	ctx, cancel := context.WithTimeout(ctx, 12*time.Second)
	defer cancel()

	path := "/v1/internal/knowledge-runtime?probe=false"
	if probe {
		path = "/v1/internal/knowledge-runtime?probe=true"
		if serviceURL != "" {
			path += "&url=" + url.QueryEscape(serviceURL)
		}
	}
	var out map[string]interface{}
	if err := h.pythonClient.GetJSON(ctx, path, &out); err != nil {
		slog.Debug("knowledge runtime unavailable", "error", err)
		return nil, false
	}
	if out == nil {
		return nil, false
	}
	return out, true
}

// knowledgeDiverged 判断"库里的配置"与"引擎在用的"是否已经分叉。
//
// 只比这两项，且**只在两边都说得清时才判定**：引擎没启动时所有保存值都"未生效"，
// 但那不是分叉，页面不该因此报警。
func knowledgeDiverged(saved, runtime map[string]interface{}) bool {
	if len(saved) == 0 || len(runtime) == 0 {
		return false
	}
	savedMode := strings.TrimSpace(strFromValue(saved[knowledgeKeyMode]))
	if savedMode != "" {
		if runtimeMode := strings.TrimSpace(strFromValue(runtime["mode"])); runtimeMode != savedMode {
			return true
		}
	}
	savedURL := strings.TrimRight(strings.TrimSpace(strFromValue(saved[knowledgeKeyURL])), "/")
	if savedURL != "" {
		if runtimeURL := strings.TrimRight(strings.TrimSpace(strFromValue(runtime["service_url"])), "/"); runtimeURL != savedURL {
			return true
		}
	}
	return false
}

// SaveKnowledgeInfo PUT /v1/admin/knowledge —— 保存模式与地址。
//
// 请求体：`{"mode": "local"|"remote", "service_url": "http://pi:8100"}`。
// 两个字段都用指针接收：`service_url` 空串是合法值（本地模式下不填），
// 不能与"字段缺席"混为一谈。
func (h *AdminHandler) SaveKnowledgeInfo(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Mode       *string `json:"mode"`
		ServiceURL *string `json:"service_url"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	cfgMap := map[string]interface{}{}
	if body.Mode != nil {
		mode, err := normalizeKnowledgeMode(*body.Mode)
		if err != nil {
			BadRequest(w, err.Error())
			return
		}
		cfgMap[knowledgeKeyMode] = mode
	}
	if body.ServiceURL != nil {
		u, err := normalizeKnowledgeServiceURL(*body.ServiceURL)
		if err != nil {
			BadRequest(w, err.Error())
			return
		}
		cfgMap[knowledgeKeyURL] = u
	}
	if len(cfgMap) == 0 {
		BadRequest(w, "nothing to save: provide mode and/or service_url")
		return
	}

	// remote 必须带地址：发给引擎后它只会记一条 warning 并保持现状
	// （`runtime.apply` 的 fail-open），表现为"保存成功、切不过去"。
	// 在这里就拦下来，比让用户对着一个"成功"的提示排查要诚实。
	if cfgMap[knowledgeKeyMode] == knowledgeModeRemote {
		if u, _ := cfgMap[knowledgeKeyURL].(string); u == "" {
			// 本次没提交地址时，看库里已有的（改模式不动地址是合法操作）。
			existing := ""
			if s := h.ensureSettingsStore(); s != nil {
				if m, err := s.LoadConfig(r.Context(), settingsCategoryKnowledge); err == nil {
					existing = strings.TrimSpace(strFromValue(m[knowledgeKeyURL]))
				}
			}
			if existing == "" {
				BadRequest(w, "service_url is required when mode is remote")
				return
			}
		}
	}

	s := h.ensureSettingsStore()
	if s == nil {
		InternalError(w, "settings store unavailable")
		return
	}
	userID := ""
	if claims := auth.GetClaims(r.Context()); claims != nil {
		userID = claims.ID
	}
	if err := s.SaveConfig(r.Context(), settingsCategoryKnowledge, cfgMap, userID); err != nil {
		if err == settings.ErrEncryptedKeyNotFound {
			InternalError(w, "APP_SECRET not configured; cannot encrypt sensitive settings")
			return
		}
		InternalError(w, "failed to save knowledge settings")
		return
	}

	// 广播是"即时生效"的唯一通道：引擎订阅到这一条就热切地址
	// （见 python-engine/app/knowledge/runtime.py::listen_for_changes）。
	if err := PublishSettingsChanged(r.Context(), settingsCategoryKnowledge, cfgMap); err != nil {
		slog.Warn("publish knowledge settings changed failed", "error", err)
	}
	slog.Info("knowledge settings saved", "keys", len(cfgMap))

	OK(w, h.buildKnowledgeInfo(r))
}

// TestKnowledgeService POST /v1/admin/knowledge/test —— 让引擎探一次候选地址。
//
// 请求体：`{"service_url": "http://pi:8100"}`。空串表示探**当前生效**的地址，
// 供"我什么都没改，就是想看看它活着没"使用。
//
// 响应恒为 200：连不上是业务结果，不是请求错误 —— 用 5xx 表达会让页面只能
// 从错误串里猜"是地址错了、还是网关坏了"。
func (h *AdminHandler) TestKnowledgeService(w http.ResponseWriter, r *http.Request) {
	var body struct {
		ServiceURL string `json:"service_url"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	target := strings.TrimSpace(body.ServiceURL)
	if target != "" {
		u, err := normalizeKnowledgeServiceURL(target)
		if err != nil {
			BadRequest(w, err.Error())
			return
		}
		target = u
	}

	runtime, ok := h.fetchKnowledgeRuntime(r.Context(), true, target)
	if !ok {
		// 引擎本身不可用：这是一条**区别于**"服务连不上"的答案，必须明说。
		OK(w, map[string]interface{}{
			"ok":               false,
			"engine_available": false,
			"detail":           "引擎不可达，无法探测知识库服务",
		})
		return
	}
	probe, _ := runtime["probe"].(map[string]interface{})
	OK(w, map[string]interface{}{
		"ok":               probe != nil && probe["ok"] == true,
		"engine_available": true,
		"detail":           strFromValue(probe["detail"]),
		"status_code":      probe["status_code"],
		"target":           target,
		"runtime":          runtime,
	})
}

// normalizeKnowledgeMode 校验模式取值。
//
// 与引擎侧 `runtime.normalize_mode` 不同：那边不认识就回落 local（配置通道不该
// 拒收整条），这边**明确报错** —— 后台保存是用户显式动作，静默改写他的选择
// 才是更坏的行为。
func normalizeKnowledgeMode(raw string) (string, error) {
	switch v := strings.ToLower(strings.TrimSpace(raw)); v {
	case knowledgeModeLocal, knowledgeModeRemote:
		return v, nil
	case "":
		return knowledgeModeLocal, nil
	default:
		return "", invalidInput("mode must be \"local\" or \"remote\"")
	}
}

// normalizeKnowledgeServiceURL 校验索引服务地址。
//
// 只接受 http/https 且带主机名：引擎侧用它拼 `/v1/answer` 这类路径，
// 裸 `pi:8100` 会被 Parse 成 scheme=pi 的相对引用，请求必然失败，
// 而错误会出现在用户点开知识库的那一刻，而不是保存时。
func normalizeKnowledgeServiceURL(raw string) (string, error) {
	v := strings.TrimSpace(raw)
	if v == "" {
		return "", nil // 本地模式下不填，合法
	}
	u, err := url.Parse(v)
	if err != nil {
		return "", invalidInput("service_url is not a valid URL")
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", invalidInput("service_url must start with http:// or https://")
	}
	if u.Host == "" {
		return "", invalidInput("service_url must include a host, e.g. http://127.0.0.1:8100")
	}
	// 末尾 `/` 去掉：引擎侧 `runtime.current_url()` 也会 rstrip，
	// 这里不统一就会让"保存值"与"生效值"在页面上看起来不等（误报分叉）。
	return strings.TrimRight(u.String(), "/"), nil
}
