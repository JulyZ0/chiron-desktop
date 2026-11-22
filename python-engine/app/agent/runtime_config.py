"""Agent 行为配置：后台「全局配置 → Agent 运行配置」的运行时入口。

## 为什么需要这一层

后台要能调整 agent 的**行为策略**（计划要不要先给你看、没人回答等多久、每做完一步
要不要说一句、连续多久没进展就提醒、输入 token 上限按窗口放大几倍）。这些值此前
要么硬编码在引擎里（``LoopGuard`` 的阈值、``scale_to_window`` 的倍数），要么只能靠
env 在**启动时**确定（``AGENT_INTERACTION_WAIT_SECONDS``）—— 改后台对运行中的引擎无效，
表现为"页面上是新值、行为还是旧的"。

本模块让同一份配置经两条路径进入进程：

1. **启动补拉**（``hydrate``）：引擎往往比网关先起来（桌面态 ``local-stack.cjs``），
   所以启动时网关可能还不可达 —— 允许失败并沿用 env 兜底，等 Redis 订阅连上后再补拉
   一次，把"重启后配置漂移"收敛掉。
2. **Redis 广播**（``listen_for_changes``）：后台点保存即热更，不需要重启。

这与 ``app/knowledge/runtime.py``、``app/media/reload.py`` 是**同一套范式**（频道名、
订阅循环、退避重连、坏配置保持现状），刻意不另造一套。

## 两条纪律

1. **坏值保持现状，绝不把正在服务的配置打掉**：一次写错的配置只该"没生效"，不该让
   所有会话改用另一个极端策略。因此每个键都过 ``_coerce_*``，认不出就跳过该键。
2. **默认值逐字保持不变**：``narration=off`` / ``plan_approval=never`` 时渲染空串，
   于是 system 段与引入本模块之前逐字相同（前缀缓存照常命中，行为零变化）。

## 消费方（改这里必须同步看它们）

| 键 | 消费点 |
|---|---|
| ``plan_approval`` | ``AgentRuntime.run`` 的 ``todo_write`` 分支 |
| ``interaction_wait_seconds`` | ``runtime._interaction_wait_seconds()`` |
| ``narration`` | ``runtime_config.render_narration_suffix()`` → system 段 |
| ``no_progress_notice`` | ``AgentRuntime.run`` 的 loop guard 命中处 |
| ``no_progress_threshold`` | ``AgentRuntime`` 构造 ``LoopGuard`` 时 |
| ``token_window_ratio`` | ``TaskBudget.scale_to_window()`` 调用处 |
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

#: ⚠️ 频道名必须与 Go 侧 `internal/api/settings_broadcast.go` 的 `settingsChangedChannel()`
#: 逐字相同（两侧各自经 key 前缀函数加 `REDIS_KEY_PREFIX`）。
SETTINGS_CHANGED_CHANNEL = "chiron:settings:changed"

#: 本模块只处理这个分类，其余与本监听无关。
AGENT_CATEGORY = "agent"

#: 订阅断开后的重连退避（秒），与 knowledge / media / harness 同一范式。
RECONNECT_DELAY_SECONDS = 5.0

#: 计划批准策略。
PLAN_APPROVAL_NEVER = "never"
PLAN_APPROVAL_ALWAYS = "always"
PLAN_APPROVALS: tuple[str, ...] = (PLAN_APPROVAL_NEVER, PLAN_APPROVAL_ALWAYS)

#: 回合间叙述详略。
NARRATION_OFF = "off"
NARRATION_BRIEF = "brief"
NARRATION_DETAILED = "detailed"
NARRATIONS: tuple[str, ...] = (NARRATION_OFF, NARRATION_BRIEF, NARRATION_DETAILED)

#: 配置键 → Settings 字段。**只收这里声明的键**：一次坏配置不该把任意字段写进 settings。
_FIELD_MAP: dict[str, str] = {
    "plan_approval": "agent_plan_approval",
    "interaction_wait_seconds": "agent_interaction_wait_seconds",
    "narration": "agent_narration",
    "no_progress_notice": "agent_no_progress_notice",
    "no_progress_threshold": "agent_no_progress_threshold",
    "token_window_ratio": "agent_token_window_ratio",
}


def _coerce_choice(raw: object, allowed: tuple[str, ...]) -> str | None:
    """归一化成白名单里的取值；不认识返回 None（调用方跳过该键）。"""
    value = str(raw or "").strip().lower()
    return value if value in allowed else None


def _coerce_bool(raw: object) -> bool | None:
    """接受 bool / "true"/"false" / 1/0；其余返回 None。"""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw or "").strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return None


def _coerce_int(raw: object, *, lo: int, hi: int) -> int | None:
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value < lo or value > hi:
        return None
    return value


def _coerce_float(raw: object, *, lo: float, hi: float) -> float | None:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value < lo or value > hi:
        return None
    return value


def apply(config: dict[str, Any] | None) -> bool:
    """把后台配置写回 ``settings``；返回是否至少采纳了一项。

    每个键独立校验、独立采纳：一个键写错**只丢该键**，其余照常生效 —— 整份拒绝会让
    用户"只改了一格却什么都没生效"，比只丢一格更难排查。
    """
    if not isinstance(config, dict) or not config:
        return False

    applied: list[str] = []
    rejected: list[str] = []

    for key, field in _FIELD_MAP.items():
        if key not in config:
            continue
        raw = config[key]
        # 显式 null = 回落到 env 默认值（与 SaveSettings 的"value 为 null 即删除"同义）。
        if raw is None:
            continue

        value: Any = None
        if key == "plan_approval":
            value = _coerce_choice(raw, PLAN_APPROVALS)
        elif key == "narration":
            value = _coerce_choice(raw, NARRATIONS)
        elif key == "no_progress_notice":
            value = _coerce_bool(raw)
        elif key == "no_progress_threshold":
            # 0 = 关闭这条规则，所以下界是 0 而不是 1。
            value = _coerce_int(raw, lo=0, hi=100)
        elif key == "token_window_ratio":
            # 0 会让 `scale_to_window` 退回默认值（见该函数的 `ratio or DEFAULT`），
            # 因此下界从 0.5 起才算"真的设了一个倍数"。
            value = _coerce_float(raw, lo=0.5, hi=100.0)
        elif key == "interaction_wait_seconds":
            # 下界 1 秒：0/负数会变成"立刻放弃"，与 `_interaction_wait_seconds` 的下限一致。
            value = _coerce_float(raw, lo=1.0, hi=24 * 3600.0)

        if value is None:
            rejected.append(key)
            continue
        setattr(settings, field, value)
        applied.append(key)

    if rejected:
        # 只警告、不抛：配置通道不该让引擎拒收一次保存。
        logger.warning(
            "agent runtime config: rejected invalid values for %s (keeping current)",
            ", ".join(rejected),
        )
    if not applied:
        return False

    logger.info("agent runtime config applied: %s", ", ".join(applied))
    return True


def state() -> dict[str, Any]:
    """当前生效值（供后台页面显示"引擎采纳了什么"，与已保存值对照）。"""
    return {
        "plan_approval": str(getattr(settings, "agent_plan_approval", PLAN_APPROVAL_NEVER)),
        "interaction_wait_seconds": float(
            getattr(settings, "agent_interaction_wait_seconds", 240.0)
        ),
        "narration": str(getattr(settings, "agent_narration", NARRATION_OFF)),
        "no_progress_notice": bool(getattr(settings, "agent_no_progress_notice", True)),
        "no_progress_threshold": int(getattr(settings, "agent_no_progress_threshold", 3)),
        "token_window_ratio": float(getattr(settings, "agent_token_window_ratio", 8.0)),
    }


def plan_approval_required() -> bool:
    """本次是否需要"计划先给用户看"。"""
    return str(getattr(settings, "agent_plan_approval", PLAN_APPROVAL_NEVER)) == PLAN_APPROVAL_ALWAYS


def no_progress_notice_enabled() -> bool:
    return bool(getattr(settings, "agent_no_progress_notice", True))


def no_progress_threshold() -> int:
    try:
        return max(0, int(getattr(settings, "agent_no_progress_threshold", 3)))
    except (TypeError, ValueError):
        return 3


def token_window_ratio() -> float:
    try:
        ratio = float(getattr(settings, "agent_token_window_ratio", 8.0))
    except (TypeError, ValueError):
        ratio = 8.0
    return ratio if ratio > 0 else 8.0


#: 叙述详略 → 注入 system 段的指令。措辞刻意与 `planning_guidance` 同风格：
#: 只说"怎么做"，不重复 persona 与安全要求。``off`` 不在表里 ⇒ 渲染空串。
_NARRATION_INSTRUCTIONS: dict[str, str] = {
    NARRATION_BRIEF: (
        "Before each tool call, say in one short sentence what you are about to do. "
        "After a step finishes, say in one short sentence what it changed. Do not "
        "narrate routine reads, and do not restate the same sentence twice."
    ),
    NARRATION_DETAILED: (
        "Before each tool call, say what you are about to do and why it is the right "
        "next step. After it finishes, report what actually changed (files, outputs, "
        "what is now verified versus still assumed) and what comes next. Keep each "
        "note to a couple of sentences — cut filler, not facts."
    ),
}


def render_narration_suffix() -> str:
    """把当前叙述策略渲染成 system 段片段；``off`` 返回空串（零行为变化）。"""
    style = str(getattr(settings, "agent_narration", NARRATION_OFF))
    body = _NARRATION_INSTRUCTIONS.get(style, "")
    if not body:
        return ""
    return "## Progress narration\n\n" + body


# ── 下发通道 ────────────────────────────────────────────────────────────


def _hydrate_once() -> tuple[bool, bool]:
    """补拉一次配置：返回 ``(网关是否可达, 是否采纳了配置)``。

    区分"可达但没配置"与"不可达"是启动补拉需要的信息：桌面态引擎先于网关启动，
    不可达时应当继续重试；可达但没有记录则不必再试（配置本来就是空的）。
    """
    import json
    import urllib.request

    token = str(getattr(settings, "internal_token", "") or "")
    if not token:
        return False, False
    base = str(getattr(settings, "gateway_internal_url", "") or "").rstrip("/")
    if not base:
        return False, False
    url = f"{base}/v1/internal/agent-config"
    try:
        req = urllib.request.Request(url, headers={"X-Internal-Token": token})
        with urllib.request.urlopen(req, timeout=2) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 - 拉不到就用 env 兜底，不阻断
        logger.debug("agent runtime config hydrate failed: %s", exc)
        return False, False

    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    if not isinstance(data, dict) or not data:
        return True, False
    return True, apply(data)


def hydrate() -> bool:
    """从网关补拉一次 ``agent`` 分类配置（引擎启动 / 订阅重连时调用）。

    网关不可达、未配置 internal_token、或返回体不是 dict 时**返回 False 并保持现状** ——
    沿用 env 兜底，绝不因为读不到配置而把策略重置成默认值（那等于"网关一抖，配置归零"）。
    """
    return _hydrate_once()[1]


async def _catch_up_until_reachable() -> None:
    """网关可达之前持续补拉（桌面态引擎先于网关启动）。

    此前的"启动补拉"只做一次且在网关起不来时必然失败，之后只订阅广播 ——
    于是用户重启应用后，上一次保存的配置**不会**被重新拉取，行为退回默认值，
    必须再进后台点一次保存才生效。
    """
    import asyncio

    delay = 1.0
    while True:
        try:
            reached, _ = await asyncio.to_thread(_hydrate_once)
        except Exception as exc:  # noqa: BLE001 - 后台补拉失败只影响"恢复旧配置"
            logger.debug("agent runtime config catch-up failed: %s", exc)
            reached = False
        if reached:
            return
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 15.0)


async def listen_for_changes() -> None:
    """订阅设置变更广播：``agent`` 分类到达即热更行为配置。

    启动补拉与订阅**并行**：订阅只对**新消息**生效，而"上次保存的配置"不会有新消息；
    但引擎启动时网关往往还没起来（桌面态顺序：检索服务 → 引擎 → 网关），所以补拉
    做成"网关可达前持续重试"，而不是失败一次就放弃。
    """
    import asyncio
    import json

    from app.redis_keys import rkey

    catch_up = asyncio.create_task(_catch_up_until_reachable())
    channel = rkey(SETTINGS_CHANGED_CHANNEL)
    try:
        while True:
            pubsub = None
            try:
                from app.redis_client import aclose_pubsub, get_redis

                redis = await get_redis()
                if redis is None:
                    await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                    continue
                pubsub = redis.pubsub()
                await pubsub.subscribe(channel)
                logger.info("agent runtime config listener subscribed: %s", channel)
                # 断线重连期间可能错过一条广播：订阅成功后补拉一次（网关已在时立即收口）。
                try:
                    await asyncio.to_thread(_hydrate_once)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("agent runtime config rehydrate failed: %s", exc)
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    raw = message.get("data")
                    try:
                        payload = json.loads(raw if isinstance(raw, str) else str(raw))
                    except Exception:
                        logger.warning("agent settings broadcast unmarshal failed")
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("category") != AGENT_CATEGORY:
                        continue
                    cfg = payload.get("config")
                    apply(cfg if isinstance(cfg, dict) else None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 订阅断了要能自己回来
                logger.warning("agent runtime config listener error, retrying: %s", exc)
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
            finally:
                if pubsub is not None:
                    try:
                        await aclose_pubsub(pubsub)
                    except Exception:  # noqa: BLE001 - 关连接失败不该盖住上面的异常
                        pass
    finally:
        catch_up.cancel()


__all__ = [
    "AGENT_CATEGORY",
    "NARRATION_BRIEF",
    "NARRATION_DETAILED",
    "NARRATION_OFF",
    "NARRATIONS",
    "PLAN_APPROVAL_ALWAYS",
    "PLAN_APPROVAL_NEVER",
    "PLAN_APPROVALS",
    "apply",
    "hydrate",
    "listen_for_changes",
    "no_progress_notice_enabled",
    "no_progress_threshold",
    "plan_approval_required",
    "render_narration_suffix",
    "state",
    "token_window_ratio",
]
