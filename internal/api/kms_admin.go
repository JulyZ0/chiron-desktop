package api

import (
	"context"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/kms"
	"github.com/athenavi/chiron/internal/settings"
)

// H20：Provider 密钥的信封加密轮换（管理端）。
//
// 为什么需要这个端点：KEK 轮换不能靠"重启时顺手做" —— 重启会让所有实例同时扫全表，
// 而且没有地方观察规模/失败。这里把三件事分开：
//   1. `GET /v1/admin/kms/status` 看当前 keyID 与待迁移规模（只读）；
//   2. `POST .../rotate {dry_run:true}` 先算影响面（不写库）；
//   3. `POST .../rotate` 真正重包，返回每张表的统计。
//
// 轮换只重包 DEK，**不重新加密数据**：v2 的密文主体保持原样，所以不存在"读出来再
// 写回去"的长事务；v1 存量会各解一次明文并升级为 v2（这条路径无法避免解密）。

type kmsStatusResponse struct {
	Provider          string `json:"provider"`
	ActiveKeyID       string `json:"active_key_id"`
	EncryptionEnabled bool   `json:"encryption_enabled"`
	SettingsEncrypted int    `json:"settings_encrypted"`
	ProviderKeys      int    `json:"provider_keys"`
	Hint              string `json:"hint,omitempty"`
}

func (h *AdminHandler) KMSStatus(w http.ResponseWriter, r *http.Request) {
	if db.Pool == nil {
		InternalError(w, "database unavailable")
		return
	}
	store := h.ensureSettingsStore()
	if store == nil {
		InternalError(w, "settings store unavailable")
		return
	}
	resp := kmsStatusResponse{
		EncryptionEnabled: store.EncryptEnabled(),
		Provider:          store.KMSProviderName(),
		ActiveKeyID:       store.KMSKeyID(),
	}
	if err := db.Pool.QueryRow(r.Context(),
		`SELECT COUNT(*) FROM system_settings WHERE encrypted = true`).Scan(&resp.SettingsEncrypted); err != nil {
		slog.Warn("kms status: count settings failed", "error", err)
	}
	if err := db.Pool.QueryRow(r.Context(),
		`SELECT COUNT(*) FROM llm_provider_keys`).Scan(&resp.ProviderKeys); err != nil {
		slog.Warn("kms status: count provider keys failed", "error", err)
	}
	if resp.ActiveKeyID == "" {
		resp.Hint = "KMS 未配置：请设置 APP_SECRET，或 KMS_KEK_KEYS + KMS_ACTIVE_KEY_ID"
	}
	OK(w, resp)
}

func (h *AdminHandler) RotateKMS(w http.ResponseWriter, r *http.Request) {
	if db.Pool == nil {
		InternalError(w, "database unavailable")
		return
	}
	store := h.ensureSettingsStore()
	if store == nil {
		InternalError(w, "settings store unavailable")
		return
	}
	if store.KMSKeyID() == "" {
		// 没有活跃 KEK 时"轮换"只会把密文迁移到未知密钥上，必须直接拒绝。
		BadRequest(w, "kms is not configured (set APP_SECRET or KMS_KEK_KEYS/KMS_ACTIVE_KEY_ID)")
		return
	}
	var body struct {
		DryRun bool `json:"dry_run"`
	}
	// 空 body 等价于 dry_run=false：显式 POST 即视为要执行。
	if r.ContentLength > 0 {
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, ErrInvalidReq)
			return
		}
	}
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()

	settingsRep, err := store.Rotate(ctx, body.DryRun)
	if err != nil {
		slog.Error("kms rotate: system_settings failed", "error", err)
		InternalError(w, "rotate system_settings failed")
		return
	}
	keyRep, providers, err := rotateProviderKeys(ctx, store, body.DryRun)
	if err != nil {
		slog.Error("kms rotate: llm_provider_keys failed", "error", err)
		InternalError(w, "rotate llm_provider_keys failed")
		return
	}

	resynced := 0
	if !body.DryRun && keyRep.Migrated+keyRep.Rewrapped > 0 {
		// Redis 镜像里放的是密文（见 syncProviderKeyset），重包后必须整体替换，
		// 否则引擎会继续用旧 keyID 的密文去解 —— 只要旧 KEK 还在就"碰巧能用"，
		// 一旦运维删掉旧 KEK 就全量失败。
		for provider := range providers {
			if err := syncProviderKeyset(ctx, provider); err != nil {
				slog.Warn("kms rotate: keyset resync failed", "provider", provider, "error", err)
				continue
			}
			resynced++
		}
	}

	OK(w, map[string]interface{}{
		"dry_run":         body.DryRun,
		"active_key_id":   store.KMSKeyID(),
		"system_settings": settingsRep,
		"provider_keys":   keyRep,
		"keysets_resynced": resynced,
	})
}

// rotateProviderKeys 重包 `llm_provider_keys.encrypted_key`。
//
// 独立于 settings.Rotate：表结构不同（没有 encrypted 标记、且 ID 是 varchar），
// 但"v1 迁移 + v2 重包"的判定复用 Store.RewrapCipher，避免两处实现漂移。
func rotateProviderKeys(
	ctx context.Context,
	store *settings.Store,
	dryRun bool,
) (settings.RotateReport, map[string]struct{}, error) {
	var rep settings.RotateReport
	providers := map[string]struct{}{}
	rows, err := db.Pool.Query(ctx, `SELECT id, provider, encrypted_key FROM llm_provider_keys`)
	if err != nil {
		return rep, providers, err
	}
	defer rows.Close()

	type pending struct {
		id   string
		next string
	}
	var updates []pending
	for rows.Next() {
		var id, provider, cipher string
		if err := rows.Scan(&id, &provider, &cipher); err != nil {
			rep.Failed++
			continue
		}
		rep.Scanned++
		legacy := !strings.HasPrefix(cipher, "v1:") && !kms.IsSealed(cipher)
		next, changed, err := store.RewrapCipher(cipher)
		if err != nil {
			rep.Failed++
			slog.Warn("kms rotate: provider key rewrap failed", "provider", provider, "error", err)
			continue
		}
		if !changed {
			rep.Unchanged++
			continue
		}
		if legacy {
			rep.Migrated++
		} else {
			rep.Rewrapped++
		}
		providers[provider] = struct{}{}
		updates = append(updates, pending{id: id, next: next})
	}
	if err := rows.Err(); err != nil {
		return rep, providers, err
	}
	if dryRun || len(updates) == 0 {
		return rep, providers, nil
	}
	tx, err := db.Pool.Begin(ctx)
	if err != nil {
		return rep, providers, err
	}
	defer tx.Rollback(ctx)
	for _, u := range updates {
		if _, err := tx.Exec(ctx,
			`UPDATE llm_provider_keys SET encrypted_key = $2, updated_at = NOW() WHERE id = $1`,
			u.id, u.next); err != nil {
			return rep, providers, err
		}
	}
	return rep, providers, tx.Commit(ctx)
}
