"""核心本地工具集（Python 端）。

首波迁移：filesystem / shell / search(grep) / web。
后续可继续扩展 memory / pm / skill / workflow 等工具。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from app.tools.registry import registry

logger = logging.getLogger(__name__)


def _safe_path(path: str, root: str) -> Path:
    """防止路径穿越，返回绝对路径。"""
    base = Path(root).resolve()
    target = (base / path).resolve()
    # 必须加 trailing separator，否则 /app 会误匹配 /appdata
    if target != base and not str(target).startswith(str(base) + os.sep):
        raise ValueError("path escapes root")
    return target


#: 一次返回的最大行数（未显式传 `limit` 时）。与 Reasonix 的 `readFileDefaultLimit` 同值：
#: 分页的意义是"给一个能继续的窗口"，不是"一次读完"。
READ_FILE_DEFAULT_LIMIT = 2000

#: 单行最大字符数。**只有行数上限是不够的**：一行 minified 的 JS/JSON/CSS 可以是几 MB，
#: `limit=2000` 行听起来很节制，实际一次就灌满整个上下文窗口，而模型看到的还只是
#: "第 1 行"——它连"这文件很大"都判断不出来。超出的部分在行内截断并标注真实长度。
READ_FILE_MAX_LINE_CHARS = 500

#: 单次读返回的总字节预算。行数上限 × 单行上限仍可能过大（2000 × 500 = 1MB），
#: 所以在行数之外再压一层字节闸门：到量即停，并把"从哪继续"如实告诉模型。
READ_FILE_MAX_BYTES = 64 * 1024

#: grep 命中的单行上限。比 `read_file` 更短：一次可能命中几十上百行，
#: 命中行的价值在"定位到哪"，不在"整行原文"——细节让模型再用 read_file 取窗口。
GREP_MAX_LINE_CHARS = 240

#: grep 单次返回的总字节预算。命中的是**线索**，不是正文。
GREP_MAX_BYTES = 48 * 1024

#: grep 一次调用最多**扫描并收集**多少条命中（G29）。
#:
#: 为什么把它和 `max_results` 拆开：修前两者是同一个数，于是"一次交付多少条"顺手
#: 决定了"一次扫描多少条" —— 想翻页看到第 201 条命中时，唯一的办法是把
#: `max_results` 调大，等于让每一页都重新按上限重扫、重算。收集上限独立之后，
#: `max_results` 只回答"这一页给几条"，`offset` 才有意义。
#:
#: 撞上这条上限时**必须说出来**（`scan_truncated` + trailer）：它是资源边界，
#: 不是"搜索结果就这么多"。
GREP_COLLECT_MAX_HITS = 2000

#: 超上限策略的环境变量名（部署方显式选择，见 :func:`resolve_grep_over_cap_policy`）。
GREP_OVER_CAP_ENV = "CHIRON_GREP_OVER_CAP"

#: 合法策略：``head`` = 按修改时间保留最新命中；``sample`` = 按顶层目录均匀抽样。
GREP_OVER_CAP_POLICIES: tuple[str, ...] = ("head", "sample")

#: 缺省策略。选 ``head`` 是因为最常见的超上限场景是"改代码/看报错"——那要的是**最新**的。
GREP_OVER_CAP_DEFAULT = "head"

#: `outline` 按扩展名取"结构行"的正则。
#:
#: 为什么值得单独一个工具：读大文件最贵的不是"读多少行"，而是**读之前不知道哪里值得读**。
#: 模型只能从第 1 行开始翻窗口，翻到第 8 屏才看见目标函数——那些窗口全进了上下文，
#: 而且大多没用。先给一份"定义行 + 行号"的骨架，模型就能直接 `read_file(offset=N)`
#: 跳到目标处，把读取轮数从"翻完整个文件"压到"一次定位 + 一次读窗口"。
#:
#: 刻意用正则而不是语法树：tree-sitter 要额外依赖与编译产物，而这里要的只是
#: "函数/类/结构体定义大致在哪一行"。正则会漏（多行签名、宏生成的声明）也会多
#: （字符串里的 `def `），但作为**索引**够用——它只负责指路，内容仍由 `read_file` 给。
OUTLINE_PATTERNS: dict[str, str] = {
    ".py": r"^(\s*)(?:async\s+)?(?:def|class)\s+\w+",
    ".js": r"^(\s*)(?:export\s+)?(?:async\s+)?(?:function|class)\s+\w+|^(\s*)(?:export\s+)?const\s+\w+\s*=\s*(?:async\s*)?\(",
    ".ts": r"^(\s*)(?:export\s+)?(?:async\s+)?(?:function|class|interface|type|enum)\s+\w+",
    ".vue": r"^(\s*)(?:export\s+)?(?:async\s+)?(?:function|class)\s+\w+|^(\s*)(?:const|function)\s+\w+\s*[=(]",
    ".go": r"^func\s|^type\s+\w+\s+(?:struct|interface)|^var\s+\w+\s*=",
    ".rs": r"^(\s*)(?:pub\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl)\s+",
    ".java": r"^(\s*)(?:public|private|protected)?\s*(?:static\s+)?(?:class|interface|enum)\s+\w+",
    ".rb": r"^(\s*)(?:def|class|module)\s+",
    ".sh": r"^(\s*)\w+\s*\(\)\s*\{",
    ".sql": r"^\s*(?:CREATE|ALTER)\s+(?:TABLE|VIEW|INDEX|FUNCTION)",
}

#: outline 的缺省正则：够宽的通用形态，不认识的扩展名也还能给点线索。
OUTLINE_FALLBACK = r"^(\s*)(?:def|class|function|func|fn|struct|interface|impl|type|enum|module)\s+\w+"

#: outline 单次最多返回的结构行数。骨架的意义是"指路"，列全了反而又是正文。
OUTLINE_MAX_LINES = 400


def _outline_regex(suffix: str) -> tuple[str, bool]:
    """取扩展名对应的结构行正则，返回 `(pattern, 是否走了兜底)`。"""
    key = suffix.lower()
    pattern = OUTLINE_PATTERNS.get(key)
    if pattern:
        return pattern, False
    return OUTLINE_FALLBACK, True


def _clip_line(line: str, limit: int) -> str:
    """把过长的行截断，并**在行内标注真实长度**。

    标注不是装饰：模型需要知道"这里被截了"，否则它会把半行代码当成完整语句去改，
    或者据此判断"这个文件就长这样"。省略号单独给出，尾巴里带原长度。
    """
    if len(line) <= limit:
        return line
    return f"{line[:limit]}… [line truncated: {len(line)} chars total]"


async def read_file(path: str, offset: int = 0, limit: int = READ_FILE_DEFAULT_LIMIT) -> dict[str, Any]:
    """读文件（分页）。

    路径一律 clamp 到当前工作区 —— 此前签名里有个 `root` 参数，模型以为能改根目录，
    实现根本不看它（已移除）。

    **续读**：`offset` 是 0-based 行偏移，`limit` 是本次最多返回的行数。被截断时返回值
    带 `truncated`，且正文末尾附一行 `[more lines below; pass offset=N to continue]` ——
    模型据此继续读，不必靠比较 `offset + limit` 与 `total_lines` 去猜还有没有下文。

    **三层预算，缺一层都会被绕过**：

    1. 行数（`limit`）：防"文件有一万行"；
    2. 单行长度（`READ_FILE_MAX_LINE_CHARS`）：防"文件只有三行，但第一行是 2MB 的
       minified bundle"。只有行数上限时，这条路一次就能把上下文灌满；
    3. 总字节（`READ_FILE_MAX_BYTES`）：行数 × 单行仍是 1MB 量级，需要最后一道闸。

    三层都在**工具层**做，不动后端契约：`limit=0` 读全文是 `edit_file` 的依赖
    （精确替换需要原始字节），截断必须发生在"给模型看"这一层。
    """
    from app.backends.context import get_backend
    from app.tools.fs_guard import observe
    from app.tools.sandbox import safe_join

    # 批 B：文件读经**后端**（默认 = 本地工作区，行为与迁移前一致）
    result = await get_backend().read(path, offset=offset, limit=limit)
    if result.error:
        return {"error": result.error}
    # 横切逻辑留在工具层：read-before-write 观测（写入校验依赖它）
    observe(safe_join(path))  # 记录版本，供 write/edit 的 read-before-write 校验

    lines = result.content.splitlines()
    kept: list[str] = []
    used = 0
    byte_capped = False
    for line in lines:
        clipped = _clip_line(line, READ_FILE_MAX_LINE_CHARS)
        # +1 是换行符的估算：判"装不装得下"要按落盘后的真实体量算
        cost = len(clipped.encode("utf-8")) + 1
        if kept and used + cost > READ_FILE_MAX_BYTES:
            byte_capped = True
            break
        kept.append(clipped)
        used += cost

    content = "\n".join(kept)
    # 行级截断（后端算好的）与字节级截断（这里算的）任一发生，都要告诉模型怎么续读。
    truncated = result.truncated or byte_capped
    if truncated:
        next_offset = result.offset + len(kept)
        if byte_capped:
            content = (
                f"{content}\n[byte budget reached: this window stopped at "
                f"{READ_FILE_MAX_BYTES} bytes; pass offset={next_offset} to continue]"
            )
        else:
            # 截断提示由**产生截断的这一层**给出（后端已算好 truncated），不在这里重算。
            content = f"{content}\n[more lines below; pass offset={next_offset} to continue]"
    return {
        "path": result.path,
        "total_lines": result.total_lines,
        "offset": result.offset,
        "limit": result.limit,
        "truncated": truncated,
        "content": content,
    }


async def read_image(path: str, max_bytes: int = 0) -> dict[str, Any]:
    """把工作区里的图片读成 data-URL（供 vision 模型）。

    类型**按字节嗅探**（不看扩展名）；超过上限或装不进 `MAX_DIM` 的一律**明确拒绝**，
    绝不把超限图片原样发给 provider —— 那会被当成 unsupported format，并连带毁掉
    这个会话之后的每一轮。降采样/重编码的裁决统一在 `app.media.image_fit`。

    路径同样 clamp 到工作区（`root` 参数已移除，理由与 `read_file` 相同）。
    """
    import base64

    from app.backends.context import get_backend
    from app.media.image_fit import MAX_IMAGE_BYTES, ImageUnfit, fit, sniff_mime
    from app.tools.fs_guard import observe
    from app.tools.sandbox import safe_join

    limit = int(max_bytes) if max_bytes and max_bytes > 0 else MAX_IMAGE_BYTES
    backend = get_backend()
    target = safe_join(path)  # 沙箱隔离（S 安全修复）

    # 批 B：大小与字节都经后端（默认 = 本地工作区）
    info = await backend.stat(path)
    if info is None:
        return {"error": f"file not found: {path}"}
    if info.size <= 0:
        return {"error": f"image is empty: {path}"}
    if info.size > limit:
        return {"error": f"image too large ({info.size} bytes, max {limit})"}
    blob = await backend.read_bytes(path)
    if blob.error:
        return {"error": blob.error}

    # 类型由**字节**说了算：扩展名是声明，声明错了到 provider 那里就是不符的 media_type。
    mime = sniff_mime(blob.data)
    if not mime:
        return {"error": f"unsupported image data: {path} is not PNG/JPEG/GIF/WebP"}
    try:
        data, mime = fit(blob.data, mime)
    except ImageUnfit as exc:
        return {"error": f"{path}: {exc}"}
    observe(target)
    return {
        "path": str(target),
        "media_type": mime,
        "bytes": len(data),
        "data_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
    }


async def write_file(
    path: str, content: str, append: bool = False
) -> dict[str, Any]:
    """写文件（会建父目录；写入前自动快照供 /undo 回滚）。

    路径 clamp 到工作区（`root` 参数已移除，理由与 `read_file` 相同）。

    **`append=True` 是为"一次装不下"准备的**：模型输出受 `max_tokens` 限制，一份大内容
    整份重发**必然再次被截断**（而且截断点还会移动），所以"重新发起调用"绝不是出路。
    分段追加才是：第一片照常写，之后每片带 `append=True`。没有这个参数时，模型遇到大文件
    只能反复重试同一个调用直到轮数耗尽 —— 表现出来就是"工具失败后不会自愈"。
    """
    from app.agent import undo_stack
    from app.backends.context import get_backend
    from app.backends.protocol import AppendingBackendProtocol
    from app.tools.context import get_session_id
    from app.tools.fs_guard import check_before_write
    from app.tools.sandbox import safe_join

    target = safe_join(path)  # 沙箱隔离
    # 横切逻辑留在工具层，且**顺序不变**：冲突检查 → undo 快照 → 写入
    conflict = check_before_write(target)
    if conflict:
        return {"error": conflict}
    # 写入前快照：`/undo` 据此**真正恢复**文件（此前 /undo 只是回显一个字符串）。
    # 快照存不下（文件过大 / 存储不可用）时**当场告知"本次不可撤销"**，而不是事后才发现。
    # 追加时快照拍的是**追加前**的内容 —— 撤销一次追加就该回到追加前，语义与覆盖写一致。
    undo_note = await undo_stack.snapshot_before_write(get_session_id(), target, "write_file")
    # 批 B：写入经**后端**（默认 = 本地工作区；建父目录也由后端负责）
    backend = get_backend()
    if append:
        # W1：优先走后端的**真追加**（O(1)，只写文件末尾）。
        #
        # 旧的"读全文→拼接→整写"是 O(N²)：分 N 片写一个文件要搬 1+2+…+N 份内容，
        # 而且每片都会拍一次全文 undo 快照。但 `append` 是**可选**能力
        # （`AppendingBackendProtocol`），FileStore / skill / 测试替身都没有实现它 ——
        # 所以这里保留回退路径：能力缺失时行为与以前完全一致，只是不再更快。
        if isinstance(backend, AppendingBackendProtocol):
            written_append = await backend.append(path, content)
            if written_append.error:
                return {"error": written_append.error}
            appended: dict[str, Any] = {
                "path": written_append.path,
                "bytes": written_append.bytes_written,
                "appended": True,
            }
            if undo_note:
                appended["undo_warning"] = undo_note
            return appended
        info = await backend.stat(path)
        base = ""
        if info is not None and info.is_file:
            existing = await backend.read(path, limit=0)
            if existing.error:
                return {"error": existing.error}
            base = existing.content
        content = base + content
    written = await backend.write(path, content)
    if written.error:
        return {"error": written.error}
    result: dict[str, Any] = {"path": written.path, "bytes": written.bytes_written}
    if append:
        result["appended"] = True
    if undo_note:
        result["undo_warning"] = undo_note
    return result


async def execute_command(command: str, timeout: int = 30) -> dict[str, Any]:
    """在沙箱内执行一条 shell 命令。

    工作目录固定为当前用户的工作区：此前签名里有 `cwd`，schema 说它能改目录，
    实现却忽略它 —— 模型据此重试过多次（已移除）。
    """
    from app.tools.sandbox import run_in_sandbox

    return await run_in_sandbox(command, timeout=timeout)


def resolve_grep_over_cap_policy() -> tuple[str, str]:
    """取 grep 的超上限策略，返回 ``(策略, 错误说明)``。

    解析顺序：``CHIRON_GREP_OVER_CAP`` 环境变量（**每次调用现读**，换策略不必重启引擎）
    → Settings 的 ``grep_over_cap`` 字段（``GREP_OVER_CAP`` 环境变量，启动时读一次）
    → ``head``。

    非法值返回 ``("", 说明)`` 而**不回落**：回落会让部署方以为自己换了抽样策略，
    实际仍在按修改时间取头部，而这种偏差不会出现在任何日志里 —— G29 要消灭的正是
    "超上限时悄悄替你决定"。
    """
    raw = (os.environ.get(GREP_OVER_CAP_ENV) or "").strip().lower()
    if not raw:
        try:
            from app.config import settings

            raw = str(getattr(settings, "grep_over_cap", "") or "").strip().lower()
        except Exception:  # noqa: BLE001 - 配置模块不可用不是拒绝搜索的理由
            raw = ""
    if not raw:
        return GREP_OVER_CAP_DEFAULT, ""
    if raw not in GREP_OVER_CAP_POLICIES:
        return "", (
            f"invalid grep over-cap policy '{raw}' (from {GREP_OVER_CAP_ENV} / grep_over_cap); "
            f"choose one of: {', '.join(GREP_OVER_CAP_POLICIES)}"
        )
    return raw, ""


def _grep_failure(message: str) -> dict[str, Any]:
    """统一的失败形状。

    为什么失败结果也要带分页字段：翻页调用方（`while not eof: offset = next_offset`）
    遇到缺键的字典会变成 `KeyError` 或"这一页到底结束了没有"无从判断 —— 一条本来
    很清楚的 `error` 会被解读成工具坏了。
    """
    return {
        "error": message,
        "count": 0,
        "matches": [],
        "offset": 0,
        "next_offset": 0,
        "eof": True,
        "total": 0,
    }


def _top_level_group(path: str, root: str) -> str:
    """命中路径的**顶层目录**（相对 `root`）；直接躺在根目录下的文件归到空串。"""
    norm = path.replace("\\", "/")
    base = (root or ".").replace("\\", "/").rstrip("/")
    if base and base != "." and norm.startswith(base + "/"):
        norm = norm[len(base) + 1 :]
    parts = [piece for piece in norm.split("/") if piece]
    return parts[0] if len(parts) > 1 else ""


def _canonical_hits(
    pool: list[tuple[str, int, str]], mtimes: dict[str, float]
) -> list[tuple[str, int, str]]:
    """稳定顺序：**修改时间降序 → 路径升序 → 行号升序**。

    为什么把 mtime 放在首位：默认超上限策略是"保留最新命中"，若展示顺序按路径、
    而保留逻辑按时间，页内与翻页就是两套顺序，`offset` 拼接必然错位。顺序只有一份。
    后端没有 mtime（全为 ``0.0``）时这一键恒等，自然退化为路径序。
    """
    return sorted(pool, key=lambda hit: (-mtimes.get(hit[0], 0.0), hit[0], hit[1]))


def _sample_hits(
    hits: list[tuple[str, int, str]], root: str
) -> list[tuple[str, int, str]]:
    """`sample` 策略的顺序：按顶层目录**轮转**，每轮每个目录取一条。

    为什么不是"均匀丢弃"：交付顺序在这里就是**优先级** —— 第一页给出"每个目录的
    代表"，后面各轮继续补齐。这样既不丢命中（全集仍可续页取回），又不会让第一个
    大目录吃光整个配额（`head` 在刚 clone 的仓库上就退化成这个毛病：所有文件 mtime
    几乎相同）。

    输入已按稳定顺序排好 ⇒ 同目录内相对次序不变 ⇒ 输出确定，分页拼得回去。
    """
    buckets: dict[str, list[tuple[str, int, str]]] = {}
    for hit in hits:
        buckets.setdefault(_top_level_group(hit[0], root), []).append(hit)
    order = sorted(buckets)
    out: list[tuple[str, int, str]] = []
    round_index = 0
    while True:
        added = False
        for key in order:
            bucket = buckets[key]
            if round_index < len(bucket):
                out.append(bucket[round_index])
                added = True
        if not added:
            return out
        round_index += 1


def _order_hits(
    pool: list[tuple[str, int, str]],
    *,
    root: str,
    policy: str,
    mtimes: dict[str, float],
) -> list[tuple[str, int, str]]:
    """按策略给出**交付顺序**（分页与 `offset` 都在这个顺序上进行）。"""
    ordered = _canonical_hits(pool, mtimes)
    if policy == "sample":
        return _sample_hits(ordered, root)
    return ordered


def _slice_page(
    hits: list[tuple[str, int, str]], *, start: int, max_results: int
) -> tuple[list[dict[str, Any]], int, bool]:
    """切一页：``[start, start+max_results)``，再按字节预算截尾。

    返回 ``(页内命中, next_offset, 是否被字节预算截断)``。`next_offset` 按**实际
    返回条数**推进 —— 按请求的 `max_results` 推进会静默跳过被预算砍掉的页尾，
    这正是 `read_file` 分页踩过的同一个坑。
    """
    page: list[dict[str, Any]] = []
    used = 0
    budget_hit = False
    index = start
    end = min(len(hits), start + max_results)
    while index < end:
        path_, line_no, line = hits[index]
        text = _clip_line(line, GREP_MAX_LINE_CHARS)
        cost = len(text.encode("utf-8")) + 1
        if page and used + cost > GREP_MAX_BYTES:
            budget_hit = True
            break
        page.append({"path": path_, "line": line_no, "text": text})
        used += cost
        index += 1
    return page, index, budget_hit


async def _stat_mtimes(backend: Any, paths: list[str], known: dict[str, float]) -> None:
    """补全每个命中文件的修改时间（已由 glob 路径填过的跳过）。"""
    for path_ in paths:
        if path_ in known:
            continue
        info = None
        try:
            info = await backend.stat(path_)
        except Exception as exc:  # noqa: BLE001 - 元信息拿不到不该让整次搜索失败
            logger.debug("Cannot stat %s for grep order: %s", path_, exc)
        known[path_] = float(getattr(info, "mtime", 0.0) or 0.0) if info is not None else 0.0


async def _store_full_hits(hits: list[tuple[str, int, str]], *, session_id: str) -> str:
    """把**全量**命中（稳定顺序）写进既有 `result_store`，返回 locator；失败返回 ``""``。

    为什么不另造存储：`read_tool_result` 已经是"大结果取回"的唯一通道，再开一条
    就必然漂移（与 `app/tools/spill.py` 的同一条理由）。行格式与 `matches` 逐字一致
    （path/line/text），模型不必学第二种形状。
    """
    body = "\n".join(
        json.dumps(
            {"path": path_, "line": line_no, "text": _clip_line(line, GREP_MAX_LINE_CHARS)},
            ensure_ascii=False,
        )
        for path_, line_no, line in hits
    )
    try:
        from app.agent import result_store
        from app.tools.spill import new_result_ref

        ref = new_result_ref()
        if await result_store.store(session_id, ref, body):
            return ref
        logger.warning("grep over-cap spill skipped (result store unavailable)")
    except Exception as exc:  # noqa: BLE001 - 落盘是旁路，存不下也要让调用方拿到这一页
        logger.warning("grep over-cap spill failed: %s", exc)
    return ""


async def grep_files(
    query: str,
    root: str = ".",
    glob: str = "",
    max_results: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """按正则搜索文件内容（批 B：匹配与读取都经后端；G29：显式超上限契约 + 分页）。

    R1（既有）：匹配下沉到**后端的流式 grep**，不再逐文件 `read(limit=0)` 读全文。

    G29 ① 超上限契约 —— 修前"保留哪些命中"根本没有契约：

    * 修前：后端按路径序流式扫描，凑满 `max_results` 停；工具层再按同一顺序截字节
      预算。trailer 只写 "result limit reached (N hits)"，**不说保留的是哪一批**。
      于是模型既不知道被丢掉的命中里有没有自己要找的，也没有通道把它们取回来，
      只能换一个更窄的正则重搜（把一个"翻页"问题变成"反复重试"问题）。
    * 现在：交付顺序由**显式策略**决定（部署方必须选；非法值报错，见
      :func:`resolve_grep_over_cap_policy`）：

      - ``head``（默认）：按**修改时间**降序 —— 先给最新命中，trailer 明写
        "按修改时间保留前 N 条"；
      - ``sample``：按**顶层目录**轮转抽样 —— 先给每个目录的代表，避免第一个大
        目录吃光配额（通读陌生仓库时这才是想要的批次）。

      两种策略都**不丢信息**：全集按同一顺序可用 `offset` 续页取回，同一份全量
      还写进既有 `result_store`（locator 在 trailer 里），两条通道互为兜底。

    G29 ② 分页 —— 修前没有任何续页参数：

    * 稳定顺序**唯一**：修改时间降序 → 路径升序 → 行号升序（后端给不出 mtime 时
      退化为路径序，并如实写明）。页只是这个顺序上的窗口 ⇒ 两页拼接逐条等于全集。
    * `offset` 是**命中序号**（0 基），不是行号也不是文件序号；越界归一到末尾，
      负值按 0 处理。`next_offset` 按实际返回条数推进（字节预算会砍掉页尾）。

    参数语义与修前对齐：`max_results` = **每页**最多多少条（默认 200），所以不传
    `offset` 的既有调用方拿到的仍是第一页。

    两条收集路径（有/无 `glob`）产出同一种三元组，后面的排序、分页、裁剪只写一遍。
    """
    from app.backends.context import get_backend
    from app.tools.context import get_session_id

    try:
        # 只做一次语法校验：真正的匹配在后端内进行（逐行流式）。
        # 无效正则若留到后端才暴露，会变成"每个候选文件各报一次错"。
        re.compile(query)
    except re.error as exc:
        return _grep_failure(f"invalid pattern: {exc}")

    policy, policy_error = resolve_grep_over_cap_policy()
    if policy_error:
        return _grep_failure(policy_error)

    try:
        start = max(0, int(offset))
    except (TypeError, ValueError):
        return _grep_failure(f"offset must be an integer, got {offset!r}")

    # 页大小至少 1：`max_results=0` 的调用方（历史遗留）不该拿到一页空白。
    page_size = max(1, int(max_results))

    backend = get_backend()
    # 命中三元组 (path, line_number, line) —— 与 GrepMatch 字段一一对应，
    # 两条路径产出同一种形状，后面的排序/分页/裁剪因此只需写一遍。
    pool: list[tuple[str, int, str]] = []
    mtimes: dict[str, float] = {}
    scan_truncated = False

    if not glob:
        found = await backend.grep(query, path=root or ".", max_results=GREP_COLLECT_MAX_HITS)
        if found.error:
            return _grep_failure(found.error)
        pool = [(m.path, m.line_number, m.line) for m in found.matches]
        scan_truncated = found.truncated
    else:
        globbed = await backend.glob(f"**/{glob}", path=root or ".")
        if globbed.error:
            return _grep_failure(globbed.error)
        for candidate in globbed.paths:
            info = await backend.stat(candidate)
            if info is None or not info.is_file:
                continue
            # glob 路径本来就要 stat 一次：顺手记下 mtime，排序时不必再问一遍。
            mtimes[candidate] = float(getattr(info, "mtime", 0.0) or 0.0)
            remaining = GREP_COLLECT_MAX_HITS - len(pool)
            if remaining <= 0:
                scan_truncated = True
                break
            one = await backend.grep(query, path=candidate, max_results=remaining)
            if one.error:
                logger.debug("Cannot grep file %s, skipping", candidate)
                continue
            pool.extend((m.path, m.line_number, m.line) for m in one.matches)
            if one.truncated:
                scan_truncated = True

    await _stat_mtimes(backend, [path_ for path_, _line, _text in pool], mtimes)
    mtime_available = any(value > 0.0 for value in mtimes.values())

    ordered = _order_hits(pool, root=root or ".", policy=policy, mtimes=mtimes)
    total = len(ordered)
    start = min(start, total)  # 越界 offset 归一到末尾，不回绕（见 docstring）

    page, next_offset, budget_hit = _slice_page(
        ordered, start=start, max_results=page_size
    )
    eof = next_offset >= total
    over_cap = total > page_size

    result: dict[str, Any] = {
        "count": len(page),
        "matches": page,
        "offset": start,
        "next_offset": next_offset,
        "eof": eof,
        "total": total,
    }
    if scan_truncated:
        result["scan_truncated"] = True

    clauses: list[str] = []
    if over_cap:
        ref = await _store_full_hits(ordered, session_id=get_session_id())
        result["over_cap"] = {
            "policy": policy,
            "total": total,
            "page": len(page),
            # `mtime_available=False` ⇒ 顺序实际是路径序。契约不能承诺后端给不出的
            # 东西，否则模型会把路径序当时间序推理（"改最近的三个文件"）。
            "mtime_available": mtime_available,
            "result_ref": ref,
            "stored": bool(ref),
        }
        if start == 0:
            if policy == "sample":
                kept = f"按顶层目录均匀抽样保留前 {len(page)} 条"
            elif mtime_available:
                kept = f"按修改时间保留前 {len(page)} 条"
            else:
                kept = f"按路径序保留前 {len(page)} 条（后端未提供修改时间）"
            clauses.append(f"命中 {total} 条超过单次上限 {page_size} 条：{kept}")
        else:
            clauses.append(
                f"命中 {total} 条超过单次上限 {page_size} 条：按同一顺序续页"
                f"（offset={start}，本页 {len(page)} 条）"
            )
        if ref:
            clauses.append(
                f'全量 {total} 条已落盘 result_ref="{ref}"；需要完整清单时用 '
                f'read_tool_result(result_ref="{ref}", offset=0, limit=8000) 取回'
            )
        else:
            clauses.append(
                f"全量 {total} 条未能落盘（结果存储不可用）；按同一顺序用 offset= 续页取回"
            )

    if scan_truncated:
        clauses.append(
            f"扫描在 {GREP_COLLECT_MAX_HITS} 条命中处停止（扫描上限），可能还有更多命中；"
            "缩小 root/glob 再搜"
        )
    if budget_hit:
        clauses.append(
            f"byte budget reached ({GREP_MAX_BYTES} bytes); pass offset={next_offset} to continue"
        )
    elif not eof:
        clauses.append(f"more hits on the next page; pass offset={next_offset} to continue")

    if over_cap or scan_truncated or budget_hit or not eof:
        result["truncated"] = True
    if clauses:
        result["note"] = "; ".join(clauses)
    return result


async def search_files(
    pattern: str, root: str = ".", max_results: int = 200
) -> dict[str, Any]:
    """按文件名模式搜索（批 B：匹配经后端）。

    与 `glob_files` 的区别是**自动递归**（`**/` 前缀）—— 这是既有行为差异，保留。
    """
    from app.backends.context import get_backend

    backend = get_backend()
    globbed = await backend.glob(f"**/{pattern or '*'}", path=root or ".")
    if globbed.error:
        return {"error": globbed.error, "count": 0, "files": []}

    files: list[str] = []
    for candidate in globbed.paths:
        info = await backend.stat(candidate)
        if info is not None and info.is_file:
            files.append(candidate)
        if len(files) >= max_results:
            break
    return {"count": len(files), "files": files}


async def outline_file(
    path: str, max_lines: int = OUTLINE_MAX_LINES
) -> dict[str, Any]:
    """给出源码文件的**结构骨架**（定义行 + 行号），不返回正文。

    定位优先级：`outline_file` → `read_file(offset=...)`。先用骨架找出目标定义在哪一行，
    再只读那一小段，比"从第 1 行开始一屏一屏翻到目标出现"省下的不只是 token ——
    每翻一屏都是一次工具往返，翻八屏就是八轮，长任务里轮数比 token 更稀缺。

    正则是有意为之的取舍（见 `OUTLINE_PATTERNS` 的说明）：它给的是**索引**，允许漏和
    多报；真正的代码仍由 `read_file` 提供。因此结果里明确带上 `fallback` 标记，
    让模型知道"这个扩展名没有专门规则，骨架可能不完整"，而不是把缺失当成"文件里没有定义"。
    """
    from app.backends.context import get_backend

    backend = get_backend()
    # `limit=0` = 读全文：这里**必须**看全文才能找全定义（与 read_file 的分页语义不同，
    # 骨架的产出是行号列表，尺寸由 OUTLINE_MAX_LINES 与 _clip_line 控制）。
    result = await backend.read(path, limit=0)
    if result.error:
        return {"error": result.error, "path": path, "count": 0, "entries": []}

    pattern, fallback = _outline_regex(Path(path).suffix)
    try:
        matcher = re.compile(pattern)
    except re.error as exc:  # pragma: no cover - 表内正则都经过测试覆盖
        return {"error": f"invalid outline pattern: {exc}", "path": path, "count": 0, "entries": []}

    entries: list[dict[str, Any]] = []
    truncated = False
    for number, raw in enumerate(result.content.splitlines(), start=1):
        if not matcher.search(raw):
            continue
        if len(entries) >= max_lines:
            # 命中数超上限：如实报告，否则"骨架只有 400 条"会被误读成"文件只有 400 个定义"。
            truncated = True
            break
        indent = len(raw) - len(raw.lstrip())
        entries.append(
            {
                "line": number,
                "depth": indent // 2,
                "text": _clip_line(raw.strip(), GREP_MAX_LINE_CHARS),
            }
        )

    payload: dict[str, Any] = {
        "path": path,
        "total_lines": result.total_lines,
        "count": len(entries),
        "entries": entries,
        "fallback": fallback,
    }
    if truncated:
        payload["truncated"] = True
        payload["note"] = (
            f"stopped at {max_lines} entries; narrow the file or read a window with "
            "read_file(offset=N) for the rest"
        )
    if fallback:
        payload["note"] = (
            payload.get("note", "")
            + ("; " if payload.get("note") else "")
            + f"no rule for '{Path(path).suffix or '(no extension)'}' — the outline may be incomplete"
        ).strip("; ")
    if not entries:
        # 空骨架有两种原因：文件里确实没有定义，或这个语言没有规则。区分不了就不能下结论。
        payload["note"] = (
            payload.get("note", "")
            + ("; " if payload.get("note") else "")
            + "no definition lines matched — the file may have none, or fall back to read_file"
        ).strip("; ")
    return payload


async def execute_python(code: str, timeout: int = 30) -> dict[str, Any]:
    """执行一段 Python 代码（沙箱内）。

    为什么不是 `python -c <代码>`：那要把源码**拼进 shell 命令行**，于是必须自己处理引号、
    换行、反斜杠的转义 —— `json.dumps` 只解决了一部分（POSIX 下可行，Windows 的 cmd.exe
    规则不同，含双引号/换行的代码会直接执行失败或截断）。

    这里改成 **base64 传递**：`-c` 的参数整体用双引号包住，内部只有 base64 字母表
    （`A-Za-z0-9+/=`，不含引号与空白），因此两种 shell 下的引号解析都无歧义。
    代价是命令行长一点（约 4/3 倍），换来的是**跨平台确定性**。
    """
    if not code:
        return {"error": "code is required"}
    import base64

    payload = base64.b64encode(code.encode("utf-8")).decode("ascii")
    command = (
        'python -c "import base64;'
        f"exec(base64.b64decode('{payload}').decode('utf-8'))\""
    )
    return await execute_command(command=command, timeout=timeout)


# ── 已删除：core 里的 web_fetch ──
#
# 这里曾有一份 `web_fetch` 实现（httpx + assert_safe_url），但它**从未注册**，
# 而 `app/tools/web.py` 里有一份更完整的同名实现（HTML→Markdown、字符上限、错误语义）。
# 两份实现并存 = 必然漂移（"两份白名单必然漂移"是同一类错）。
# 只保留 web.py 的那一份；本模块不再有出网能力。


# 注册工具（schema 与 Go 侧保持兼容）
registry.register(
    name="read_file",
    description=(
        "Read a text file from the workspace with pagination. Use `offset`/`limit` to page "
        "through large files; a truncated result says so in a trailer and names the offset to "
        "continue from. **Prefer `grep_files`/`glob_files` to locate the interesting region "
        "first**: a read returns at most ~64KB with over-long lines clipped, so pulling a whole "
        "large file page by page to find one function wastes the context you need for the work "
        "itself. Read the window around what you actually need."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace root"},
            "offset": {"type": "integer", "default": 0, "description": "Line to start from (0-based)"},
            "limit": {
                "type": "integer",
                "default": 2000,
                "description": "Max lines to return (default 2000)",
            },
        },
        "required": ["path"],
    },
    handler=read_file,
    # G15：只读文件内容 ⇒ 可并发（write_file/shell_exec 等有副作用，不声明）。
    concurrency_safe=True,
)

registry.register(
    name="write_file",
    description=(
        "Write text to a file in the workspace (parent directories are created). "
        "This is for work-in-progress files; to hand the user a file they can open or "
        "download, use media_create instead. "
        "**If the content is too large for one response** (your output is capped by "
        "max_tokens), write the first chunk normally, then send the remaining chunks with "
        "`append=true` — do NOT retry the whole call, it will be truncated again."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace root"},
            "content": {"type": "string", "description": "Content to write (one chunk when appending)"},
            "append": {
                "type": "boolean",
                "default": False,
                "description": "Append to the file instead of overwriting it (use for chunked writes)",
            },
        },
        "required": ["path", "content"],
    },
    handler=write_file,
)

registry.register(
    name="shell_exec",
    description=(
        "Run a shell command in the sandbox. The working directory is always the workspace "
        "(it cannot be changed). Returns stdout/stderr/exit_code."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "timeout": {"type": "integer", "default": 30, "description": "Timeout in seconds"},
        },
        "required": ["command"],
    },
    handler=execute_command,
)

registry.register(
    name="grep_files",
    description=(
        "Regex-search file contents under a workspace directory. **Start here when you do not "
        "already know where something lives** — locating first and then reading a window costs "
        "a fraction of the tokens of opening files to find out. Returns `path`/`line`/`text` per "
        "match with match lines clipped to a byte budget; treat it as an index, then call "
        "`read_file` with `offset` to read the surrounding code.\n"
        "**Paging**: hits come in one stable order (newest file first, then path, then line). "
        "Each page reports `offset`, `next_offset`, `eof` and `total`; when `eof` is false, call "
        "again with `offset=next_offset` — the pages concatenate to the full hit list, so you "
        "never have to invent a narrower regex just to see the rest.\n"
        "**Over the cap**: the order is an explicit deployment policy (default keeps the most "
        "recently modified hits; a repo-wide sampling policy is also available). When hits "
        "exceed what one page can carry, the trailer says which policy was applied and gives a "
        "`result_ref` locator for the complete list (`read_tool_result`)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Regular expression to match against each line"},
            "root": {"type": "string", "default": ".", "description": "Directory to search under (relative to the workspace)"},
            "glob": {"type": "string", "default": "", "description": "Only scan files matching this glob (e.g. '*.py')"},
            "max_results": {"type": "integer", "default": 200, "description": "Max matches returned per page (default 200)"},
            "offset": {
                "type": "integer",
                "default": 0,
                "description": (
                    "Hit index to start from (0-based, in the documented stable order). "
                    "Pass the `next_offset` of the previous page to continue."
                ),
            },
        },
        "required": ["query"],
    },
    handler=grep_files,
    # G15：只读检索 ⇒ 可并发。
    concurrency_safe=True,
)

registry.register(
    name="search_files",
    description="Search files by glob pattern",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "root": {"type": "string", "default": "."},
            "max_results": {"type": "integer", "default": 200},
        },
        "required": ["pattern"],
    },
    handler=search_files,
    # G15：只读 glob 检索 ⇒ 可并发。
    concurrency_safe=True,
)

registry.register(
    name="outline_file",
    description=(
        "List the definition lines (functions / classes / types) of a source file with their "
        "line numbers, **without** returning the body. Call this before `read_file` on any file "
        "you have not read: one outline call usually replaces several paged reads, and each "
        "avoided read is a saved round trip. Then `read_file(path, offset=line)` at the entry you "
        "want. Treat it as an index — regex-based rules can miss or over-report (`fallback: true` "
        "means this extension has no dedicated rule)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace"},
            "max_lines": {
                "type": "integer",
                "default": OUTLINE_MAX_LINES,
                "description": "Stop after this many definition lines",
            },
        },
        "required": ["path"],
    },
    handler=outline_file,
    # G15：只读文件大纲 ⇒ 可并发。
    concurrency_safe=True,
)

registry.register(
    name="execute_python",
    description=(
        "Run a short Python snippet in the sandbox. The code is passed as base64 so quoting "
        "works identically on every platform. For multi-step work that should call several "
        "tools in one shot, prefer run_code."
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source to execute"},
            "timeout": {"type": "integer", "default": 30, "description": "Timeout in seconds"},
        },
        "required": ["code"],
    },
    handler=execute_python,
)

registry.register(
    name="read_image",
    description=(
        "Read an image file (PNG/JPEG/GIF/WebP, sniffed from the bytes rather than the "
        "extension) as a base64 data-URL for vision-capable models. Images larger than "
        "MAX_DIM (1568px) are downscaled to fit before encoding."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Image path relative to the workspace root"},
            "max_bytes": {
                "type": "integer",
                "default": 10485760,
                "description": "Max image bytes (default 10 MB)",
            },
        },
        "required": ["path"],
    },
    handler=read_image,
    # G15：只读图片字节 ⇒ 可并发（另见 spill.EXEMPT_TOOLS：它的数据 URL 不能被裁）。
    concurrency_safe=True,
)
