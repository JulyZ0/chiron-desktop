// Package settings 提供一个基于 AES-256-GCM 的加密设置存取层。
//
// 功能：
//   - 用部署主密钥（APP_SECRET 派生密钥）对敏感配置（LLM/S3/支付密钥、redis/pg 密码等）
//     做加密后存入 system_settings 表；非敏感配置明文存储。
//   - 提供按分类的 SaveConfig / LoadConfig，供后台「系统设置」与管理端 API 使用。
//   - 新写入使用**信封加密**（`v2:`，见 internal/kms）：每条值一个 DEK，DEK 由可插拔
//     KMS 包裹；v1 密文继续可读，`Rotate` 可把存量迁移并重包到当前 KEK。
package settings

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"

	"github.com/athenavi/chiron/internal/kms"
	"github.com/jackc/pgx/v5/pgxpool"
)

// 密文格式前缀：`v1:` + base64(nonce || ciphertext)；v2 见 internal/kms。
const (
	cipherVersion = "v1:"
	nonceSize     = 12
)

var (
	// ErrEncryptedKeyNotFound APP_SECRET 为空或无法派生密钥时返回。
	ErrEncryptedKeyNotFound = errors.New("settings encryption key unavailable (APP_SECRET not set)")
)

// Store 为 system_settings 表提供带加密的读写。
type Store struct {
	pool *pgxpool.Pool
	aead cipher.AEAD    // v1 兼容解密（APP_SECRET 派生）；nil 表示无 v1 能力
	env  *kms.Envelope  // v2 信封（新写入走这里）；nil 表示 KMS 未配置
}

// New 创建 Store。appSecret 为空时返回一个仅支持非敏感存取的 Store（敏感键写入会报错）。
//
// KMS_KEK_KEYS / KMS_ACTIVE_KEY_ID 存在时用显式 KEK；否则 KEK 由 APP_SECRET 派生
// （keyID `app-secret-v1`）—— 老部署零改动即可写入 v2 信封格式。
func New(pool *pgxpool.Pool, appSecret string) *Store {
	s := &Store{pool: pool, aead: newAEAD(appSecret)}
	if provider, err := kms.FromEnv(appSecret); err == nil {
		s.env = kms.NewEnvelope(provider)
	} else if !errors.Is(err, kms.ErrKeyUnavailable) {
		// 配置写错（比如 base64 非法）必须让运维看见；静默退回 v1 会掩盖轮换配置的错。
		slog.Warn("settings: KMS provider unavailable, falling back to v1 encryption", "error", err)
	}
	return s
}

// EncryptEnabled 报告当前主密钥是否可用（可加密敏感配置）。
func (s *Store) EncryptEnabled() bool { return s.env != nil || s.aead != nil }

// KMSKeyID 返回新写入会使用的 keyID（空 = 未配置 KMS）。
func (s *Store) KMSKeyID() string { return s.env.ActiveKeyID() }

// KMSProviderName 返回提供者名（"local" 等；空 = 未配置）。
func (s *Store) KMSProviderName() string { return s.env.ProviderName() }

// nullableUser 将空 userID 转为 NULL，避免 uuid 列绑定空字符串报错。
func nullableUser(userID string) interface{} {
	if userID == "" {
		return nil
	}
	return userID
}

// newAEAD 从 APP_SECRET 派生 AES-GCM AEAD；秘密为空返回 nil。
func newAEAD(appSecret string) cipher.AEAD {
	if appSecret == "" {
		return nil
	}
	key := sha256.Sum256([]byte("chiron-settings:" + appSecret))
	block, err := aes.NewCipher(key[:])
	if err != nil {
		return nil
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil
	}
	return aead
}

// EncryptString 将明文编码为可落库的密文串。
//
// 新写入一律走 v2 信封；只有 KMS 未配置时才退回 v1（兼容期）。调用方拿到的都是
// "可落库的密文"，不需要知道版本。
func (s *Store) EncryptString(plain string) (string, error) {
	if s.env != nil {
		return s.env.Seal([]byte(plain))
	}
	return s.encryptV1(plain)
}

func (s *Store) encryptV1(plain string) (string, error) {
	if s.aead == nil {
		return "", ErrEncryptedKeyNotFound
	}
	nonce := make([]byte, nonceSize)
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return "", err
	}
	sealed := s.aead.Seal(nil, nonce, []byte(plain), nil)
	raw := append(nonce, sealed...)
	return cipherVersion + base64.StdEncoding.EncodeToString(raw), nil
}

// DecryptString 解密 EncryptString 产生的密文（兼容 v1 / v2）。
func (s *Store) DecryptString(enc string) (string, error) {
	if strings.HasPrefix(enc, kms.VersionV2) {
		if s.env == nil {
			return "", ErrEncryptedKeyNotFound
		}
		plain, err := s.env.Open(enc)
		if err != nil {
			return "", err
		}
		return string(plain), nil
	}
	if s.aead == nil {
		return "", ErrEncryptedKeyNotFound
	}
	raw, err := base64.StdEncoding.DecodeString(strings.TrimPrefix(enc, cipherVersion))
	if err != nil {
		return "", err
	}
	if len(raw) < nonceSize {
		return "", errors.New("invalid ciphertext")
	}
	nonce, sealed := raw[:nonceSize], raw[nonceSize:]
	plain, err := s.aead.Open(nil, nonce, sealed, nil)
	if err != nil {
		return "", err
	}
	return string(plain), nil
}

// RotateReport 是一次轮换的统计。`Failed` 不为 0 时调用方必须告警/返回错误。
type RotateReport struct {
	Scanned int `json:"scanned"`
	// Rewrapped 是 v2 旧 keyID → 当前 keyID（数据密文未动）。
	Rewrapped int `json:"rewrapped"`
	// Migrated 是 v1 → v2（必须解密一次明文；只在内存里）。
	Migrated int `json:"migrated"`
	// Unchanged 已是当前 keyID。
	Unchanged int `json:"unchanged"`
	Failed    int `json:"failed"`
}

// RewrapCipher 把单条密文迁移/重包到当前 KEK，返回 (新密文, 是否有变化, 错误)。
// 已是当前 keyID 时第二返回值为 false，调用方不必写库。
//
// v1 没有独立 DEK，只能"解一次明文 → 重新封"；这一步只在内存里发生。
func (s *Store) RewrapCipher(cipher string) (string, bool, error) {
	if s.env == nil {
		return "", false, ErrEncryptedKeyNotFound
	}
	if kms.KeyID(cipher) == s.env.ActiveKeyID() {
		return cipher, false, nil
	}
	if kms.KeyID(cipher) != "" {
		return s.env.Rewrap(cipher)
	}
	plain, err := s.DecryptString(cipher)
	if err != nil {
		return "", false, err
	}
	next, err := s.env.Seal([]byte(plain))
	if err != nil {
		return "", false, err
	}
	return next, true, nil
}

// Rotate 把 system_settings 里的加密值迁移/重包到当前活跃 KEK。
//
// dryRun 只统计不写库 —— 轮换前先看一眼规模，避免在业务高峰把整表重写。
func (s *Store) Rotate(ctx context.Context, dryRun bool) (RotateReport, error) {
	var rep RotateReport
	if s.pool == nil {
		return rep, errors.New("database unavailable")
	}
	if s.env == nil {
		return rep, ErrEncryptedKeyNotFound
	}
	rows, err := s.pool.Query(ctx,
		`SELECT id, category, key, value::text FROM system_settings WHERE encrypted = true`)
	if err != nil {
		return rep, err
	}
	defer rows.Close()

	type pending struct {
		id   int
		next string
	}
	var updates []pending
	for rows.Next() {
		var id int
		var category, key, raw string
		if err := rows.Scan(&id, &category, &key, &raw); err != nil {
			rep.Failed++
			continue
		}
		rep.Scanned++
		// 落库形态是 `"<cipher>"`（SaveConfig 对密文再做了一次 JSON 编码）。
		cipher := strings.Trim(raw, `"`)
		if cipher == "" {
			rep.Failed++
			slog.Warn("settings rotate: empty ciphertext", "category", category, "key", key)
			continue
		}
		legacy := kms.KeyID(cipher) == ""
		next, changed, err := s.RewrapCipher(cipher)
		if err != nil {
			rep.Failed++
			slog.Warn("settings rotate failed", "category", category, "key", key, "error", err)
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
		updates = append(updates, pending{id: id, next: next})
	}
	if err := rows.Err(); err != nil {
		return rep, err
	}
	if dryRun || len(updates) == 0 {
		return rep, nil
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return rep, err
	}
	defer tx.Rollback(ctx)
	for _, u := range updates {
		valueJSON, err := json.Marshal(u.next)
		if err != nil {
			return rep, err
		}
		if _, err := tx.Exec(ctx,
			`UPDATE system_settings SET value = $2::jsonb, updated_at = NOW() WHERE id = $1`,
			u.id, string(valueJSON)); err != nil {
			return rep, fmt.Errorf("settings rotate update id=%d: %w", u.id, err)
		}
	}
	return rep, tx.Commit(ctx)
}

// IsSensitive 判断某个配置键是否为敏感值（需加密入库）。
// 依据：键名包含 password / secret / private_key / api_key / token / dsn。
func IsSensitive(key string) bool {
	k := strings.ToLower(key)
	for _, needle := range []string{"password", "secret", "private_key", "api_key", "dsn", "token"} {
		if strings.Contains(k, needle) {
			return true
		}
	}
	return false
}

// SaveConfig 将某分类的 config 逐键 upsert 到 system_settings。
//   - val 为 nil：删除该键（回落 env/默认值）
//   - 敏感键且 aead 可用：加密后落库，标记 encrypted=true
func (s *Store) SaveConfig(ctx context.Context, category string, config map[string]interface{}, userID string) error {
	if s.pool == nil {
		return errors.New("database unavailable")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	for key, val := range config {
		if val == nil {
			if _, err := tx.Exec(ctx,
				`DELETE FROM system_settings WHERE category=$1 AND key=$2`,
				category, key); err != nil {
				return err
			}
			continue
		}
		valueJSON, err := json.Marshal(val)
		if err != nil {
			return err
		}
		encrypted := false
		value := string(valueJSON)
		if sensitive := IsSensitive(key); sensitive {
			if s.aead == nil {
				return ErrEncryptedKeyNotFound
			}
			enc, err := s.EncryptString(value)
			if err != nil {
				return err
			}
			// 密文以 JSON 字符串形态入库（value 列为 jsonb，裸串无法强转）
			encJSON, err := json.Marshal(enc)
			if err != nil {
				return err
			}
			value = string(encJSON)
			encrypted = true
		}
		// upsert（先 UPDATE，未命中再 INSERT）——**不依赖任何唯一约束**。
		//
		// 这里曾用 `INSERT ... ON CONFLICT (category, key) DO UPDATE`。但 system_settings
		// 的实际表结构（见 migrations/sql/init.sql 与生产库导出）只有 id 主键，
		// (category, key) 上并没有唯一约束，Postgres 于是直接报
		//   42P10: there is no unique or exclusion constraint matching the ON CONFLICT specification
		// 导致**所有**保存路径（后台「系统设置」、后台「支付配置」、LLM Key 等）全军覆没。
		// 条件插入不要求任何 DDL：应用启动不做 schema 变更（改为由发布流程/DBA 管理），
		// 因此这里不能靠"补一个唯一索引"来修，只能不依赖它。
		//
		// 保留 `$3::jsonb`：value 列历史上是 json（不是 jsonb），裸参数会被推断成 text 而报
		//   column "value" is of type json but expression is of type text
		// 显式 cast 到 jsonb 后由 Postgres 做 jsonb→json 的赋值转换，json / jsonb 两种列都能写。
		//
		// 注意：极端并发（同一键首次写入被两个副本同时插入）仍可能产生两行，
		// 因为不再有唯一约束兜底。若需要强保证，请由发布流程补唯一索引后改回 ON CONFLICT。
		if _, err := tx.Exec(ctx,
			`WITH updated AS (
			     UPDATE system_settings
			        SET value=$3::jsonb, encrypted=$4, updated_by=$5, updated_at=NOW()
			      WHERE category=$1 AND key=$2
			     RETURNING 1
			 )
			 INSERT INTO system_settings (category, key, value, encrypted, updated_by, updated_at)
			 SELECT $1, $2, $3::jsonb, $4, $5, NOW()
			  WHERE NOT EXISTS (SELECT 1 FROM updated)`,
			category, key, value, encrypted, nullableUser(userID)); err != nil {
			return fmt.Errorf("upsert setting %s/%s: %w", category, key, err)
		}
	}
	return tx.Commit(ctx)
}

// LoadConfig 读取某分类全部配置，敏感值解密后返回（面向管理员，需鉴权）。
func (s *Store) LoadConfig(ctx context.Context, category string) (map[string]interface{}, error) {
	out := map[string]interface{}{}
	if s.pool == nil {
		return out, nil
	}
	rows, err := s.pool.Query(ctx,
		`SELECT key, value, encrypted FROM system_settings WHERE category=$1`, category)
	if err != nil {
		return out, err
	}
	defer rows.Close()
	for rows.Next() {
		var key string
		var raw json.RawMessage
		var encrypted bool
		if err := rows.Scan(&key, &raw, &encrypted); err != nil {
			continue
		}
		// 落库形态是 json.Marshal(val) 的结果（敏感键再整体加密一层），因此必须按
		// JSON 反序列化还原。**不能**用"去掉外层引号"来替代反序列化：那会把值内的
		// 转义序列变成字面字符 —— 典型受害者是 PEM 私钥/公钥，读回后变成
		// "-----BEGIN PRIVATE KEY-----\nMIIE..."（反斜杠 + n，而非真实换行），
		// 于是支付渠道客户端构造时 PEM 解析失败、渠道静默不可用。
		encoded := raw
		if encrypted {
			plain, err := s.DecryptString(strings.Trim(string(raw), `"`))
			if err != nil {
				slog.Warn("settings decrypt failed", "category", category, "key", key, "error", err)
				continue // 无法解密则不返回该键（避免把密文当明文展示）
			}
			encoded = json.RawMessage(plain)
		}
		var val interface{}
		if err := json.Unmarshal(encoded, &val); err != nil {
			// 兼容历史上以裸字符串落库的值
			val = strings.Trim(string(encoded), `"`)
		}
		out[key] = val
	}
	return out, nil
}
