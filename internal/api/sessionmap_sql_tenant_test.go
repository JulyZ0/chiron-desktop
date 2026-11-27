package api

// sessionmap_sql_tenant_test.go —— 钉住「SQL 里的 tenant_id 一律是 smallint」。
//
// 缺陷形态（线上）：`smTenant(claims)` 返回**字符串**（Redis key / 设备通道用），
// 被直接塞进 `tenant_id = $N`（smallint 列）。pgx 找不到 string → int2 的编码计划，
// 整条查询报错；上层把它映射成 404，用户看到「项目文件无法加载：请求的资源不存在」。
//
// 结构体里的 `TenantID` 仍是字符串（热层 JSON 与前端兼容），消费点必须显式转
// int16 —— 这里只锁 SQL 参数，不锁结构体字段。

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/jackc/pgx/v5/pgtype"
)

// 这条不是风格检查，而是根因的可执行证明：字符串无法编码成 int2 参数。
// 线上 `tenant_id = $2` 收到 `smTenant()` 的字符串时，pgx 返回
// "unable to encode ... into binary format for int2"，整条查询失败 → 404。
func TestSmTenantStringIsNotEncodableAsSmallint(t *testing.T) {
	claims := &auth.Claims{TenantID: 1}
	m := pgtype.NewMap()
	if plan := m.PlanEncode(pgtype.Int2OID, pgtype.BinaryFormatCode, smTenant(claims)); plan != nil {
		t.Fatal("smTenant 返回字符串却能被编码成 int2 —— 本用例的前提已不成立，请重新评估 SQL 参数")
	}
	if plan := m.PlanEncode(pgtype.Int2OID, pgtype.BinaryFormatCode, smTenantID(claims)); plan == nil {
		t.Fatal("smTenantID 必须能编码成 int2 —— 否则所有 SQL 租户参数仍然会失败")
	}
}

func TestSessionMapSQLTenantParamsAreSmallint(t *testing.T) {
	cases := map[string][]string{
		"project_files.go": {
			"wsID, smTenantID(claims), claims.UserID",
		},
		"checkpoint_diff.go": {
			"cid, smTenantID(claims), claims.UserID",
		},
		"checkpoint_restore.go": {
			"cid, smTenantID(claims), claims.UserID",
		},
		"device_write_lease.go": {
			"id, smTenantID(claims), claims.UserID",
		},
		"file_refs.go": {
			"sessionID, smTenantID(claims), claims.UserID",
		},
		"project_checkpoints.go": {
			"args := []interface{}{wsID, smTenantID(claims), claims.UserID}",
		},
		"sessionmap.go": {
			"string(viewport), ws.RootPath, ws.SortIndex, tenantID, ws.UserID",
			"wsID, smTenantID(claims), claims.UserID",
		},
		"sessionmap_projects.go": {
			"projectLocatorTaken(r.Context(), smTenantID(claims)",
			"wsID, smTenantID(claims), claims.UserID, name",
		},
	}
	for file, wants := range cases {
		raw, err := os.ReadFile(filepath.Join(".", file))
		if err != nil {
			t.Fatalf("read %s: %v", file, err)
		}
		src := string(raw)
		for _, want := range wants {
			if !strings.Contains(src, want) {
				t.Errorf("%s 缺少 smallint 租户参数 %q（SQL 不能直接传 smTenant 的字符串）", file, want)
			}
		}
	}
}

// 反向断言：这些文件里不允许再把 smTenant(claims) 直接作为 SQL 的租户参数。
// 只检查明确的「与 UserID 成对」形态 —— 设备通道 / Redis key 仍应使用字符串版。
func TestSessionMapSQLDoesNotPassStringTenant(t *testing.T) {
	bad := []string{
		"wsID, smTenant(claims), claims.UserID",
		"cid, smTenant(claims), claims.UserID",
		"id, smTenant(claims), claims.UserID",
		"sessionID, smTenant(claims), claims.UserID",
	}
	files := []string{
		"project_files.go", "checkpoint_diff.go", "checkpoint_restore.go",
		"device_write_lease.go", "file_refs.go", "project_checkpoints.go",
		"sessionmap.go", "sessionmap_projects.go",
	}
	for _, file := range files {
		raw, err := os.ReadFile(filepath.Join(".", file))
		if err != nil {
			t.Fatalf("read %s: %v", file, err)
		}
		src := string(raw)
		for _, pattern := range bad {
			if strings.Contains(src, pattern) {
				t.Errorf("%s 仍把 smTenant 的字符串传给 SQL 租户参数: %q", file, pattern)
			}
		}
	}
}
