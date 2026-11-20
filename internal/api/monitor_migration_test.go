package api

// monitor_migration_test.go —— 迁移链的静态校验（不连库）。
//
// 为什么能在 Go 里测 Python 迁移：alembic 在这里只是一组**文本契约** ——
// revision/down_revision 必须构成单链、0003 必须建出网关实际读写的列。
// 破坏这两点的代价是部署时 `alembic upgrade head` 直接失败（或更糟：起来了
// 但表缺列，rollup 写入一直报错而没人发现）。CI 里跑不起 PG，静态校验是
// 能做的最有价值的那一层。
//
// 只断言"网关用到的列都在"和"链无分叉"，不去比对 DDL 逐字 —— 后者会让
// 任何格式调整都变红，反而促使人改测试而不是看迁移。

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/db"
)

// migrationsDir 走与生产同一条解析（config/resources.go）：以前写死
// `../../migrations/versions`，把"测试恰好从 internal/api 运行"当成了前提。
var migrationsDir = db.MigrationsDir()

var (
	migRevisionRe = regexp.MustCompile(`(?m)^revision:\s*str\s*=\s*"([^"]+)"`)
	migDownRe     = regexp.MustCompile(`(?m)^down_revision:[^=\n]*=\s*(?:"([^"]+)"|None)`)
)

// migrationFiles 读出所有迁移文件的 revision / down_revision / 正文。
func migrationFiles(t *testing.T) map[string]struct {
	down string
	body string
} {
	t.Helper()
	files, err := filepath.Glob(filepath.Join(migrationsDir, "*.py"))
	if err != nil || len(files) == 0 {
		t.Fatalf("找不到迁移文件（目录 %s）: %v", migrationsDir, err)
	}
	out := make(map[string]struct {
		down string
		body string
	}, len(files))
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			t.Fatalf("读迁移 %s 失败: %v", f, err)
		}
		body := string(raw)
		rev := migRevisionRe.FindStringSubmatch(body)
		if rev == nil {
			t.Fatalf("%s 缺少 revision 声明", f)
		}
		downRef := migDownRe.FindStringSubmatch(body)
		down := ""
		if downRef != nil {
			down = downRef[1] // None 时为空
		}
		out[rev[1]] = struct {
			down string
			body string
		}{down: down, body: body}
	}
	return out
}

func TestMonitorMigrationChainSingleHead(t *testing.T) {
	migs := migrationFiles(t)

	downs := make(map[string]bool, len(migs))
	for _, m := range migs {
		if m.down != "" {
			if _, ok := migs[m.down]; !ok {
				t.Fatalf("down_revision 指向不存在的 revision: %q", m.down)
			}
			downs[m.down] = true
		}
	}
	heads := make([]string, 0, 1)
	for rev := range migs {
		if !downs[rev] {
			heads = append(heads, rev)
		}
	}
	if len(heads) != 1 {
		t.Fatalf("迁移链有 %d 个 head（应为 1）: %v", len(heads), heads)
	}
	// 单链：0005 → 0006_price_backfill → 0007_metrics_rollup_instance。
	if heads[0] != "0007_metrics_rollup_instance" {
		t.Fatalf("链头应为 0007_metrics_rollup_instance，实际 %q", heads[0])
	}
	if migs["0006_price_backfill"].down != "0005_share_ops" {
		t.Fatalf("0006_price_backfill 的 down_revision 应为 0005_share_ops，实际 %q", migs["0006_price_backfill"].down)
	}
	}
	if migs["0003_metrics_rollup"].down != "0002_inapp_notifications" {
		t.Fatalf("0003 的 down_revision 被改动了: %q", migs["0003_metrics_rollup"].down)
	}
	if migs["0007_metrics_rollup_instance"].down != "0006_price_backfill" {
		t.Fatalf("0007 必须接在 0006_price_backfill 之后，实际 %q", migs["0007_metrics_rollup_instance"].down)
	}
}

func TestMonitorMigrationRollupTableShape(t *testing.T) {
	migs := migrationFiles(t)
	m, ok := migs["0003_metrics_rollup"]
	if !ok {
		t.Fatalf("缺少 0003_metrics_rollup 迁移")
	}
	body := m.body
	// upgrade 必须建出网关读写的表与列。
	required := []string{
		"CREATE TABLE public.metrics_rollup_minute",
		"ts_minute timestamp with time zone NOT NULL",
		"route_class text NOT NULL",
		"count bigint NOT NULL",
		"errors bigint NOT NULL",
		"p95_ms double precision",
		"PRIMARY KEY (ts_minute, route_class)",
	}
	for _, frag := range required {
		if !strings.Contains(body, frag) {
			t.Fatalf("0003 upgrade 缺少 %q", frag)
		}
	}
	// downgrade 必须能回滚（可重复执行用 IF EXISTS）。
	if !strings.Contains(body, "DROP TABLE IF EXISTS public.metrics_rollup_minute") {
		t.Fatalf("0003 downgrade 必须 DROP TABLE IF EXISTS metrics_rollup_minute")
	}
	// revision 标识本身不能被改动 —— 后续迁移依赖它。
	if !strings.Contains(body, `revision: str = "0003_metrics_rollup"`) {
		t.Fatalf("0003 的 revision 标识被改动了")
	}
}

// TestMonitorMigrationRollupInstanceColumn 钉住 0006：instance 列存在、默认空串
// （存量行不必回填），且 downgrade 可重复执行。
//
// 少这一列时 rollup 写入会在运行期报 "column instance does not exist" ——
// 表现为监控历史永远为空，而错误只出现在日志里。
func TestMonitorMigrationRollupInstanceColumn(t *testing.T) {
	migs := migrationFiles(t)
	m, ok := migs["0006_metrics_rollup_instance"]
	if !ok {
		t.Fatalf("缺少 0006_metrics_rollup_instance 迁移")
	}
	body := m.body
	for _, frag := range []string{
		"ALTER TABLE public.metrics_rollup_minute",
		"ADD COLUMN IF NOT EXISTS instance text NOT NULL DEFAULT ''",
	} {
		if !strings.Contains(body, frag) {
			t.Fatalf("0006 upgrade 缺少 %q", frag)
		}
	}
	if !strings.Contains(body, "DROP COLUMN IF EXISTS instance") {
		t.Fatalf("0006 downgrade 必须可重复执行地删列")
	}
	if !strings.Contains(body, `revision: str = "0006_metrics_rollup_instance"`) {
		t.Fatalf("0006 的 revision 标识被改动了")
	}
}
