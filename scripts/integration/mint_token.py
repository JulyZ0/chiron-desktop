#!/usr/bin/env python3
"""用共享 JWT_SECRET 现签一个 owner JWT（集成测试专用）。

与 Go 侧 `internal/auth.Authenticator.GenerateToken` 的形态**逐字段对齐**
（见 internal/auth/jwt.go 的 Claims）：`uid` / `email` / `role` / `tenant_id` /
`perms` 缺省（nil 时走角色默认权限，owner 即全权限），外加标准 `iss/iat/nbf/exp/jti`。

为什么不复用产品登录：集成环境没有口令（种子账号 password_set=false），
而本地免口令引导要求回环来源（容器 NAT 后不是）。自签是本测试唯一稳定的入口。

用法：
    python scripts/integration/mint_token.py <jwt_secret> [user_id] [email] [tenant_id]
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
import uuid

#: 必须与 migrate_entrypoint.py 的种子账号一致。
DEFAULT_USER_ID = "00000000-0000-0000-0000-0000000000a1"
DEFAULT_EMAIL = "owner@chiron.local"
DEFAULT_TENANT_ID = 1
DEFAULT_ROLE = "owner"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def mint(
    secret: str,
    user_id: str = DEFAULT_USER_ID,
    email: str = DEFAULT_EMAIL,
    *,
    tenant_id: int = DEFAULT_TENANT_ID,
    role: str = DEFAULT_ROLE,
    ttl_seconds: int = 6 * 3600,
    now: int | None = None,
) -> str:
    """返回 HS256 JWT。secret 必须与网关的 JWT_SECRET 逐字一致。"""
    if not secret:
        raise ValueError("jwt secret must not be empty")
    issued = int(time.time() if now is None else now)
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "uid": user_id,
        "email": email,
        "role": role,
        "tenant_id": tenant_id,
        "iss": "chiron",
        "iat": issued,
        "nbf": issued,
        "exp": issued + ttl_seconds,
        "jti": uuid.uuid4().hex,
    }
    signing_input = (
        b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
        + "."
        + b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    )
    signature = hmac.new(
        secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return signing_input + "." + b64url(signature)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: mint_token.py <jwt_secret> [user_id] [email]", file=sys.stderr)
        return 2
    secret = argv[1]
    user_id = argv[2] if len(argv) > 2 else DEFAULT_USER_ID
    email = argv[3] if len(argv) > 3 else DEFAULT_EMAIL
    # 第 4 个参数：租户。集成带用它签第二租户的 token，验证"隐式建会话落对桶"（A3）。
    tenant_id = int(argv[4]) if len(argv) > 4 else DEFAULT_TENANT_ID
    print(mint(secret, user_id, email, tenant_id=tenant_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
