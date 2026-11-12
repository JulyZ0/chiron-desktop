package api

import (
	"context"
	"fmt"
	"net/http"
	"path/filepath"
	"sort"
	"strings"

	"github.com/athenavi/chiron/internal/db"
)

// kb_file_types.go —— 知识库原文类型白名单在 **Go 侧**的副本。
//
// 为什么 Go 必须有这份清单：桌面端的知识库文档走的是**分片上传**
// （uploads.go::UploadHandler）。浏览器把文件切块传给 Go，由
// finalizeKBDoc 直接写对象存储并落 knowledge_documents —— 整条路径**不经过**
// Python 引擎。因此 app/knowledge/remote.py::RemoteProvider.supports() 那道
// 「上传前就问」的校验，在桌面端从来没有被执行过：任何扩展名都能进库，直到
// 索引阶段才解析失败，变成一条 error 文档。
//
// 两份清单必须逐字一致。kb_file_types_test.go 会直接读 python-engine 的源码
// 做比对 —— 只改一边会立刻让测试变红，不会悄悄漂移。
var (
	// selfhosted（PageIndex local 模式）只吃 text-based PDF。
	kbSelfHostedSuffixes = []string{".pdf"}
	// cloud 额外支持 Office 文档。
	kbCloudSuffixes = []string{".doc", ".docx", ".pdf", ".ppt", ".pptx"}
)

// kbAllowedSuffixes 返回该 provider 接受的后缀（已排序，可直接拼进提示文案）。
func kbAllowedSuffixes(provider string) []string {
	switch strings.ToLower(strings.TrimSpace(provider)) {
	case "selfhosted":
		return kbSelfHostedSuffixes
	case "cloud":
		return kbCloudSuffixes
	default:
		return kbUnionSuffixes()
	}
}

// kbUnionSuffixes 是全部已知类型的并集。
//
// 未知 provider（未来新增的取值、或库里的历史脏值）按并集放行：这份白名单的
// 职责是"明确不支持的类型别传"，不是"只允许我认识的那两个 provider 名"。
// 认不出名字就把用户挡在门外，只会让新 provider 上线当天所有人都传不了文件。
func kbUnionSuffixes() []string {
	seen := map[string]bool{}
	for _, ext := range kbSelfHostedSuffixes {
		seen[ext] = true
	}
	for _, ext := range kbCloudSuffixes {
		seen[ext] = true
	}
	out := make([]string, 0, len(seen))
	for ext := range seen {
		out = append(out, ext)
	}
	sort.Strings(out)
	return out
}

// kbSuffixAllowed 按扩展名判断（大小写不敏感）。无扩展名的文件一律拒绝：
// 旧实现把 `ext == ""` 当成 txt 落库，等于给任意文件开了一个后门。
func kbSuffixAllowed(provider, name string) bool {
	ext := strings.ToLower(filepath.Ext(name))
	if ext == "" {
		return false
	}
	for _, allowed := range kbAllowedSuffixes(provider) {
		if ext == allowed {
			return true
		}
	}
	return false
}

// kbTypeRejection 生成拒绝文案。
//
// 必须把"当前库接受什么"写出来：只报"不支持该类型"的话，用户会拿另一个同样
// 不受支持的后缀再试一遍，来回几次才意识到问题不在单个文件。
func kbTypeRejection(provider, name string) string {
	ext := strings.ToLower(filepath.Ext(name))
	if ext == "" {
		ext = "无扩展名"
	}
	return fmt.Sprintf("知识库（%s）不支持 %s 类型的文档，当前支持：%s",
		kbProviderLabel(provider), ext, strings.Join(kbAllowedSuffixes(provider), " "))
}

func kbProviderLabel(provider string) string {
	if strings.TrimSpace(provider) == "" {
		return "未设置"
	}
	return provider
}

// kbUnsupportedTypeError 让 Complete 能把"类型不支持"和"存储故障"分开返回：
// 前者是用户可修正的 400，后者才是 500。旧实现把所有 finalize 错误都当 500，
// 用户看到的是"服务器错误"，再怎么换文件也没用。
type kbUnsupportedTypeError struct{ msg string }

func (e kbUnsupportedTypeError) Error() string { return e.msg }

// kbProviderOf 取某个知识库的 provider。查不到（库不存在、或不属于该租户）
// 返回 ok=false，由调用方决定后续行为 —— 这里不替它们编造错误。
func kbProviderOf(ctx context.Context, kbID, tenantID string) (string, bool) {
	if strings.TrimSpace(kbID) == "" {
		return "", false
	}
	var provider string
	if err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT provider FROM knowledge_bases WHERE id = $1 AND tenant_id = $2`,
		kbID, tenantID).Scan(&provider); err != nil {
		return "", false
	}
	return provider, true
}

// kbProviderOfRequest 是 kbProviderOf 的请求级便捷包装。
func kbProviderOfRequest(r *http.Request, kbID, tenantID string) (string, bool) {
	return kbProviderOf(r.Context(), kbID, tenantID)
}
