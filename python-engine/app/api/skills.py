"""Skill API endpoints — CRUD + discover, backed by SkillStore.

## 多租户 / 用户级隔离 + 租户共享层（四级目录解析）

- 所有 handler 从 query 参数读取 `user_id` / `tenant_id`（由网关在代理时注入），
  每个请求构造带身份的 `SkillStore`（根目录解析见 app/skill/store.py）：
  读查找沿 `data/skills/{tenant_id}/{user_id}/` → `data/skills/{tenant_id}/_shared/`
  （租户共享层）→ `data/skills/_shared/`（全局共享）三级合并；两者都缺失
  （未登录 / 系统任务）时回退 `data/skills/_shared/`（首次访问自动迁移旧全局目录）。
- 写目标：默认 user 私有目录；`?scope=tenant` 时写租户共享层（需 tenant_id），
  `?scope=private` / 缺省时写 user 私有目录。
- `GET /v1/skills` 返回结构每个技能含 `source` 字段（user/tenant/shared），
  前端可据此显示“团队共享”徽标（source == 'tenant'）。
- 身份段非法（如 `../` 路径穿越）→ 400，拒绝构造 store。
- `run` handler 会把请求身份写入 tool context（app.tools.context），保证
  tools/skill.py 的 `skill_run` 执行链（含沙箱工具）落在同一身份目录下。
- 删除：`DELETE /v1/skills/{name}` 默认删 user 私有目录中的技能；带 `?scope=tenant` 时
  删**租户共享层**（团队资产，网关侧要求技能管理权限 `PermMarketManage`）。目标层里没有
  该技能时返回 404（不会"删了个空"却回 200）。

## P1-2：触发策略 / 来源 / 升级

- `GET /v1/skills` 的每个技能额外带 `auto_inject`（缺省 true）与 `origin`
  （`market:<source>/<id>` / `local` / `generated` / `builtin`；老数据空串）；
- `PATCH /v1/skills/{name}/meta` `{auto_inject}` —— 关掉后不进每轮 system 上下文，
  但仍可被 @ 选中或工具调用（过滤在 app/tools/skill_catalog.py）；
- `POST /v1/skills/{name}/upgrade` `{source_url?}` —— 只对市场来源生效：取源清单 → 比版本 →
  **原子覆盖**（见 app/skill/upgrade.py）。已最新回 200，不可升级回 409。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.skill.store import ORIGIN_LOCAL, SkillDef, SkillStore, validate_skill_name

router = APIRouter(tags=["skills"])


def _valid_scope(scope: str = "") -> str:
    """校验安装/注册目标 scope：空 / 'private' → user 私有；'tenant' → 租户共享。"""
    if scope in ("", "private"):
        return ""
    if scope == "tenant":
        return "tenant"
    raise HTTPException(
        status_code=400, detail="invalid scope: must be 'tenant' or 'private'"
    )


def _validate_skill_name(name: str) -> str:
    try:
        return validate_skill_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _save_skill(store: SkillStore, skill: SkillDef) -> None:
    try:
        store.save(skill)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _store_for(user_id: str = "", tenant_id: int = 0, scope: str = "") -> SkillStore:
    """按请求身份构造 store；身份非法时 400（防路径穿越）。"""
    try:
        return SkillStore(tenant_id=tenant_id, user_id=user_id, scope=scope)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid identity: {e}") from e


def _skill_payload(s: SkillDef) -> dict[str, Any]:
    """序列化技能并附带 source 标记（user/tenant/shared），供前端展示共享徽标。"""
    payload = s.to_dict()
    payload["source"] = s.scope
    return payload


@router.get("/v1/skills")
async def list_skills(user_id: str = "", tenant_id: int = 0) -> dict[str, Any]:
    """列出全部可用技能：合并 user 目录 + 租户 _shared + 全局 _shared（去重，user 优先）。"""
    store = _store_for(user_id, tenant_id)
    skills = store.list()
    return {
        "skills": [_skill_payload(s) for s in skills],
        "count": len(skills),
    }


class SkillInstallRequest(BaseModel):
    url: str = ""
    file: str = ""
    inline: str = ""
    #: P1-2：来源标记（`market:<source>/<id>` / `local` / `generated` / `builtin`）。
    #: 市场安装流水线（internal/api/market_install.go）显式传 `market:<source>/<id>`；
    #: 其它调用方缺省按 `local` 处理。
    origin: str = ""
    #: H5：内容摘要（inline 字节的 hex sha256）。市场安装流水线在网关侧校验完
    #: manifest 之后把摘要一并带下来，引擎再复核**收到的字节**没有被中间环节改写。
    sha256: str = ""
    #: H5：是否**强制**要求摘要。网关路径恒为 true（外部内容必须带摘要），
    #: 缺摘要直接拒绝且不落盘；老调用方缺省 false，行为与引入前完全一致。
    integrity_required: bool = False
    #: H75：是否**惰性安装**（`enabled=false` + `auto_inject=false`）。
    #: 市场安装流水线对外部内容恒传 true：装完不注入、不可运行，等用户显式启用。
    #: 老调用方缺省 false，行为与引入前完全一致。
    inert: bool = False


def _verify_inline_integrity(raw: bytes, req: "SkillInstallRequest") -> None:
    """复核 inline 字节的 sha256（H5 的第二道闸）。

    两种失败语义分开：

    * **缺摘要**：`integrity_required` 为真时是硬失败（发布方/网关路径必须带摘要），
      否则按老行为放行 —— 老调用方（本地导入、测试）不因为多了个字段就不能用；
    * **摘要不匹配**：无论是否强制，只要声明了摘要就必须对得上（声明的意思就是
      "我保证是这个内容"）。

    两种失败都在 `store.save` 之前抛出，因此**不落盘**。
    """
    declared = (req.sha256 or "").strip().lower()
    if not declared:
        if req.integrity_required:
            raise HTTPException(
                status_code=400,
                detail="skill install requires a sha256 integrity digest",
            )
        return
    if len(declared) != 64 or any(c not in "0123456789abcdef" for c in declared):
        raise HTTPException(
            status_code=400, detail="sha256 must be 64 hex characters"
        )
    actual = hashlib.sha256(raw).hexdigest()
    if not hmac.compare_digest(actual, declared):
        raise HTTPException(
            status_code=400,
            detail=(
                "skill install integrity check failed: sha256 mismatch "
                f"(declared {declared[:12]}…, got {actual[:12]}…)"
            ),
        )


@router.post("/v1/skills/{name}/register")
async def register_skill(
    name: str, user_id: str = "", tenant_id: int = 0, scope: str = ""
) -> dict[str, Any]:
    """将能力注册中心的技能注册为本地技能（SkillStore），供对话/Agent 工具链调用。

    前端 SkillMarketCard 调用 POST /v1/skills/{capabilityId}/register；
    能力注册中心查询不到时返回 404，避免假注册。
    scope=tenant 时注册进租户共享层（团队共享），缺省写 user 私有目录。
    """
    from app.core.capabilities import get_registry

    store = _store_for(user_id, tenant_id, _valid_scope(scope))
    name = _validate_skill_name(name)

    cap = await get_registry().get_by_id(name, tenant_id=tenant_id)
    if cap is None:
        raise HTTPException(status_code=404, detail=f"capability not found: {name}")

    existing = [s for s in store.list() if s.name == name]
    if existing:
        return {
            "success": True,
            "skill": name,
            "registered": True,
            "scope": store.write_scope,
        }

    skill = SkillDef(
        name=_validate_skill_name(cap.name or name),
        description=cap.description or f"Skill {name}",
        version="0.1.0",
        exec_type="prompt",
        source=f"Use the {name} capability. {cap.description or ''}".strip(),
    )
    _save_skill(store, skill)
    return {
        "success": True,
        "skill": skill.name,
        "registered": True,
        "description": skill.description,
        "scope": store.write_scope,
    }


@router.post("/v1/skills/install")
async def install_skill(
    body: SkillInstallRequest, user_id: str = "", tenant_id: int = 0, scope: str = ""
) -> dict[str, Any]:
    """安装技能（url / file / inline）；scope=tenant 时安装进租户共享层。"""
    store = _store_for(user_id, tenant_id, _valid_scope(scope))

    if not body.url and not body.file and not body.inline:
        raise HTTPException(status_code=400, detail="provide url, file, or inline")

    try:
        if body.inline:
            raw_inline = body.inline.encode("utf-8")
            # H5：先校验摘要，再解析内容 —— 校验失败时连 JSON 都不解析，更不会落盘。
            _verify_inline_integrity(raw_inline, body)
            data = json.loads(body.inline)
        elif body.file:
            from app.tools.core import _safe_path

            safe_file = _safe_path(body.file, str(store.root))
            data = json.loads(safe_file.read_text(encoding="utf-8"))
        else:
            from app.config import settings
            from app.http_client import shared_client

            async with shared_client(timeout=settings.http_timeout_default) as client:
                # SSRF 防护：目标 URL 完全由调用方控制，必须走解析级校验
                #（与 web_fetch / media / browser 等出站路径一致），否则可被用来
                # 探测内网服务与云元数据端点。
                from app.tools.ssrf import fetch_url_safe

                resp = await fetch_url_safe(client, body.url)
                resp.raise_for_status()
                data = resp.json()
    except HTTPException:
        # 完整性校验的拒绝必须原样透出（400 + 具体原因）：
        # 被下面那个兜底的 500 覆盖会让"内容不可信"看起来像"引擎坏了"。
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"failed to load skill definition: {e}"
        ) from e

    exec_cfg = data.get("exec", {})
    skill_name = _validate_skill_name(data.get("name", ""))
    # P1-2：来源标记。请求体优先，其次定义自带，最后按"本地安装"处理。
    # 用 `url` 安装时把安装地址记进 metadata.source_url —— 升级端点靠它找到源清单，
    # 否则用户装完就只能重装才能升级。
    origin = (body.origin or str(data.get("origin") or "")).strip() or ORIGIN_LOCAL
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    if body.url:
        metadata = {**metadata, "source_url": body.url}
    skill = SkillDef(
        name=skill_name,
        description=data.get("description", ""),
        version=data.get("version", "0.1.0"),
        author=data.get("author", ""),
        tags=data.get("tags", []),
        exec_type=exec_cfg.get("type", "prompt"),
        source=exec_cfg.get("source", ""),
        parameters=data.get("parameters", []),
        metadata=metadata,
        origin=origin,
    )
    if body.inert:
        # H75：外部内容装完**不注入、不执行**。两个开关一起关 ——
        # 只关 enabled 会让它继续占每轮 system 上下文，只关 auto_inject 会让它"看着装了却跑不了"，
        # 两种半开状态都让"未启用"这件事说不清。
        skill.enabled = False
        skill.auto_inject = False
    _save_skill(store, skill)
    payload = skill.to_dict()
    payload["source"] = store.write_scope
    return {
        "skill": payload,
        "message": f"Skill '{skill.name}' installed",
        "scope": store.write_scope,
        # 让调用方（市场页）能明确提示"装了但未启用"，而不是只看到安装成功。
        "enabled": skill.enabled,
    }


class SkillGenerateRequest(BaseModel):
    description: str
    auto_install: bool = False


@router.post("/v1/skills/generate")
async def generate_skill(
    body: SkillGenerateRequest, user_id: str = "", tenant_id: int = 0, scope: str = ""
) -> dict[str, Any]:
    """用 LLM 生成技能（D4）；`auto_install` 时落盘（`scope=tenant` 则落租户共享层）。

    与工具 `skill_generate` 走**同一条**实现（`app/skill/generate.py`）：校验不通过时返回
    400 + **具体原因**（前端据此提示 —— 笼统的"生成失败"没法排查）。
    """
    store = _store_for(user_id, tenant_id, _valid_scope(scope))

    from app.skill.generate import generate_skill_md

    outcome = await generate_skill_md(
        description=body.description,
        install=bool(body.auto_install),
        store=store if body.auto_install else None,
    )
    if not outcome.ok:
        raise HTTPException(status_code=400, detail=outcome.error)

    result: dict[str, Any] = {"skill": outcome.skill, "message": "Skill generated"}
    if body.auto_install:
        result["message"] = f"Skill '{outcome.name}' generated and installed"
        result["scope"] = store.write_scope
        result["path"] = outcome.path

    return result


@router.delete("/v1/skills/{name}")
async def delete_skill(
    name: str, user_id: str = "", tenant_id: int = 0, scope: str = ""
) -> dict[str, str]:
    """删除技能。

    `scope` 决定删**哪一层**（即 store 的写目标，见 `_store_for` 与 `store.write_scope`）：

    * 缺省 / `private`：调用者**私有**目录 —— 任何登录用户都能删自己的技能（既有行为不变）；
    * `tenant`：**租户共享层**（团队资产）。网关已在 `scope=tenant` 时校验技能管理权限
      （`internal/api/skill_handler.go` 的 proxyDelete → `PermMarketManage`）；
      引擎无鉴权能力，网关是唯一可信边界，故此处不重复判权。
    """
    if scope not in ("", "private", "tenant"):
        raise HTTPException(status_code=400, detail=f"invalid scope: {scope}")
    _validate_skill_name(name)
    store = _store_for(user_id, tenant_id, scope)

    found = store.get(name)
    if not found:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    # 关键：store.delete 只作用于**写目标目录**。若该技能是从别的层解析出来的
    # （如请求 scope=tenant 但它其实只在全局共享层），删除会是空操作 ——
    # 必须明确 404，而不是回 200 让调用方以为删掉了团队资产。
    if found.scope != store.write_scope:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Skill '{name}' is not in scope '{store.write_scope}' "
                f"(it resolves from '{found.scope}')"
            ),
        )

    store.delete(name)
    return {"message": f"Skill '{name}' deleted", "scope": store.write_scope}


class SkillToggleRequest(BaseModel):
    enabled: bool = True
    #: H75：启用/停用同时切注入策略（市场启用是"让这条内容真正可用"，
    #: 不是只把它从列表里去掉 disabled 标记）。缺省 None = 不动 injection 策略 ——
    #: 既有的 PUT /v1/skills/{name} 调用方行为完全不变。
    auto_inject: bool | None = None


class SkillMetaRequest(BaseModel):
    """PATCH /v1/skills/{name}/meta 的请求体（仅元数据，不含执行内容）。

    `auto_inject` 是唯一对外可改的字段：`origin` / `version` 由安装与升级维护，
    放开给前端改等于让"来源"变成用户可伪造的标签（分组/升级判定都会跟着错）。
    """

    auto_inject: bool | None = None


@router.patch("/v1/skills/{name}/meta")
async def patch_skill_meta(
    name: str, body: SkillMetaRequest, user_id: str = "", tenant_id: int = 0
) -> dict[str, Any]:
    """更新技能元数据（P1-2 的触发策略开关）。

    `auto_inject=false` ⇒ 该技能**不进每轮 system 上下文**（`skill_catalog` 过滤），
    但仍可被对话里显式 @（工作台选中）或工具（`skill_run` / `read_file`）使用。
    """
    store = _store_for(user_id, tenant_id)

    _validate_skill_name(name)
    if body.auto_inject is None:
        raise HTTPException(status_code=400, detail="nothing to update: provide auto_inject")

    skill = store.update_meta(name, auto_inject=body.auto_inject)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    return {
        "skill": _skill_payload(skill),
        "message": (
            f"Skill '{name}' will "
            + ("be injected into every turn" if skill.auto_inject else "not be auto-injected")
        ),
    }


class SkillUpgradeRequest(BaseModel):
    """POST /v1/skills/{name}/upgrade 的请求体。

    `source_url` 可省略：安装了地址（`metadata.source_url`）的技能会自动用它。
    需要显式给出时，它可以是一份**技能定义**（JSON 对象）或一份**源清单**（JSON 数组，
    按 name 命中）—— 后者正是"从市场目录检查更新"的形态。
    """

    source_url: str = ""


@router.post("/v1/skills/{name}/upgrade")
async def upgrade_skill_endpoint(
    name: str, body: SkillUpgradeRequest | None = None, user_id: str = "", tenant_id: int = 0
) -> dict[str, Any]:
    """升级市场安装的技能（比较版本 → 原子覆盖；失败不破坏旧版本）。

    状态码契约：

    * 200 `{upgraded: true, from_version, to_version, skill}` —— 升级成功；
    * 200 `{upgraded: false, reason: "already_latest", current_version, latest_version}`
      —— 已是最新（**不是错误**，前端据此显示"已是最新"）；
    * 409 —— 不可升级的**稳定原因**：非市场来源 / 没有源清单地址；
    * 502 —— 取源清单失败（网络 / SSRF 拒绝）；
    * 400 —— 远端定义非法（名字不符 / 缺字段）；
    * 404 —— 技能不存在。
    """
    store = _store_for(user_id, tenant_id)

    _validate_skill_name(name)
    if store.get(name) is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")

    from app.skill.upgrade import (
        REASON_ALREADY_LATEST,
        REASON_FETCH_FAILED,
        REASON_NAME_MISMATCH,
        REASON_NO_SOURCE,
        REASON_NOT_MARKET,
        upgrade_skill,
    )

    outcome = await upgrade_skill(
        store, name, source_url=(body.source_url if body else "")
    )

    if not outcome.ok:
        status = 409 if outcome.reason in (REASON_NOT_MARKET, REASON_NO_SOURCE) else 400
        if outcome.reason == REASON_FETCH_FAILED:
            status = 502
        elif outcome.reason == REASON_NAME_MISMATCH:
            status = 400
        raise HTTPException(
            status_code=status,
            detail={"reason": outcome.reason, "error": outcome.error},
        )

    if outcome.reason == REASON_ALREADY_LATEST:
        return {
            "upgraded": False,
            "reason": REASON_ALREADY_LATEST,
            "current_version": outcome.current,
            "latest_version": outcome.latest,
            "message": f"Skill '{name}' is already at the latest version ({outcome.latest})",
        }

    return {
        "upgraded": True,
        "from_version": outcome.current,
        "to_version": outcome.latest,
        "skill": {**outcome.skill, "source": store.write_scope},
        "message": f"Skill '{name}' upgraded {outcome.current} → {outcome.latest}",
    }


@router.put("/v1/skills/{name}")
async def toggle_skill(
    name: str, body: SkillToggleRequest, user_id: str = "", tenant_id: int = 0
) -> dict[str, Any]:
    """启用/停用技能（停用后不进 agent 目录、不可运行）。

    命中的技能可能来自租户/全局共享层；回写时落在 user 私有目录形成“本地覆盖”
    （仅影响当前用户，团队共享技能可被成员本地停用）。

    `auto_inject` 非 nil 时一起写回（H75：市场启用 = 可用 + 进注入）。为 nil 表示
    只切 `enabled` —— 既有调用方的行为与引入该字段前完全一致。
    """
    store = _store_for(user_id, tenant_id)

    _validate_skill_name(name)
    skill = store.get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    skill.enabled = body.enabled
    if body.auto_inject is not None:
        skill.auto_inject = bool(body.auto_inject)
    _save_skill(store, skill)
    return {
        "skill": _skill_payload(skill),
        "message": f"Skill '{name}' {'已启用' if body.enabled else '已停用'}",
    }


class SkillRunRequest(BaseModel):
    params: dict[str, Any] = {}


@router.post("/v1/skills/{name}/run")
async def run_skill(
    name: str, body: SkillRunRequest, user_id: str = "", tenant_id: int = 0
) -> dict[str, Any]:
    """运行技能（校验启用状态后调 skill_run）。

    把请求身份写入 tool context 再执行，保证 tools/skill.py 的 skill_run
    （及其内部沙箱/LLM 调用链）落在与校验相同的身份目录下；运行查找同样遵循
    user → 租户 _shared → 全局 _shared 四级规则。
    """
    store = _store_for(user_id, tenant_id)

    _validate_skill_name(name)
    skill = store.get(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    if not skill.enabled:
        raise HTTPException(status_code=400, detail=f"Skill '{name}' 已停用")

    import json as _json

    from app.tools.context import get_all, restore_context, set_tool_context
    from app.tools.skill import skill_run

    snapshot = get_all()
    set_tool_context(user_id=user_id, tenant_id=tenant_id)
    try:
        return await skill_run(name, _json.dumps(body.params))
    finally:
        restore_context(snapshot)


@router.get("/v1/skills/discover")
async def discover_skills(
    url: str = "", user_id: str = "", tenant_id: int = 0
) -> dict[str, Any]:
    """发现技能：`local` = 已安装，`remote` = 远端目录（给了 `url` 才有）。

    为什么两份都要返回：安装决策是**对比**出来的 —— 前端要知道"远端这个我装没装"
    （`installed`）以及"本地已经有的是哪些"。此前 url 分支把远端条目**塞进 `local`**、
    `remote` 恒为空，等于键名在说谎；只传 url 时本地列表还会整个消失。

    取回走 `app/tools/skill.py:_fetch_skill_json`（与 `skill_discover` / `skill_install`
    同一入口）：SSRF 解析级校验 + **流式**体积上限都在那里。此前本端点自己调
    `fetch_url_safe`，没有体积上限，同一个 `url` 在两个入口上策略不一致。
    """
    store = _store_for(user_id, tenant_id)

    local: list[dict[str, Any]] = [
        {
            "name": s.name,
            "description": s.description,
            "version": s.version,
            "author": s.author,
            "source": s.source,
            "installed": True,
        }
        for s in store.list()
    ]

    if not url:
        return {"local": local, "remote": []}

    from app.tools.skill import _fetch_skill_json

    try:
        payload = await _fetch_skill_json(url)
    except ValueError as e:
        # SSRF 校验不通过：这是**调用方**给错了 url，不是引擎故障。
        raise HTTPException(status_code=400, detail=f"discover remote rejected: {e}") from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"discover remote failed: {e}") from e

    if not isinstance(payload, list):
        raise HTTPException(
            status_code=400, detail="remote skill catalog must be a JSON array"
        )

    installed_names = {s.name for s in store.list()}
    remote: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            # 目录里混进非对象条目时跳过该条，不让一个坏条目毁掉整份目录。
            continue
        name = item.get("name", "")
        if not isinstance(name, str) or not name:
            continue
        # `source` 是**安装地址**，不是"这个条目从哪份目录来的"。
        #
        # 目录（数组）本身不能当安装地址 —— `POST /v1/skills/install {url}` 要求目标是
        # 单个技能定义（JSON 对象），拿目录 url 去装只会报 "must be a JSON object"。
        # 所以缺省留空（前端据此禁用安装按钮并说明原因），**不回填** `url`；
        # 目录出处另放 `catalog_url`，两个概念各归各的键。
        skill_url = item.get("source", "")
        remote.append(
            {
                "name": name,
                "description": item.get("description", ""),
                "version": item.get("version", ""),
                "author": item.get("author", ""),
                "source": skill_url if isinstance(skill_url, str) else "",
                "catalog_url": url,
                "installed": name in installed_names,
            }
        )

    return {"local": local, "remote": remote}
