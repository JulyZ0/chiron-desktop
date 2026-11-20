"""提权执行 helper 的线协议 —— 引擎侧与 helper 侧共用的**唯一事实源**。

## 为什么需要 helper

`restricted` 档要求 `CreateProcessAsUserW`，而它在非提权进程里恒 `err=5`
（缺 `SeIncreaseQuotaPrivilege`，本机实测确认）。把整个引擎提权是**反向**的做法：
引擎承载模型输出，爆炸半径最大，恰恰最该留在 Medium IL。

所以只提权一个"瘦"进程：它唯一的能力是**用受限令牌起进程**，自己从不直接
执行命令（见 `elevated_helper` 的模块注释）。即使引擎被完全攻破，攻击者拿到的
也只是"受限执行"通道 —— 隔离强度不降，这是整个设计的前提。

## 鉴权

共享密钥 32 字节，由**引擎侧**生成并写入受 DACL 保护的状态目录（见
`helper_state_dir`）。密钥不走命令行 —— 命令行对同机任意进程可见（`wmic
process get commandline`），那等于没有鉴权。

每个请求体都要带 HMAC-SHA256 签名，用 `hmac.compare_digest` 比对（防时序侧信道）。
签名失败**立刻断开**，不返回任何信息，也不记录请求内容 —— 失败的连接可能是探测。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

#: 协议版本。helper 与引擎版本不一致时**拒绝**而不是尽力解析：
#: 半懂不懂地解析一条"起进程"指令，比直接拒绝危险得多。
PROTOCOL_VERSION = 1

#: 请求/响应都以单行 JSON 传输，这里是单帧上限。
#:
#: 定得比 `launch_restricted` 的 `max_output` 默认值（1MB）宽一截，因为响应里要装
#: stdout + stderr 两份，且 JSON 转义会膨胀（换行→`\n`、非 ASCII 走 `\uXXXX`）。
MAX_FRAME_BYTES = 8 * 1024 * 1024

#: HMAC 头名。放在 HTTP 头而不是 JSON 体里，是为了让签名覆盖**整个**请求体 ——
#: 若把 signature 放进 JSON，签的就是"除自己以外的字段"，构造歧义空间。
SIGNATURE_HEADER = "X-Chiron-Helper-Signature"

#: helper 只监听环回地址。它提供的是**提权**执行能力，绝不能暴露在网络上。
LOOPBACK = "127.0.0.1"

#: 密钥长度（字节）。32 字节 = 256 位，与 HMAC-SHA256 的输出宽度匹配。
KEY_BYTES = 32

#: 单次连接的读写超时。helper 侧用它防"连上不发数据"的连接占满线程；
#: 引擎侧用它兜住 helper 卡死（卡死时必须能降级，不能把整轮对话拖住）。
SOCKET_TIMEOUT_SECONDS = 30.0

#: 健康探测（`ping`）的超时。比执行短得多 —— 探测只是为了判断"要不要走 helper"。
PING_TIMEOUT_SECONDS = 2.0


def helper_state_dir() -> Path:
    """helper 与引擎交换密钥/端口的目录。

    刻意放在 `%LOCALAPPDATA%` 而不是临时目录：临时目录在多用户机器上可能被
    其他账户预置同名文件（Windows 的 `%TEMP%` 是每用户独立的，但共享目录不是），
    而这里放的是**提权通道的密钥**，必须只有本用户可写。
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.getcwd()
    return Path(base) / "Chiron" / "isolation"


def key_path() -> Path:
    return helper_state_dir() / "helper.key"


def port_path() -> Path:
    return helper_state_dir() / "helper.port"


def sign(key: bytes, body: bytes) -> str:
    """对请求体算 HMAC-SHA256，十六进制小写。"""
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def verify(key: bytes, body: bytes, provided: str | None) -> bool:
    """校验签名。

    `compare_digest` 是必需的：`==` 的短路比较会把"前几位是否匹配"泄漏成
    时序差异，足以逐字节猜出签名。这里不省这一步。
    """
    if not provided:
        return False
    return hmac.compare_digest(sign(key, body), provided.strip().lower())


def encode_frame(payload: dict[str, Any]) -> bytes:
    """把一个请求/响应编码成单行 JSON 帧（含结尾换行）。"""
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def read_key(path: Path | None = None) -> bytes | None:
    """读共享密钥。不存在或长度不对都返回 `None`（调用方据此判"helper 不可用"）。"""
    target = path or key_path()
    try:
        raw = target.read_bytes()
    except OSError:
        return None
    if len(raw) < KEY_BYTES:
        return None
    return raw[:KEY_BYTES]


def write_key(key: bytes, path: Path | None = None) -> str:
    """**首次**写入共享密钥并尽量收紧 DACL。

    已存在时**不覆盖**：密钥一旦被换掉，已经在跑的 helper 会立刻鉴权失败，
    而它是个提权进程 —— 这种"悄悄换钥匙"的后果比留一把旧钥匙严重得多。
    返回空串表示成功，否则是失败原因。

    DACL 收紧失败**不算**致命 —— 密钥文件在每用户目录下，默认 ACL 已包含
    当前用户与 Administrators。但要把失败如实报出来，否则就是"以为收紧了、
    其实没有"。
    """
    target = path or key_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # `xb` = 独占创建：并发调用时只有一个真正写入，其余按"已存在"走下面。
        with target.open("xb") as handle:
            handle.write(key)
    except FileExistsError:
        return ""
    except OSError as exc:
        return f"写入密钥失败: {exc}"

    detail = ""
    try:
        from app.isolation import win

        ok, err = win.grant_current_user_full_control(str(target))
        if not ok:
            detail = f"密钥文件 DACL 未收紧: {err}"
    except Exception as exc:  # noqa: BLE001 — 收紧失败不影响可用性，但要留痕
        detail = f"密钥文件 DACL 未收紧: {exc}"
    return detail


__all__ = [
    "KEY_BYTES",
    "LOOPBACK",
    "MAX_FRAME_BYTES",
    "PING_TIMEOUT_SECONDS",
    "PROTOCOL_VERSION",
    "SIGNATURE_HEADER",
    "SOCKET_TIMEOUT_SECONDS",
    "encode_frame",
    "helper_state_dir",
    "key_path",
    "port_path",
    "read_key",
    "sign",
    "verify",
    "write_key",
]
