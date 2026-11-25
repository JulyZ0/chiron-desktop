package api

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"strings"
)

// ── H54：本机栈身份挑战（PRD 9.7）────────────────────────────────────────────
//
// 缺陷形态：桌面外壳判断"本机栈还在跑"时只看 loopback `/ready` 是否 200 且
// `postgres/redis = up`。这等于**任何**在这台机器上起过的、恰好也连了 PG/Redis 的
// 服务都会被认为是"我们的栈"：
//   - 端口被别的 Chiron 实例（另一个 data dir、另一份配置）占着 → 复用了错误的栈，
//     用户的本地数据写进了别人的库；
//   - 端口被任意第三方服务占着且它也应答 `/ready` → 更糟。
//
// 治法：本地启动时给进程一组**只属于这次部署**的标识（实例 ID、协议版本、
// data dir 指纹），`/ready` 原样回显；外壳逐项比对，不一致就不认。
// 这些值来自环境变量，云端部署不设置 ⇒ 不对外暴露任何东西。

// instanceProtocolVersion 是"外壳 ↔ 本地栈"的握手协议版本。
// 任何一侧改变语义都要升它：老外壳遇到新版本应拒绝接管，而不是猜。
const instanceProtocolVersion = 1

// instanceIdentity 是可对外回显的实例标识（仅本地模式有意义）。
type instanceIdentity struct {
	InstanceID string `json:"instance_id"`
	DataDirKey string `json:"data_dir_key"`
	Protocol   int    `json:"protocol"`
}

// instanceIdentityFromEnv 读取本地启动时注入的标识。
//
// 三个变量都缺 ⇒ 返回零值，调用方据此**不**往 /ready 里加字段（云端保持干净）。
func instanceIdentityFromEnv() instanceIdentity {
	return instanceIdentity{
		InstanceID: strings.TrimSpace(os.Getenv("CHIRON_INSTANCE_ID")),
		DataDirKey: strings.TrimSpace(os.Getenv("CHIRON_DATA_DIR_KEY")),
		Protocol:   instanceProtocolFromEnv(),
	}
}

func instanceProtocolFromEnv() int {
	raw := strings.TrimSpace(os.Getenv("CHIRON_INSTANCE_PROTOCOL"))
	if raw == "" {
		return instanceProtocolVersion
	}
	n := 0
	for _, ch := range raw {
		if ch < '0' || ch > '9' {
			return instanceProtocolVersion
		}
		n = n*10 + int(ch-'0')
	}
	if n <= 0 {
		return instanceProtocolVersion
	}
	return n
}

// localDataDirKey 把 data dir 归一化成稳定指纹（外壳用它做"根目录绑定"）。
//
// 用哈希而不是明文：`/ready` 是**无鉴权**端点，路径属于部署隐私。
func localDataDirKey(dataDir string) string {
	cleaned := strings.TrimSpace(dataDir)
	if cleaned == "" {
		return ""
	}
	// Windows 路径大小写不敏感：先规范化再哈希，避免同一目录算出两个指纹。
	cleaned = strings.ToLower(strings.ReplaceAll(cleaned, "\\", "/"))
	sum := sha256.Sum256([]byte(cleaned))
	return hex.EncodeToString(sum[:8])
}

// mergeInstanceIdentity 把实例标识并入 /ready 的响应体（仅本地模式）。
func mergeInstanceIdentity(deps map[string]string) {
	ident := instanceIdentityFromEnv()
	if ident.InstanceID == "" {
		return
	}
	deps["instance_id"] = ident.InstanceID
	deps["protocol"] = itoa(ident.Protocol)
	if ident.DataDirKey != "" {
		deps["data_dir_key"] = ident.DataDirKey
	}
}
