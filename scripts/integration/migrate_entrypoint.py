#!/usr/bin/env python3
"""集成测试的一次性迁移入口：alembic upgrade head + 种子 owner 账号。

为什么种子账号要在这里做，而不是让脚本走 `POST /v1/auth/session`（本地引导）：

* 本地引导端点的第二道闸门是 **loopback 来源**（internal/api/auth_local_session.go
  的 isLoopbackPeer，只看 RemoteAddr）。集成脚本在宿主机上跑，容器看到的源地址是
  docker NAT 后的网桥地址，不是 127.0.0.1 —— 走那条路必然 403。
* 用共享 JWT_SECRET 自签 owner JWT（scripts/integration/mint_token.py）就足够：
  鉴权中间件只校验签名与 claims；`users` 行则被会话/项目的外键需要，从这里补上。

种子是幂等的：重复 `up` 同一套 compose 不会因为主键冲突失败。
"""

from __future__ import annotations

import os
import subprocess
import sys

import psycopg2

#: 与 run.sh / mint_token.py 共用的固定 owner id（36 字符，符合 varchar(36)）。
OWNER_ID = "00000000-0000-0000-0000-0000000000a1"
OWNER_EMAIL = "owner@chiron.local"
OWNER_NAME = "Integration Owner"
TENANT_ID = 1


def run_migrations() -> None:
    print("[migrate] alembic upgrade head", flush=True)
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        check=True,
    )


def seed() -> None:
    dsn = os.environ.get("DATABASE_DSN", "")
    if not dsn:
        raise SystemExit("[migrate] DATABASE_DSN is required")
    print("[migrate] seed tenant/owner", flush=True)
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        # tenants 是 users/sessions 等表的外键目标；网关启动时也会幂等补一次，
        # 这里先插入是为了让下面的 users 行不依赖启动顺序。
        cur.execute(
            "INSERT INTO tenants (id, name) VALUES (%s, 'default') "
            "ON CONFLICT (id) DO NOTHING",
            (TENANT_ID,),
        )
        cur.execute(
            """
            INSERT INTO users (id, tenant_id, email, name, role, password_set,
                               settings, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 'owner', false, '{}'::json, NOW(), NOW())
            ON CONFLICT (id) DO UPDATE
               SET role = 'owner',
                   tenant_id = EXCLUDED.tenant_id
            """,
            (OWNER_ID, TENANT_ID, OWNER_EMAIL, OWNER_NAME),
        )
    print("[migrate] done", flush=True)


if __name__ == "__main__":
    run_migrations()
    seed()
