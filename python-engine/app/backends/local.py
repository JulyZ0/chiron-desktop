"""本地工作区后端（今天的默认行为，等价实现）。

**这一层刻意不做横切逻辑**：

- read-before-write 观测（`app/tools/fs_guard.py`）
- 写入前 undo 快照（`app/agent/undo_stack.py`）
- 动作分级与审批（`app/agent/tool_policy.py`）

它们留在**工具层**。理由：这样"引入后端抽象"就是纯粹的**内部实现替换** —— 对外返回契约与
安全语义都不动，现有工具测试即成为回归网（批 B 的验收标准就是"零行为漂移"）。

> 片 1 只把 `read` / `write` 接到工具上（`read_file` / `write_file`）；`ls` / `glob` / `grep`
> 的实现在片 2 迁移工具时与现有实现逐条对齐后再启用 —— 先定义协议、后迁移，避免一次改太多。

**并发约束（P1 修复）**：本类是 `async` 契约，但底层 `pathlib` 是**阻塞 I/O**。
此前直接在 event loop 里调用 `read_text` / `write_text` / `rglob` / `stat`，一旦并发
（尤其 `grep` 会遍历整个工作区并逐文件读取）就会冻结该 event loop 上的**所有**请求。
现统一经 `asyncio.to_thread` 下沉到线程池；返回契约与安全语义保持不变。
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import threading
from pathlib import Path

from app.backends.protocol import (
    DeleteResult,
    EditResult,
    ExecuteResult,
    FileStat,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadBytesResult,
    ReadResult,
    WriteResult,
)
from app.tools.sandbox import safe_join

#: 搜索引擎要跳过的**内部**目录（相对 workspace）。
#:
#: `.chiron` 是引擎自己的产物（执行输出落盘在 `.chiron/exec`，见 sandbox 的
#: `EXEC_OFFLOAD_DIR`）。它不是用户的源码，但位于 workspace 内 —— 不过滤的话，
#: 一次 grep 会把几十份构建日志当成命中返回，把真正的代码挤到预算之外。
#: 只跳过自己的目录：`.git` / `node_modules` 等属于既有语义，改它们会改变用户
#: 能搜到的范围，不做顺手的扩大。
_INTERNAL_DIR = ".chiron"


def _is_internal(base: Path, candidate: Path) -> bool:
    try:
        parts = candidate.relative_to(base).parts
    except ValueError:
        return False
    # `relative_to` 对 `candidate == base` 返回 `"."`（parts 为空）—— 直接取 [0]
    # 会 IndexError。grep 允许直接指向单个文件，这条路径是真实会走到的。
    return bool(parts) and parts[0] == _INTERNAL_DIR


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """同目录临时文件 + `os.replace`：观察者只可能看到完整旧内容或完整新内容。

    临时文件必须与目标**同一目录** —— 跨文件系统的 `os.replace` 会退化成
    copy+delete，原子性随之消失（Windows 上直接抛 OSError）。
    任何一步失败都要清掉临时文件，否则长任务会在workspace 里堆一地 `.tmp`。
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            # fsync 之后再 replace：否则崩溃时可能"目录项已换、数据块未落盘"，
            # 得到一个长度正确但内容是旧块的全零文件。
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _append_bytes(target: Path, data: bytes) -> None:
    """`O_APPEND` 追加：不需要读回已有内容，代价与文件大小无关。"""
    with open(target, "ab") as handle:
        handle.write(data)



def _grep_sync(base: Path, regex: re.Pattern[str], max_results: int) -> GrepResult:
    """`grep` 的同步实现（在工作线程中运行）：目录遍历 + **逐行流式**匹配。

    此前是 `read_text()` 读全文再 `splitlines()`：大仓库里一次 grep 会把每个候选
    文件的完整内容读进内存，命中行的价值只是"定位到哪"，代价却按文件大小走。
    改成流式读之后，驻留内存只有当前行 —— 匹配语义、扫描顺序、`max_results`
    截断点都与原实现一致（`sorted` 保证确定性顺序，换来的内存是 O(1)）。
    """
    candidates = sorted(base.rglob("*")) if base.is_dir() else [base]
    matches: list[GrepMatch] = []
    truncated = False
    for file in candidates:
        if not file.is_file() or _is_internal(base, file):
            continue
        try:
            with open(file, encoding="utf-8", errors="replace") as handle:
                for number, raw in enumerate(handle, start=1):
                    # NUL 意味着这是二进制（图片/字体/压缩包）。全文读取时它会变成
                    # 一长串替换字符，既浪费预算又不可能命中；当场放弃这个文件。
                    if "\x00" in raw:
                        break
                    line = raw.rstrip("\n").rstrip("\r")
                    if not regex.search(line):
                        continue
                    if len(matches) >= max_results:
                        truncated = True
                        break
                    matches.append(
                        GrepMatch(path=str(file), line_number=number, line=line)
                    )
        except OSError:  # 不可读文件跳过，不让整次搜索失败
            continue
        if truncated:
            break
    return GrepResult(
        matches=matches, truncated=truncated, reason="budget" if truncated else None
    )


#: 判二进制时探测的头部字节数（与 Reasonix 的 `readFileBinaryPeek` 同值）。
_BINARY_PEEK = 8 * 1024
#: UTF-16 的每个 ASCII 字符都含 0x00 —— 带 BOM 的 UTF-16 不是二进制（Reasonix 同款教训：
#: 先查 BOM 再查 NUL，否则 Windows 源文件会被整片误判）。
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


def _looks_binary(target: Path) -> bool:
    """头部含 NUL 即判二进制；带 BOM 的 UTF-16 除外。"""
    try:
        with open(target, "rb") as handle:
            head = handle.read(_BINARY_PEEK)
    except OSError:  # 读不到头部就不拦，让真正的读取给出确切错误
        return False
    if head.startswith(_UTF16_BOMS):
        return False
    return b"\x00" in head


#: 行数缓存：绝对路径 → (mtime_ns, size, total_lines)。
#:
#: 为什么值得缓存：`total_lines` 是给前端的既有承诺（显示「N 行」/「还有 N 行未渲染」），
#: 但**每翻一屏都要重新数一遍整个文件**，于是翻 N 屏的代价是 O(N × 文件行数)。
#: 文件没变时行数当然没变，重数纯粹是浪费。
_TOTAL_CACHE: dict[str, tuple[int, int, int]] = {}
_TOTAL_CACHE_LOCK = threading.Lock()
#: 缓存条数上限。长任务会读几百个文件，不设上限就是慢性内存泄漏。
#: 满了直接清空而不是做 LRU：缓存只影响快慢，清空一次只是下一次多扫一遍。
_TOTAL_CACHE_MAX = 256


def _stamp(target: Path) -> tuple[int, int] | None:
    """取 (mtime_ns, size) 作为缓存键；读不到元信息返回 `None`（不缓存）。"""
    try:
        info = target.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _cached_total(target: Path, stamp: tuple[int, int] | None) -> int | None:
    if stamp is None:
        return None
    with _TOTAL_CACHE_LOCK:
        hit = _TOTAL_CACHE.get(str(target))
    if hit is None:
        return None
    mtime_ns, size, total = hit
    # 指纹不一致 = 文件被改过，旧行数作废（否则前端会把"还剩几行"算错）
    if (mtime_ns, size) != stamp:
        return None
    return total


def _remember_total(target: Path, stamp: tuple[int, int], total: int) -> None:
    with _TOTAL_CACHE_LOCK:
        if len(_TOTAL_CACHE) >= _TOTAL_CACHE_MAX:
            _TOTAL_CACHE.clear()
        _TOTAL_CACHE[str(target)] = (stamp[0], stamp[1], total)


def _read_window(target: Path, offset: int, limit: int) -> tuple[list[str], int]:
    """流式取窗口，并把总行数数完 —— 只有窗口内容驻留内存。

    数完 `total_lines` 是为了它对前端的既有承诺（`ToolResultBlock.vue` 显示「N 行」）；
    代价是要扫一遍文件，换来的收益是内存从 O(文件大小) 降到 O(窗口)。

    R2：行数按 `(mtime, size)` 缓存，且**已知总数时扫到窗口末尾就停**。
    大文件上连续翻页因此从 O(N × 行长) 降到 O(窗口)；文件一被改，指纹变化即失效。
    """
    page: list[str] = []
    total = 0
    before = _stamp(target)
    known = _cached_total(target, before)
    stop_at = offset + limit if known is not None else None
    with open(target, encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            total += 1
            if total <= offset:
                continue
            if len(page) < limit:
                page.append(raw.rstrip("\n").rstrip("\r"))
            # 总数已知且窗口已填满：后面的行不影响返回值，不必再读。
            if stop_at is not None and total >= stop_at:
                break
    if known is not None and stop_at is not None and total >= stop_at:
        return page, known
    # 只有"读前读后指纹一致"才缓存：读的过程中文件被改，`total` 是旧快照的数，
    # 缓存它会让前端一直按错误的总行数计算剩余行数。
    if before is not None and _stamp(target) == before:
        _remember_total(target, before, total)
    return page, total



class LocalWorkspaceBackend:
    """`sandbox.safe_join` + 本地文件系统。"""

    async def read(self, path: str, *, offset: int = 0, limit: int = 200) -> ReadResult:
        target = safe_join(path)
        if not await asyncio.to_thread(target.exists):
            return ReadResult(path=str(target), error=f"file not found: {path}")

        if limit <= 0:
            # `limit <= 0` = 读**全文原文**（忽略 offset）：`edit_file` 需要逐字节精确的内容
            # 做唯一性校验与 diff —— 若这里做行归一化（`"\n".join(splitlines())`），
            # 原文末尾的换行会丢、CRLF 会被改写，替换语义随之漂移。
            text = await asyncio.to_thread(target.read_text, encoding="utf-8", errors="replace")
            return ReadResult(
                path=str(target),
                content=text,
                total_lines=len(text.splitlines()),
                offset=0,
                limit=0,
                truncated=False,
            )

        # 分页路径：先探头部判二进制，再**流式**取窗口 —— 文件不进内存，大文件才读得动。
        # `total_lines` 仍然数完整文件（前端 `ToolResultBlock.vue` 要显示「N 行」），
        # 但内容只驻留窗口。
        if await asyncio.to_thread(_looks_binary, target):
            return ReadResult(
                path=str(target),
                error=(
                    f"{path} looks like a binary file (NUL byte in its first "
                    f"{_BINARY_PEEK} bytes); read_file reads text"
                ),
            )
        page, total_lines = await asyncio.to_thread(_read_window, target, offset, limit)
        # 截断由**产生截断的这一层**给出，不由调用方扫文本猜测（协议约束 2）
        truncated = (offset + len(page)) < total_lines
        return ReadResult(
            path=str(target),
            content="\n".join(page),
            total_lines=total_lines,
            offset=offset,
            limit=limit,
            truncated=truncated,
        )

    async def read_bytes(self, path: str) -> ReadBytesResult:
        target = safe_join(path)
        if not await asyncio.to_thread(target.exists):
            return ReadBytesResult(path=str(target), error=f"file not found: {path}")
        try:
            data = await asyncio.to_thread(target.read_bytes)
            return ReadBytesResult(path=str(target), data=data)
        except OSError as exc:
            # 目录 / 权限 / 占用：把 OS 错误变成结构化错误，而不是让调用方接异常
            return ReadBytesResult(path=str(target), error=str(exc))

    async def stat(self, path: str) -> FileStat | None:
        target = safe_join(path)
        try:
            info = await asyncio.to_thread(target.stat)
            is_file = await asyncio.to_thread(target.is_file)
        except OSError:
            return None
        return FileStat(path=str(target), size=info.st_size, is_file=is_file)

    async def write(self, path: str, content: str) -> WriteResult:
        """原子覆盖写：先写同目录临时文件，再 `os.replace` 换上去。

        **为什么必须原子**：长任务里进程被 kill（超时、OOM、用户关窗口）是常态。
        `write_text` 直接在目标文件上截断再写 —— 中途死掉就留下**半截文件**，
        而且 undo 快照也随之失去意义（文件既不是旧的也不是新的）。`os.replace`
        在同一文件系统内是原子的：观察者要么看到旧内容，要么看到新内容。

        临时文件必须落在**同目录**：`os.replace` 跨文件系统会退化成 copy+delete，
        原子性就没了。用 `tempfile.mkstemp` 的 `dir` 参数保证这一点。
        """
        target = safe_join(path)
        data = content.encode("utf-8")
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(_atomic_write_bytes, target, data)
        return WriteResult(path=str(target), bytes_written=len(data))

    async def append(self, path: str, content: str) -> WriteResult:
        """真追加：只在文件末尾追加，不读回已有内容（O(1) 而非 O(文件大小)）。

        这是 `AppendingBackendProtocol` 的实现。分片写大文件时，旧路径"读全文→拼→
        整写"是 O(N²)，这里每片只把自己写进去。

        追加模式本身是原子的（内核保证 `O_APPEND` 的写入不覆盖他人数据），但
        **它会绕过 `_atomic_write_bytes`** —— 这是有意的：追加的语义就是"在末尾加"，
        不存在"旧内容还是新内容"的二选一。中途失败只会留下**前缀**，而前缀正是
        分片追加可以接着续写的状态。
        """
        target = safe_join(path)
        data = content.encode("utf-8")
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(_append_bytes, target, data)
        return WriteResult(path=str(target), bytes_written=len(data))

    async def edit(self, path: str, old: str, new: str) -> EditResult:
        target = safe_join(path)
        if not await asyncio.to_thread(target.exists):
            return EditResult(path=str(target), error=f"file not found: {path}")
        text = await asyncio.to_thread(
            target.read_text, encoding="utf-8", errors="replace"
        )
        occurrences = text.count(old)
        if occurrences == 0:
            return EditResult(path=str(target), error="old text not found")
        # 原地替换也必须走原子写：`edit` 是"读全文→改→写回"，中途被 kill
        # 留下的半截文件与 `write` 的后果完全一样。这里原先直接 `write_text`，
        # 成了原子写的唯一缺口。
        await asyncio.to_thread(
            _atomic_write_bytes, target, text.replace(old, new).encode("utf-8")
        )
        return EditResult(path=str(target), occurrences=occurrences)

    async def delete(self, path: str) -> DeleteResult:
        """删除**文件**（不递归删目录 —— 那是更危险的动作，不做隐式扩展）。"""
        target = safe_join(path)
        if not await asyncio.to_thread(target.exists):
            return DeleteResult(path=str(target), deleted=False)
        if await asyncio.to_thread(target.is_dir):
            return DeleteResult(
                path=str(target),
                error="path is a directory; delete_file only removes files",
            )
        try:
            await asyncio.to_thread(target.unlink)
        except OSError as exc:
            # 权限 / 占用：把 OS 错误变成结构化错误，而不是让调用方接异常
            return DeleteResult(path=str(target), error=str(exc))
        return DeleteResult(path=str(target), deleted=True)

    async def ls(self, path: str = ".") -> LsResult:
        target = safe_join(path)
        if not await asyncio.to_thread(target.exists):
            return LsResult(error=f"path not found: {path}")
        if await asyncio.to_thread(target.is_file):
            return LsResult(entries=[str(target)])
        entries = await asyncio.to_thread(
            lambda: sorted(str(p) for p in target.iterdir())
        )
        return LsResult(entries=entries)

    async def glob(self, pattern: str, *, path: str = ".") -> GlobResult:
        base = safe_join(path)
        paths = await asyncio.to_thread(
            lambda: sorted(str(p) for p in base.glob(pattern))
        )
        return GlobResult(paths=paths)

    async def grep(
        self, pattern: str, *, path: str = ".", max_results: int = 100
    ) -> GrepResult:
        base = safe_join(path)
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return GrepResult(error=f"invalid pattern: {exc}")

        # 目录遍历 + 逐文件读取下沉到线程池：这是本层最重的阻塞点。
        return await asyncio.to_thread(_grep_sync, base, regex, max_results)

    def supports_execution(self) -> bool:
        return True

    async def execute(self, command: str, *, timeout: float | None = None) -> ExecuteResult:
        from app.tools.sandbox import run_in_sandbox

        result = await run_in_sandbox(command, timeout=int(timeout) if timeout else 120)
        if "error" in result:
            return ExecuteResult(output=str(result["error"]), error=str(result["error"]))
        output = f"{result.get('stdout', '')}{result.get('stderr', '')}"
        exit_code = result.get("exit_code")
        # 片 5：`run_in_sandbox` 已按上限截断，这里把"是否截断"如实带出来 ——
        # 让上层不必扫文本猜测（协议约束 2）
        return ExecuteResult(
            output=output,
            exit_code=exit_code if isinstance(exit_code, int) else None,
            truncated=bool(result.get("truncated")),
        )
