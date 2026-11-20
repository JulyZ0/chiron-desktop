package api

import (
	"encoding/binary"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgtype"

	"github.com/athenavi/chiron/internal/auth"
)

// 计费聚合「求和恒为 0」的回归（用户实测形态）。
//
// 缺陷链（三段拼起来才是完整的 bug）：
//
//  1. `usage_events.input_tokens / output_tokens / cached_tokens` 的列类型是
//     **bigint**，而 PG 的 `SUM(bigint)` 返回 **numeric**（只有 `SUM(int4)` 才回
//     int8）。`cost_cents` 是 integer，所以它的 SUM 是 int8 —— 这就是为什么
//     "花费有时是对的、token 永远是 0"。
//  2. `FetchAll` 把每一行扫进 `*interface{}`，numeric 解出来是 `pgtype.Numeric`。
//  3. `intOf` 以前只认 int/int32/int64/float64/string ⇒ numeric 落进 default 返回 0。
//
// 于是：`count(*)`（int8）正确、`event_count` 正确，而 daily/total 的 token 全 0。
//
// 修复是两道保险，这个用例两道都钉：
//   - SQL 侧统一 `COALESCE(SUM(x), 0)::bigint`（→ int64）；
//   - `intOf` 认 pgtype.Numeric（→ 任何漏 cast 的 SUM 也不会静默变 0）。

// numericFromInt 构造一个与 PG 二进制 numeric 解码结果**同构**的值。
//
// 不用 `pgtype.Numeric{Int: ..., Exp: ...}` 手搓：这里直接走 `Map.Scan` 走一遍真实
// 解码路径（与 `checkpoint_ingest_state_test.go` 的 int2 用例同一手法），
// 这样"驱动给出什么形状"是**证出来**的，不是假设出来的。
func numericFromInt(t *testing.T, v int64) any {
	t.Helper()
	m := pgtype.NewMap()

	// numeric 二进制格式：ndigits(int16) weight(int16) sign(int16) dscale(int16)
	// + 每 4 位十进制一个 digit(int16)。43620 = digits [4, 3620]，weight=1。
	buf := make([]byte, 8+2*2)
	binary.BigEndian.PutUint16(buf[0:2], 2) // ndigits = 2
	binary.BigEndian.PutUint16(buf[2:4], 1) // weight = 1（首 digit 位于 10^4 位）
	binary.BigEndian.PutUint16(buf[4:6], 0) // sign = 0（正）
	binary.BigEndian.PutUint16(buf[6:8], 0) // dscale = 0（整数）
	binary.BigEndian.PutUint16(buf[8:10], 4)
	binary.BigEndian.PutUint16(buf[10:12], 3620)

	var decoded any
	if err := m.Scan(pgtype.NumericOID, pgtype.BinaryFormatCode, buf, &decoded); err != nil {
		t.Fatalf("decode numeric: %v", err)
	}
	// 前置证明：pgx 对 numeric 给的就是 pgtype.Numeric（值类型，不是指针）。
	if _, ok := decoded.(pgtype.Numeric); !ok {
		t.Fatalf("前提不成立：pgx 把 numeric 解成了 %T（期望 pgtype.Numeric）", decoded)
	}
	// 顺带确认这段字节确实编码了 43620（否则下面的断言只是"吻合的巧合"）。
	if got := numericToInt(decoded.(pgtype.Numeric)); got != 43620 {
		t.Fatalf("numeric 二进制解码结果应为 43620，实际 %d", got)
	}
	_ = v
	return decoded
}

func TestBillingIntOfDecodesPGNumeric(t *testing.T) {
	decoded := numericFromInt(t, 43620)

	if got := intOf(decoded); got != 43620 {
		t.Fatalf("intOf(numeric 43620) = %d，期望 43620 —— 计费页 token 会恒为 0", got)
	}

	// 指针形态也要认：`rows.Scan` 的通用路径在部分 pgx 版本里会给 *Numeric。
	ptr := decoded.(pgtype.Numeric)
	if got := intOf(&ptr); got != 43620 {
		t.Fatalf("intOf(*numeric) = %d，期望 43620", got)
	}

	// 小数（dscale>0，如 AVG 的结果）按四舍五入取整，不能截断成 0 或丢位。
	var half any
	m := pgtype.NewMap()
	// 1234.5 ⇒ digits [1234, 5000]，weight=0，dscale=1
	raw := []byte{0x00, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x04, 0xD2, 0x13, 0x88}
	if err := m.Scan(pgtype.NumericOID, pgtype.BinaryFormatCode, raw, &half); err != nil {
		t.Fatalf("decode numeric(1234.5): %v", err)
	}
	if got := intOf(half); got != 1235 {
		t.Fatalf("intOf(numeric 1234.5) = %d，期望 1235（四舍五入）", got)
	}
}

// TestIntOfAcceptsAllSumShapes 钉住"两种修法都成立"：
// cast 之后是 int64，漏 cast 则是 pgtype.Numeric / 十进制串。三种都得给出同一个数。
func TestBillingIntOfAcceptsAllSumShapes(t *testing.T) {
	const want = 43620
	cases := []struct {
		name string
		val  any
	}{
		{"bigint after ::bigint cast", int64(want)},
		{"pgx numeric (no cast)", numericFromInt(t, want)},
		{"decimal string", "43620.0000"},
		{"integral string", "43620"},
		{"nil", nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := intOf(c.val)
			if c.val == nil {
				if got != 0 {
					t.Fatalf("nil 应为 0，实际 %d", got)
				}
				return
			}
			if got != want {
				t.Fatalf("intOf(%T) = %d，期望 %d", c.val, got, want)
			}
		})
	}
}

// TestNoUncastBigintSumInAPISource 是**源码棘轮**：`internal/api` 里任何 `SUM(`
// 都必须带 `::bigint`（注释除外）。
//
// 为什么要用"扫源码"这种非常规断言：这类缺陷不会报错、不会 panic，只会让界面显示
// 0 —— 而新增一条 SUM 比记住"PG 的 SUM(bigint) 是 numeric"容易得多。把它变成
// 一次编译期就红的失败，比写在文档里有效。
func TestBillingNoUncastBigintSumInAPISource(t *testing.T) {
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatalf("glob: %v", err)
	}
	if len(files) == 0 {
		t.Fatal("没有扫到任何源文件（工作目录不对？）")
	}
	for _, name := range files {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		raw, err := os.ReadFile(name)
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		for i, line := range strings.Split(string(raw), "\n") {
			trimmed := strings.TrimSpace(line)
			if strings.HasPrefix(trimmed, "//") {
				continue // 注释里可以讨论 SUM() OVER ()
			}
			if !strings.Contains(trimmed, "SUM(") {
				continue
			}
			if !strings.Contains(trimmed, "::bigint") {
				t.Errorf("%s:%d 的 SUM 缺少 ::bigint（PG 的 SUM(bigint) 是 numeric，intOf 会读成 0）：%s",
					name, i+1, trimmed)
			}
		}
	}
}

// ── provider 归属 ──

// TestEnsureBreakdownProviderKeysAlwaysPresent 钉住"键必须存在"：
// 前端按 `provider` 渲染列、按 `provider_missing` 决定是否显示"—"。缺键会让它读到
// undefined 而不是空串，界面就是把 "undefined" 当厂商名印出来。
func TestBillingEnsureBreakdownProviderKeysAlwaysPresent(t *testing.T) {
	rows := buildBreakdownRows([]usageBucketRow{
		{bucket: "deepseek-v4.1-flash", events: 2, inputTokens: 10, outputTokens: 1},
		{bucket: "gpt-4o", provider: "openai", events: 1, inputTokens: 5, outputTokens: 1, costCents: 2, pricedCalls: 1},
	})

	for _, row := range rows {
		if _, ok := row["provider"]; !ok {
			t.Fatalf("每行必须带 provider 键：%v", row)
		}
		if _, ok := row["provider_missing"]; !ok {
			t.Fatalf("每行必须带 provider_missing 键：%v", row)
		}
	}
	if rows[0]["provider_missing"] != true || rows[0]["provider"] != "" {
		t.Fatalf("没有 provider 的行必须标 missing 且留空（不猜厂商），实际 %v", rows[0])
	}
	if rows[1]["provider_missing"] != false || rows[1]["provider"] != "openai" {
		t.Fatalf("已知 provider 的行不该标 missing，实际 %v", rows[1])
	}
}

// TestEnrichBreakdownProvidersKeepsUnknownEmpty 在没有数据库的测试进程里，
// 反查必然失败 —— 此时必须**留空并标 missing**，绝不允许出现编出来的厂商名。
func TestBillingEnrichBreakdownProvidersKeepsUnknownEmpty(t *testing.T) {
	rows := []map[string]any{
		{"bucket": "deepseek-v4.1-flash"},
		{"bucket": "gpt-4o", "provider": "openai"},
	}

	missing := enrichBreakdownProviders(t.Context(), rows)

	if rows[1]["provider"] != "openai" || rows[1]["provider_missing"] != false {
		t.Fatalf("已有 provider 不该被覆盖：%v", rows[1])
	}
	if rows[0]["provider"] != "" || rows[0]["provider_missing"] != true {
		t.Fatalf("查不到时必须留空并标 missing，实际 %v", rows[0])
	}
	if missing != 1 {
		t.Fatalf("缺失计数应为 1，实际 %d", missing)
	}
}

// TestRecordUsageEventFillsProviderOnlyWhenEmpty 钉住回填的边界：
// 调用方给了 provider 就**不动它**（它可能比反查更准：同一模型可经不同网关），
// 只有为空时才按模型反查。
func TestRecordUsageEventFillsProviderOnlyWhenEmpty(t *testing.T) {
	cap := captureUsageEvents(t)
	clearProviderCache()

	RecordUsageEvent(t.Context(), UsageEvent{Model: "gpt-4o", Provider: "custom-gateway", InputTokens: 10})
	RecordUsageEvent(t.Context(), UsageEvent{Model: "gpt-4o", InputTokens: 10})

	rows := cap.snapshot()
	if len(rows) != 2 {
		t.Fatalf("应写 2 行，实际 %d", len(rows))
	}
	if rows[0].Provider != "custom-gateway" {
		t.Fatalf("调用方给的 provider 不该被覆盖，实际 %q", rows[0].Provider)
	}
	// 测试进程没有可用连接池 ⇒ 反查失败 ⇒ 留空（不猜、也不报错）。
	if rows[1].Provider != "" {
		t.Fatalf("反查不到时应留空，实际 %q", rows[1].Provider)
	}
}

// ── 单价写入端点 ──

func pricePutRequest(t *testing.T, model, body string, authed bool) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodPut, "/v1/admin/model-prices/"+model, strings.NewReader(body))
	req.SetPathValue("model", model)
	if authed {
		claims := &auth.Claims{UserID: "owner-1", TenantID: 0, Role: "owner"}
		req = req.WithContext(auth.WithClaims(req.Context(), claims))
	}
	return req
}

func TestBillingUpsertModelPriceRequiresAuth(t *testing.T) {
	rec := httptest.NewRecorder()
	UpsertModelPrice(rec, pricePutRequest(t, "m", `{"input_per_1k":1,"output_per_1k":2}`, false))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("未认证应 401，实际 %d", rec.Code)
	}
}

func TestBillingUpsertModelPriceRejectsBadBodies(t *testing.T) {
	cases := []struct{ name, body string }{
		{"both zero", `{"input_per_1k":0,"output_per_1k":0}`},
		{"negative", `{"input_per_1k":-1,"output_per_1k":1}`},
		{"non USD", `{"input_per_1k":1,"output_per_1k":1,"currency":"CNY"}`},
		{"malformed", `{`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			UpsertModelPrice(rec, pricePutRequest(t, "deepseek-v4.1-flash", c.body, true))
			if rec.Code != http.StatusBadRequest {
				t.Fatalf("%s 应 400，实际 %d：%s", c.name, rec.Code, rec.Body.String())
			}
		})
	}
}

// TestUpsertModelPriceRejectsNaNAndInf 单独一条：NaN/Inf 在 JSON 里通常不合法，
// 但一旦漏进来写进 numeric 列，之后**每一次 SUM 都被污染**（PG 的 numeric NaN 会
// 传染）。校验必须在落库之前。
func TestBillingUpsertModelPriceRejectsNaNAndInf(t *testing.T) {
	for _, v := range []float64{math.NaN(), math.Inf(1), math.Inf(-1)} {
		if isFiniteNonNegative(v) {
			t.Fatalf("%v 不该被判为合法单价", v)
		}
	}
	if !isFiniteNonNegative(0) || !isFiniteNonNegative(1.25) {
		t.Fatal("0 与非负有限值应合法")
	}
}

// TestUpsertModelPricePassesValidationThenFailsWithoutDB 是"校验通过"的证据：
// 合法请求在没有数据库的测试进程里落到 503（服务端暂时不可用），而不是 400。
//
// 如果这里回 400，说明校验把合法请求也挡了；回 500 说明错误分类错了（写不进去
// 不是"调用方的问题"）。
func TestBillingUpsertModelPricePassesValidationThenFailsWithoutDB(t *testing.T) {
	rec := httptest.NewRecorder()
	body := `{"input_per_1k":0.27,"output_per_1k":1.1,"currency":"USD"}`
	UpsertModelPrice(rec, pricePutRequest(t, "deepseek-v4.1-flash", body, true))
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("合法请求在无库时应 503，实际 %d：%s", rec.Code, rec.Body.String())
	}
}

// TestUpsertModelPriceAcceptsModelFromBody 兼容"模型名里带 /"的场景：
// 路径段必须 %2F 编码，客户端如果直接拼进 body 也要能用。
func TestBillingUpsertModelPriceAcceptsModelFromBody(t *testing.T) {
	rec := httptest.NewRecorder()
	body := `{"model":"meta-llama/Llama-3","input_per_1k":0.5,"output_per_1k":0.5}`
	req := httptest.NewRequest(http.MethodPut, "/v1/admin/model-prices/", strings.NewReader(body))
	// 路径段为空：靠 body 里的 model 回填
	req.SetPathValue("model", "")
	claims := &auth.Claims{UserID: "owner-1", Role: "owner"}
	req = req.WithContext(auth.WithClaims(req.Context(), claims))

	UpsertModelPrice(rec, req)

	if rec.Code == http.StatusBadRequest {
		t.Fatalf("应接受请求体里的 model，实际 400：%s", rec.Body.String())
	}
}
