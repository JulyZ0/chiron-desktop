package api

// 历史 provider 回填：POST /v1/admin/billing/backfill-provider
//
// **它补的是哪一段**：`model_prices` 早先被手工/导入写入时 `provider` 可能是空串
// （表默认值 `''`）。空 provider 不影响取价（`fetchPrice` 不按 provider 过滤），
// 但价格表按 `(provider, model)` 排序展示、"哪个厂商的模型缺价"的统计都要它 ——
// 一堆空 provider 会把「deepseek 的模型缺价」归到「未知厂商」。
//
// ## 回填规则（只有一条，见 `billing_price_provider_map.go`）
//
// **只按 `llm_models`（name / id）映射**；同名模型对应多个厂商时不映射；
// 目录里没有就保持空。**禁止按模型名猜厂商**。
//
// ## 为什么先 dry_run
//
// 这是**批量改写历史行**的操作。默认 `dry_run: true`：返回将影响的行数与最多 10 条
// 示例，让人先看清「哪些行会被改成什么」。只有显式 `dry_run: false` 才真的 UPDATE。
// 与检查点 GC（`POST /v1/admin/checkpoints/gc`）同一默认口径。
//
// ## 冲突与幂等
//
// 主键是 `(model, provider)`：若 `(model, 目标 provider)` 已经有行，回填这一行会撞
// 主键。`planProviderBackfill` 提前排除这些行 —— 它们**跳过**而不是让整条 UPDATE 失败。
// 重复执行是安全的：第二次已经没有空 provider 行可改。
//
// ## 为什么计划在 Go 里算
//
// 预览（dry_run）与执行必须**用同一份计划**，否则「预览说 3 行、实际改 5 行」永远
// 解释不清。SQL 只负责"读出全部价格行与目录行 / 按主键谓词执行更新"，判定逻辑集中
// 在纯函数里，单测可以直接喂假目录行验证歧义与冲突。

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"

	"github.com/jackc/pgx/v5"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// providerBackfillSampleLimit 是 dry_run 返回的示例条数上限（够看出规律，又不刷屏）。
const providerBackfillSampleLimit = 10

// providerBackfillStore 是回填端点对存储的全部依赖（单测用假 DB 替换）。
type providerBackfillStore interface {
	// LoadRows 返回**全部**价格行与模型目录行（映射与冲突判定在 Go 里做）。
	LoadRows(ctx context.Context) (priceRows []modelProviderRow, catalog []modelProviderRow, err error)
	// ApplyAssignments 在一个事务里执行计划；返回实际更新行数。
	ApplyAssignments(ctx context.Context, plan []providerBackfillSample) (int64, error)
}

// dbProviderBackfillStore 是 providerBackfillStore 的 PG 实现。
type dbProviderBackfillStore struct{}

// LoadRows 读出价格表与目录的 (model, provider) 两列。
//
// 两条独立查询而不是一个大 JOIN：目录行要用于"别名 → 厂商"映射（一个模型名可能
// 对多行），JOIN 会把它们压成一行或多行重复；分开读让映射规则留在纯函数里。
func (dbProviderBackfillStore) LoadRows(ctx context.Context) ([]modelProviderRow, []modelProviderRow, error) {
	if db.GlobalDBManager == nil {
		return nil, nil, errors.New("price store unavailable")
	}
	priceRowsRaw, err := db.GlobalDBManager.FetchAll(ctx, `
		SELECT model, COALESCE(provider, '') AS provider
		  FROM model_prices`)
	if err != nil {
		return nil, nil, err
	}
	catalogRaw, err := db.GlobalDBManager.FetchAll(ctx, `
		SELECT btrim(name)     AS model, btrim(provider) AS provider
		  FROM llm_models
		 WHERE name IS NOT NULL AND btrim(name) <> ''
		   AND provider IS NOT NULL AND btrim(provider) <> ''
		UNION ALL
		SELECT btrim(id)       AS model, btrim(provider) AS provider
		  FROM llm_models
		 WHERE id IS NOT NULL AND btrim(id) <> ''
		   AND provider IS NOT NULL AND btrim(provider) <> ''`)
	if err != nil {
		return nil, nil, err
	}
	return toModelProviderRows(priceRowsRaw), toModelProviderRows(catalogRaw), nil
}

// ApplyAssignments 逐行执行计划（单事务）。
//
// 每行都带 `provider = ”` 与 `NOT EXISTS` 谓词：即使两次回填并发、或期间有人手工
// 改了同一行，也只会更新"目标仍然为空且目标键未被占用"的行 —— 幂等且不会撞主键。
func (dbProviderBackfillStore) ApplyAssignments(ctx context.Context, plan []providerBackfillSample) (int64, error) {
	if len(plan) == 0 {
		return 0, nil
	}
	if db.GlobalDBManager == nil {
		return 0, errors.New("price store unavailable")
	}
	var applied int64
	err := db.GlobalDBManager.WithTransaction(ctx, func(tx pgx.Tx) error {
		for _, item := range plan {
			tag, execErr := tx.Exec(ctx, `
				UPDATE model_prices
				   SET provider = $2, updated_at = now()
				 WHERE model = $1
				   AND (provider IS NULL OR btrim(provider) = '')
				   AND NOT EXISTS (
				           SELECT 1 FROM model_prices q
				            WHERE q.model = $1 AND q.provider = $2
				       )`, item.Model, item.Provider)
			if execErr != nil {
				return execErr
			}
			applied += tag.RowsAffected()
		}
		return nil
	})
	if err != nil {
		// 计划整体回滚：宁可这一次一行都不改，也不要留下"改了一半"的历史行。
		return 0, err
	}
	return applied, nil
}

// toModelProviderRows 把查询结果折成 (model, provider) 行。
func toModelProviderRows(rows []map[string]any) []modelProviderRow {
	out := make([]modelProviderRow, 0, len(rows))
	for _, row := range rows {
		out = append(out, modelProviderRow{
			Model:    stringOf(row["model"]),
			Provider: stringOf(row["provider"]),
		})
	}
	return out
}

// BackfillModelPriceProviders 回填历史价格行的 provider（平台 owner 写权限）。
func BackfillModelPriceProviders(store providerBackfillStore) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		claims := auth.GetClaims(r.Context())
		if claims == nil {
			Unauthorized(w, ErrAuthRequired)
			return
		}
		if store == nil {
			ServiceUnavailable(w, "price store unavailable")
			return
		}

		dryRun, err := parseBackfillDryRun(r)
		if err != nil {
			BadRequest(w, "invalid request body (expected {\"dry_run\": true|false})")
			return
		}

		priceRows, catalog, err := store.LoadRows(r.Context())
		if err != nil {
			slog.Warn("model price backfill: load rows failed", "user", claims.UserID, "error", err)
			ServiceUnavailable(w, "backfill preview failed")
			return
		}
		plan := planProviderBackfill(priceRows, catalog)
		samples := plan
		if len(samples) > providerBackfillSampleLimit {
			samples = samples[:providerBackfillSampleLimit]
		}

		if dryRun {
			OK(w, map[string]any{
				"dry_run":     true,
				"will_update": len(plan),
				"samples":     samples,
				"applied":     0,
				"rule":        providerBackfillRule,
			})
			return
		}

		applied, err := store.ApplyAssignments(r.Context(), plan)
		if err != nil {
			slog.Warn("model price backfill: apply failed", "user", claims.UserID,
				"planned", len(plan), "error", err)
			ServiceUnavailable(w, "backfill failed (rolled back); nothing was written")
			return
		}
		slog.Info("model price backfill: applied", "applied", applied,
			"planned", len(plan), "user", claims.UserID)
		OK(w, map[string]any{
			"dry_run":     false,
			"will_update": len(plan),
			"applied":     applied,
			"samples":     samples,
			"rule":        providerBackfillRule,
		})
	}
}

// providerBackfillRule 随响应回传：界面不必各写一份说明，也不会说错。
const providerBackfillRule = "仅按 llm_models(name/id) 映射 provider；无目录行或有歧义时保持为空，不按名字猜厂商。"

// parseBackfillDryRun 读 `{dry_run}`；**请求体为空时按 true 处理**（安全默认）。
//
// 只在 body 为空或显式给了合法布尔值时才通过：`{"dry_run":"yes"}` 这类拼错不能
// 静默当成 false（那会直接改库）。
func parseBackfillDryRun(r *http.Request) (bool, error) {
	decoder := json.NewDecoder(http.MaxBytesReader(nil, r.Body, 4<<10))
	decoder.DisallowUnknownFields()
	var body struct {
		DryRun *bool `json:"dry_run"`
	}
	if err := decoder.Decode(&body); err != nil {
		if errors.Is(err, io.EOF) {
			// 空 body：默认 dry_run
			return true, nil
		}
		return false, err
	}
	if body.DryRun == nil {
		return true, nil
	}
	return *body.DryRun, nil
}
