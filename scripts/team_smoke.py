#!/usr/bin/env python
"""团队服务模式冒烟：账密登录 + 建号 + 重置 + 禁用 + 强制下线 + 软删除。

只依赖标准库，适合在部署主机或任意能访问域名的机器上跑：

    set CHIRON_OWNER_PASSWORD=你的密码
    python scripts/team_smoke.py --base-url https://chiron.example.com --email owner@company.com

安全约定：
* 密码只从环境变量读取，不放进命令行参数（避免进 shell 历史/进程列表）；
* 不打印任何密码/token；
* 结束时删除本次创建的测试账号（软删除）。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import secrets
import sys
import urllib.error
import urllib.request


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.status, _json(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _json(exc.read())


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode() or "{}")
        return value if isinstance(value, dict) else {"data": value}
    except Exception:
        return {}


def expect(step: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {step}")
        return
    print(f"  FAIL {step} {detail}")
    raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True, help="例如 https://chiron.example.com")
    parser.add_argument("--email", required=True, help="owner 邮箱或用户名")
    parser.add_argument("--admin", action="store_true", help="用 admin 账号跑（默认 owner）")
    args = parser.parse_args()

    password = os.environ.get("CHIRON_OWNER_PASSWORD", "")
    if not password:
        print("缺少 CHIRON_OWNER_PASSWORD 环境变量", file=sys.stderr)
        return 2

    client = Client(args.base_url)
    suffix = secrets.token_hex(4)
    email = f"smoke-{suffix}@invalid.local"
    username = f"smoke{suffix}"

    print(f"base = {args.base_url}")
    code, body = client.call(
        "POST", "/v1/auth/login", {"identifier": args.email, "email": args.email, "password": password}
    )
    expect("owner 登录", code == 200 and body.get("success") is True, f"HTTP {code}")

    code, body = client.call("GET", "/v1/admin/users?page=1&per_page=5")
    expect("读取用户列表", code == 200, f"HTTP {code}")

    code, body = client.call(
        "POST",
        "/v1/admin/users",
        {"email": email, "username": username, "nickname": "Smoke Test", "role": "user"},
    )
    new_user = (body.get("data") or {}).get("user") or {}
    temp = (body.get("data") or {}).get("initial_password") or ""
    expect("创建用户", code == 201 or code == 200, f"HTTP {code}")
    expect("返回一次性初始密码", bool(temp), "empty password")
    uid = new_user.get("id") or ""
    expect("返回用户 id", bool(uid), "missing id")

    # 新账号首登必须被要求改密：先确认登录成功且 mcp=true，再访问业务接口应被 403 拦。
    member = Client(args.base_url)
    code, body = member.call(
        "POST", "/v1/auth/login", {"identifier": username, "email": email, "password": temp}
    )
    user = (body.get("data") or {}).get("user") or {}
    expect("新用户登录", code == 200, f"HTTP {code}")
    expect("首登标记 must_change_password", user.get("must_change_password") is True, str(user))
    code, _ = member.call("GET", "/v1/conversations?page=1&per_page=1")
    expect("未改密访问业务接口被拦", code == 403, f"HTTP {code}")

    code, body = member.call(
        "POST", "/v1/auth/password", {"old_password": temp, "new_password": f"Smoke!{suffix}Aa1"}
    )
    expect("首次改密", code == 200, f"HTTP {code}")
    code, _ = member.call("GET", "/v1/conversations?page=1&per_page=1")
    expect("改密后业务接口可用", code == 200, f"HTTP {code}")

    # ── 邀请闭环：建号 → 一次性邀请 → 受邀人自设密码 → 登录可用 ──
    invitee_email = f"smoke-inv-{suffix}@invalid.local"
    code, body = client.call(
        "POST",
        "/v1/admin/users",
        {"email": invitee_email, "username": f"inv{suffix}", "nickname": "Smoke Invite", "role": "user"},
    )
    invitee = (body.get("data") or {}).get("user") or {}
    invitee_id = invitee.get("id") or ""
    expect("创建受邀用户", (code == 201 or code == 200) and bool(invitee_id), f"HTTP {code}")

    code, body = client.call("POST", f"/v1/admin/users/{invitee_id}/invite")
    data = body.get("data") or {}
    token = data.get("token") or ""
    expect("生成一次性邀请", code == 200 and bool(token), f"HTTP {code}")
    expect(
        "邀请链接带 token",
        (not data.get("invite_url")) or (token in data["invite_url"]),
        str(data.get("invite_url")),
    )

    invite_password = f"Invite!{suffix}Aa1"
    guest = Client(args.base_url)
    code, _ = guest.call("POST", "/v1/auth/invite/accept", {"token": token, "password": invite_password})
    expect("受邀人设置密码", code == 200, f"HTTP {code}")
    code, _ = guest.call("POST", "/v1/auth/invite/accept", {"token": token, "password": invite_password})
    expect("邀请只能用一次", code == 400, f"HTTP {code}")

    invited = Client(args.base_url)
    code, body = invited.call(
        "POST",
        "/v1/auth/login",
        {"identifier": invitee_email, "email": invitee_email, "password": invite_password},
    )
    expect("受邀人登录", code == 200, f"HTTP {code}")
    user = (body.get("data") or {}).get("user") or {}
    expect("受邀人无需再改密", user.get("must_change_password") is not True, str(user))
    code, _ = invited.call("GET", "/v1/conversations?page=1&per_page=1")
    expect("受邀人业务接口可用", code == 200, f"HTTP {code}")

    code, _ = client.call("DELETE", f"/v1/admin/users/{invitee_id}")
    expect("软删除受邀用户", code == 200, f"HTTP {code}")

    code, _ = client.call("POST", f"/v1/admin/users/{uid}/password/reset")
    expect("管理员重置密码", code == 200, f"HTTP {code}")

    code, _ = client.call("POST", f"/v1/admin/users/{uid}/disable")
    expect("禁用账号", code == 200, f"HTTP {code}")

    code, _ = member.call("GET", "/v1/conversations?page=1&per_page=1")
    expect("禁用后旧会话失效", code == 401, f"HTTP {code}")

    code, _ = client.call("POST", f"/v1/admin/users/{uid}/enable")
    expect("启用账号", code == 200, f"HTTP {code}")

    code, _ = client.call("POST", f"/v1/admin/users/{uid}/force-logout")
    expect("强制下线", code == 200, f"HTTP {code}")

    code, _ = client.call("DELETE", f"/v1/admin/users/{uid}")
    expect("软删除测试账号", code == 200, f"HTTP {code}")

    print("团队模式冒烟全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
