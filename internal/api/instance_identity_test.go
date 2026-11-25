package api

import (
	"testing"
)

// ── H54：本机栈身份挑战（PRD 9.7）──────────────────────────────────────────
//
// 缺陷形态：桌面外壳判断"本机栈还在跑"只看 loopback `/ready` 返回 200 且
// postgres/redis 都 up —— 任何在这台机器上起过、恰好也连了 PG/Redis 的服务都会被
// 当成"我们的栈"。于是端口被另一个 data dir 的实例（或第三方服务）占着时，外壳会
// 接管一个不属于本次部署的后端。
//
// 修法：本地启动时注入实例 ID / 协议版本 / data dir 指纹，`/ready` 原样回显，
// 外壳逐项比对。这一组钉住服务端侧的三件事：
//   1. 没注入就不回显（云端保持干净，不白送部署信息）；
//   2. 注入了就回显三项（外壳才能比对）；
//   3. data dir 指纹本身稳定且不可逆（大小写/分隔符归一，输出是哈希不是路径）。

func TestInstanceIdentityAbsentWhenNotInjected(t *testing.T) {
	t.Setenv("CHIRON_INSTANCE_ID", "")
	t.Setenv("CHIRON_DATA_DIR_KEY", "")
	deps := map[string]string{"postgres": "up", "redis": "up"}
	mergeInstanceIdentity(deps)

	if _, ok := deps["instance_id"]; ok {
		t.Fatal("没有注入实例 ID 时不得回显 instance_id（云端不该暴露部署信息）")
	}
	if len(deps) != 2 {
		t.Fatalf("不该多出字段: %+v", deps)
	}
}

func TestInstanceIdentityEchoedWhenInjected(t *testing.T) {
	t.Setenv("CHIRON_INSTANCE_ID", "inst-abc")
	t.Setenv("CHIRON_DATA_DIR_KEY", "deadbeefdeadbeef")
	t.Setenv("CHIRON_INSTANCE_PROTOCOL", "1")

	deps := map[string]string{"postgres": "up", "redis": "up"}
	mergeInstanceIdentity(deps)

	if deps["instance_id"] != "inst-abc" {
		t.Fatalf("instance_id 未回显: %+v", deps)
	}
	if deps["data_dir_key"] != "deadbeefdeadbeef" {
		t.Fatalf("data_dir_key 未回显: %+v", deps)
	}
	if deps["protocol"] != "1" {
		t.Fatalf("protocol 未回显: %+v", deps)
	}
}

func TestInstanceProtocolFallsBackToConstant(t *testing.T) {
	for _, raw := range []string{"", "abc", "0", "-3", "1.5"} {
		t.Setenv("CHIRON_INSTANCE_PROTOCOL", raw)
		if got := instanceProtocolFromEnv(); got != instanceProtocolVersion {
			t.Fatalf("protocol=%q 应回落到 %d，得到 %d", raw, instanceProtocolVersion, got)
		}
	}
	t.Setenv("CHIRON_INSTANCE_PROTOCOL", "7")
	if got := instanceProtocolFromEnv(); got != 7 {
		t.Fatalf("应解析出 7，得到 %d", got)
	}
}

func TestLocalDataDirKeyIsStableAndOpaque(t *testing.T) {
	win := localDataDirKey(`C:\Users\Alice\AppData\Roaming\Chiron`)
	winLower := localDataDirKey(`c:/users/alice/appdata/roaming/chiron`)
	if win == "" {
		t.Fatal("非空路径必须给出指纹")
	}
	if win != winLower {
		t.Fatalf("Windows 路径应大小写/分隔符无关：%q vs %q", win, winLower)
	}
	if len(win) != 16 {
		t.Fatalf("指纹长度应为 16 位十六进制，得到 %d", len(win))
	}
	// 不可逆：不能包含原始路径的任何片段（/ready 无鉴权，路径属于部署隐私）。
	for _, frag := range []string{"users", "alice", "chiron", "appdata"} {
		if containsFold(win, frag) {
			t.Fatalf("指纹里出现了路径片段 %q: %s", frag, win)
		}
	}
	if localDataDirKey("") != "" {
		t.Fatal("空路径应返回空串（外壳据此跳过比对）")
	}
	if localDataDirKey(`C:\a`) == localDataDirKey(`C:\b`) {
		t.Fatal("不同目录必须给出不同指纹")
	}
}

func containsFold(haystack, needle string) bool {
	h := []rune(haystack)
	n := []rune(needle)
	for i := 0; i+len(n) <= len(h); i++ {
		match := true
		for j := range n {
			a, b := h[i+j], n[j]
			if a >= 'A' && a <= 'Z' {
				a += 'a' - 'A'
			}
			if b >= 'A' && b <= 'Z' {
				b += 'a' - 'A'
			}
			if a != b {
				match = false
				break
			}
		}
		if match {
			return true
		}
	}
	return false
}
