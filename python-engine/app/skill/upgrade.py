"""P1-2：技能版本升级（比较 → 取源清单 → **原子覆盖**）。

三条约束（对应评审要求）：

1. **只有市场来源可升级**：`origin` 必须是 `market:<source>/<id>`（见 `store.py` 的
   `ORIGIN_MARKET_PREFIX`）。本地 / 生成 / 内置技能没有"上游"，谎称能升级只会让用户
   等一个永远不来的新版本 —— 调用方据此回 409 并说明原因。
2. **必须先比版本**：远端不比本地新时**不做任何写入**，直接回「已最新」（200）。
   否则每次点"升级"都会重写文件、刷新 `installed_at`，看不出到底发生了什么。
3. **失败不破坏旧版本**：新定义先写同目录临时文件（`<name>.skill.json` 的临时兄弟），
   校验通过后 `os.replace` **原子替换**。任何一步失败都只留下临时文件，旧文件完好。

关于载体：升级产出的是**扁平**定义（`{root}/{name}.skill.json`），因为市场清单是 JSON
对象。若旧版本是目录型技能，新扁平定义会在读取时**优先命中**（`_iter_search` 里 json 先于
SKILL.md 产出）—— 效果就是"升级后以新版本为准"，旧目录仍在磁盘上但被遮住；这是**非破坏性**
的选择：升级出错时把扁平文件删掉就回到旧目录版本。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.skill.store import (
    ORIGIN_MARKET_PREFIX,
    SkillDef,
    SkillStore,
    _atomic_write_text,
    parse_market_origin,
)

logger = logging.getLogger(__name__)

#: 升级结果原因码（HTTP 层按它选状态码 / 前端按它显示文案）
REASON_ALREADY_LATEST = "already_latest"
REASON_NOT_MARKET = "not_market"
REASON_NO_SOURCE = "no_source"
REASON_FETCH_FAILED = "fetch_failed"
REASON_INVALID = "invalid_definition"
REASON_NAME_MISMATCH = "name_mismatch"
REASON_WRITE_FAILED = "write_failed"

_VERSION_PART_RE = re.compile(r"[.+-]")


def version_key(version: str) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """把版本号切成 `(数字核心, 预发布标签)` 两段键。

    * 数字核心按**数值**比（否则 `1.10` 会被判成小于 `1.9`）；
    * 预发布标签（`beta` / `rc1`）单独成段，比较时**正式版高于预发布**
      —— 直接混在一起按元组比大小会得出 `1.0.0-beta > 1.0.0` 这种反语义结果。
    """
    raw = str(version or "").strip()
    if raw[:1] in ("v", "V"):
        raw = raw[1:]
    core: list[int] = []
    pre: list[str] = []
    for part in _VERSION_PART_RE.split(raw):
        if not part:
            continue
        if part.isdigit():
            core.append(int(part))
        else:
            pre.append(part)
    return tuple(core), tuple(pre)


def compare_versions(a: str, b: str) -> int:
    """比较两个版本号：a>b → 1，a<b → -1，相等 → 0（宽松：脏版本号不抛异常）。

    数字核心缺失的段按 **0** 补齐（`1.0` == `1.0.0`）；核心相同时，
    **没有**预发布标签的一方更大（`1.0.0` > `1.0.0-beta`）。
    """
    core_a, pre_a = version_key(a)
    core_b, pre_b = version_key(b)
    for i in range(max(len(core_a), len(core_b))):
        x = core_a[i] if i < len(core_a) else 0
        y = core_b[i] if i < len(core_b) else 0
        if x != y:
            return 1 if x > y else -1
    if pre_a == pre_b:
        return 0
    if not pre_a:
        return 1
    if not pre_b:
        return -1
    return 1 if pre_a > pre_b else -1


def is_newer(remote: str, current: str) -> bool:
    """远端是否**严格更新**（相同或更旧都算"已最新"）。"""
    return compare_versions(remote, current) > 0


@dataclass
class UpgradeOutcome:
    """一次升级的结构化结果（`ok=False` 时 `reason` 一定有内容）。"""

    ok: bool
    updated: bool = False
    reason: str = ""
    current: str = ""
    latest: str = ""
    error: str = ""
    skill: dict[str, Any] = field(default_factory=dict)


def pick_definition(manifest: Any, name: str) -> dict[str, Any] | None:
    """从「源清单」里取出目标技能定义。

    两种形态都接受：

    * **单个定义**（安装地址直指一份技能 JSON）→ 原样返回；
    * **目录数组**（`GET /v1/skills/discover` 那种 `[{name, ...}]`）→ 取 `name` 命中的一条。
      这正好对上"能取到源清单"的说法：用户装的那个技能来自一份目录，升级时再去目录里找它。
    """
    if isinstance(manifest, dict):
        return manifest
    if isinstance(manifest, list):
        for item in manifest:
            if isinstance(item, dict) and str(item.get("name") or "") == name:
                return item
    return None


def build_upgraded_skill(
    data: dict[str, Any], *, name: str, previous: SkillDef
) -> tuple[SkillDef | None, str]:
    """把远端定义转成新 `SkillDef`；返回 `(skill, 原因码)`（失败时 skill 为 None）。

    保留用户侧的**本机状态**：`enabled` / `auto_inject` 是用户的选择，升级不该把它们
    重置成默认值；`origin` 也必须保留（否则升级后就再也升不了级了）。
    """
    remote_name = str(data.get("name") or "").strip()
    if not remote_name:
        return None, REASON_INVALID
    if remote_name != name:
        return None, REASON_NAME_MISMATCH

    exec_cfg = data.get("exec") or {}
    if not isinstance(exec_cfg, dict):
        return None, REASON_INVALID

    version = str(data.get("version") or "0.1.0").strip() or "0.1.0"
    description = data.get("description")
    if description is None:
        description = ""
    if not isinstance(description, str) or not description.strip():
        # 描述是技能目录里唯一被注入的字段，空的等于"装了也用不上"
        return None, REASON_INVALID

    try:
        tags = [str(t) for t in (data.get("tags") or [])]
        parameters = data.get("parameters") or []
        if not isinstance(parameters, list):
            parameters = []
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        return (
            SkillDef(
                name=name,
                description=description.strip(),
                version=version,
                author=str(data.get("author") or ""),
                tags=tags,
                exec_type=str(exec_cfg.get("type") or "prompt"),
                source=str(exec_cfg.get("source") or ""),
                parameters=parameters,
                installed_at=time.time(),
                enabled=previous.enabled,
                license=str(data.get("license") or ""),
                compatibility=str(data.get("compatibility") or ""),
                allowed_tools=[str(t) for t in (data.get("allowed_tools") or [])],
                metadata=metadata,
                auto_inject=previous.auto_inject,
                origin=previous.origin,
            ),
            "",
        )
    except (TypeError, ValueError) as exc:  # 远端字段类型怪 → 明确报"定义非法"
        logger.info("skill upgrade: bad definition for %r: %s", name, exc)
        return None, REASON_INVALID


def write_upgrade(store: SkillStore, skill: SkillDef) -> Path:
    """原子落盘一份扁平技能定义；返回最终路径。

    先写临时文件再 `os.replace`：替换是原子的，中途失败不会留下半个定义；
    `SkillStore.save()` 用的是同一条原子写（`_atomic_write_text`）。
    """
    path = store.root / f"{skill.name}.skill.json"
    _atomic_write_text(path, json.dumps(skill.to_dict(), ensure_ascii=False, indent=2))
    return path


async def upgrade_skill(
    store: SkillStore,
    name: str,
    *,
    source_url: str = "",
) -> UpgradeOutcome:
    """升级一个技能：校验来源 → 取清单 → 比版本 → 原子覆盖。

    `source_url` 显式给出时优先用它；否则回落到技能自身的 `metadata.source_url`
    （安装时记下的安装地址）。两者都没有 ⇒ `no_source`。
    """
    previous = store.get(name)
    if previous is None:
        return UpgradeOutcome(ok=False, reason=REASON_NO_SOURCE, error=f"skill {name!r} not found")

    current = previous.version or "0.1.0"
    if not str(previous.origin or "").startswith(ORIGIN_MARKET_PREFIX):
        return UpgradeOutcome(
            ok=False,
            reason=REASON_NOT_MARKET,
            current=current,
            error=(
                f"skill {name!r} is not market-installed (origin={previous.origin or '<empty>'}); "
                "only market skills can be upgraded"
            ),
        )
    if parse_market_origin(previous.origin) is None:
        return UpgradeOutcome(
            ok=False, reason=REASON_NO_SOURCE, current=current,
            error=f"malformed market origin: {previous.origin!r}",
        )

    url = (source_url or "").strip() or str(previous.metadata.get("source_url") or "").strip()
    if not url:
        return UpgradeOutcome(
            ok=False,
            reason=REASON_NO_SOURCE,
            current=current,
            error=(
                "no upgrade source: pass `source_url` (the market catalog or skill definition "
                "URL) or reinstall the skill so its source is recorded"
            ),
        )

    from app.tools.skill import _fetch_skill_json  # 与 install/discover 同一取回入口（SSRF + 体积上限）

    try:
        manifest = await _fetch_skill_json(url)
    except Exception as exc:  # noqa: BLE001 — 上游/校验失败都以结构化结果返回
        logger.info("skill upgrade: fetch %s failed: %s", url, str(exc)[:200])
        return UpgradeOutcome(
            ok=False, reason=REASON_FETCH_FAILED, current=current,
            error=f"failed to fetch {url}: {exc}",
        )

    data = pick_definition(manifest, name)
    if data is None:
        return UpgradeOutcome(
            ok=False, reason=REASON_INVALID, current=current,
            error=f"source manifest has no definition for {name!r}",
        )

    latest = str(data.get("version") or "0.1.0").strip() or "0.1.0"
    if not is_newer(latest, current):
        return UpgradeOutcome(
            ok=True, updated=False, reason=REASON_ALREADY_LATEST, current=current, latest=latest,
        )

    skill, reason = build_upgraded_skill(data, name=name, previous=previous)
    if skill is None:
        return UpgradeOutcome(
            ok=False, reason=reason, current=current, latest=latest,
            error=f"remote definition rejected: {reason}",
        )

    try:
        write_upgrade(store, skill)
    except OSError as exc:
        # 旧版本仍然完好（原子写失败只留临时文件）
        return UpgradeOutcome(
            ok=False, reason=REASON_WRITE_FAILED, current=current, latest=latest,
            error=f"failed to write upgraded skill: {exc}",
        )

    return UpgradeOutcome(
        ok=True, updated=True, current=current, latest=latest, skill=skill.to_dict(),
    )
