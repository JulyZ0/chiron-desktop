#!/usr/bin/env python
"""打包版本地栈冒烟（只读，不打印令牌）。

在应用已启动、本地栈就绪后运行：

    python scripts/desktop_smoke.py
    python scripts/desktop_smoke.py --runtime-state "C:/Users/.../runtime-state.json"
    python scripts/desktop_smoke.py --base-url http://127.0.0.1:18245

它做三件事：
1. 从 `local/runtime-state.json` 读本次运行的 gatewayPort（端口每次启动会变）；
2. 用**仅回环可用**的 `POST /v1/auth/session` 换取 owner 会话；
3. 依次探测健康、项目/会话、引擎池、保留期、邮件配置等只读端点。

安全约定：token 只在内存里，不打印、不落盘。任何一步失败都以非零码退出。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def default_runtime_state() -> str:
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "gh_Athenavi_chiron", "local", "runtime-state.json")


def gateway_port(runtime_state: str) -> int:
    with open(runtime_state, "r", encoding="utf-8") as f:
        state = json.load(f)
    port = int(state.get("gatewayPort") or 0)
    if port <= 0:
        raise SystemExit(f"runtime-state 里没有有效的 gatewayPort: {runtime_state}")
    return port


def call(base: str, method: str, path: str, token: str = "") -> tuple[int, dict, bytes]:
    req = urllib.request.Request(base + path, method=method)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, _json(raw), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, _json(raw), raw


def _json(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode() or "{}")
        return value if isinstance(value, dict) else {"data": value}
    except Exception:
        return {}


def expect(step: str, cond: bool, detail: str = "") -> bool:
    if cond:
        print(f"  ok   {step}")
        return True
    print(f"  FAIL {step} {detail}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-state", default=default_runtime_state())
    parser.add_argument("--base-url", default="", help="显式覆盖，例如源码头启动时的 http://127.0.0.1:8088")
    args = parser.parse_args()

    base = args.base_url.rstrip("/") if args.base_url else f"http://127.0.0.1:{gateway_port(args.runtime_state)}"
    print(f"base = {base}")
    ok = True

    code, body, _ = call(base, "GET", "/health")
    ok &= expect("网关 /health", code == 200, f"HTTP {code}")

    code, body, _ = call(base, "POST", "/v1/auth/session")
    token = ((body.get("data") or {}).get("token") or "") if code == 200 else ""
    ok &= expect("回环 owner 会话", code == 200 and bool(token), f"HTTP {code}")
    if not token:
        # 后续全部需要鉴权；没有令牌就没有继续的意义，但也不打印响应体（可能含敏感信息）。
        print("无法取得回环会话：请确认应用已在本机启动且本地模式就绪。")
        return 1

    code, body, _ = call(base, "GET", "/v1/conversations?page=1&per_page=1", token)
    ok &= expect("会话列表", code == 200, f"HTTP {code}")

    code, body, _ = call(base, "GET", "/v1/session-map/projects", token)
    ok &= expect("项目列表", code == 200, f"HTTP {code}")

    code, body, _ = call(base, "GET", "/v1/admin/engines", token)
    data = body.get("data") or {}
    ok &= expect("引擎池快照", code == 200 and isinstance(data.get("count"), int), f"HTTP {code} {data}")

    code, body, _ = call(base, "GET", "/v1/admin/retention", token)
    ok &= expect("保留期配置", code == 200, f"HTTP {code}")

    code, body, _ = call(base, "GET", "/v1/admin/mail", token)
    mail = body.get("data") or {}
    ok &= expect("邮件通道配置", code == 200 and "ready" in mail, f"HTTP {code}")

    if ok:
        print("桌面本地栈冒烟全部通过。")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
