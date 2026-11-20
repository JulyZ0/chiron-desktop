package api

// 批量导入模型单价：POST /v1/admin/billing/model-prices/import
//
// **它补的是哪一段**：手工设置单价（`billing_price_admin.go`）一次只能补一个模型；
// 用户实测的场景是"价格表整体缺行"（价格源失败时 deepseek-v4.1-flash 这类模型
// 一条都没有），逐个点开对话框填 20 个模型不现实。这里让人一次贴上 CSV/JSON。
//
// ## 契约
//
//	POST /v1/admin/billing/model-prices/import
//	{ "format": "csv", "data": "model,input_per_1k,...\n..." }
//	或 multipart/form-data：file=<csv/json>、format=(可选，按扩展名推断)
//
// CSV 表头（顺序可变，大小写与首尾空白不敏感）：
//
//	model,input_per_1k,output_per_1k,currency[,provider]
//
// JSON：对象数组 `[{"model":…,"input_per_1k":…,"output_per_1k":…,"currency":"USD"}]`
// （也接受 `{"prices":[...]}` 与"字符串里再放一段 JSON"两种包装）。
//
// ## 校验口径（与手工单价**同一套**，见 `UpsertModelPrice`）
//
//   - `input_per_1k` / `output_per_1k`：有限非负数，且**不能同时为 0**（0/0 不是免费，
//     而是"不知道"，落库后仍然算不出钱）；
//   - `currency` 缺省 USD，**只接受 USD**（读侧只认 USD 行）；
//   - 模型名 ≤128 字符、provider ≤50 字符（与列宽对齐；超长直接跳过而不是静默截断，
//     导入场景里"被改了一个名字"比"整行被拒"更难发现）；
//   - 单次最多 2000 行；同一批里重复的 `(model, provider)` 只落第一条，后续跳过。
//
// ## 事务与"全不落"
//
// 逐行校验是**先做完整遍**，再在一个事务里写全部合法行。任何一行写失败（约束、
// 连接中断）都会整体回滚并返回 503 —— 返回体里的 `imported` 只有在提交成功后才
// 是真实行数。**全部行都非法时不碰数据库**：`imported: 0` + 完整 `skipped` 列表，
// 让用户能照着改。
//
// ## 为什么不写 `provider` 就让它空着
//
// 与手工单价一致：只按 `llm_models`（name / id）反查，**不按模型名猜厂商**。
// 一次导入用**一条** SQL 反查全部模型（而不是每行一次），2000 行也不会变成
// 2000 次往返。

import (
	"bytes"
	"context"
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime"
	"net/http"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

const (
	// priceImportMaxRows 是单次导入的上限。2000 行 ≈ 几百 KB，一次事务写得完；
	// 再多就该走离线脚本，而不是把管理端接口当 ETL。
	priceImportMaxRows = 2000
	// priceImportMaxBytes 是 multipart 文件与 JSON data 的上限（4MB）。
	priceImportMaxBytes = 4 << 20
)

// priceImportSkip 是"这一行为什么没导进去"。`row` 是**文件里的行号**：
// CSV 的表头是第 1 行，因此第一条数据是 2；JSON 是数组下标 + 1。
type priceImportSkip struct {
	Row    int    `json:"row"`
	Reason string `json:"reason"`
}

// rawPriceRow 是解析出来但**尚未校验**的一行（价格保持字符串，便于报"不是数字"）。
type rawPriceRow struct {
	Row       int
	Model     string
	Provider  string
	Currency  string
	InputRaw  string
	OutputRaw string
}

// priceImportRow 是校验通过、准备写库的一行。
type priceImportRow struct {
	Model       string
	Provider    string
	Currency    string
	InputPer1K  float64
	OutputPer1K float64
}

// priceTx 是导入写路径需要的最小事务能力（便于单测注入"第 N 行失败"的假实现）。
type priceTx interface {
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
}

// priceImportStore 是导入端点对存储的全部依赖。
type priceImportStore interface {
	// ResolveProviders 批量反查模型 → 厂商（查不到/有歧义的模型不出现在结果里）。
	ResolveProviders(ctx context.Context, models []string) map[string]string
	// WithTx 在一个事务里执行 fn；fn 返回错误则整体回滚。
	WithTx(ctx context.Context, fn func(tx priceTx) error) error
}

// dbPriceStore 是 priceImportStore 的 PG 实现。
type dbPriceStore struct{}

// ResolveProviders 用一条 SQL 反查全部模型的厂商归属。
//
// 同一模型名可能对应多个厂商（不同网关重复注册）；`HAVING count(DISTINCT provider) = 1`
// 让这种歧义**不产生映射** —— 宁可 provider 留空，也不要随机挑一个。
func (dbPriceStore) ResolveProviders(ctx context.Context, models []string) map[string]string {
	out := make(map[string]string, len(models))
	if len(models) == 0 || db.GlobalDBManager == nil {
		return out
	}
	rows, err := db.GlobalDBManager.FetchAll(ctx, `
		SELECT btrim(name)     AS model, btrim(provider) AS provider
		  FROM llm_models
		 WHERE name = ANY($1::text[])
		   AND provider IS NOT NULL AND btrim(provider) <> ''
		UNION ALL
		SELECT btrim(id)       AS model, btrim(provider) AS provider
		  FROM llm_models
		 WHERE id = ANY($1::text[])
		   AND provider IS NOT NULL AND btrim(provider) <> ''`, models)
	if err != nil {
		// 反查失败不是导入失败：provider 只是归属信息，留空后价格照样能查到
		// （读侧不按 provider 过滤）。
		slog.Warn("model price import: provider resolve failed", "models", len(models), "error", err)
		return out
	}
	// 歧义规则与历史回填**共用同一个纯函数**（见 billing_price_provider_map.go）：
	// 同名模型对应多个厂商时两边都留空，不会出现"导入填了、回填又改成另一个"。
	return collapseProviderMap(toModelProviderRows(rows))
}

// WithTx 在一个事务里执行 fn（任何错误都回滚）。
func (dbPriceStore) WithTx(ctx context.Context, fn func(tx priceTx) error) error {
	if db.GlobalDBManager == nil {
		return errors.New("price store unavailable")
	}
	return db.GlobalDBManager.WithTransaction(ctx, func(tx pgx.Tx) error {
		return fn(pgxPriceTx{tx: tx})
	})
}

// pgxPriceTx 让 pgx.Tx 满足 priceTx（Exec 的签名本来就一致，这里只是显式收窄）。
type pgxPriceTx struct{ tx pgx.Tx }

func (t pgxPriceTx) Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	return t.tx.Exec(ctx, sql, args...)
}

// ImportModelPrices 批量写入模型单价（平台 owner 写权限，见路由注册）。
func ImportModelPrices(store priceImportStore) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		claims := auth.GetClaims(r.Context())
		if claims == nil {
			Unauthorized(w, ErrAuthRequired)
			return
		}

		format, data, err := readPriceImportPayload(w, r)
		if err != nil {
			BadRequest(w, err.Error())
			return
		}

		raw, skipped, err := parsePriceImport(format, data)
		if err != nil {
			BadRequest(w, err.Error())
			return
		}
		if len(raw) > priceImportMaxRows {
			// 整批拒绝而不是截断：截断会让用户以为"都导进去了"，而后面几百行静默丢失。
			BadRequest(w, fmt.Sprintf("too many rows: %d (max %d)", len(raw), priceImportMaxRows))
			return
		}
		if store == nil {
			ServiceUnavailable(w, "price store unavailable")
			return
		}

		// 只为**缺 provider** 的行反查，且一次查完。
		needProviders := make([]string, 0, 8)
		seen := make(map[string]bool, 8)
		for _, row := range raw {
			model := strings.TrimSpace(row.Model)
			if strings.TrimSpace(row.Provider) != "" || model == "" || seen[model] {
				continue
			}
			seen[model] = true
			needProviders = append(needProviders, model)
		}
		providers := store.ResolveProviders(r.Context(), needProviders)

		rows, validationSkips := validatePriceImportRows(raw, providers)
		skipped = append(skipped, validationSkips...)

		if len(rows) == 0 {
			// **不碰数据库**：全部行非法时没有任何写入可言，返回完整 skipped 让人照着改。
			OK(w, map[string]any{
				"imported":  0,
				"total":     len(raw),
				"skipped":   skipped,
				"format":    format,
				"effective": "next_call",
				"note":      "没有任何合法行，未写入数据库。",
			})
			return
		}

		// 先校验后事务写：到这里 rows 已全部合法，剩下的失败只可能是存储层问题，
		// 此时**整体回滚**（imported 不会出现"写了一半"的数字）。
		err = store.WithTx(r.Context(), func(tx priceTx) error {
			for _, row := range rows {
				if _, execErr := tx.Exec(r.Context(), modelPriceUpsertSQL,
					row.Model, row.Provider, row.InputPer1K, row.OutputPer1K, row.Currency); execErr != nil {
					return execErr
				}
			}
			return nil
		})
		if err != nil {
			slog.Warn("model price import: transaction failed", "rows", len(rows),
				"user", claims.UserID, "error", err)
			ServiceUnavailable(w, "price import failed (rolled back); nothing was written")
			return
		}

		// 与手工单价同一口径：保存即清缓存，下一次调用立刻按新价折算。
		clearPriceCache()
		slog.Info("model price import: committed", "imported", len(rows),
			"skipped", len(skipped), "user", claims.UserID)

		OK(w, map[string]any{
			"imported":  len(rows),
			"total":     len(raw),
			"skipped":   skipped,
			"format":    format,
			"effective": "next_call",
			"note":      "新调用的成本立即按新单价折算；历史行 cost_cents 不回填，价格覆盖率立即生效。",
		})
	}
}

// modelPriceUpsertSQL 与手工单价的 UPSERT 逐字一致（含 sources='manual' 语义）。
//
// 导入与手工设置共用同一条写语句：两处各写一份迟早会出现"手工写 sources、导入忘了写"
// 这类漂移，而价格表的"这行从哪来"正是核对成本口径时的第一手证据。
const modelPriceUpsertSQL = `
	INSERT INTO model_prices
	       (model, provider, input_per_1k, output_per_1k, currency,
	        sources, source_count, verified_at, updated_at)
	VALUES ($1, $2, $3, $4, $5, 'import', 1, now(), now())
	ON CONFLICT (model, provider) DO UPDATE
	   SET input_per_1k  = EXCLUDED.input_per_1k,
	       output_per_1k = EXCLUDED.output_per_1k,
	       currency      = EXCLUDED.currency,
	       sources       = EXCLUDED.sources,
	       source_count  = EXCLUDED.source_count,
	       verified_at   = now(),
	       updated_at    = now()`

// readPriceImportPayload 取出 `format` 与原始文本（JSON body 或 multipart 文件）。
func readPriceImportPayload(w http.ResponseWriter, r *http.Request) (string, []byte, error) {
	mediaType, _, _ := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if strings.HasPrefix(mediaType, "multipart/") {
		return readMultipartPriceImport(w, r)
	}

	// 自己限长而不用 `DecodeJSON`：它固定 1MB，而 2000 行 JSON（每行带完整字段）
	// 可能超过 —— 那会把一个合法批次报成 "invalid request body"，且看不出原因。
	r.Body = http.MaxBytesReader(w, r.Body, priceImportMaxBytes)
	var body struct {
		Format string          `json:"format"`
		Data   json.RawMessage `json:"data"`
	}
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&body); err != nil {
		return "", nil, errors.New("invalid request body")
	}
	format := normalizePriceImportFormat(body.Format)
	if len(body.Data) == 0 {
		return "", nil, errors.New("data is required")
	}

	// data 允许两种形态：CSV 的字符串，或 JSON 数组/对象（原样透传给解析器）。
	// 若给的是字符串但格式是 json，说明客户端把 JSON 又包了一层 —— 解开一次。
	var asString string
	if err := json.Unmarshal(body.Data, &asString); err == nil {
		if format == "" {
			format = sniffPriceImportFormat([]byte(asString))
		}
		if format == "" {
			format = "csv"
		}
		return format, []byte(asString), nil
	}
	if format == "" {
		format = sniffPriceImportFormat(body.Data)
	}
	if format == "" {
		return "", nil, errors.New("format is required (csv|json)")
	}
	return format, body.Data, nil
}

// readMultipartPriceImport 读 multipart 里的文件（字段名 file，回退 data）。
func readMultipartPriceImport(w http.ResponseWriter, r *http.Request) (string, []byte, error) {
	// ParseMultipartForm 的入参是**内存**上限，超过部分落临时文件；这里再显式限制总量，
	// 避免有人上传一个 1GB 的文件把磁盘写满。
	r.Body = http.MaxBytesReader(w, r.Body, priceImportMaxBytes)
	if err := r.ParseMultipartForm(priceImportMaxBytes); err != nil {
		return "", nil, errors.New("invalid multipart form (or file larger than 4MB)")
	}
	format := normalizePriceImportFormat(r.FormValue("format"))

	file, header, err := r.FormFile("file")
	if err != nil {
		file, header, err = r.FormFile("data")
	}
	if err != nil {
		return "", nil, errors.New("file is required")
	}
	defer func() { _ = file.Close() }()

	if format == "" && header != nil {
		// 按扩展名推断：让"选择文件"这条路径不必额外传 format。
		switch strings.ToLower(filepathExt(header.Filename)) {
		case ".json":
			format = "json"
		case ".csv", ".txt":
			format = "csv"
		}
	}
	raw, err := io.ReadAll(io.LimitReader(file, priceImportMaxBytes+1))
	if err != nil {
		return "", nil, errors.New("failed to read uploaded file")
	}
	if len(raw) > priceImportMaxBytes {
		return "", nil, errors.New("file larger than 4MB")
	}
	if format == "" {
		format = sniffPriceImportFormat(raw)
	}
	if format == "" {
		return "", nil, errors.New("format is required (csv|json)")
	}
	return format, raw, nil
}

// filepathExt 取扩展名（不引 path/filepath：上传文件名可能是任意字符串）。
func filepathExt(name string) string {
	if idx := strings.LastIndexByte(name, '.'); idx >= 0 {
		return name[idx:]
	}
	return ""
}

// normalizePriceImportFormat 归一化 format 参数；非法值返回空串（由推断接手）。
func normalizePriceImportFormat(raw string) string {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "csv":
		return "csv"
	case "json":
		return "json"
	default:
		return ""
	}
}

// sniffPriceImportFormat 按首个非空白字符判断：`[` / `{` 是 JSON，其余按 CSV。
func sniffPriceImportFormat(data []byte) string {
	trimmed := strings.TrimSpace(string(data))
	if trimmed == "" {
		return ""
	}
	if trimmed[0] == '[' || trimmed[0] == '{' {
		return "json"
	}
	return "csv"
}

// parsePriceImport 按格式解析成原始行（未校验）。
func parsePriceImport(format string, data []byte) ([]rawPriceRow, []priceImportSkip, error) {
	switch format {
	case "csv":
		return parsePriceCSV(data)
	case "json":
		return parsePriceJSON(data)
	default:
		return nil, nil, errors.New("format must be csv or json")
	}
}

// priceCSVRequiredColumns 是 CSV 表头里必须出现的列（provider 可选）。
var priceCSVRequiredColumns = []string{"model", "input_per_1k", "output_per_1k", "currency"}

// parsePriceCSV 解析 CSV（表头必须含 model/input_per_1k/output_per_1k/currency）。
//
// 行号按**文件行**报：表头是第 1 行。列数不足 / 表头缺列都是"整批拒绝"的错误
// （结构问题）；行列数不齐是**单行跳过**（数据问题），因为 CSV 里常见末尾多一个逗号。
func parsePriceCSV(data []byte) ([]rawPriceRow, []priceImportSkip, error) {
	// 去掉 UTF-8 BOM：Excel 导出的 CSV 默认带，不处理会让表头列名变成 "\ufeffmodel"，
	// 报错却是"缺少 model 列" —— 用户看不出为什么。
	data = stripBOM(data)
	reader := csv.NewReader(strings.NewReader(string(data)))
	reader.FieldsPerRecord = -1
	reader.TrimLeadingSpace = true

	header, err := reader.Read()
	if err != nil {
		return nil, nil, errors.New("csv is empty or has no header")
	}
	index := make(map[string]int, len(header))
	for i, name := range header {
		key := strings.ToLower(strings.TrimSpace(stripBOMString(name)))
		if key == "" {
			continue
		}
		if _, dup := index[key]; !dup {
			index[key] = i
		}
	}
	for _, required := range priceCSVRequiredColumns {
		if _, ok := index[required]; !ok {
			return nil, nil, fmt.Errorf("csv header missing column %q "+
				"(expected model,input_per_1k,output_per_1k,currency[,provider])", required)
		}
	}

	rows := make([]rawPriceRow, 0, 16)
	skipped := make([]priceImportSkip, 0)
	line := 1 // 已消费表头
	for {
		record, readErr := reader.Read()
		if errors.Is(readErr, io.EOF) {
			break
		}
		line++
		if readErr != nil {
			// 引号不闭合这类解析错误：跳过这一行（csv.Reader 之后会继续可读）。
			skipped = append(skipped, priceImportSkip{Row: line, Reason: "malformed csv row: " + readErr.Error()})
			continue
		}
		if isBlankRecord(record) {
			continue
		}
		if len(record) != len(header) {
			skipped = append(skipped, priceImportSkip{
				Row:    line,
				Reason: fmt.Sprintf("column count mismatch: got %d, want %d", len(record), len(header)),
			})
			continue
		}
		pick := func(key string) string {
			if idx, ok := index[key]; ok && idx < len(record) {
				return record[idx]
			}
			return ""
		}
		rows = append(rows, rawPriceRow{
			Row:       line,
			Model:     pick("model"),
			Provider:  pick("provider"),
			Currency:  pick("currency"),
			InputRaw:  pick("input_per_1k"),
			OutputRaw: pick("output_per_1k"),
		})
	}
	return rows, skipped, nil
}

// jsonPriceEntry 是 JSON 导入的一行（数字用 json.Number 保原文，便于报错）。
type jsonPriceEntry struct {
	Model       string       `json:"model"`
	Provider    string       `json:"provider"`
	Currency    string       `json:"currency"`
	InputPer1K  *json.Number `json:"input_per_1k"`
	OutputPer1K *json.Number `json:"output_per_1k"`
}

// parsePriceJSON 解析 JSON（数组 / {"prices":[...]} / 字符串套 JSON）。
func parsePriceJSON(data []byte) ([]rawPriceRow, []priceImportSkip, error) {
	trimmed := strings.TrimSpace(strings.TrimPrefix(string(stripBOM(data)), "\xef\xbb\xbf"))
	if trimmed == "" {
		return nil, nil, errors.New("json is empty")
	}
	// 允许 data 是一段"字符串形式的 JSON"（把文件内容原样塞进 data 的客户端会这样）
	if strings.HasPrefix(trimmed, `"`) {
		var inner string
		if err := json.Unmarshal([]byte(trimmed), &inner); err != nil {
			return nil, nil, errors.New("invalid json string")
		}
		return parsePriceJSON([]byte(inner))
	}

	var entries []jsonPriceEntry
	if strings.HasPrefix(trimmed, "[") {
		if err := json.Unmarshal([]byte(trimmed), &entries); err != nil {
			return nil, nil, errors.New("invalid json array of price rows")
		}
	} else {
		var wrapper struct {
			Prices []jsonPriceEntry `json:"prices"`
		}
		if err := json.Unmarshal([]byte(trimmed), &wrapper); err != nil {
			return nil, nil, errors.New("invalid json: expected an array or {\"prices\": [...]}")
		}
		if wrapper.Prices == nil {
			return nil, nil, errors.New("invalid json: expected an array or {\"prices\": [...]}")
		}
		entries = wrapper.Prices
	}

	rows := make([]rawPriceRow, 0, len(entries))
	for i, entry := range entries {
		row := rawPriceRow{
			Row:      i + 1,
			Model:    entry.Model,
			Provider: entry.Provider,
			Currency: entry.Currency,
		}
		if entry.InputPer1K != nil {
			row.InputRaw = entry.InputPer1K.String()
		}
		if entry.OutputPer1K != nil {
			row.OutputRaw = entry.OutputPer1K.String()
		}
		rows = append(rows, row)
	}
	return rows, nil, nil
}

// validatePriceImportRows 逐行校验（纯函数，单测钉矩阵）。
//
// `providers` 是"缺 provider 的行"的批量反查结果；查不到的模型保持空 provider
// （表默认值），**不猜厂商**。
func validatePriceImportRows(raw []rawPriceRow, providers map[string]string) ([]priceImportRow, []priceImportSkip) {
	rows := make([]priceImportRow, 0, len(raw))
	skipped := make([]priceImportSkip, 0)
	// 同一批里的重复键：只落第一条。后一条若覆盖前一条，用户会看到"我明明填了新价，
	// 导进去还是旧价"——而 skipped 列表能直接说明原因。
	seen := make(map[string]int, len(raw))

	for _, item := range raw {
		model := strings.TrimSpace(item.Model)
		if model == "" {
			skipped = append(skipped, priceImportSkip{Row: item.Row, Reason: "model is required"})
			continue
		}
		if utf8.RuneCountInString(model) > modelPriceNameMax {
			skipped = append(skipped, priceImportSkip{
				Row: item.Row, Reason: fmt.Sprintf("model name too long (max %d)", modelPriceNameMax),
			})
			continue
		}

		input, ok := parsePriceNumber(item.InputRaw)
		if !ok {
			skipped = append(skipped, priceImportSkip{
				Row: item.Row, Reason: "input_per_1k must be a finite number >= 0",
			})
			continue
		}
		output, ok := parsePriceNumber(item.OutputRaw)
		if !ok {
			skipped = append(skipped, priceImportSkip{
				Row: item.Row, Reason: "output_per_1k must be a finite number >= 0",
			})
			continue
		}
		if input == 0 && output == 0 {
			// 0/0 会被 `usageCostUSD` 判为"不知道"，保存了也不会有任何效果。
			skipped = append(skipped, priceImportSkip{
				Row: item.Row, Reason: "input_per_1k and output_per_1k cannot both be 0",
			})
			continue
		}

		currency := strings.ToUpper(strings.TrimSpace(item.Currency))
		if currency == "" {
			currency = "USD"
		}
		if currency != "USD" {
			// 读侧只认 USD；写别的币种等于写一行永远查不到的价格。
			skipped = append(skipped, priceImportSkip{Row: item.Row, Reason: "only USD is supported"})
			continue
		}

		provider := strings.TrimSpace(item.Provider)
		if provider == "" {
			provider = providers[model]
		}
		if utf8.RuneCountInString(provider) > modelPriceProviderMax {
			skipped = append(skipped, priceImportSkip{
				Row: item.Row, Reason: fmt.Sprintf("provider too long (max %d)", modelPriceProviderMax),
			})
			continue
		}

		key := model + "\x00" + provider
		if first, dup := seen[key]; dup {
			skipped = append(skipped, priceImportSkip{
				Row: item.Row, Reason: fmt.Sprintf("duplicate model/provider (first seen at row %d)", first),
			})
			continue
		}
		seen[key] = item.Row

		rows = append(rows, priceImportRow{
			Model:       model,
			Provider:    provider,
			Currency:    currency,
			InputPer1K:  input,
			OutputPer1K: output,
		})
	}
	return rows, skipped
}

// parsePriceNumber 解析单价文本：必须有限且 >= 0（拒绝 NaN/Inf/负数/空串）。
func parsePriceNumber(raw string) (float64, bool) {
	text := strings.TrimSpace(raw)
	if text == "" {
		return 0, false
	}
	value, err := strconv.ParseFloat(text, 64)
	if err != nil {
		return 0, false
	}
	if !isFiniteNonNegative(value) {
		return 0, false
	}
	return value, true
}

func isBlankRecord(record []string) bool {
	for _, field := range record {
		if strings.TrimSpace(field) != "" {
			return false
		}
	}
	return true
}

// stripBOM 去掉 UTF-8 BOM（Excel 导出的 CSV 默认带）。
func stripBOM(data []byte) []byte {
	return bytes.TrimPrefix(data, []byte("\xef\xbb\xbf"))
}

func stripBOMString(s string) string {
	return strings.TrimPrefix(s, "\xef\xbb\xbf")
}
