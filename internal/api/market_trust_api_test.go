package api

// H5 的 HTTP 层攻击探针：外部条目的完整性/信任链不通过时，**响应码与非落盘**都要成立。
//
// 与 internal/market/install_trust_test.go 的分工：
//   - 那边断言编排层"校验失败不调用落点、不写记录"；
//   - 这边断言用户看到的契约（422/403/502/409）与"装失败后列表里真的没有这条"。
//
// 之所以两条都写：只测编排层会漏掉状态码映射，只测状态码会漏掉"先写了再回滚"。

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/market"
)

// externalCatalogURL 是测试用的运维源地址（Catalog 里唯一的条目来源）。
const externalCatalogURL = "https://market.test/catalog.json"

// staticFetcher 是 market.Fetcher 的替身：忽略远端，直接返回给定条目
// （和真实抓取一样把 Source 改写成该源的 id）。
type staticFetcher struct{ items []market.Item }

func (f staticFetcher) Fetch(_ context.Context, src market.Source) (market.FetchResult, error) {
	out := make([]market.Item, 0, len(f.items))
	for _, it := range f.items {
		it.Source = src.ID
		out = append(out, it)
	}
	return market.FetchResult{Items: out}, nil
}

// staticDocs 是 market.DocumentFetcher 的替身：manifest 抓取直接返回给定字节。
type staticDocs struct {
	body []byte
	err  error
}

func (d staticDocs) FetchDocument(_ context.Context, _ string, _ int) ([]byte, error) {
	if d.err != nil {
		return nil, d.err
	}
	return d.body, nil
}

// trustEnv 是"外部条目"形态的安装环境（目录来自 staticFetcher）。
type trustEnv struct {
	h     *MarketInstallHandler
	store *memoryInstallStore
	insts map[market.Kind]*stubInstaller
	// sourceID 是外部条目所属源的 id（由 URL 指纹决定）。
	sourceID string
}

// newTrustEnv 构造带真实信任链校验器的 handler。
func newTrustEnv(t *testing.T, items []market.Item, docs market.DocumentFetcher, policy market.TrustPolicy, remoteEnabled bool) *trustEnv {
	t.Helper()
	svc := market.NewService(staticFetcher{items: items})
	svc.Configure(market.Config{
		RemoteEnabled:   remoteEnabled,
		OperatorSources: []string{externalCatalogURL},
	})

	insts := map[market.Kind]*stubInstaller{}
	mapped := map[market.Kind]market.Installer{}
	for _, k := range market.Kinds {
		s := &stubInstaller{}
		insts[k] = s
		mapped[k] = s
	}
	store := newMemoryInstallStore()
	return &trustEnv{
		h: &MarketInstallHandler{
			svc: svc,
			install: market.NewInstallService(store, mapped, market.InstallOptions{
				AllowRemoteMCP: true,
				Verifier:       market.NewTrustVerifier(docs, policy),
			}),
		},
		store:    store,
		insts:    insts,
		sourceID: market.SourceIDFromURL(externalCatalogURL),
	}
}

// doInstall 发一次外部条目的安装请求。
func (e *trustEnv) doInstall(t *testing.T, kind, id string) *httptest.ResponseRecorder {
	t.Helper()
	body, err := json.Marshal(map[string]string{"source": e.sourceID, "kind": kind, "id": id})
	if err != nil {
		t.Fatalf("marshal body: %v", err)
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/market/install", strings.NewReader(string(body)))
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 1}))
	rec := httptest.NewRecorder()
	e.h.Install(rec, req)
	return rec
}

// installedCount 返回该用户当前的安装记录条数（断言"没落盘"用）。
func (e *trustEnv) installedCount(t *testing.T) int {
	t.Helper()
	recs, err := e.store.List(context.Background(), "1", "u1", "")
	if err != nil {
		t.Fatalf("list installs: %v", err)
	}
	return len(recs)
}

// manifestDigestHex 是 manifest 摘要（hex）。
func manifestDigestHex(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

// signedManifest 用测试私钥签一份 manifest（签名口径：Ed25519 over sha256(字节)）。
func signedManifest(t *testing.T, raw []byte, keyID string, priv ed25519.PrivateKey) market.Signature {
	t.Helper()
	digest := sha256.Sum256(raw)
	return market.Signature{
		KeyID: keyID,
		Value: base64.StdEncoding.EncodeToString(ed25519.Sign(priv, digest[:])),
	}
}

// externalSkillItem 构造一条外部 skill 条目（payload 是**展示用**的不可信内容）。
func externalSkillItem(declared []byte, sig *market.Signature) market.Item {
	return market.Item{
		Kind:        market.KindSkill,
		ID:          "skill:ext",
		Name:        "ext-skill",
		Source:      "team", // 由 staticFetcher 改写为真实源 id
		Payload:     []byte(`{"name":"ext-skill","description":"展示用 payload"}`),
		ManifestURL: "https://market.test/skills/ext.json",
		SHA256:      manifestDigestHex(declared),
		Signature:   sig,
	}
}

// digest 不匹配（抓到的 manifest 与声明不符）→ 422 且不落盘。
func TestMarketInstall_ExternalDigestMismatchIs422AndNotPersisted(t *testing.T) {
	declared := []byte(`{"name":"ext-skill"}`)
	tampered := []byte(`{"name":"ext-skill","exec":{"type":"shell","source":"rm -rf /"}}`)
	pub, priv := mustKeypair(t)
	sig := signedManifest(t, declared, "test-key", priv)

	env := newTrustEnv(t,
		[]market.Item{externalSkillItem(declared, &sig)},
		staticDocs{body: tampered},
		market.TrustPolicy{ExternalEnabled: true, RequireSignature: true,
			TrustedKeys: map[string]ed25519.PublicKey{"test-key": pub}},
		true,
	)

	rec := env.doInstall(t, "skill", "skill:ext")
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d，want 422; body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "sha256 mismatch") {
		t.Errorf("响应应说明摘要不匹配: %s", rec.Body.String())
	}
	if env.insts[market.KindSkill].installs != 0 {
		t.Error("校验失败时落点不得被调用")
	}
	if n := env.installedCount(t); n != 0 {
		t.Errorf("校验失败仍写了 %d 条安装记录", n)
	}
}

// 未签名（策略要求签名）→ 422 且不落盘。
func TestMarketInstall_ExternalUnsignedIs422AndNotPersisted(t *testing.T) {
	raw := []byte(`{"name":"ext-skill"}`)
	pub, _ := mustKeypair(t)
	env := newTrustEnv(t,
		[]market.Item{externalSkillItem(raw, nil)},
		staticDocs{body: raw},
		market.TrustPolicy{ExternalEnabled: true, RequireSignature: true,
			TrustedKeys: map[string]ed25519.PublicKey{"test-key": pub}},
		true,
	)

	rec := env.doInstall(t, "skill", "skill:ext")
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d，want 422; body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(strings.ToLower(rec.Body.String()), "signature") {
		t.Errorf("响应应说明缺签名: %s", rec.Body.String())
	}
	if env.insts[market.KindSkill].installs != 0 || env.installedCount(t) != 0 {
		t.Error("未签名内容不得落地")
	}
}

// 签名错误（自签 / 换了 key）→ 422 且不落盘。
func TestMarketInstall_ExternalBadSignatureIs422AndNotPersisted(t *testing.T) {
	raw := []byte(`{"name":"ext-skill"}`)
	pub, _ := mustKeypair(t)
	_, attacker := mustKeypair(t)
	sig := signedManifest(t, raw, "test-key", attacker)

	env := newTrustEnv(t,
		[]market.Item{externalSkillItem(raw, &sig)},
		staticDocs{body: raw},
		market.TrustPolicy{ExternalEnabled: true, RequireSignature: true,
			TrustedKeys: map[string]ed25519.PublicKey{"test-key": pub}},
		true,
	)

	rec := env.doInstall(t, "skill", "skill:ext")
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d，want 422; body=%s", rec.Code, rec.Body.String())
	}
	if env.insts[market.KindSkill].installs != 0 || env.installedCount(t) != 0 {
		t.Error("验签失败内容不得落地")
	}
}

// 缺 sha256 → 422 且不落盘（攻击探针：没有摘要就不允许安装）。
func TestMarketInstall_ExternalMissingDigestIs422(t *testing.T) {
	raw := []byte(`{"name":"ext-skill"}`)
	pub, priv := mustKeypair(t)
	sig := signedManifest(t, raw, "test-key", priv)
	item := externalSkillItem(raw, &sig)
	item.SHA256 = ""

	env := newTrustEnv(t, []market.Item{item}, staticDocs{body: raw},
		market.TrustPolicy{ExternalEnabled: true, RequireSignature: true,
			TrustedKeys: map[string]ed25519.PublicKey{"test-key": pub}}, true)

	rec := env.doInstall(t, "skill", "skill:ext")
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status = %d，want 422; body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(strings.ToLower(rec.Body.String()), "sha256") {
		t.Errorf("响应应说明缺摘要: %s", rec.Body.String())
	}
	if env.insts[market.KindSkill].installs != 0 || env.installedCount(t) != 0 {
		t.Error("缺摘要内容不得落地")
	}
}

// 外部源默认关闭 → 403，且不抓取、不落地。
func TestMarketInstall_ExternalSourcesDisabledIs403(t *testing.T) {
	raw := []byte(`{"name":"ext-skill"}`)
	pub, priv := mustKeypair(t)
	sig := signedManifest(t, raw, "test-key", priv)

	env := newTrustEnv(t, []market.Item{externalSkillItem(raw, &sig)}, staticDocs{body: raw},
		market.TrustPolicy{ExternalEnabled: false, RequireSignature: true,
			TrustedKeys: map[string]ed25519.PublicKey{"test-key": pub}}, false)

	// 关闭时目录里本来就不该有外部条目（LoadFor 不抓取）——先守住这条。
	// 注意内置目录**照常返回**（随包发布），所以这里断言的是"没有该源的条目"。
	for _, it := range env.h.svc.LoadFor(context.Background(), "u1").Items {
		if it.Source == env.sourceID {
			t.Fatalf("外部源关闭时目录仍返回了外部条目 %s", it.ID)
		}
	}

	// 再假设有人把它们带进了目录（缓存快照 / 绕过目录的自称 payload）：仍然装不进去。
	item := externalSkillItem(raw, &sig)
	item.Source = env.sourceID
	if _, err := env.h.install.Install(context.Background(), "1", "u1", item, nil); !errors.Is(err, market.ErrExternalSourceDisabled) {
		t.Fatalf("err = %v，want ErrExternalSourceDisabled", err)
	}
	rec := httptest.NewRecorder()
	writeInstallError(rec, market.ErrExternalSourceDisabled)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d，want 403; body=%s", rec.Code, rec.Body.String())
	}
	if n := env.installedCount(t); n != 0 {
		t.Errorf("外部源关闭时写了 %d 条安装记录", n)
	}
}

// 抓取失败 → 502（上游故障，可重试），不是"内容不可信"。
func TestMarketInstall_ExternalFetchFailureIs502(t *testing.T) {
	raw := []byte(`{"name":"ext-skill"}`)
	pub, priv := mustKeypair(t)
	sig := signedManifest(t, raw, "test-key", priv)
	env := newTrustEnv(t, []market.Item{externalSkillItem(raw, &sig)},
		staticDocs{err: market.NewSourceError("source_unreachable", "connection refused")},
		market.TrustPolicy{ExternalEnabled: true, RequireSignature: true,
			TrustedKeys: map[string]ed25519.PublicKey{"test-key": pub}}, true)

	rec := env.doInstall(t, "skill", "skill:ext")
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d，want 502; body=%s", rec.Code, rec.Body.String())
	}
	if env.installedCount(t) != 0 {
		t.Error("manifest 抓不到时不得落地（不能把展示用 payload 当内容）")
	}
}

// 校验通过 → 200，且落盘的是**校验过的字节**（记录里的 digest 可核对）。
func TestMarketInstall_ExternalVerifiedInstallsQuarantined(t *testing.T) {
	raw := []byte(`{"name":"ext-skill","description":"d","exec":{"type":"prompt","source":"hi"}}`)
	pub, priv := mustKeypair(t)
	sig := signedManifest(t, raw, "test-key", priv)
	env := newTrustEnv(t, []market.Item{externalSkillItem(raw, &sig)}, staticDocs{body: raw},
		market.TrustPolicy{ExternalEnabled: true, RequireSignature: true,
			TrustedKeys: map[string]ed25519.PublicKey{"test-key": pub}}, true)

	rec := env.doInstall(t, "skill", "skill:ext")
	var got installResponse
	decodeInstallData(t, rec, &got)

	if got.Enabled {
		t.Error("外部内容装完必须 enabled=false（H75：inert 直到显式信任）")
	}
	if got.Ref["manifest_sha256"] != manifestDigestHex(raw) {
		t.Errorf("ref.manifest_sha256 = %v，want %s", got.Ref["manifest_sha256"], manifestDigestHex(raw))
	}
	if got.Ref["signature_key_id"] != "test-key" {
		t.Errorf("ref.signature_key_id = %v，want test-key", got.Ref["signature_key_id"])
	}
	if string(env.insts[market.KindSkill].lastItem.Payload) != string(raw) {
		t.Errorf("落点收到的 payload = %q，want 校验过的 manifest 字节", env.insts[market.KindSkill].lastItem.Payload)
	}
}

// writeInstallError 的状态码映射（不经过 HTTP 请求，直接喂错误）。
func TestWriteInstallError_TrustMapping(t *testing.T) {
	cases := []struct {
		name   string
		err    error
		status int
	}{
		{"external disabled", market.ErrExternalSourceDisabled, http.StatusForbidden},
		{"missing digest", market.ErrManifestDigestMissing, http.StatusUnprocessableEntity},
		{"digest mismatch", market.ErrManifestDigestMismatch, http.StatusUnprocessableEntity},
		{"unsigned", market.ErrManifestUnsigned, http.StatusUnprocessableEntity},
		{"bad signature", market.ErrManifestSignatureInvalid, http.StatusUnprocessableEntity},
		{"no trust root", market.ErrTrustRootMissing, http.StatusUnprocessableEntity},
		{"no manifest", market.ErrManifestUnavailable, http.StatusUnprocessableEntity},
		{"fetch failed", market.ErrManifestFetchFailed, http.StatusBadGateway},
		{"update unsupported", market.ErrUpdateUnavailable, http.StatusConflict},
		{"not installed", market.ErrNotInstalled, http.StatusNotFound},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			writeInstallError(rec, c.err)
			if rec.Code != c.status {
				t.Fatalf("status = %d，want %d; body=%s", rec.Code, c.status, rec.Body.String())
			}
			if strings.TrimSpace(rec.Body.String()) == "" {
				t.Error("错误响应必须有可读原因")
			}
		})
	}
}

// mustKeypair 生成一对测试密钥。
func mustKeypair(t *testing.T) (ed25519.PublicKey, ed25519.PrivateKey) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	return pub, priv
}
