#!/usr/bin/env bash
# Chiron 备份：数据库 + 运行目录 + 密钥材料清点。
#
# 用法：
#   POSTGRES_DSN='postgresql://...' \
#   CHIRON_DATA_DIR=/data \
#   ./scripts/backup.sh /backups/chiron-$(date +%F)
#
# 说明：
# - 数据库用 `pg_dump -Fc`（可被 restore.sh 以 pg_restore 恢复）。
# - 卷目录（项目文件 / 检查点 / 知识库原文）直接 tar；**先停写**更一致：
#   维护窗口内停网关/引擎，或至少停新提交（本脚本不替你做停机决策）。
# - `APP_SECRET` / `KMS_KEK_KEYS` **不写进备份包**：它们应当由 Vault/KMS/密钥托管
#   单独保存。丢失它们时，备份里的密文配置与 llm_provider_keys 不可解。
# - 备份包内含用户内容与密文凭据，权限按 0600 处理，并应加密后离机保存。
set -euo pipefail

DEST="${1:-}"
if [[ -z "$DEST" ]]; then
  echo "usage: $0 <backup-dir>" >&2
  exit 2
fi
if [[ -z "${POSTGRES_DSN:-}" ]]; then
  echo "POSTGRES_DSN is required" >&2
  exit 2
fi

DATA_DIR="${CHIRON_DATA_DIR:-${STORAGE_ROOT:-}}"
PG_DUMP_BIN="${PG_DUMP_BIN:-pg_dump}"
mkdir -p "$DEST"
chmod 700 "$DEST"

echo "==> pg_dump -> $DEST/db.dump"
"$PG_DUMP_BIN" --format=custom --no-owner --no-privileges \
  --file="$DEST/db.dump" "$POSTGRES_DSN"

if [[ -n "$DATA_DIR" && -d "$DATA_DIR" ]]; then
  echo "==> volumes <- $DATA_DIR"
  # 只打包运行数据；`local/` 里的嵌入式 PG 数据目录不进包（用 db.dump 恢复，
  # 直接拷 PGDATA 在跨版本升级时不可靠）。
  ( cd "$DATA_DIR" && tar --exclude='local/pgdata' --exclude='local/redis' \
      -czf "$DEST/volumes.tgz" . )
else
  echo "WARN: CHIRON_DATA_DIR/STORAGE_ROOT 未设置或不存在，跳过卷备份" >&2
fi

{
  echo "created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "host=$(hostname)"
  echo "postgres_dsn_scheme=${POSTGRES_DSN%%://*}"
  echo "data_dir=$DATA_DIR"
  echo "git=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "note=APP_SECRET/KMS_KEK_KEYS are NOT included; escrow them separately"
} > "$DEST/manifest.txt"

( cd "$DEST" && sha256sum db.dump volumes.tgz manifest.txt > SHA256SUMS 2>/dev/null || \
  shasum -a 256 db.dump volumes.tgz manifest.txt > SHA256SUMS )

echo "==> done: $DEST"
echo "    恢复见 docs/ops-runbook.md（先演练，再上线）"
