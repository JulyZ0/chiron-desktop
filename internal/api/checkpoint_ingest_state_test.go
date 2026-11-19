package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgtype"
)

// TestIngestCheckpointAcceptsFailureStateWithoutSHAs 固定住"失败态也能上报"这条校验契约。
//
// 缺陷形态：`IngestProjectCheckpoint` 曾无条件要求 commit_sha / tree_sha / ref_name 非空。
// 而检查点失败时这三项**本来就没有**（那正是它失败的原因），于是失败记录被 400 挡回去
// ⇒ 时间线永远只有成功的点。列表端注释明确写着"失败的检查点也要显示，用户需要知道
// 这一刻没能保护成功"，但没有任何代码路径能写进一行失败态。
//
// 判据只看**校验层**的结果：失败态不该被判 400。
// 这里没有真实数据库，所以校验通过后会落到 503（db unavailable）—— 那正是"校验已放行"。
func TestIngestCheckpointAcceptsFailureStateWithoutSHAs(t *testing.T) {
	body := `{
	  "session_id": "s-1",
	  "seq": 3,
	  "kind": "pre_turn",
	  "state": "failed",
	  "error": "write-tree failed: not a git repository",
	  "error_code": "git"
	}`
	req := httptest.NewRequest(http.MethodPost, "/v1/internal/checkpoints", strings.NewReader(body))
	rec := httptest.NewRecorder()

	IngestProjectCheckpoint(rec, req)

	if rec.Code == http.StatusBadRequest {
		t.Fatalf("失败态被校验层挡回 400：%s —— 时间线将永远看不到失败", rec.Body.String())
	}
}

// TestIngestCheckpointRejectsOKStateWithoutSHAs 是对照组：成功态**必须**带 commit/tree/ref。
//
// 否则会落进一行"声称成功、却没有可回滚对象"的记录 —— 用户点回滚才失败，
// 那时现场已经离真因很远了。
func TestIngestCheckpointRejectsOKStateWithoutSHAs(t *testing.T) {
	body := `{"session_id": "s-1", "seq": 3, "kind": "pre_turn", "state": "ok"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/internal/checkpoints", strings.NewReader(body))
	rec := httptest.NewRecorder()

	IngestProjectCheckpoint(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("成功态缺 commit/tree/ref 应当 400，实际 %d：%s", rec.Code, rec.Body.String())
	}
}

// TestIngestCheckpointDefaultsOmittedStateToOK 保证后向兼容：
// 老引擎不发明 `state` 字段，缺省必须当作成功态，而不是"未知态"被拒。
func TestIngestCheckpointDefaultsOmittedStateToOK(t *testing.T) {
	// 缺 state 且缺 sha ⇒ 按成功态判，应当 400（说明缺省确实被当成 ok）。
	body := `{"session_id": "s-1", "seq": 3, "kind": "pre_turn"}`
	req := httptest.NewRequest(http.MethodPost, "/v1/internal/checkpoints", strings.NewReader(body))
	rec := httptest.NewRecorder()

	IngestProjectCheckpoint(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("缺省 state 应等价于 ok（因此缺 sha 要 400），实际 %d", rec.Code)
	}
}

// TestCheckpointTenantIDFromSmallint 固定住"上报的检查点能按租户查出来"这条前提。
//
// 缺陷形态（两个环节拼起来才是完整的 bug）：
//
//  1. `FetchOne` 把行扫进 `*interface{}`，pgx 走 `DecodeValue`，Postgres 的
//     `smallint` 解出来是 **int16**（int4→int32、int8→int64）；
//  2. `intOf` 只认 int / int32 / int64 / float64 / string ⇒ int16 落进 default，
//     返回 0。
//
// 而 `project_checkpoints.tenant_id` 与 `session_map_workspaces.tenant_id` 都是
// smallint，于是上报时租户被写成 **0**：INSERT 成功、日志一片安静，但时间线按真实
// 租户（"1"）查永远查不到 —— 用户看到的就是"检查点从没工作过"。
//
// 这里用 pgtype 直接解码一段 int2 二进制来做**前置证明**，避免"假设驱动"的修复。
func TestCheckpointTenantIDFromSmallint(t *testing.T) {
	m := pgtype.NewMap()
	var decoded any
	// int2 的二进制表示是 2 字节大端；3 => 0x00 0x03
	if err := m.Scan(pgtype.Int2OID, pgtype.BinaryFormatCode, []byte{0x00, 0x03}, &decoded); err != nil {
		t.Fatalf("decode int2: %v", err)
	}
	if _, ok := decoded.(int16); !ok {
		t.Fatalf("前提不成立：pgx 把 int2 解成了 %T（期望 int16）", decoded)
	}
	if got := intOf(decoded); got != 3 {
		t.Fatalf("intOf(smallint 3) = %d，期望 3 —— 租户会被写成 0，时间线恒空", got)
	}
}
