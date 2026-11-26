#!/usr/bin/env bash
# Chiron 恢复：把 backup.sh 的产物恢复到**空环境**。
#
# 用法：
#   POSTGRES_DSN='postgresql://...' CHIRON_DATA_DIR=/data \
#   ./scripts/restore.sh /backups/chiron-2026-11-27 --yes
#
# 安全闸门：
# - 必须显式 `--yes`（会覆盖目标库与卷目录）。
# - 建议恢复到**新建的空库**；`pg_restore --clean` 会删同名对象。
# - 恢复前先确认 `APP_SECRET` / `KMS_KEK_KEYS` 与备份时一致，否则密文配置与
#   provider key 解不开（表现为配置为空、模型不可用，而不是报错）。
set -euo pipefail

SRC="${1:-}"
CONFIRM="${2:-}"
if [[ -z "$SRC" || "$CONFIRM" != "--yes" ]]; then
  echo "usage: $0 <backup-dir> --yes" >&2
  exit 2
fi
if [[ -z "${POSTGRES_DSN:-}" ]]; then
  echo "POSTGRES_DSN is required" >&2
  exit 2
fi
if [[ ! -f "$SRC/db.dump" ]]; then
  echo "missing $SRC/db.dump" >&2
  exit 2
fi

DATA_DIR="${CHIRON_DATA_DIR:-${STORAGE_ROOT:-}}"
PG_RESTORE_BIN="${PG_RESTORE_BIN:-pg_restore}"

echo "==> verify checksums"
( cd "$SRC" && (sha256sum -c SHA256SUMS 2>/dev/null || shasum -a 256 -c SHA256SUMS) )

echo "==> pg_restore -> database"
"$PG_RESTORE_BIN" --clean --if-exists --no-owner --no-privileges \
  --dbname="$POSTGRES_DSN" "$SRC/db.dump"

if [[ -f "$SRC/volumes.tgz" ]]; then
  if [[ -z "$DATA_DIR" ]]; then
    echo "CHIRON_DATA_DIR/STORAGE_ROOT is required to restore volumes" >&2
    exit 2
  fi
  mkdir -p "$DATA_DIR"
  echo "==> volumes -> $DATA_DIR"
  tar -xzf "$SRC/volumes.tgz" -C "$DATA_DIR"
fi

echo "==> done."
echo "下一步："
echo "  1) 确认 APP_SECRET / KMS_KEK_KEYS 与备份时一致"
echo "  2) 启动网关（迁移门禁会校验 alembic head；不一致时先跑 alembic upgrade head）"
echo "  3) curl -fsS https://<域名>/ready 与 python scripts/team_smoke.py --sse 验收"
