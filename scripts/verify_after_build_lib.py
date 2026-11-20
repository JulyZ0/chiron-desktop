#!/usr/bin/env python3
"""重建后的自动验收（P0-1）—— 对着**已经跑起来的**桌面本地栈逐项断言。

## 为什么要有它

重建（打包 / 换版本 / 改完引擎后重新安装）最怕的是"界面能打开"这种弱证据：
窗口起来了，但依赖页 502、计费全 0、分享页白屏、隔离悄悄降级 —— 这些都不会
让应用启动失败，只会让用户在两天后撞上。本脚本把"重建后必须成立的事实"写成
可执行的断言，一条命令给出 PASS/FAIL/SKIP 加**下一步该去哪里查**。

## 运行前提

本机桌面应用**已经启动**（本地模式），且 `runtime-state.json` 存在：

    %APPDATA%\\gh_Athenavi_chiron\\local\\runtime-state.json

端口一律从状态文件读，不接受手填 —— 桌面栈的网关/引擎端口是**随机**的，
写死一个默认值只会在下次启动时指向空气。

## 断言清单（与任务 P0-1 的 a–f 一一对应）

| id | 端点 | 判据 |
| --- | --- | --- |
| 0a | runtime-state.json | 可读、schema=1、gatewayPort 合法 |
| 0b | GET /ready → POST /v1/auth/session | 栈就绪（PG/Redis up）且拿到 owner 会话 |
| a | GET /v1/admin/deps/list | 200 且 `tool` ∈ {uv, pip} 且 `packages` 非空 |
| b | GET /v1/system/isolation | `effective` 与 `requested` 一致且不低于 floor；`restricted` 不可用即 FAIL |
| c | GET /v1/billing/usage?range=90d | `total_input` > 0（0 恒等于"SUM 聚合坏了"） |
| c' | GET /v1/admin/billing/price-coverage | **只提示**未定价模型，不计 FAIL |
| d | GET /v1/shares → 匿名 GET /s/<token> | 200 且含 `<div id="app"`；无分享则 SKIP |
| e | GET /v1/subagent/runs → GET /v1/models | 路由在册；模型列表非空并报默认模型 |
| f | GET /v1/admin/diagnostics/summary | 逐项报告 `checks[].status`（只读，不判失败） |

## 三条硬规矩

1. **绝不打印 token**。会话换来的 Bearer 只活在内存里；任何要回显的文本都先过
   `redact()`，`--json` 报告里也不含该字段（有单测钉住）。
2. **判据是纯函数**。所有 IO（HTTP / 读文件）与"这算 PASS 还是 FAIL"分开，
   后者留在 `judge_*` 里 —— 测试注一个假 transport 就能覆盖每个分支。
3. **端点读不到 ≠ 断言失败**。SKIP 表示"这次没条件验"（没有分享、没有子 Agent
   run），必须与 FAIL 分开计数；否则一次"没数据"会把重建判成坏的。

## 退出码

* `0` —— 没有 FAIL（可以有 SKIP/INFO）；
* `1` —— 至少一条断言 FAIL（含"`/ready` 连得上但依赖 down"：那是重建缺陷）；
* `2` —— 环境不具备：状态文件缺失/损坏、网关端口连不上（应用没启动）、换不到会话。

## 用法

    python scripts/verify_after_build_lib.py              # 人读输出
    python scripts/verify_after_build_lib.py --json       # 机器可读
    python scripts/verify_after_build_lib.py --runtime-state D:/tmp/runtime-state.json --base-url http://127.0.0.1:8080

`scripts/verify-after-build.ps1`（Windows）与 `scripts/verify-after-build.sh`
（git-bash）是同一份实现的两个壳，行为与输出逐字一致。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: 状态文件的格式版本。与 `frontend-vue/electron/runtime-state.cjs::STATE_SCHEMA`
#: 是同一个数：读不懂的版本一律拒绝，绝不"猜字段"。
STATE_SCHEMA = 1

#: Electron userData 的目录名（契约 §5：`%APPDATA%\\gh_Athenavi_chiron`）。
APP_DIR_NAME = "gh_Athenavi_chiron"

#: 本地模式的数据子目录（contracts.md §5）与状态文件名。
LOCAL_DIR_NAME = "local"
STATE_FILENAME = "runtime-state.json"

#: 检出的四态。INFO 与 SKIP 都不影响退出码，区别是"看到了事实"与"没条件验"。
PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "INFO"
_STATUS_ORDER = (PASS, FAIL, SKIP, INFO)

#: 请求预算（秒）。deps 走 `pip list` / `uv pip list`，引擎侧自己的上限是 30s
#: （`python-engine/app/api/deps.py::LIST_TIMEOUT_SECONDS`），所以这里必须比它长，
#: 否则我们会先超时、把"引擎慢慢算出来了"误报成"引擎不可达"。
DEFAULT_TIMEOUT = 30.0
DEPS_TIMEOUT = 90.0

#: 分享页断言要看到的最小 SPA 骨架（`frontend-vue/index.html` 里的挂载点）。
#: 只认 `<div id="app"`，退一步也接受 `id="app"`（打包压缩会改写空白）。
SPA_MARKERS = ('<div id="app"', 'id="app"')

#: 响应体回显上限：错误信息够定位就行，整页日志塞进报告只会淹掉结论。
BODY_PREVIEW = 400

#: 形似 JWT 的三段式长串（每段至少 8 个 URL-safe 字符）。用于 `_redact_jwt`。
_JWT_RE = re.compile(r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")


class StateError(RuntimeError):
    """runtime-state 不可用（缺失 / 坏 JSON / 版本不认识 / 端口非法）。

    单独一个异常类型而不是 `ValueError`：调用方要能把"状态文件问题"直接映射成
    退出码 2（环境不具备），与"断言失败"（退出码 1）分开。
    """


# ── runtime-state 解析 ──────────────────────────────────────────────────


def default_runtime_state_path(env: dict[str, str] | None = None) -> str:
    """按桌面壳的同一优先级给出状态文件路径。

    顺序与 `frontend-vue/electron/config.cjs::userDataDir` 一致：
    `CHIRON_DESKTOP_USER_DATA` 覆盖优先，其次 Windows 的 `APPDATA`。
    非 Windows 上 `APPDATA` 不存在时返回空串，由调用方给出可操作的报错。
    """
    env = dict(os.environ if env is None else env)
    override = str(env.get("CHIRON_DESKTOP_USER_DATA") or "").strip()
    if override:
        return os.path.join(override, LOCAL_DIR_NAME, STATE_FILENAME)
    appdata = str(env.get("APPDATA") or "").strip()
    if appdata:
        return os.path.join(appdata, APP_DIR_NAME, LOCAL_DIR_NAME, STATE_FILENAME)
    return ""


def resolve_runtime_state_path(
    cli_path: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """决定读哪个状态文件：命令行 > 环境变量 > 默认位置。

    `CHIRON_VERIFY_RUNTIME_STATE` 单独一个变量名（不复用应用自己的），是为了让
    "验收脚本指向一份拷出来的状态文件"这件事不改变应用的行为。
    """
    env = dict(os.environ if env is None else env)
    for candidate in (cli_path, env.get("CHIRON_VERIFY_RUNTIME_STATE")):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return default_runtime_state_path(env)


def parse_runtime_state(text: str) -> dict[str, Any]:
    """把状态文件的文本解析成端口表；任何不可用都抛 `StateError`（带 next step）。

    不提供"缺字段就用 8080"的兜底：端口是随机的，猜一个只会把"应用没起来"变成
    "连接被拒绝"，而后者更难看懂。
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateError(f"runtime-state.json 不是合法 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise StateError("runtime-state.json 顶层必须是对象")

    schema = parsed.get("schema")
    if schema != STATE_SCHEMA:
        raise StateError(
            f"runtime-state schema={schema!r}，本脚本只认识 {STATE_SCHEMA}；"
            "应用升级后请同步更新 scripts/verify_after_build_lib.py"
        )

    gateway_port = _as_port(parsed.get("gatewayPort"))
    if gateway_port is None:
        raise StateError(
            "runtime-state.json 缺 gatewayPort（或不是 1..65535 的整数）；"
            "状态文件可能写了一半，重启桌面应用会重写它"
        )
    engine_port = _as_port(parsed.get("enginePort"))
    return {
        "schema": schema,
        "pid": parsed.get("pid"),
        "startedAt": parsed.get("startedAt"),
        "gatewayPort": gateway_port,
        "enginePort": engine_port,
        "raw": parsed,
    }


def load_runtime_state(path: str) -> dict[str, Any]:
    """读 + 解析状态文件。读不到时把"该看哪里"写进报错。"""
    if not path:
        raise StateError(
            "找不到 %APPDATA%：请用 --runtime-state 指定状态文件，"
            "或在桌面端设置「安装设置」里设置 CHIRON_DESKTOP_USER_DATA"
        )
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError as exc:
        raise StateError(
            f"状态文件不存在：{path} —— 桌面应用当前没在运行"
            "（正常退出会删掉该文件），或从未在本账户下启动过"
        ) from exc
    except OSError as exc:
        raise StateError(f"读不了状态文件 {path}：{exc}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StateError(f"状态文件不是 UTF-8：{path}（{exc}）") from exc
    parsed = parse_runtime_state(text)
    parsed["path"] = path
    return parsed


def _as_port(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


# ── 请求组装（纯函数）──────────────────────────────────────────────────


@dataclass
class HttpResult:
    """一次 HTTP 往返的结果。`status=None` 表示连接层就失败了（见 `error`）。"""

    status: int | None
    payload: Any = None
    text: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= int(self.status) < 300


@dataclass
class Request:
    """待发请求。拆成数据而不是直接发，是为了让测试能逐字段断言。"""

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None


def build_base_url(gateway_port: int) -> str:
    """网关基址。**固定 127.0.0.1**：loopback 免口令那条闸门只认 loopback 来源，
    换成 `localhost` 在解析出 `::1` 时反而会被判成非本机（取决于 hosts 与绑定族）。"""
    return f"http://127.0.0.1:{int(gateway_port)}"


def build_request(
    base_url: str,
    path: str,
    method: str = "GET",
    token: str | None = None,
    body: Any = None,
    anonymous: bool = False,
) -> Request:
    """组装一条请求。`anonymous=True` 时**不带**任何凭据（分享页就是这么被验的）。"""
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token and not anonymous:
        headers["Authorization"] = "Bearer " + token
    return Request(method=method, url=base_url.rstrip("/") + path, headers=headers, body=data)


def build_session_request(base_url: str) -> Request:
    """换取 owner 会话的请求：`POST /v1/auth/session`（loopback 免口令）。"""
    return build_request(base_url, "/v1/auth/session", method="POST", body={}, anonymous=True)


def build_page_request(base_url: str, share_token: str) -> Request:
    """匿名打开分享页 `/s/<token>`：显式不带 Authorization，模拟"链接发给别人"。"""
    token = str(share_token or "").strip()
    return build_request(base_url, "/s/" + token, anonymous=True)


def redact(text: Any, secrets: Any = ()) -> str:
    """回显前洗一遍：把已知凭据与形似 JWT 的内容替换掉。

    两道都留着：`secrets` 是精确的已知值，正则兜住"错误体里带了别人的 token"
    （例如上游把请求头原样回显）。宁可多遮，不可漏一次。
    """
    out = str(text if text is not None else "")
    for secret in secrets or ():
        secret = str(secret or "")
        if secret:
            out = out.replace(secret, "***")
    return _redact_jwt(out)


def _redact_jwt(text: str) -> str:
    """兜住"别人把令牌回显进错误体"：形如 `a.b.c` 的长串一律遮掉。

    不能按空格切词再判断前缀：真实日志里它总带着前缀（`token=eyJ...`、
    `Bearer eyJ...`、`Authorization: Bearer eyJ...`），按 token 边界匹配才可靠。
    """
    return _JWT_RE.sub("***", text)


def preview(text: str, limit: int = BODY_PREVIEW) -> str:
    """截断一段回显文本。"""
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else body[:limit] + "…"


def envelope_data(payload: Any) -> Any:
    """剥掉网关的 `{success, data}` 外壳；没有外壳就原样返回。

    脱敏中间件（`internal/api/sanitize_response.go`）会把 `/v1/admin/` 下**字段名
    含 key/secret/token 等子串**的值替换成 `********`，所以这里不假设字段值可信。
    """
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def pick_token(payload: Any) -> str:
    """从会话响应里取 Bearer。取不到（或已被脱敏成 `********`）返回空串。

    脱敏后的 `********` **必须当成"没有 token"**：它不是凭据，拿它去打后续接口
    只会得到一串 401，而真正的病因（脱敏名单把 token 列了进去）会被埋掉。
    """
    data = envelope_data(payload)
    if isinstance(data, dict):
        token = data.get("token")
        if isinstance(token, str):
            token = token.strip()
            if token and set(token) != {"*"}:
                return token
    return ""


def pick_default_model(settings_payload: Any) -> str:
    """从 `GET /v1/admin/settings?category=python` 的响应里取 `default_model`。"""
    data = envelope_data(settings_payload)
    config = data.get("config") if isinstance(data, dict) else None
    if isinstance(config, dict):
        value = config.get("default_model")
        if isinstance(value, str):
            return value.strip()
    return ""


# ── 判定（纯函数，逐项对应任务 a–f）────────────────────────────────────


@dataclass
class Check:
    """清单里的一行：状态 + 结论 + **下一步去哪里查** + 机器可读证据。"""

    id: str
    title: str
    status: str
    detail: str = ""
    hint: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
            "evidence": self.evidence,
            "notes": self.notes,
        }


def _http_failure(result: HttpResult, base_hint: str) -> str:
    """把"请求没成功"翻译成一句带证据的话（供 FAIL 的 detail 用）。"""
    if result.error:
        return f"请求失败：{preview(result.error)}"
    status = result.status
    body = preview(result.text)
    return f"HTTP {status}：{body}" if body else f"HTTP {status}（空响应）；{base_hint}"


def judge_state(state: dict[str, Any]) -> Check:
    """0a：状态文件本身就是第一条断言（端口错，后面全是噪音）。"""
    engine_port = state.get("enginePort")
    detail = f"gatewayPort={state['gatewayPort']}"
    if engine_port:
        detail += f"，enginePort={engine_port}"
    else:
        detail += "，enginePort 缺失（只影响直连引擎的排查，不影响本脚本）"
    return Check(
        id="0a",
        title="runtime-state 可读",
        status=PASS,
        detail=detail,
        evidence={
            "path": state.get("path", ""),
            "pid": state.get("pid"),
            "startedAt": state.get("startedAt"),
            "gateway_port": state.get("gatewayPort"),
            "engine_port": engine_port,
        },
    )


def judge_ready(result: HttpResult) -> Check:
    """0b 前半：`GET /ready` 证明端口上跑的是**我们的**栈，且 PG/Redis 都 up。

    只看端口通不够 —— 强杀后端口会被别的程序复用（`runtime-state.cjs` 的注释
    里写了同一件事），ready 里的两个依赖状态才是"这是我们的栈"的证据。
    """
    if not result.ok:
        return Check(
            id="0b",
            title="本地栈就绪（/ready）",
            status=FAIL,
            detail=_http_failure(result, "网关端口可能已过期"),
            hint=(
                "① 桌面应用是否在运行；② runtime-state.json 是否过期（重启应用会重写）；"
                "③ 看 %APPDATA%\\gh_Athenavi_chiron\\logs\\main.log 与 local\\logs\\ 里的启动错误"
            ),
        )
    data = envelope_data(result.payload)
    data = data if isinstance(data, dict) else {}
    pg, redis = str(data.get("postgres") or ""), str(data.get("redis") or "")
    status = PASS if pg == "up" and redis == "up" else FAIL
    hint = ""
    if status == FAIL:
        hint = (
            "数据栈没起来：看 local\\logs\\ 下的 pg / redis 日志与 main.log 的 launcher 退出码；"
            "端口在 runtime-state.json 的 pgPort / redisPort"
        )
    return Check(
        id="0b",
        title="本地栈就绪（/ready）",
        status=status,
        detail=f"postgres={pg or '未知'}，redis={redis or '未知'}",
        hint=hint,
        evidence={"postgres": pg, "redis": redis},
    )


def judge_session(result: HttpResult, secrets: Any = ()) -> Check:
    """0b 后半：换取 owner 会话。**只看"有没有 token"，绝不回显它**。"""
    title = "owner 会话（POST /v1/auth/session）"
    if not result.ok:
        hint = _session_hint(result.status)
        return Check(
            id="0b2",
            title=title,
            status=FAIL,
            detail=redact(_http_failure(result, hint), secrets),
            hint=hint,
        )
    token = pick_token(result.payload)
    user = envelope_data(result.payload)
    user = user.get("user") if isinstance(user, dict) else None
    role = user.get("role") if isinstance(user, dict) else None
    if not token:
        return Check(
            id="0b2",
            title=title,
            status=FAIL,
            detail="响应里没有 token（或已被脱敏中间件改写成 ********）",
            hint=(
                "会话端点须返回 {token, user}；若 body 里 token 变成 ********，"
                "检查 internal/api/sanitize_response.go 的 SensitiveFields 是否把 token 列入"
            ),
        )
    return Check(
        id="0b2",
        title=title,
        status=PASS,
        detail=f"已拿到 Bearer（长度 {len(token)}，值不打印）；role={role or '未知'}",
        evidence={"role": role, "email": (user or {}).get("email") if isinstance(user, dict) else ""},
    )


def _session_hint(status: int | None) -> str:
    """把会话端点的失败码翻译成"下一步查哪里"。"""
    if status == 404:
        return (
            "端点未注册：网关须以 LOCAL_BOOTSTRAP=1 启动（internal/api/routes_auth.go），"
            "且连的是本地模式网关而不是云端地址"
        )
    if status == 403:
        return "来源不是 loopback：必须用 127.0.0.1 访问，别用机器名/局域网 IP（internal/api/auth_local_session.go）"
    if status == 405:
        return (
            "该网关没有注册 POST /v1/auth/session（LOCAL_BOOTSTRAP 未开）——"
            "常见于连到了源码态 `python run.py start` 的开发栈，而不是桌面本地栈"
        )
    if status == 500:
        return "服务端 500：看引擎/网关日志与数据库是否可迁移（owner 账号由 ensureLocalOwner 幂等建）"
    return "检查网关日志 logs/main.log 与 /v1/auth/session 的注册开关（LOCAL_BOOTSTRAP）"


def judge_deps(result: HttpResult) -> Check:
    """a：依赖通道。`tool` 必须是 uv 或 pip，清单不能空。

    为什么把 `tool=metadata` 判成 FAIL：那是引擎"两个工具都跑不了"的降级路径
    （`python-engine/app/api/deps.py::list_packages` 的 degraded 分支），页面能看，
    但装/卸/回滚全都做不了 —— 重建产物缺 uv 就是这么暴露的。
    """
    title = "依赖通道（/v1/admin/deps/list）"
    hint_base = (
        "看 internal/api/deps_admin.go::List → python-engine/app/api/deps.py::list_packages；"
        "网关日志里的 `python GET /internal/deps/list` 一行有引擎原文"
    )
    if not result.ok:
        return Check(
            id="a",
            title=title,
            status=FAIL,
            detail=_http_failure(result, "取决于引擎是否启动"),
            hint=hint_base + "；端点 404 ⇒ 网关版本不含该路由",
        )
    data = envelope_data(result.payload)
    data = data if isinstance(data, dict) else {}
    tool = str(data.get("tool") or "")
    packages = data.get("packages")
    count = len(packages) if isinstance(packages, list) else int(data.get("count") or 0)
    degraded = bool(data.get("degraded"))
    python = str(data.get("python") or "")
    evidence = {"tool": tool, "packages": count, "degraded": degraded, "python": python}
    if degraded or tool not in ("uv", "pip"):
        return Check(
            id="a",
            title=title,
            status=FAIL,
            detail=f"tool={tool or '缺失'}，degraded={degraded}，packages={count}"
            + (f"，tool_reason={preview(str(data.get('tool_reason') or ''), 160)}" if data.get("tool_reason") else ""),
            hint=(
                "运行时装包工具不可用：重建产物应随带 resources/uv/<platform>/uv（或用 CHIRON_UV_BIN 指定）；"
                "引擎侧会先试 uv 再试 pip，两者都没有才退到 metadata（只读）"
            ),
            evidence=evidence,
        )
    if count <= 0:
        return Check(
            id="a",
            title=title,
            status=FAIL,
            detail=f"tool={tool} 但 packages 为空（python={python or '未知'}）",
            hint="引擎列出的包清单为空：确认引擎跑的是自带 venv（看 python 字段），而不是裸系统解释器",
            evidence=evidence,
        )
    return Check(
        id="a",
        title=title,
        status=PASS,
        detail=f"tool={tool}，packages={count}" + (f"，python={python}" if python else ""),
        evidence=evidence,
    )


def judge_isolation(result: HttpResult, require_restricted: bool = True) -> Check:
    """b：隔离档位。三条判据，任一命中即 FAIL（而不是抛异常把脚本打断）：

    1. `effective` 与 `requested` 不一致（被降级）或跌破 `floor`；
    2. `restricted` 不可用（`unavailable_reasons.restricted` 有内容）——
       任务 P0-1 明确要求这一条给 FAIL；本机不打算用该档位时加
       `--ignore-restricted`，本项转 PASS 并在 note 里留下原因；
    3. 响应结构不认识（缺 effective/requested）—— 那说明网关与引擎的契约漂了。
    """
    title = "隔离档位（/v1/system/isolation）"
    hint_base = (
        "看 python-engine/app/isolation/__init__.py::status 与 internal/api/routes_proxy.go 的 "
        "isoP 路由；restricted 档依赖提权助手（GET/POST /v1/system/isolation/helper，"
        "启用会弹一次 UAC）"
    )
    if not result.ok:
        return Check(
            id="b",
            title=title,
            status=FAIL,
            detail=_http_failure(result, "取决于引擎是否启动"),
            hint=hint_base,
        )
    data = envelope_data(result.payload)
    if not isinstance(data, dict) or not data.get("effective") or not data.get("requested"):
        return Check(
            id="b",
            title=title,
            status=FAIL,
            detail=f"响应缺 effective/requested：{preview(json.dumps(data, ensure_ascii=False) if data is not None else 'null', 200)}",
            hint="网关透传的引擎响应形状变了（契约见 docs/desktop-local/contracts.md）；先确认引擎版本与网关一致",
        )

    requested = str(data.get("requested"))
    effective = str(data.get("effective"))
    floor = str(data.get("floor") or "")
    degraded = bool(data.get("degraded"))
    below_floor = bool(data.get("below_floor"))
    reasons = data.get("reasons")
    unavailable = data.get("unavailable_reasons")
    unavailable = unavailable if isinstance(unavailable, dict) else {}
    restricted_reason = str(unavailable.get("restricted") or "").strip()
    selectable = data.get("selectable") if isinstance(data.get("selectable"), list) else []

    problems: list[str] = []
    if effective != requested:
        problems.append(f"被降级：requested={requested} → effective={effective}")
    if below_floor:
        problems.append(f"effective={effective} 低于安全底线 floor={floor or '未知'}")
    if restricted_reason and require_restricted:
        problems.append(f"restricted 不可用：{restricted_reason}")

    detail = (
        f"requested={requested}，effective={effective}"
        + (f"，floor={floor}" if floor else "")
        + f"，degraded={degraded}，selectable={selectable}"
    )
    if reasons:
        detail += f"，reasons={preview(json.dumps(reasons, ensure_ascii=False), 160)}"
    if unavailable:
        detail += f"，unavailable_reasons={preview(json.dumps(unavailable, ensure_ascii=False), 200)}"

    evidence = {
        "requested": requested,
        "effective": effective,
        "floor": floor,
        "degraded": degraded,
        "below_floor": below_floor,
        "selectable": selectable,
        "unavailable_reasons": unavailable,
    }

    if problems:
        return Check(
            id="b",
            title=title,
            status=FAIL,
            detail="；".join(problems) + " | " + detail,
            hint=hint_base,
            evidence=evidence,
            notes=["restricted 不可用时其余档位仍可正常工作；补上提权助手即可消除本项 FAIL"],
        )
    notes = []
    if restricted_reason:
        notes.append(
            f"restricted 不可用：{restricted_reason}（当前 requested={requested} 不受影响；"
            "按 --ignore-restricted 降为提示，启用提权助手可恢复该档位）"
        )
    return Check(id="b", title=title, status=PASS, detail=detail, evidence=evidence, notes=notes)


def judge_usage(result: HttpResult) -> Check:
    """c：计费总量。`total_input=0` 是"SUM 聚合坏了"的**判据**，不是"还没用过"。

    证据链：`total_calls > 0` 说明记账行在，`zero_token_events` 很小说明那些行本该
    带 token —— 于是 total_input 为 0 只可能是聚合/扫描把 bigint 读丢了
    （`internal/api/billing_usage.go::usageDaily` 的 `::bigint` cast 与
    `db.FetchAll` → `intOf` 的 numeric 分支）。
    """
    title = "计费用量（/v1/billing/usage）"
    hint_base = (
        "直查库：SELECT count(*), sum(input_tokens), sum(output_tokens) FROM usage_events "
        "WHERE created_at >= now() - interval '90 days'；再看 "
        "internal/api/billing_usage.go::usageDaily 的 COALESCE(SUM(...))::bigint 与 intOf 的 numeric 分支"
    )
    if not result.ok:
        return Check(
            id="c",
            title=title,
            status=FAIL,
            detail=_http_failure(result, "端点 404 ⇒ 网关版本不含 billing_usage.go"),
            hint=hint_base,
        )
    data = envelope_data(result.payload)
    data = data if isinstance(data, dict) else {}
    total_input = _as_int(data.get("total_input"))
    total_calls = _as_int(data.get("total_calls"))
    zero_token = _as_int(data.get("zero_token_events"))
    unpriced = _as_int(data.get("total_unpriced_calls"))
    period = data.get("period_days")
    evidence = {
        "total_input": total_input,
        "total_output": _as_int(data.get("total_output")),
        "total_calls": total_calls,
        "zero_token_events": zero_token,
        "total_unpriced_calls": unpriced,
        "period_days": period,
    }
    detail = (
        f"total_input={total_input}，total_calls={total_calls}，"
        f"zero_token_events={zero_token}，period_days={period}"
    )
    if total_input > 0:
        return Check(id="c", title=title, status=PASS, detail=detail, evidence=evidence)
    return Check(
        id="c",
        title=title,
        status=FAIL,
        detail=detail + " ⇒ total_input 恒为 0",
        hint="检查 SUM 聚合/重建版本：" + hint_base,
        evidence=evidence,
    )


def judge_price_coverage(result: HttpResult) -> Check:
    """c'：价格覆盖率。**只提示，不计 FAIL** —— 缺价是运营补数据的事，不是重建坏了。"""
    title = "价格覆盖率（/v1/admin/billing/price-coverage，仅提示）"
    if not result.ok:
        return Check(
            id="c2",
            title=title,
            status=INFO,
            detail="读不到：" + _http_failure(result, "不计 FAIL"),
            hint="internal/api/billing_price_coverage.go::GetPriceCoverage；503 表示价格查询失败而不是缺价",
        )
    data = envelope_data(result.payload)
    data = data if isinstance(data, dict) else {}
    unpriced = data.get("unpriced") if isinstance(data.get("unpriced"), list) else []
    listed = [
        f"{str(item.get('model') or '?')}({_as_int(item.get('calls'))} 次)"
        for item in unpriced[:8]
        if isinstance(item, dict)
    ]
    coverage = data.get("coverage")
    detail = (
        f"coverage={coverage}，total_models={data.get('total_models')}，"
        f"unpriced_models={data.get('unpriced_models')}"
    )
    if listed:
        detail += "，未定价：" + "、".join(listed)
        if len(unpriced) > len(listed):
            detail += f"（另有 {len(unpriced) - len(listed)} 个）"
    return Check(
        id="c2",
        title=title,
        status=INFO,
        detail=detail,
        hint="补价格入口：" + str(data.get("prices_entry_url") or "/billing/prices"),
        evidence={
            "coverage": coverage,
            "total_models": data.get("total_models"),
            "unpriced_models": data.get("unpriced_models"),
            "unpriced": [item.get("model") for item in unpriced if isinstance(item, dict)][:20],
        },
    )


def judge_shares(result: HttpResult) -> tuple[Check, str]:
    """d 前半：取一条有效分享。没有就 SKIP —— "没有分享"不等于"分享坏了"。"""
    title = "分享链接（/v1/shares）"
    hint = (
        "分享行由 internal/api/share.go 写入 conversation_shares（user_id + 未撤销 + 未过期）；"
        "列表为空时先在界面里分享一次会话"
    )
    if not result.ok:
        return (
            Check(id="d", title=title, status=FAIL, detail=_http_failure(result, "端点 404 ⇒ 路由未注册"), hint=hint),
            "",
        )
    data = envelope_data(result.payload)
    items = data.get("items") if isinstance(data, dict) else None
    items = items if isinstance(items, list) else []
    if not items:
        return (
            Check(
                id="d",
                title=title,
                status=SKIP,
                detail="没有有效分享，跳过匿名访问页断言",
                hint="要覆盖这一项：在界面里分享一条会话后重跑",
            ),
            "",
        )
    first = items[0] if isinstance(items[0], dict) else {}
    token = str(first.get("share_id") or "").strip()
    detail = f"共 {len(items)} 条，取样 share_id={token[:8]}…" if token else f"共 {len(items)} 条但首条缺 share_id"
    return (
        Check(
            id="d",
            title=title,
            status=PASS if token else FAIL,
            detail=detail,
            hint="" if token else "列表项缺 share_id，前端取不到可打开的链接（internal/api/share.go::List）",
            evidence={"count": len(items), "sample_share_id_prefix": token[:8]},
        ),
        token,
    )


def judge_share_page(result: HttpResult) -> Check:
    """d 后半：匿名打开 `/s/<token>`，必须是 200 且含 SPA 挂载点。

    这一项验的是"静态托管 + SPA 兜底"这条**打包物**链路（`/s/` 是客户端路由，
    网关回 index.html 由它接管），而不是分享内容本身 —— 内容有独立的 JSON 端点
    `GET /v1/share/{id}`，本脚本不做（它需要真实分享数据才能命中）。
    """
    title = "分享页（匿名 GET /s/<token>）"
    if not result.ok:
        return Check(
            id="d2",
            title=title,
            status=FAIL,
            detail=_http_failure(result, "404 常见于未托管前端静态物"),
            hint=(
                "看 internal/api/web_static.go（WEB_ROOT/静态目录）与打包产物是否含 index.html；"
                "网关日志里 `SPA hosting enabled` 一行是判据"
            ),
        )
    body = result.text or ""
    marker = next((m for m in SPA_MARKERS if m in body), "")
    if not marker:
        return Check(
            id="d2",
            title=title,
            status=FAIL,
            detail=f"HTTP {result.status} 但响应里没有 SPA 挂载点（前 120 字符：{preview(body, 120)}）",
            hint="静态目录指错了（可能回了目录列表或别的页面）；对照 frontend-vue/index.html 的 <div id=\"app\">",
        )
    return Check(
        id="d2",
        title=title,
        status=PASS,
        detail=f"HTTP {result.status}，含 {marker}；响应长度 {len(body)}",
        evidence={"marker": marker, "bytes": len(body)},
    )


def judge_subagent_runs(result: HttpResult) -> Check:
    """e 前半：`/v1/subagent/runs` 路由在册。**无运行记录是 SKIP，不是 FAIL**。

    实际派发需要用户在界面里触发一次子 Agent —— 脚本不做这件事（那要真调模型、
    真要花 token）。这里只证明"路由没缺、鉴权没挡"。
    """
    title = "子 Agent 路由（/v1/subagent/runs）"
    if result.status == 404:
        return Check(
            id="e",
            title=title,
            status=FAIL,
            detail="HTTP 404：路由未注册",
            hint="internal/api/routes_system.go 里的 subagent runs 组；网关版本可能不含该功能",
        )
    if result.error:
        return Check(
            id="e",
            title=title,
            status=FAIL,
            detail="请求失败：" + preview(result.error),
            hint="看网关日志与 /ready",
        )
    data = envelope_data(result.payload)
    runs = data.get("runs") if isinstance(data, dict) else None
    runs = runs if isinstance(runs, list) else []
    return Check(
        id="e",
        title=title,
        status=SKIP if not runs else PASS,
        detail=(
            "路由在册，无历史 run（实际派发需用户在界面触发一次子 Agent）"
            if not runs
            else f"路由在册，历史 run {len(runs)} 条（source={data.get('source') if isinstance(data, dict) else '未知'}）"
        ),
        hint="" if runs else "要覆盖派发链路：在界面里让主 Agent 派一个子 Agent，再重跑本脚本",
        evidence={"runs": len(runs)},
    )


def judge_models(result: HttpResult, default_model: str = "") -> Check:
    """e 后半：模型列表非空 + 报默认模型（派发链路的下游前提）。"""
    title = "模型列表（/v1/models）"
    if not result.ok:
        return Check(
            id="e2",
            title=title,
            status=FAIL,
            detail=_http_failure(result, "取决于库与 provider 配置"),
            hint="internal/api/model_discovery.go::ListModelsForUser；库里 llm_models 需有 enabled 行",
        )
    data = envelope_data(result.payload)
    models = data.get("models") if isinstance(data, dict) else None
    models = models if isinstance(models, list) else []
    names = [str(m.get("name") or "") for m in models if isinstance(m, dict)]
    detail = f"models={len(models)}，default_model={default_model or '未设置（引擎兜底 deepseek-chat）'}"
    if names[:3]:
        detail += "，示例：" + "、".join(names[:3])
    evidence = {"count": len(models), "default_model": default_model, "sample": names[:10]}
    if not models:
        return Check(
            id="e2",
            title=title,
            status=FAIL,
            detail=detail,
            hint=(
                "一个模型都没有 ⇒ 对话必然起不来：看「模型」页与 GET /v1/admin/models，"
                "确认 provider key 可用、llm_models 有 enabled 行"
            ),
            evidence=evidence,
        )
    return Check(id="e2", title=title, status=PASS, detail=detail, evidence=evidence)


def judge_diagnostics(result: HttpResult) -> Check:
    """f：诊断摘要。只报告 `checks[].status`，不改写退出码。

    顺手抓一个真实坑：`/v1/admin/` 在脱敏白名单里，而 `key` 是**子串**匹配的
    敏感字段名 ⇒ `checks[].key` 被改写成 `********`，前端再按 key 映射图标/跳转
    就会全部落到兜底分支。脚本把这件事显式写出来（不改状态：它不影响"重建是否
    可用"，但影响诊断页的正确性）。
    """
    title = "诊断摘要（/v1/admin/diagnostics/summary）"
    if not result.ok:
        return Check(
            id="f",
            title=title,
            status=INFO,
            detail="读不到：" + _http_failure(result, "端点 404 ⇒ 网关版本不含 diagnostics_summary.go"),
            hint="internal/api/diagnostics_summary.go；引擎不可达时各项应为 unknown 而不是 500",
        )
    data = envelope_data(result.payload)
    data = data if isinstance(data, dict) else {}
    checks = data.get("checks") if isinstance(data.get("checks"), list) else []
    rows, masked = [], []
    for item in checks:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        label = str(item.get("title") or key or "?")
        rows.append(f"{label}={item.get('status')}")
        if key and set(key) == {"*"}:
            masked.append(label)
    summary = "，".join(rows) if rows else "（无检查项）"
    detail = f"整体 status={data.get('status')}，counts={data.get('counts')}；{summary}"
    notes = []
    if masked:
        notes.append(
            "响应脱敏中间件把 checks[].key 改成了 ********（internal/api/sanitize_response.go 的 "
            "SensitiveFields 含 'key' 子串匹配，/v1/admin/ 在脱敏白名单内）⇒ 前端无法按 key 映射图标/跳转"
        )
    worst = str(data.get("status") or "")
    if worst in ("warn", "error"):
        notes.append(
            f"诊断整体为 {worst}：逐项结论见上（warn/unknown 多为「没配」或「看不见」，不等于重建坏了）"
        )
    return Check(
        id="f",
        title=title,
        status=INFO,
        detail=detail,
        evidence={
            "status": worst,
            "counts": data.get("counts"),
            "checks": [
                {"title": c.get("title"), "key": c.get("key"), "status": c.get("status")}
                for c in checks
                if isinstance(c, dict)
            ],
        },
        notes=notes,
    )


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ── 汇总 / 渲染 ────────────────────────────────────────────────────────


def summarize(checks: list[Check]) -> dict[str, int]:
    """四态计数。`PASS/FAIL/SKIP` 是任务要的汇总口径，`INFO` 单列（只提示）。"""
    counts = {status: 0 for status in _STATUS_ORDER}
    for check in checks:
        counts[check.status if check.status in counts else INFO] += 1
    return counts


def exit_code(checks: list[Check]) -> int:
    """只有 FAIL 让脚本失败；SKIP/INFO 不影响（"没条件验"不是"坏了"）。"""
    return 1 if any(c.status == FAIL for c in checks) else 0


def build_report(checks: list[Check], meta: dict[str, Any]) -> dict[str, Any]:
    """组装机器可读报告。**不含 token**（meta 由调用方保证只放端口/路径）。"""
    counts = summarize(checks)
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "ok": exit_code(checks) == 0,
        "counts": counts,
        "meta": meta,
        "checks": [c.as_dict() for c in checks],
    }


def render_text(report: dict[str, Any]) -> str:
    """人读视图：一行一条结论，FAIL/SKIP 下挂 hint（"下一步查哪里"）。"""
    lines: list[str] = []
    meta = report.get("meta") or {}
    lines.append("重建后自动验收（本地模式）")
    lines.append(
        f"  base_url={meta.get('base_url', '')}  runtime-state={meta.get('runtime_state_path', '')}"
    )
    if meta.get("fatal"):
        lines.append(f"  致命：{meta['fatal']}")
    lines.append("")
    width = max([len(c["id"]) for c in report["checks"]] + [2])
    for check in report["checks"]:
        lines.append(f"[{check['status']:<4}] {check['id']:<{width}} {check['title']}")
        if check.get("detail"):
            lines.append(f"         {check['detail']}")
        if check.get("hint") and check["status"] in (FAIL, SKIP):
            lines.append(f"         next: {check['hint']}")
        for note in check.get("notes") or []:
            lines.append(f"         note: {note}")
    counts = report["counts"]
    lines.append("")
    lines.append(
        f"汇总：PASS={counts.get(PASS, 0)}  FAIL={counts.get(FAIL, 0)}  "
        f"SKIP={counts.get(SKIP, 0)}  INFO={counts.get(INFO, 0)}"
    )
    failed = [f"{c['id']} {c['title']}" for c in report["checks"] if c["status"] == FAIL]
    if failed:
        lines.append("FAIL 项：" + "、".join(failed))
    lines.append("结论：" + _verdict(report))
    return "\n".join(lines)


def _verdict(report: dict[str, Any]) -> str:
    """"失败"要分两种说：环境没起来 ≠ 这一版重建坏了。

    混为一谈的后果是运维拿着"重建不可验收"去回滚一个其实没问题的版本，或者
    反过来对着一个坏版本重试"是不是应用没启动"。
    """
    counts = report.get("counts") or {}
    if counts.get(FAIL, 0) == 0:
        return "通过（无 FAIL）"
    kind = str((report.get("meta") or {}).get("fatal_kind") or "")
    if kind in ("unreachable", "session", "state"):
        return "环境不具备：先让桌面应用正常起来（不是重建缺陷，退出码 2）"
    return "存在 FAIL，重建不可验收"


# ── 编排（IO 全部走可注入的 transport）───────────────────────────────────

#: transport 的形状：(Request, timeout) -> HttpResult。测试注入假实现即可全程离线。
Transport = Callable[[Request, float], HttpResult]


def urllib_transport(request: Request, timeout: float) -> HttpResult:
    """默认 transport：stdlib urllib（脚本要能在裸 Python 上跑，不引第三方依赖）。"""
    req = urllib.request.Request(
        request.url, data=request.body, headers=request.headers, method=request.method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return HttpResult(status=None, error=f"{type(exc).__name__}: {reason}")
    text = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    return HttpResult(status=status, payload=payload, text=text)


def _call(transport: Transport, request: Request, timeout: float) -> HttpResult:
    try:
        return transport(request, timeout)
    except Exception as exc:  # noqa: BLE001 - 任何 transport 异常都要变成一条 FAIL，而不是栈回溯
        return HttpResult(status=None, error=f"{type(exc).__name__}: {exc}")


def run_checks(
    transport: Transport,
    state: dict[str, Any],
    base_url: str,
    timeout: float = DEFAULT_TIMEOUT,
    require_restricted: bool = True,
) -> tuple[list[Check], dict[str, Any]]:
    """按依赖顺序跑完全部检查。返回 (checks, meta)；不抛异常（失败都变成 Check）。"""
    meta: dict[str, Any] = {
        "runtime_state_path": state.get("path", ""),
        "gateway_port": state.get("gatewayPort"),
        "engine_port": state.get("enginePort"),
        "base_url": base_url,
    }
    checks: list[Check] = [judge_state(state)]

    ready = _call(transport, build_request(base_url, "/ready"), min(timeout, 10.0))
    checks.append(judge_ready(ready))
    meta["ready"] = ready.status
    if not ready.ok:
        # 两种"没就绪"要分开：连不上 = 应用没启动（环境问题，退出码 2）；
        # 连得上但 5xx/依赖 down = 这一版重建真的坏了（退出码 1）。
        meta["fatal_kind"] = "unreachable" if ready.status is None else "unhealthy"
        meta["fatal"] = (
            "网关端口连不上：桌面应用没在运行，或 runtime-state.json 已过期"
            if ready.status is None
            else "本地栈起来了但 /ready 未通过：这是重建缺陷，不是环境问题"
        )
        return checks, meta

    session_result = _call(transport, build_session_request(base_url), timeout)
    checks.append(judge_session(session_result))
    token = pick_token(session_result.payload) if session_result.ok else ""
    if not token:
        meta["fatal_kind"] = "session"
        meta["fatal"] = "换不到 owner 会话：后续断言无意义（鉴权/loopback 闸门先修）"
        return checks, meta
    secrets = (token,)

    deps = _call(transport, build_request(base_url, "/v1/admin/deps/list", token=token), max(timeout, DEPS_TIMEOUT))
    checks.append(judge_deps(deps))

    isolation = _call(transport, build_request(base_url, "/v1/system/isolation", token=token), timeout)
    checks.append(judge_isolation(isolation, require_restricted=require_restricted))

    (usage_check, coverage_check) = _judge_billing(transport, base_url, token, timeout)
    checks.extend([usage_check, coverage_check])

    checks.extend(_judge_shares(transport, base_url, token, timeout))
    checks.extend(_judge_subagent(transport, base_url, token, timeout))
    checks.append(judge_diagnostics(
        _call(transport, build_request(base_url, "/v1/admin/diagnostics/summary", token=token), max(timeout, 60.0))
    ))

    for check in checks:
        _scrub(check, secrets)
    return checks, meta


def _judge_billing(
    transport: Transport, base_url: str, token: str, timeout: float
) -> tuple[Check, Check]:
    """计费两项。用量端点带 `range=90d`（现行契约），另附 `days=90` 兼容旧参数。"""
    usage = _call(
        transport,
        build_request(base_url, "/v1/billing/usage?range=90d&days=90", token=token),
        timeout,
    )
    coverage = _call(
        transport, build_request(base_url, "/v1/admin/billing/price-coverage", token=token), timeout
    )
    return judge_usage(usage), judge_price_coverage(coverage)


def _judge_shares(
    transport: Transport, base_url: str, token: str, timeout: float
) -> list[Check]:
    """分享两项：先列，再匿名打开取样的那一条（没有可用的就 SKIP）。"""
    listed = _call(transport, build_request(base_url, "/v1/shares", token=token), timeout)
    list_check, share_token = judge_shares(listed)
    out = [list_check]
    if not share_token:
        return out
    page = _call(transport, build_page_request(base_url, share_token), timeout)
    out.append(judge_share_page(page))
    return out


def _judge_subagent(
    transport: Transport, base_url: str, token: str, timeout: float
) -> list[Check]:
    """子 Agent 两项：路由在册（无 run 则 SKIP）+ 模型列表非空（报默认模型）。"""
    probe_session = "verify-no-such-session"
    runs = _call(
        transport,
        build_request(base_url, f"/v1/subagent/runs?session_id={probe_session}", token=token),
        timeout,
    )
    out = [judge_subagent_runs(runs)]

    models = _call(transport, build_request(base_url, "/v1/models", token=token), timeout)
    default_model = ""
    settings = _call(
        transport, build_request(base_url, "/v1/admin/settings?category=python", token=token), timeout
    )
    if settings.ok:
        default_model = pick_default_model(settings.payload)
    out.append(judge_models(models, default_model))
    return out


def _scrub(check: Check, secrets: Any) -> None:
    """最后一道：把凭据从任何会被打印的字符串里清掉（就地改写）。"""
    check.detail = redact(check.detail, secrets)
    check.hint = redact(check.hint, secrets)
    check.notes = [redact(n, secrets) for n in check.notes]
    for key, value in list(check.evidence.items()):
        if isinstance(value, str):
            check.evidence[key] = redact(value, secrets)


# ── CLI ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify-after-build",
        description="重建后自动验收：对着已启动的桌面本地栈逐项断言（P0-1）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码：0=无 FAIL，1=有 FAIL，2=环境不具备（状态文件/就绪/会话）。\n"
            "token 只活在内存里，任何输出都不会打印它。"
        ),
    )
    parser.add_argument("--runtime-state", metavar="PATH", help="runtime-state.json 路径（默认从 APPDATA 推）")
    parser.add_argument("--base-url", metavar="URL", help="直接指定网关基址，跳过状态文件的端口解析")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON（stdout 只有 JSON）")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"单请求超时秒（默认 {DEFAULT_TIMEOUT:g}）")
    parser.add_argument(
        "--ignore-restricted",
        action="store_true",
        help="本机不打算启用 restricted 档时把该项降为 INFO（默认按任务要求给 FAIL）",
    )
    return parser


def _utf8_stdout() -> None:
    """Windows 控制台默认 cp936，直接打印服务端中文会变成乱码或抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(
    argv: list[str] | None = None,
    transport: Transport | None = None,
    env: dict[str, str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    _utf8_stdout()
    transport = transport or urllib_transport

    def emit(report: dict[str, Any], code: int) -> int:
        if args.json:
            text = json.dumps(report, ensure_ascii=False, indent=2)
        else:
            text = render_text(report)
        print(text)
        return code

    state_path = resolve_runtime_state_path(args.runtime_state, env)
    try:
        state = load_runtime_state(state_path)
    except StateError as exc:
        report = build_report(
            [Check(id="0a", title="runtime-state 可读", status=FAIL, detail=str(exc), hint="启动桌面应用后重跑")],
            {"runtime_state_path": state_path, "fatal": str(exc), "fatal_kind": "state", "base_url": args.base_url or ""},
        )
        return emit(report, 2)

    base_url = str(args.base_url or "").strip() or build_base_url(state["gatewayPort"])
    checks, meta = run_checks(
        transport,
        state,
        base_url,
        timeout=args.timeout,
        require_restricted=not args.ignore_restricted,
    )
    report = build_report(checks, meta)
    return emit(report, _exit_for(checks, meta))


def _exit_for(checks: list[Check], meta: dict[str, Any]) -> int:
    """退出码：环境不具备优先于断言失败，但"栈起来了却不健康"仍算断言失败。

    `fatal_kind=unhealthy` 是**刻意**的例外：`/ready` 连通但依赖 down，说明端口上
    确实是这一版重建的栈，那就是重建缺陷（退出码 1）；只有"连不上"与"换不到会话"
    才让脚本以 2 退出，避免 CI 把"应用没启动"读成"重建坏了"。
    """
    if meta.get("fatal_kind") in ("unreachable", "session"):
        return 2
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())
