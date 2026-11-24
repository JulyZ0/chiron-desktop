"""迁移 DSN 的解析/脱敏/单次编码与 .env 加载（H32）。

``migrations/env.py`` 是 alembic 的入口脚本：import 它就会立刻跑迁移，因此无法直接
单测。这里放置**纯函数**，env.py 只负责把它们串起来，回归测试按文件路径加载本模块
断言行为（不连数据库）。

三条不变量（PRD 9.7 H32 验收）：

1. **显式环境变量优先**：``load_env_files`` 用 ``override=False``，``.env`` 只补齐
   未显式设置的值，绝不覆盖 ``DATABASE_DSN``（托管 PG 的迁移通道依赖这一点）。
2. **密码只编码一次**：``normalize_dsn`` 先 ``unquote`` 再 ``quote``，对"原始密码"
   与"已编码密码"都收敛到同一种写法；重复调用不再变化（幂等）。
3. **默认不输出 DSN**：日志只打印 ``redact_dsn`` 的脱敏串，异常文本经
   ``sanitize_text`` 抹掉密码的原始/编码两种形态。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查用
    pass

#: 脱敏后的占位符。集中一处，避免各处写出略有差异的掩码。
MASK = "***"

#: 短于此长度的"秘密"不参与替换：太短的串会把正常日志撕碎。
_MIN_SECRET_LEN = 4


def _split_userinfo(dsn: str) -> tuple[str, str, str] | None:
    """拆成 ``(scheme, userinfo, host)``；按**最后一个** ``@`` 定位 host。

    密码可能含 ``@`` 与 ``:``（如 ``p@ss:w0rd``），所以不能用第一个 ``@`` 切分：
    那样密码后半段会被当成 host 打进日志（凭据泄露）。
    """
    scheme, sep, rest = dsn.partition("://")
    if not sep or not scheme or not rest:
        return None
    userinfo, at, host = rest.rpartition("@")
    if not at or not userinfo or not host:
        return None
    return scheme, userinfo, host


def password_of(dsn: str) -> str:
    """取 DSN 里的原始密码片段（无则为空串）。用于异常文本脱敏。"""
    parts = _split_userinfo(dsn)
    if parts is None:
        return ""
    _scheme, userinfo, _host = parts
    _user, colon, password = userinfo.partition(":")
    return password if colon else ""


def redact_dsn(dsn: str | None) -> str:
    """把 DSN 变成可安全打印的形式：``scheme://user:***@host``。"""
    if not dsn:
        return "(none)"
    parts = _split_userinfo(dsn)
    if parts is None:
        # 解析不了就一个字符都不回显 —— "看不懂"不能变成"照原样打印"。
        return "(redacted)"
    scheme, userinfo, host = parts
    user, colon, _password = userinfo.partition(":")
    if not colon:
        return f"{scheme}://{userinfo}@{host}"
    return f"{scheme}://{user}:{MASK}@{host}"


def normalize_dsn(dsn: str) -> str:
    """统一 scheme 并把密码收敛到**恰好一次** URL 编码。

    - ``postgres://`` → ``postgresql://``（SQLAlchemy 只认后者）；
    - 密码 ``unquote`` 后再 ``quote``：原始 ``p@ss`` 与已编码 ``p%40ss`` 都得到
      ``p%40ss``，重复调用不再变化（旧实现会对 ``%`` 再编码成 ``%25``）。
    """
    value = (dsn or "").strip()
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://") :]
    parts = _split_userinfo(value)
    if parts is None:
        return value
    scheme, userinfo, host = parts
    user, colon, password = userinfo.partition(":")
    if not colon or not password:
        return value
    encoded = quote(unquote(password), safe="")
    return f"{scheme}://{user}:{encoded}@{host}"


def sanitize_text(text: str, *secrets: str) -> str:
    """抹掉文本里出现的秘密（同时覆盖原始与 URL 编码两种形态）。"""
    out = text or ""
    for secret in secrets:
        if not secret:
            continue
        variants = {secret, quote(secret, safe=""), unquote(secret), quote(secret)}
        for variant in sorted(
            (item for item in variants if len(item) >= _MIN_SECRET_LEN),
            key=len,
            reverse=True,
        ):
            out = out.replace(variant, MASK)
    return out


def target_line(dsn: str | None, head: str = "", source: str = "") -> str:
    """迁移前打印的一行摘要：脱敏目标 + migration head（+ 配置来源）。"""
    parts = [f"target={redact_dsn(dsn)}", f"head={head or '(unknown)'}"]
    if source:
        parts.append(f"source={source}")
    return "[Alembic] " + " ".join(parts)


def load_env_files(paths: Iterable[Path]) -> Path | None:
    """按顺序加载第一个存在的 ``.env``，**不覆盖**已显式设置的进程环境变量。

    返回实际加载的文件路径；都不存在时返回 None（调用方负责打印搜索路径）。
    """
    from dotenv import load_dotenv

    for path in paths:
        if path.exists():
            load_dotenv(path, override=False)
            return path
    return None
