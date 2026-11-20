"""用户级知识库设置：provider 选择与 Cloud key 密文。

**一个用户只能选一种 provider**（产品约束）：选 Cloud 必须配 key，选自托管则清掉 Cloud key。
不做"两个都留着、运行时再挑" —— 那样"为什么这次检索走了云"就没有答案。

密文复用 ``internal/settings`` 的 AEAD（见 ``app/knowledge/crypto.py``）；``APP_SECRET``
未配置时**拒绝保存密钥**，而不是用一把空密钥写进一份永远解不开的密文。

表有 ``user_id`` 主键（0023），所以写入用 ``ON CONFLICT`` 原子 upsert —— 这正是旧
``system_settings`` 做不到的（那张表没有唯一约束，并发写会各自成功并互相覆盖）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.knowledge import crypto
from app.knowledge.errors import KnowledgeError, ProviderUnavailableError
from app.knowledge.providers.base import (
    PROVIDER_CLOUD,
    PROVIDER_SELFHOSTED,
    STATUS_DISABLED,
    STATUS_ERROR,
    is_known_provider,
)
from app.tools.context import to_tenant_int

logger = logging.getLogger(__name__)


def builtin_service_url() -> str:
    """内置检索服务（pageindex-service）的地址；未部署/未启动时为空串。

    单独一个函数而不是各处 `getattr(settings, ...)`：判断"内置服务在不在"的口径
    必须只有一处，否则默认 provider、健康状态与提示文案会各说各话。
    """
    return str(getattr(settings, "knowledge_service_url", "") or "").strip()


def default_provider() -> str:
    """没有显式选择时使用的 provider。

    **内置服务可用即默认自托管**（需求：桌面/单机开箱即用）。没有内置服务时不猜 ——
    返回空串，由调用方给出"请先选择 provider"的可操作提示。
    """
    return PROVIDER_SELFHOSTED if builtin_service_url() else ""


def effective_provider_name(stored: "UserKnowledgeSettings | None") -> str:
    """用户设置 → 实际生效的 provider 名（未选择时回落到 :func:`default_provider`）。"""
    if stored is not None and stored.provider:
        return stored.provider
    return default_provider()


async def effective_provider(user_id: str) -> str:
    """读取用户设置并给出实际生效的 provider 名（不落库）。"""
    if not user_id:
        return ""
    return effective_provider_name(await load(user_id))


@dataclass(frozen=True)
class UserKnowledgeSettings:
    user_id: str
    tenant_id: int
    provider: str
    cloud_api_key: str = ""          # 解密后的明文，仅存在于内存
    cloud_folder_id: str = ""        # PageIndex Cloud 的用户级 folder（非安全边界，见迁移 0024）
    status: str = STATUS_DISABLED
    error_message: str = ""


async def _pool() -> Any:
    from app.db import get_pool

    return get_pool()


async def load(user_id: str) -> UserKnowledgeSettings | None:
    """读取并解密用户设置；密文解不开时返回 status=error（不抛异常）。"""
    if not user_id:
        return None
    pool = await _pool()
    row = await pool.fetchrow(
        """SELECT user_id, tenant_id, provider, cloud_api_key_enc, cloud_folder_id,
                  status, error_message
             FROM user_knowledge_settings WHERE user_id = $1""",
        user_id,
    )
    if row is None:
        return None

    encrypted = row["cloud_api_key_enc"] or ""
    api_key = ""
    status = str(row["status"] or STATUS_DISABLED)
    error_message = str(row["error_message"] or "")
    if encrypted:
        try:
            api_key = crypto.decrypt_secret(encrypted, settings.app_secret)
        except crypto.SecretCryptoError:
            # 主密钥轮换后旧密文解不开：这是**配置事故**，必须让它可见（状态标 error），
            # 而不是静默当成"没配 key"
            status = STATUS_ERROR
            error_message = "Cloud API key 无法解密（APP_SECRET 可能已变更），请重新填写"
            logger.warning("knowledge: cannot decrypt cloud key for user=%s", user_id)

    return UserKnowledgeSettings(
        user_id=str(row["user_id"]),
        tenant_id=to_tenant_int(row["tenant_id"]),
        provider=str(row["provider"] or ""),
        cloud_api_key=api_key,
        cloud_folder_id=str(row["cloud_folder_id"] or ""),
        status=status,
        error_message=error_message,
    )


async def save(
    *,
    user_id: str,
    tenant_id: int,
    provider: str,
    cloud_api_key: str | None = None,
) -> UserKnowledgeSettings:
    """保存 provider 选择（Cloud 时同时写入加密后的 key）。

    ``cloud_api_key`` 为 None 表示"沿用已存的 key"（改选 provider 之外的场景不强迫重填）；
    显式传空串表示"清空 key"。
    """
    # 延迟导入：`app.tools` 包会在 __init__ 里拉入知识库相关模块，
    # 顶部 import 会与本模块形成循环（ImportError: partially initialized module）。
    from app.tools.context import to_tenant_int

    if not user_id:
        raise KnowledgeError("user_id is required")
    if not is_known_provider(provider):
        raise KnowledgeError(f"unknown knowledge provider: {provider!r}")

    status = STATUS_DISABLED
    encrypted = ""
    fingerprint = ""

    if provider == PROVIDER_CLOUD:
        existing = await load(user_id)
        key = cloud_api_key if cloud_api_key is not None else (existing.cloud_api_key if existing else "")
        key = (key or "").strip()
        if not key:
            raise KnowledgeError("选择 Cloud 模式必须提供 PageIndex Cloud API key")
        if not crypto.encryption_available(settings.app_secret):
            raise ProviderUnavailableError("APP_SECRET 未配置，无法保存 Cloud API key")
        encrypted = crypto.encrypt_secret(key, settings.app_secret)
        fingerprint = crypto.key_fingerprint(key)
        status = "active"
    # 自托管：清空 key（二选一），状态留给健康检查决定（disabled/ready/error）

    pool = await _pool()
    await pool.execute(
        """INSERT INTO user_knowledge_settings
               (user_id, tenant_id, provider, cloud_api_key_enc, cloud_key_hash, status, updated_at)
           VALUES ($1, $2, $3, $4, $5, $6, NOW())
           ON CONFLICT (user_id) DO UPDATE
              SET tenant_id = EXCLUDED.tenant_id,
                  provider = EXCLUDED.provider,
                  cloud_api_key_enc = EXCLUDED.cloud_api_key_enc,
                  cloud_key_hash = EXCLUDED.cloud_key_hash,
                  status = EXCLUDED.status,
                  error_message = NULL,
                  -- folder 属于"那个账号"的空间：换了 provider/key 就必须重新建
                  cloud_folder_id = CASE
                      WHEN user_knowledge_settings.provider IS DISTINCT FROM EXCLUDED.provider
                          THEN NULL
                      ELSE user_knowledge_settings.cloud_folder_id
                  END,
                  updated_at = NOW()""",
        user_id,
        to_tenant_int(tenant_id),
        provider,
        encrypted or None,
        fingerprint or None,
        status,
    )
    logger.info("knowledge: user=%s provider=%s saved", user_id, provider)
    saved = await load(user_id)
    assert saved is not None  # 刚 upsert，必然读得到
    return saved


async def update_status(*, user_id: str, status: str, error_message: str = "") -> None:
    """回写探测结果（"测试连接"或后台巡检）。"""
    if not user_id:
        return
    pool = await _pool()
    await pool.execute(
        """UPDATE user_knowledge_settings
              SET status = $2, error_message = $3, last_checked_at = NOW(), updated_at = NOW()
            WHERE user_id = $1""",
        user_id,
        status,
        error_message or None,
    )


async def set_cloud_folder(*, user_id: str, folder_id: str) -> None:
    """记录用户的 Cloud 顶层 folder（首次索引建好后回写，之后不必再列取）。

    只写这一列：folder 与 key 属于同一个账号空间，改 key/provider 时由 ``save`` 清空，
    这里不做任何推断。
    """
    if not user_id or not folder_id:
        return
    pool = await _pool()
    await pool.execute(
        """UPDATE user_knowledge_settings
              SET cloud_folder_id = $2, updated_at = NOW()
            WHERE user_id = $1""",
        user_id,
        folder_id,
    )


# ── 逐库模型覆盖（P1-6：索引模型 / 问答模型可切换） ──
#
# 存哪里：**知识库行的 `config` jsonb 列**（已存在的列）。为什么不加列：迁移文件
# 本轮冻结（并行开发下固定 revision 链），而这项覆盖天然就是"逐库设置"—— 放在
# `user_knowledge_settings`（用户级）反而会强迫同一个用户的所有库共用一个模型。
#
# 缺省语义：键不存在 ⇒ 沿用网关 `/v1/internal/knowledge-model` 注入的默认模型
# （见 pageindex-service/app/models.py 的 env → gateway 解析）。**只存显式覆盖过的值**，
# 不把默认值抄一份进来 —— 抄进来之后"管理员改了默认模型"对所有库都不生效了。

#: 允许覆盖的模型字段（键名与 pageindex-service 的 ResolvedModels 逐字对齐）。
MODEL_OVERRIDE_KEYS: tuple[str, ...] = ("index_model", "chat_model")
#: 模型名的长度上限：正常名字几十字符，超过这个数基本是粘错内容了。
MAX_MODEL_NAME_LENGTH = 120


def normalize_model_name(raw: Any) -> str:
    """模型名收敛：去空白、拒绝换行/控制字符、限长。

    非法输入**抛 `KnowledgeError`** 而不是静默截断：这是显式配置项，用户写错时
    最该看到的是"这个名字不合法"，而不是"保存成功但检索时用了别的模型"。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if len(text) > MAX_MODEL_NAME_LENGTH:
        raise KnowledgeError(
            f"模型名过长（>{MAX_MODEL_NAME_LENGTH} 字符）：{text[:40]}…"
        )
    if any(ch.isspace() or ord(ch) < 32 for ch in text):
        raise KnowledgeError(f"模型名不能包含空白或控制字符：{text!r}")
    return text


def kb_model_overrides(config: Any) -> dict[str, str]:
    """从库的 config 里读出显式覆盖的模型（未覆盖的键**不出现**）。"""
    from app.knowledge.service import _as_dict

    data = _as_dict(config) if not isinstance(config, dict) else config
    out: dict[str, str] = {}
    for key in MODEL_OVERRIDE_KEYS:
        value = normalize_model_name(data.get(key) or "")
        if value:
            out[key] = value
    return out


async def save_kb_models(
    *, kb_id: str, index_model: str | None = None, chat_model: str | None = None
) -> dict[str, str]:
    """写逐库模型覆盖（``None`` = 不动该键，空串 = 清除覆盖回落到默认）。

    单条 SQL 完成"删键 + 并键"：先 ``- text[]`` 删掉要清除的键，再 ``|| jsonb`` 覆盖
    新值。读—改—写会与并发保存互相覆盖（两个标签页各存一个字段就会丢一个），
    而这条语句的语义是确定的。
    """
    updates: dict[str, str] = {}
    removals: list[str] = []
    if index_model is not None:
        value = normalize_model_name(index_model)
        if value:
            updates["index_model"] = value
        else:
            removals.append("index_model")
    if chat_model is not None:
        value = normalize_model_name(chat_model)
        if value:
            updates["chat_model"] = value
        else:
            removals.append("chat_model")
    if not updates and not removals:
        return kb_model_overrides((await _load_config(kb_id)))

    import json

    pool = await _pool()
    await pool.execute(
        """UPDATE knowledge_bases
              SET config = ((COALESCE(config, '{}'::jsonb) - $2::text[]) || $3::jsonb),
                  updated_at = NOW()
            WHERE id = $1""",
        kb_id,
        removals,
        json.dumps(updates, ensure_ascii=False),
    )
    logger.info("knowledge: kb=%s model overrides saved (%s)", kb_id, ",".join(updates) or "-")
    return kb_model_overrides(await _load_config(kb_id))


async def _load_config(kb_id: str) -> Any:
    pool = await _pool()
    return await pool.fetchval("SELECT config FROM knowledge_bases WHERE id = $1", kb_id)
