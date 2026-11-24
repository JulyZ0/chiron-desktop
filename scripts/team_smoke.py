#!/usr/bin/env python
"""团队服务模式冒烟：登录 + 建号 + 首登改密 + 邀请 + 重置/禁用/强制下线 + 匿名分享页。

可选 `--sse` 追加：创建会话 → 提交一轮 → 首 token 与 turn_done（需要可用的模型配置）。
分享匿名页需要会话里已有消息，因此默认在 `--sse` 之后验证；没有消息时明确 SKIP，
不会把"没测"记成 PASS。

只依赖标准库，适合在部署主机或任意能访问域名的机器上跑：

    set CHIRON_OWNER_PASSWORD=你的密码
    python scripts/team_smoke.py --base-url https://chiron.example.com --email owner@company.com
    python scripts/team_smoke.py --base-url https://... --email ... --sse

安全约定：
* 密码只从环境变量读取，不放进命令行参数（避免进 shell 历史/进程列表）；
* 不打印任何密码/token，报告里只留布尔值与计数；
* 结束时尽力删除本次创建的测试账号（软删除），失败会记为一条 FAIL 供追查。

可复用：`run_suite()` 返回结构化报告（不退出进程），统一验收入口
`scripts/team_acceptance.py` 调用它；独立运行时的退出码：0=无 FAIL，1=有 FAIL。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request


class StepError(Exception):
    """某个验收步骤失败；用于在保留已完成步骤的前提下终止。"""

    def __init__(self, name: str, detail: str = "") -> None:
        super().__init__(f"{name}: {detail}")
        self.name = name
        self.detail = detail


class Runner:
    """收集步骤结果；失败即抛 StepError，但报告保留已跑过的步骤。"""

    def __init__(self, log=print) -> None:
        self.steps: list[dict] = []
        self._log = log

    def ok(self, name: str, detail: str = "") -> None:
        self.steps.append({"name": name, "status": "PASS", "detail": detail})
        self._log(f"  ok   {name}")

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.ok(name, detail)
            return
        self.steps.append({"name": name, "status": "FAIL", "detail": detail})
        self._log(f"  FAIL {name} {detail}".rstrip())
        raise StepError(name, detail)

    def skip(self, name: str, reason: str = "") -> None:
        self.steps.append({"name": name, "status": "SKIP", "detail": reason})
        self._log(f"  skip {name} {reason}".rstrip())

    def info(self, msg: str) -> None:
        self._log(f"  info {msg}")


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

    def fetch_text(self, path: str, accept: str = "text/html") -> tuple[int, str, str]:
        """取一个非 JSON 资源（分享页 HTML）。返回 (状态码, Content-Type, 文本)。"""
        req = urllib.request.Request(self.base + path, headers={"Accept": accept})
        try:
            with self.opener.open(req, timeout=30) as resp:
                raw = resp.read(256 * 1024)
                return resp.status, resp.headers.get("Content-Type", ""), raw.decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            headers = exc.headers.get("Content-Type", "") if exc.headers else ""
            return exc.code, headers, ""

    def open_stream(self, path: str, timeout: float):
        """打开 SSE 长连接。返回可按行读取的上下文管理器。"""
        req = urllib.request.Request(self.base + path, headers={"Accept": "text/event-stream"})
        return self.opener.open(req, timeout=timeout)


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode() or "{}")
        return value if isinstance(value, dict) else {"data": value}
    except Exception:
        return {}


def _data(body: dict) -> dict:
    value = body.get("data")
    return value if isinstance(value, dict) else {}


def _run_sse(
    runner: Runner,
    member: Client,
    base_url: str,
    sid: str,
    timeout: int,
    metrics: dict,
) -> None:
    path = f"/v1/events?session_id={sid}&client_id=smoke-{secrets.token_hex(4)}"
    first_ms = None
    done = False
    try:
        with member.open_stream(path, timeout) as stream:
            started = time.monotonic()
            code, _ = member.call(
                "POST",
                "/v1/agent/submit",
                {"session_id": sid, "content": "Reply with the single word: pong"},
            )
            runner.check("提交 SSE 回合", code in (200, 202), f"HTTP {code}")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                raw = stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    payload = json.loads(line[5:].strip())
                except Exception:
                    continue
                etype = payload.get("type") or payload.get("event") or ""
                if etype == "text" and first_ms is None:
                    first_ms = (time.monotonic() - started) * 1000
                if etype in ("turn_done", "done") and first_ms is not None:
                    done = True
                    break
    except StepError:
        raise
    except Exception as exc:  # noqa: BLE001 - 超时/断流都按验收失败处理
        runner.info(f"SSE 读取异常：{type(exc).__name__}: {exc}")

    metrics["sse_first_token_ms"] = round(first_ms, 1) if first_ms is not None else None
    runner.check(
        "SSE 首 token 到达",
        first_ms is not None,
        "未收到 text 事件（模型未配置/不可用，或 SSE 被代理缓冲）",
    )
    if first_ms is not None:
        runner.info(f"首 token ≈ {int(first_ms)} ms")
    runner.check("SSE 回合完成", done, "未收到 turn_done")


def _conversation_messages(member: Client, sid: str) -> list[dict]:
    code, body = member.call("GET", f"/v1/conversations/{sid}?limit=50")
    if code != 200:
        return []
    messages = _data(body).get("messages") or []
    return [m for m in messages if isinstance(m, dict) and m.get("id") and m.get("role") in ("user", "assistant")]


def _wait_messages(member: Client, sid: str, wait_sec: float) -> list[dict]:
    """turn_done 之后消息可能还在落库：短暂轮询，避免把竞态记成"没有消息"。"""
    deadline = time.monotonic() + max(0.0, wait_sec)
    messages = _conversation_messages(member, sid)
    while not messages and time.monotonic() < deadline:
        time.sleep(1)
        messages = _conversation_messages(member, sid)
    return messages


def _run_share(
    runner: Runner,
    *,
    member: Client,
    base_url: str,
    client_factory,
    sid: str,
    strict_web: bool,
    can_wait_messages: bool,
    metrics: dict,
    on_session=None,
) -> str:
    """匿名分享页闭环：建分享 → 无凭据读取 → HTML 落地页 → 撤销后 410。

    返回本次使用的会话 id（调用方负责清理）。`on_session` 在会话确定后立刻回调，
    这样即使中途失败，收尾也能撤销分享/删除会话。
    """
    if not sid and not can_wait_messages:
        # 没跑 --sse 就没有可信的消息来源；新建空会话再"验证分享"只会浪费一次调用。
        runner.skip("匿名分享页", "未跑 --sse，没有可分享的消息；不伪造 PASS")
        return ""
    if not sid:
        code, body = member.call("POST", "/v1/conversations", {"title": "smoke-share"})
        sid = (_data(body).get("id") or "") if code == 200 else ""
        runner.check("创建分享测试会话", bool(sid), f"HTTP {code}")
    if on_session:
        on_session(sid)

    messages = _wait_messages(member, sid, 10.0) if can_wait_messages else _conversation_messages(member, sid)
    if not messages:
        runner.skip(
            "匿名分享页",
            "会话没有 user/assistant 消息（未跑 --sse 或模型不可用），不伪造 PASS",
        )
        return sid

    message_ids = [str(m["id"]) for m in messages[:5]]
    code, body = member.call("POST", f"/v1/conversations/{sid}/share", {"message_ids": message_ids})
    data = _data(body)
    token = data.get("share_id") or ""
    runner.check("创建分享链接", code == 200 and bool(token), f"HTTP {code}")
    url = data.get("url") or ""
    runner.check("分享链接为公开短链", (not url) or ("/s/" in url), "url 不含 /s/ 前缀")

    anon = client_factory(base_url)  # 独立 cookie jar，绝不带登录态
    code, body = anon.call("GET", f"/v1/share/{token}")
    shared = (_data(body).get("messages") or []) if code == 200 else []
    metrics["share_messages"] = len(shared)
    runner.check("匿名读取分享内容", code == 200 and len(shared) > 0, f"HTTP {code} messages={len(shared)}")

    code, ctype, text = anon.fetch_text(f"/s/{token}")
    looks_html = code == 200 and "html" in ctype.lower() and "<" in text
    if looks_html:
        runner.ok("匿名分享页 HTML 可访问")
    elif strict_web:
        runner.check("匿名分享页 HTML 可访问", False, f"HTTP {code} Content-Type={ctype or '-'}")
    else:
        runner.skip(
            "匿名分享页 HTML 可访问",
            f"HTTP {code} Content-Type={ctype or '-'}（网关未托管 SPA 时只验证数据面）",
        )

    code, _ = member.call("DELETE", f"/v1/conversations/{sid}/share")
    runner.check("撤销分享", code == 200, f"HTTP {code}")
    code, _ = anon.call("GET", f"/v1/share/{token}")
    runner.check("撤销后匿名访问返回 410", code == 410, f"HTTP {code}")
    return sid


def run_suite(
    base_url: str,
    email: str,
    password: str,
    *,
    sse: bool = False,
    sse_timeout: int = 120,
    share: bool = True,
    strict_web: bool = False,
    client_factory=Client,
    log=print,
) -> dict:
    """跑完整 Web E2E，返回机器可读报告。不打印密码/token，不调用 sys.exit。"""
    runner = Runner(log)
    owner = client_factory(base_url)
    metrics: dict = {"sse_first_token_ms": None, "share_messages": None}
    cleanup = {"attempted": 0, "errors": []}
    created_users: list[str] = []
    session_id = ""
    suffix = secrets.token_hex(4)
    member_email = f"smoke-{suffix}@invalid.local"
    member_username = f"smoke{suffix}"
    member_password = f"Smoke!{suffix}Aa1"

    try:
        # ── 1. owner 登录与后台可读 ─────────────────────────────────────
        code, body = owner.call(
            "POST", "/v1/auth/login", {"identifier": email, "email": email, "password": password}
        )
        runner.check("owner 登录", code == 200 and body.get("success") is True, f"HTTP {code}")

        code, _ = owner.call("GET", "/v1/admin/users?page=1&per_page=5")
        runner.check("读取用户列表", code == 200, f"HTTP {code}")

        # ── 2. 建号 + 首登强制改密 ─────────────────────────────────────
        code, body = owner.call(
            "POST",
            "/v1/admin/users",
            {"email": member_email, "username": member_username, "nickname": "Smoke Test", "role": "user"},
        )
        data = _data(body)
        new_user = data.get("user") or {}
        temp = data.get("initial_password") or ""
        runner.check("创建用户", code in (201, 200), f"HTTP {code}")
        runner.check("返回一次性初始密码", bool(temp), "empty password")
        uid = str(new_user.get("id") or "")
        runner.check("返回用户 id", bool(uid), "missing id")
        created_users.append(uid)

        member = client_factory(base_url)
        code, body = member.call(
            "POST", "/v1/auth/login", {"identifier": member_username, "email": member_email, "password": temp}
        )
        user = _data(body).get("user") or {}
        runner.check("新用户登录", code == 200, f"HTTP {code}")
        runner.check(
            "首登标记 must_change_password",
            user.get("must_change_password") is True,
            f"must_change_password={user.get('must_change_password')!r}",
        )
        code, _ = member.call("GET", "/v1/conversations?page=1&per_page=1")
        runner.check("未改密访问业务接口被拦", code == 403, f"HTTP {code}")

        code, _ = member.call(
            "POST", "/v1/auth/password", {"old_password": temp, "new_password": member_password}
        )
        runner.check("首次改密", code == 200, f"HTTP {code}")
        code, _ = member.call("GET", "/v1/conversations?page=1&per_page=1")
        runner.check("改密后业务接口可用", code == 200, f"HTTP {code}")

        # ── 3. SSE（可选）：首 token 与 turn_done ──────────────────────
        if sse:
            code, body = member.call("POST", "/v1/conversations", {"title": "smoke-sse"})
            session_id = (_data(body).get("id") or "") if code == 200 else ""
            runner.check("创建 SSE 测试会话", bool(session_id), f"HTTP {code}")
            _run_sse(runner, member, base_url, session_id, sse_timeout, metrics)

        # ── 4. 分享匿名页（有消息才判定）───────────────────────────────
        if share:

            def _track_share_session(sid: str) -> None:
                nonlocal session_id
                session_id = sid

            _run_share(
                runner,
                member=member,
                base_url=base_url,
                client_factory=client_factory,
                sid=session_id,
                strict_web=strict_web,
                can_wait_messages=sse,
                metrics=metrics,
                on_session=_track_share_session,
            )

        # 分享闭环结束后立刻删除测试会话：此时会话仍然有效；若等到强制下线后再清，
        # 令牌已失效，清理只会拿到 401 并把"测试数据没删掉"留到下次。
        if session_id:
            code, _ = member.call("DELETE", f"/v1/conversations/{session_id}")
            if code in (200, 204, 404):
                session_id = ""

        # ── 5. 邀请闭环：建号 → 一次性邀请 → 受邀人自设密码 → 登录可用 ──
        invitee_email = f"smoke-inv-{suffix}@invalid.local"
        code, body = owner.call(
            "POST",
            "/v1/admin/users",
            {
                "email": invitee_email,
                "username": f"inv{suffix}",
                "nickname": "Smoke Invite",
                "role": "user",
            },
        )
        invitee = _data(body).get("user") or {}
        invitee_id = str(invitee.get("id") or "")
        runner.check("创建受邀用户", code in (201, 200) and bool(invitee_id), f"HTTP {code}")
        created_users.append(invitee_id)

        code, body = owner.call("POST", f"/v1/admin/users/{invitee_id}/invite")
        data = _data(body)
        token = data.get("token") or ""
        runner.check("生成一次性邀请", code == 200 and bool(token), f"HTTP {code}")
        runner.check(
            "邀请链接带 token",
            (not data.get("invite_url")) or (token in data["invite_url"]),
            "invite_url 与 token 不一致",
        )

        invite_password = f"Invite!{suffix}Aa1"
        guest = client_factory(base_url)
        code, _ = guest.call("POST", "/v1/auth/invite/accept", {"token": token, "password": invite_password})
        runner.check("受邀人设置密码", code == 200, f"HTTP {code}")
        code, _ = guest.call("POST", "/v1/auth/invite/accept", {"token": token, "password": invite_password})
        runner.check("邀请只能用一次", code == 400, f"HTTP {code}")

        invited = client_factory(base_url)
        code, body = invited.call(
            "POST",
            "/v1/auth/login",
            {"identifier": invitee_email, "email": invitee_email, "password": invite_password},
        )
        user = _data(body).get("user") or {}
        runner.check("受邀人登录", code == 200, f"HTTP {code}")
        runner.check(
            "受邀人无需再改密",
            user.get("must_change_password") is not True,
            f"must_change_password={user.get('must_change_password')!r}",
        )
        code, _ = invited.call("GET", "/v1/conversations?page=1&per_page=1")
        runner.check("受邀人业务接口可用", code == 200, f"HTTP {code}")

        code, _ = owner.call("DELETE", f"/v1/admin/users/{invitee_id}")
        runner.check("软删除受邀用户", code == 200, f"HTTP {code}")
        if invitee_id in created_users:
            created_users.remove(invitee_id)

        # ── 6. 重置 / 禁用 / 启用 / 强制下线 ───────────────────────────
        code, body = owner.call("POST", f"/v1/admin/users/{uid}/password/reset")
        reset_temp = _data(body).get("temporary_password") or ""
        runner.check("管理员重置密码返回临时密码", code == 200 and bool(reset_temp), f"HTTP {code}")

        code, _ = owner.call("POST", f"/v1/admin/users/{uid}/disable")
        runner.check("禁用账号", code == 200, f"HTTP {code}")
        code, _ = member.call("GET", "/v1/conversations?page=1&per_page=1")
        runner.check("禁用后旧会话失效", code == 401, f"HTTP {code}")

        code, _ = owner.call("POST", f"/v1/admin/users/{uid}/enable")
        runner.check("启用账号", code == 200, f"HTTP {code}")

        # 用重置后的临时密码重新登录，并按首登规则改密，才有可被强制下线的会话。
        relogin = client_factory(base_url)
        code, body = relogin.call(
            "POST", "/v1/auth/login", {"identifier": member_username, "email": member_email, "password": reset_temp}
        )
        runner.check("重置密码后可登录", code == 200, f"HTTP {code}")
        final_password = f"Reset!{suffix}Aa1"
        code, _ = relogin.call(
            "POST", "/v1/auth/password", {"old_password": reset_temp, "new_password": final_password}
        )
        runner.check("重置后首登改密", code == 200, f"HTTP {code}")
        code, _ = relogin.call("GET", "/v1/conversations?page=1&per_page=1")
        runner.check("重置后业务接口可用", code == 200, f"HTTP {code}")

        code, _ = owner.call("POST", f"/v1/admin/users/{uid}/force-logout")
        runner.check("强制下线", code == 200, f"HTTP {code}")
        code, _ = relogin.call("GET", "/v1/conversations?page=1&per_page=1")
        runner.check("强制下线后旧会话失效", code == 401, f"HTTP {code}")

        code, _ = owner.call("DELETE", f"/v1/admin/users/{uid}")
        runner.check("软删除测试账号", code == 200, f"HTTP {code}")
        if uid in created_users:
            created_users.remove(uid)
    except StepError:
        pass
    except Exception as exc:  # noqa: BLE001 - 网络/解析异常也必须折成一条可见失败
        runner.steps.append(
            {"name": "运行异常", "status": "FAIL", "detail": f"{type(exc).__name__}: {exc}"}
        )
    finally:
        # 失败时也收尾：先撤销分享/删会话（依赖测试账号的会话），**再**删账号 ——
        # 反过来会让刚删掉的账号令牌失效，分享链接留在公网可读。
        if session_id:
            try:
                code, _ = member.call("DELETE", f"/v1/conversations/{session_id}/share")
                if code not in (200, 204, 404):
                    cleanup["errors"].append(f"撤销残留分享返回 HTTP {code}")
            except Exception as exc:  # noqa: BLE001 - 撤销失败由会话删除兜底
                cleanup["errors"].append(f"撤销残留分享失败：{type(exc).__name__}")
            try:
                code, _ = member.call("DELETE", f"/v1/conversations/{session_id}")
                if code not in (200, 204, 404):
                    cleanup["errors"].append(f"删除测试会话返回 HTTP {code}")
            except Exception as exc:  # noqa: BLE001 - 会话删除失败不掩盖主结论
                cleanup["errors"].append(f"删除测试会话失败：{type(exc).__name__}")
        for user_id in created_users:
            cleanup["attempted"] += 1
            try:
                code, _ = owner.call("DELETE", f"/v1/admin/users/{user_id}")
                if code not in (200, 404):
                    cleanup["errors"].append(f"删除用户 {user_id[-6:]} 返回 HTTP {code}")
            except Exception as exc:  # noqa: BLE001
                cleanup["errors"].append(f"删除用户 {user_id[-6:]} 失败：{type(exc).__name__}")
        if cleanup["errors"]:
            runner.steps.append(
                {"name": "清理测试数据", "status": "FAIL", "detail": "; ".join(cleanup["errors"])}
            )

    failed = [s for s in runner.steps if s["status"] == "FAIL"]
    return {
        "ok": not failed,
        "steps": runner.steps,
        "metrics": metrics,
        "cleanup": cleanup,
        "failed_step": failed[0]["name"] if failed else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CHIRON_TEAM_BASE_URL", ""),
        help="例如 https://chiron.example.com",
    )
    parser.add_argument("--email", default=os.environ.get("CHIRON_TEAM_EMAIL", ""), help="owner 邮箱或用户名")
    parser.add_argument("--admin", action="store_true", help="用 admin 账号跑（默认 owner）")
    parser.add_argument(
        "--sse",
        action="store_true",
        help="额外验证 SSE：创建会话 → 提交一轮 → 首 token 与 turn_done（需要服务端已配置可用模型）",
    )
    parser.add_argument("--sse-timeout", type=int, default=120, help="SSE 等待秒数")
    parser.add_argument("--no-share", action="store_true", help="跳过匿名分享页闭环")
    parser.add_argument(
        "--strict-web",
        action="store_true",
        help="要求 /s/<token> 返回 HTML（团队 Web 由网关托管 SPA 时打开）",
    )
    parser.add_argument("--json", action="store_true", help="只输出 JSON 报告")
    parser.add_argument("--json-out", default="", help="把 JSON 报告写到文件")
    args = parser.parse_args()

    if not args.base_url:
        print("缺少 --base-url（或 CHIRON_TEAM_BASE_URL）", file=sys.stderr)
        return 2
    if not args.email:
        print("缺少 --email（或 CHIRON_TEAM_EMAIL）", file=sys.stderr)
        return 2
    password = os.environ.get("CHIRON_OWNER_PASSWORD", "")
    if not password:
        print("缺少 CHIRON_OWNER_PASSWORD 环境变量", file=sys.stderr)
        return 2

    log = (lambda _msg: None) if args.json else print
    if not args.json:
        print(f"base = {args.base_url}")
    report = run_suite(
        args.base_url,
        args.email,
        password,
        sse=args.sse,
        sse_timeout=args.sse_timeout,
        share=not args.no_share,
        strict_web=args.strict_web,
        log=log,
    )
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif report["ok"]:
        skipped = sum(1 for s in report["steps"] if s["status"] == "SKIP")
        print(f"团队模式冒烟全部通过（{len(report['steps'])} 步，{skipped} 步 SKIP）。")
    else:
        print(f"团队模式冒烟失败：{report['failed_step']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
