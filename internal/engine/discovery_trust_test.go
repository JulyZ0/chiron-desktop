package engine

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"testing"
)

// ── H35：引擎注册地址的最小信任 ──────────────────────────────────────────────
//
// 旧实现直接信任 Redis 里任意 `url` 字段：任何能写 `engine:instance:*` 的主体都能把
// 网关的引擎流量整体改道，任务内容与凭据随之流出。这里钉住三层：
//   1. URL 必须过协议/主机/端口校验（失败即拒绝，不"尽量修一下"）；
//   2. 可选的 host 白名单与环回开关；
//   3. 可选 HMAC 签名 —— 拿到 Redis 写权限但没有密钥写不出可采纳的记录。

func TestValidateEngineRegistrationURLRejectsMalformed(t *testing.T) {
	cases := []string{
		"",                            // 空
		"engine:8000",                 // 无 scheme
		"ftp://engine:8000",           // 非 http(s)
		"http://u:p@engine:8000",      // userinfo（把凭据塞进地址）
		"http://engine:8000/api",      // 带 path
		"http://engine:8000/?a=1",     // 带 query
		"http://engine:8000/#frag",    // 带 fragment
		"http://engine:0",             // 非法端口
		"http://engine:70000",         // 非法端口
		"http://eng ine:8000",         // 主机含空白
		"http://evil/path\\x:8000",    // 反斜杠混淆
		"https://user@127.0.0.1:8080", // userinfo + 环回
	}
	for _, raw := range cases {
		if got, err := ValidateEngineRegistrationURL(raw); err == nil {
			t.Fatalf("应拒绝 %q，却得到 %q", raw, got)
		}
	}
}

func TestValidateEngineRegistrationURLNormalizes(t *testing.T) {
	cases := map[string]string{
		"http://engine:8000":       "http://engine:8000",
		"HTTP://ENGINE:8000":       "http://ENGINE:8000",
		"http://engine:8000/":      "http://engine:8000",
		"  https://engine.local  ": "https://engine.local",
		"http://[::1]:8000":        "http://[::1]:8000",
		"https://10.0.0.7:8443":    "https://10.0.0.7:8443",
	}
	for raw, want := range cases {
		got, err := ValidateEngineRegistrationURL(raw)
		if err != nil {
			t.Fatalf("应接受 %q，却报错 %v", raw, err)
		}
		if got != want {
			t.Fatalf("%q 归一化结果 = %q，期望 %q", raw, got, want)
		}
	}
}

func TestValidateEngineRegistrationURLHonoursHostAllowlist(t *testing.T) {
	t.Setenv("ENGINE_DISCOVERY_ALLOWED_HOSTS", "engine-a, Engine-B ")
	if _, err := ValidateEngineRegistrationURL("http://engine-a:8000"); err != nil {
		t.Fatalf("白名单内主机应放行: %v", err)
	}
	if _, err := ValidateEngineRegistrationURL("http://engine-b:8000"); err != nil {
		t.Fatalf("白名单匹配应大小写无关: %v", err)
	}
	if _, err := ValidateEngineRegistrationURL("http://engine-c:8000"); err == nil {
		t.Fatal("白名单外主机必须拒绝")
	}
}

func TestValidateEngineRegistrationURLHonoursLoopbackSwitch(t *testing.T) {
	// 默认允许环回（桌面本地栈就是 127.0.0.1）
	t.Setenv("ENGINE_DISCOVERY_ALLOW_LOOPBACK", "")
	if _, err := ValidateEngineRegistrationURL("http://127.0.0.1:8000"); err != nil {
		t.Fatalf("默认应允许环回: %v", err)
	}
	// 多机部署建议关掉
	t.Setenv("ENGINE_DISCOVERY_ALLOW_LOOPBACK", "0")
	for _, raw := range []string{
		"http://127.0.0.1:8000",
		"http://localhost:8000",
		"http://10.1.2.3:8000",
		"http://192.168.1.5:8000",
		"http://[fe80::1]:8000",
	} {
		if _, err := ValidateEngineRegistrationURL(raw); err == nil {
			t.Fatalf("关闭环回后应拒绝 %q", raw)
		}
	}
	// 公网地址不受这条开关影响
	if _, err := ValidateEngineRegistrationURL("https://engine.example.com"); err != nil {
		t.Fatalf("公网地址不该被环回开关拦下: %v", err)
	}
}

func signRegistration(secret, instanceID, url string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(instanceID + "|" + url))
	return hex.EncodeToString(mac.Sum(nil))
}

func TestVerifyRegistrationSignature(t *testing.T) {
	t.Setenv("ENGINE_DISCOVERY_SECRET", "")
	if !verifyRegistrationSignature("inst-1", engineInstanceRecord{URL: "http://engine:8000"}) {
		t.Fatal("未配置密钥时向后兼容（老引擎不签名）")
	}

	t.Setenv("ENGINE_DISCOVERY_SECRET", "s3cr3t")
	good := signRegistration("s3cr3t", "inst-1", "http://engine:8000")

	if !verifyRegistrationSignature("inst-1", engineInstanceRecord{URL: "http://engine:8000", Sig: good}) {
		t.Fatal("签名正确应采纳")
	}
	if !verifyRegistrationSignature("inst-1", engineInstanceRecord{URL: "http://engine:8000", Sig: uppercase(good)}) {
		t.Fatal("签名比较应大小写无关（hex 大小写不构成安全差异）")
	}
	if verifyRegistrationSignature("inst-1", engineInstanceRecord{URL: "http://engine:8000"}) {
		t.Fatal("配置密钥后缺签名必须拒绝")
	}
	if verifyRegistrationSignature("inst-1", engineInstanceRecord{URL: "http://evil:8000", Sig: good}) {
		t.Fatal("签名绑定 URL：换地址后旧签名必须失效")
	}
	if verifyRegistrationSignature("inst-2", engineInstanceRecord{URL: "http://engine:8000", Sig: good}) {
		t.Fatal("签名绑定 instance_id：换实例后旧签名必须失效")
	}
	if verifyRegistrationSignature("inst-1", engineInstanceRecord{URL: "http://engine:8000", Sig: "deadbeef"}) {
		t.Fatal("伪造签名必须拒绝")
	}
}

func uppercase(s string) string {
	out := []rune(s)
	for i, r := range out {
		if r >= 'a' && r <= 'f' {
			out[i] = r - 'a' + 'A'
		}
	}
	return string(out)
}

func TestRecordAddressChangeReportsRotation(t *testing.T) {
	// 只测变化计算：轮换与撤销必须能在日志里对齐
	engineAddressState.mu.Lock()
	engineAddressState.urls = nil
	engineAddressState.mu.Unlock()

	added, removed := recordAddressChange([]string{"http://a:8000", "http://b:8000"})
	if len(added) != 2 || len(removed) != 0 {
		t.Fatalf("首次登记应全部是新增，得到 added=%v removed=%v", added, removed)
	}
	added, removed = recordAddressChange([]string{"http://b:8000", "http://c:8000"})
	if len(added) != 1 || added[0] != "http://c:8000" {
		t.Fatalf("轮换应报出新增 http://c:8000，得到 %v", added)
	}
	if len(removed) != 1 || removed[0] != "http://a:8000" {
		t.Fatalf("轮换应报出撤销 http://a:8000，得到 %v", removed)
	}
}
