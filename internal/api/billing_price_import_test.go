package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgconn"

	"github.com/athenavi/chiron/internal/auth"
)

// 批量导入（P0-4）的校验矩阵与事务性用例。
//
// 关键口径（每条都对应一个用户可见的后果）：
//
//  1. **与手工单价同一套规则**：非负、不得 0/0、只接受 USD。两处规则一旦分叉，
//     同一行价格在"手填"和"导入"两条路上会有不同下场。
//  2. **全部行非法时不碰数据库**：返回 `imported: 0` + 完整 skipped，用户照着改。
//  3. **写库是单事务**：第 N 行存储失败必须整体回滚，`imported` 不许出现"写了一半"。
//  4. **不猜厂商**：provider 只按 llm_models 反查；查不到就留空。

// fakePriceStore 是 priceImportStore 的假实现（本仓没有 pgxmock，见
// api_keys_handler_test.go 的说明）：记录写入行数，可注入"第 N 行失败"。
type fakePriceStore struct {
	providers map[string]string
	// failAtRow > 0 时，第 failAtRow 次 Exec 返回错误（模拟约束冲突/连接中断）。
	failAtRow int
	execRows  []priceImportRow
	txCalls   int
	committed bool
}

func (f *fakePriceStore) ResolveProviders(_ context.Context, models []string) map[string]string {
	out := make(map[string]string, len(models))
	for _, model := range models {
		if p, ok := f.providers[model]; ok {
			out[model] = p
		}
	}
	return out
}

func (f *fakePriceStore) WithTx(_ context.Context, fn func(tx priceTx) error) error {
	f.txCalls++
	tx := &fakePriceTx{store: f}
	if err := fn(tx); err != nil {
		// 事务回滚：已写入的行不再算数
		f.execRows = nil
		return err
	}
	f.committed = true
	return nil
}

type fakePriceTx struct{ store *fakePriceStore }

func (t *fakePriceTx) Exec(_ context.Context, _ string, args ...any) (pgconn.CommandTag, error) {
	if t.store.failAtRow > 0 && len(t.store.execRows)+1 == t.store.failAtRow {
		return pgconn.CommandTag{}, errors.New("simulated constraint violation")
	}
	row := priceImportRow{
		Model:       args[0].(string),
		Provider:    args[1].(string),
		InputPer1K:  args[2].(float64),
		OutputPer1K: args[3].(float64),
		Currency:    args[4].(string),
	}
	t.store.execRows = append(t.store.execRows, row)
	return pgconn.NewCommandTag("INSERT 0 1"), nil
}

// importRequest 构造一个带 claims 的导入请求（handler 直接取 claims，不经过中间件）。
func importRequest(t *testing.T, payload string) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/admin/billing/model-prices/import",
		strings.NewReader(payload))
	req.Header.Set("Content-Type", "application/json")
	return req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{
		UserID: "owner-1", Role: "owner",
	}))
}

// runImport 调 handler 并解出响应体里的 data。
func runImport(t *testing.T, store priceImportStore, payload string) (int, map[string]any) {
	t.Helper()
	rec := httptest.NewRecorder()
	ImportModelPrices(store)(rec, importRequest(t, payload))
	var body struct {
		Data map[string]any `json:"data"`
		Code string         `json:"code"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &body)
	return rec.Code, body.Data
}

const importCSVHeader = "model,input_per_1k,output_per_1k,currency,provider\n"

// csvPayload 用 encoding/json 组装请求体：CSV 里的换行必须转义成 \n，
// 手写字符串字面量几乎一定会漏（漏了就是 400 "invalid request body"）。
func csvPayload(t *testing.T, data string) string {
	t.Helper()
	raw, err := json.Marshal(map[string]string{"format": "csv", "data": data})
	if err != nil {
		t.Fatalf("组装请求体失败：%v", err)
	}
	return string(raw)
}

func TestPriceImportCSVHappyPath(t *testing.T) {
	store := &fakePriceStore{providers: map[string]string{"deepseek-v4.1-flash": "deepseek"}}
	payload := csvPayload(t, importCSVHeader+
		"deepseek-v4.1-flash,0.27,1.10,USD,\n"+
		"gpt-4o,2.5,10,usd,openai\n")

	code, data := runImport(t, store, payload)
	if code != http.StatusOK {
		t.Fatalf("应 200，实际 %d（%v）", code, data)
	}
	if data["imported"] != float64(2) {
		t.Fatalf("应导入 2 行，实际 %v", data["imported"])
	}
	if skipped := data["skipped"].([]any); len(skipped) != 0 {
		t.Fatalf("合法行不该被跳过：%v", skipped)
	}
	// 空 provider 走 llm_models 反查；写进去的是 deepseek，不是猜的
	if len(store.execRows) != 2 || store.execRows[0].Provider != "deepseek" {
		t.Fatalf("provider 应由 llm_models 反查得到：%#v", store.execRows)
	}
	if store.execRows[1].Currency != "USD" {
		t.Fatalf("小写 usd 应归一化成 USD：%#v", store.execRows[1])
	}
	if !store.committed {
		t.Fatal("合法批次必须提交")
	}
}

func TestPriceImportValidationMatrix(t *testing.T) {
	// 直接钉校验函数：每种非法输入必须被**单独**说明原因，而不是笼统的"格式错误"。
	raw := []rawPriceRow{
		{Row: 2, Model: "", InputRaw: "1", OutputRaw: "1", Currency: "USD"},
		{Row: 3, Model: strings.Repeat("m", 129), InputRaw: "1", OutputRaw: "1", Currency: "USD"},
		{Row: 4, Model: "m", InputRaw: "abc", OutputRaw: "1", Currency: "USD"},
		{Row: 5, Model: "m", InputRaw: "1", OutputRaw: "-1", Currency: "USD"},
		{Row: 6, Model: "m", InputRaw: "0", OutputRaw: "0", Currency: "USD"},
		{Row: 7, Model: "m", InputRaw: "1", OutputRaw: "1", Currency: "CNY"},
		{Row: 8, Model: "m", InputRaw: "1", OutputRaw: "1", Currency: "USD", Provider: strings.Repeat("p", 51)},
		{Row: 9, Model: "m", InputRaw: "NaN", OutputRaw: "1", Currency: "USD"},
	}

	rows, skipped := validatePriceImportRows(raw, nil)

	if len(rows) != 0 {
		t.Fatalf("矩阵里没有合法行，实际写出 %#v", rows)
	}
	if len(skipped) != len(raw) {
		t.Fatalf("每行都要有跳过原因，实际 %d/%d", len(skipped), len(raw))
	}
	wantReasons := map[int]string{
		2: "model is required",
		3: "model name too long",
		4: "input_per_1k must be a finite number",
		5: "output_per_1k must be a finite number",
		6: "cannot both be 0",
		7: "only USD",
		8: "provider too long",
		9: "input_per_1k must be a finite number",
	}
	for _, skip := range skipped {
		want, ok := wantReasons[skip.Row]
		if !ok {
			t.Fatalf("意外行号 %d：%s", skip.Row, skip.Reason)
		}
		if !strings.Contains(skip.Reason, want) {
			t.Fatalf("第 %d 行原因应含 %q，实际 %q", skip.Row, want, skip.Reason)
		}
	}
}

func TestPriceImportDuplicateRowsKeepFirst(t *testing.T) {
	raw := []rawPriceRow{
		{Row: 2, Model: "gpt-4o", Provider: "openai", InputRaw: "1", OutputRaw: "2", Currency: "USD"},
		{Row: 3, Model: "gpt-4o", Provider: "openai", InputRaw: "3", OutputRaw: "4", Currency: "USD"},
	}

	rows, skipped := validatePriceImportRows(raw, nil)

	if len(rows) != 1 || rows[0].InputPer1K != 1 {
		t.Fatalf("同批重复键只落第一条：%#v", rows)
	}
	if len(skipped) != 1 || !strings.Contains(skipped[0].Reason, "duplicate") {
		t.Fatalf("重复行必须给出原因：%#v", skipped)
	}
	// skipped 里要能指回第一条的位置，否则用户不知道该删哪一行
	if !strings.Contains(skipped[0].Reason, "row 2") {
		t.Fatalf("重复原因应指出首个出现行号：%q", skipped[0].Reason)
	}
}

func TestPriceImportCSVHeaderMissingColumn(t *testing.T) {
	store := &fakePriceStore{}
	code, data := runImport(t, store,
		`{"format":"csv","data":"model,input_per_1k,output_per_1k\nm,1,2\n"}`)

	if code != http.StatusBadRequest {
		t.Fatalf("缺 currency 列应 400，实际 %d（%v）", code, data)
	}
	if store.txCalls != 0 {
		t.Fatal("解析失败时不该碰数据库")
	}
}

func TestPriceImportCSVRowColumnMismatchIsSkipped(t *testing.T) {
	// 行列数与表头不一致：跳过这一行，不整批失败（CSV 常见末尾多一个逗号）
	rows, skipped, err := parsePriceCSV([]byte(importCSVHeader + "gpt-4o,1,2,USD\n"))
	if err != nil {
		t.Fatalf("解析不该失败：%v", err)
	}
	// 表头 5 列、数据 4 列 ⇒ 跳过
	if len(rows) != 0 || len(skipped) != 1 {
		t.Fatalf("列数不齐应跳过：rows=%#v skipped=%#v", rows, skipped)
	}
	if skipped[0].Row != 2 {
		t.Fatalf("行号按文件行报（表头是 1）：%#v", skipped[0])
	}
}

func TestPriceImportJSONArrayAndWrapper(t *testing.T) {
	array := `[{"model":"a","input_per_1k":1,"output_per_1k":2,"currency":"USD"}]`
	wrapped := `{"prices":[{"model":"b","input_per_1k":3,"output_per_1k":4}]}`

	rows, skipped, err := parsePriceJSON([]byte(array))
	if err != nil || len(rows) != 1 || len(skipped) != 0 {
		t.Fatalf("数组形态解析失败：%v %#v %#v", err, rows, skipped)
	}
	rows, _, err = parsePriceJSON([]byte(wrapped))
	if err != nil || len(rows) != 1 || rows[0].Model != "b" {
		t.Fatalf("{\"prices\":[]} 形态解析失败：%v %#v", err, rows)
	}
	// currency 缺省 USD
	valid, _ := validatePriceImportRows(rows, nil)
	if len(valid) != 1 || valid[0].Currency != "USD" {
		t.Fatalf("currency 缺省应为 USD：%#v", valid)
	}
}

func TestPriceImportAllInvalidNeverTouchesDB(t *testing.T) {
	store := &fakePriceStore{}
	code, data := runImport(t, store,
		`{"format":"csv","data":"model,input_per_1k,output_per_1k,currency\n,1,2,USD\nm,0,0,USD\n"}`)

	if code != http.StatusOK {
		t.Fatalf("全部行非法应回 200 + skipped（不是 500），实际 %d", code)
	}
	if data["imported"] != float64(0) {
		t.Fatalf("imported 应为 0，实际 %v", data["imported"])
	}
	skipped := data["skipped"].([]any)
	if len(skipped) != 2 {
		t.Fatalf("两行都要有原因：%v", skipped)
	}
	if store.txCalls != 0 {
		t.Fatal("没有任何合法行时不许开事务")
	}
}

func TestPriceImportTransactionRollsBackOnStorageFailure(t *testing.T) {
	// 第 2 行写入失败 ⇒ 整体回滚：execRows 被清空，响应 503，不能假装导入成功。
	store := &fakePriceStore{failAtRow: 2}
	code, _ := runImport(t, store,
		csvPayload(t, "model,input_per_1k,output_per_1k,currency\na,1,2,USD\nb,3,4,USD\n"))

	if code != http.StatusServiceUnavailable {
		t.Fatalf("存储失败应 503，实际 %d", code)
	}
	if len(store.execRows) != 0 {
		t.Fatalf("事务必须整体回滚，实际残留 %#v", store.execRows)
	}
	if store.committed {
		t.Fatal("失败的事务不能被标记为已提交")
	}
}

func TestPriceImportRejectsOverRowLimit(t *testing.T) {
	var b strings.Builder
	b.WriteString(`{"format":"csv","data":"model,input_per_1k,output_per_1k,currency\n`)
	for i := 0; i < priceImportMaxRows+1; i++ {
		b.WriteString("m")
		b.WriteString(strings.Repeat("x", i%3))
		b.WriteString(",1,2,USD\n")
	}
	b.WriteString(`"}`)
	store := &fakePriceStore{}

	code, data := runImport(t, store, b.String())

	if code != http.StatusBadRequest {
		t.Fatalf("超过 %d 行应 400，实际 %d（%v）", priceImportMaxRows, code, data)
	}
	if store.txCalls != 0 {
		t.Fatal("超限请求不该碰数据库")
	}
}

func TestPriceImportRequiresAuth(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/admin/billing/model-prices/import",
		strings.NewReader(`{"format":"csv","data":"model,input_per_1k,output_per_1k,currency\n"}`))
	ImportModelPrices(&fakePriceStore{})(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("未认证应 401，实际 %d", rec.Code)
	}
}

func TestPriceImportMultipartFileInfersFormatFromExtension(t *testing.T) {
	// "选择文件"路径：multipart 上传 .csv，**不传 format**，按扩展名推断。
	var body bytes.Buffer
	writer := multipart.NewWriter(&body)
	part, err := writer.CreateFormFile("file", "prices.csv")
	if err != nil {
		t.Fatalf("构造 multipart 失败：%v", err)
	}
	if _, err := part.Write([]byte("model,input_per_1k,output_per_1k,currency\n" +
		"deepseek-v4.1-flash,0.27,1.1,USD\n")); err != nil {
		t.Fatalf("写入文件内容失败：%v", err)
	}
	_ = writer.Close()

	req := httptest.NewRequest(http.MethodPost, "/v1/admin/billing/model-prices/import", &body)
	req.Header.Set("Content-Type", writer.FormDataContentType())
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "owner-1"}))

	store := &fakePriceStore{}
	rec := httptest.NewRecorder()
	ImportModelPrices(store)(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("multipart 导入应 200，实际 %d（%s）", rec.Code, rec.Body.String())
	}
	if len(store.execRows) != 1 || store.execRows[0].Model != "deepseek-v4.1-flash" {
		t.Fatalf("上传的文件应被导入：%#v", store.execRows)
	}
}

func TestPriceImportRejectsUnsupportedCurrencyFromJSON(t *testing.T) {
	// JSON 里显式给 CNY：必须跳过（读侧只认 USD），且原因可见。
	store := &fakePriceStore{}
	code, data := runImport(t, store,
		`{"format":"json","data":[{"model":"m","input_per_1k":1,"output_per_1k":2,"currency":"CNY"}]}`)

	if code != http.StatusOK || data["imported"] != float64(0) {
		t.Fatalf("非 USD 行应只跳过不报错：%d %#v", code, data)
	}
	skipped := data["skipped"].([]any)
	if len(skipped) != 1 || !strings.Contains(skipped[0].(map[string]any)["reason"].(string), "USD") {
		t.Fatalf("跳过原因应说明只支持 USD：%v", skipped)
	}
}
