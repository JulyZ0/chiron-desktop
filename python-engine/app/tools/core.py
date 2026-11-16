"""核心本地工具集（Python 端）。

首波迁移：filesystem / shell / search(grep) / web。
后续可继续扩展 memory / pm / skill / workflow 等工具。
"""

from __future__ import annotations

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


async def grep_files(
    query: str, root: str = ".", glob: str = "", max_results: int = 200
) -> dict[str, Any]:
    """按正则搜索文件内容（批 B：匹配与读取都经后端）。

    R1：匹配下沉到**后端的流式 grep**，不再逐文件 `read(limit=0)` 读全文。

    此前这里是"glob 拿文件列表 → 对每个文件读完整内容 → 逐行匹配"，于是大仓库
    一次搜索的内存开销按**仓库总字节**走，而产出只是一小把行号。后端本来就有
    `grep`（逐行读，驻留内存只有当前行），却一直没被调用 —— 工具层自己抄了一份
    全文读取的实现。

    两条路径都保持原有语义（`max_results` 截断点、命中行裁剪、字节预算）：

    - 无 `glob` 限定：一次 `backend.grep(root)`，后端只遍历一遍。
    - 有 `glob` 限定：先按名筛出候选，再对每个候选走同一套流式匹配 ——
      仍然是流式，只是分多次调用，不退回全文读取。
    """
    from app.backends.context import get_backend

    try:
        # 只做一次语法校验：真正的匹配在后端内进行（逐行流式）。
        # 无效正则若留到后端才暴露，会变成"每个候选文件各报一次错"。
        re.compile(query)
    except re.error as exc:
        return {"error": f"invalid pattern: {exc}", "count": 0, "matches": []}

    backend = get_backend()
    # 命中三元组 (path, line_number, line) —— 与 GrepMatch 字段一一对应，
    # 两条路径产出同一种形状，后面的裁剪逻辑因此只需写一遍。
    pool: list[tuple[str, int, str]] = []
    backend_cut = False

    if not glob:
        found = await backend.grep(query, path=root or ".", max_results=max_results)
        if found.error:
            return {"error": found.error, "count": 0, "matches": []}
        pool = [(m.path, m.line_number, m.line) for m in found.matches]
        backend_cut = found.truncated
    else:
        globbed = await backend.glob(f"**/{glob}", path=root or ".")
        if globbed.error:
            return {"error": globbed.error, "count": 0, "matches": []}
        for candidate in globbed.paths:
            info = await backend.stat(candidate)
            if info is None or not info.is_file:
                continue
            remaining = max_results - len(pool)
            if remaining <= 0:
                backend_cut = True
                break
            one = await backend.grep(query, path=candidate, max_results=remaining)
            if one.error:
                logger.debug("Cannot grep file %s, skipping", candidate)
                continue
            pool.extend((m.path, m.line_number, m.line) for m in one.matches)
            if one.truncated:
                backend_cut = True

    # **命中行按预算裁剪**（`GREP_MAX_LINE_CHARS` / `GREP_MAX_BYTES`）。定位类工具的产出
    # 是"线索"，不是"正文"：一次命中 200 行、每行都是压缩过的长行时，不裁剪的 grep
    # 结果本身就够把上下文撑爆 —— 而模型真正要的只是"哪个文件第几行匹配到了"。
    # 需要细节时用 `read_file` 带着 offset 去读那个位置。
    matches: list[dict[str, Any]] = []
    used = 0
    budget_hit = False
    for path_, line_no, line in pool:
        text = _clip_line(line, GREP_MAX_LINE_CHARS)
        cost = len(text.encode("utf-8")) + 1
        if matches and used + cost > GREP_MAX_BYTES:
            budget_hit = True
            break
        matches.append({"path": path_, "line": line_no, "text": text})
        used += cost
        if len(matches) >= max_results:
            return {"count": len(matches), "matches": matches}

    if budget_hit:
        return {
            "count": len(matches),
            "matches": matches,
            "truncated": True,
            "note": (
                f"byte budget reached ({GREP_MAX_BYTES} bytes); "
                "narrow the pattern or pass a smaller `root`/`glob` for the rest"
            ),
        }
    if backend_cut:
        return {
            "count": len(matches),
            "matches": matches,
            "truncated": True,
            "note": (
                f"result limit reached ({max_results} hits); "
                "narrow the pattern or pass a smaller `root`/`glob` for the rest"
            ),
        }
    return {"count": len(matches), "matches": matches}


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
# 两份实现并存 = 必然漂移（规划 §5.3 的"两份白名单必然漂移"是同一类错）。
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
        "`read_file` with `offset` to read the surrounding code. Narrow with `glob`/`root` "
        "instead of raising `max_results` when the hit list is too long."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Regular expression to match against each line"},
            "root": {"type": "string", "default": ".", "description": "Directory to search under (relative to the workspace)"},
            "glob": {"type": "string", "default": "", "description": "Only scan files matching this glob (e.g. '*.py')"},
            "max_results": {"type": "integer", "default": 200, "description": "Stop after this many matches"},
        },
        "required": ["query"],
    },
    handler=grep_files,
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
)
