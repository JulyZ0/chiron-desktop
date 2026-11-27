#!/usr/bin/env python
"""R2 决策门：对 rclone/WinFsp 挂载目录跑文件基准（团队主机上执行）。

前置：团队主机已安装 rclone + WinFsp，并已把 client 项目挂到某个盘符/目录
（rclone mount 的挂载点）。脚本只在该目录下创建临时子目录做写测试，结束时删除。

    python scripts/r2_mount_bench.py --root R:\\project

判定阈值（PRD §8）：单文件读 P95 <= 50ms、写 P95 <= 150ms，且 git 可用
（`git -C <root> rev-parse --is-inside-work-tree`）。rename/delete 的实际生效与
10 分钟稳定性仍需人工/上层联调确认，本脚本只覆盖 I/O 数字、rename/delete 时延
与 git 基本可用性。

退出码：0 = 通过；1 = 未通过（有错误或超阈值）；2 = 参数/环境不具备。

本文件同时是可复用模块：`run_bench()` 返回报告（不打印），`evaluate()` 给出
门禁结论。统一验收入口 `scripts/team_acceptance.py` 直接调用它，避免两套口径。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import uuid

DEFAULT_READ_P95_MS = 50.0
DEFAULT_WRITE_P95_MS = 150.0


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(p / 100 * (len(ordered) - 1)))))
    return ordered[idx]


def timed(fn) -> tuple[float, object]:
    start = time.perf_counter()
    result = fn()
    return (time.perf_counter() - start) * 1000.0, result


def _metric(values: list[float]) -> dict:
    return {
        "p50_ms": round(percentile(values, 50), 3),
        "p95_ms": round(percentile(values, 95), 3),
        "samples": len(values),
    }


def _git_probe(root: str, timeout: float = 30.0) -> dict:
    """git 可用性：能在挂载目录里识别出工作树（只读命令，不改仓库）。"""
    start = time.perf_counter()
    result = {"available": False, "ms": None, "error": ""}
    try:
        proc = subprocess.run(
            ["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            timeout=timeout,
            text=True,
        )
        result["ms"] = round((time.perf_counter() - start) * 1000.0, 3)
        result["available"] = proc.returncode == 0 and proc.stdout.strip() == "true"
        if not result["available"]:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            result["error"] = detail[0][:200] if detail else f"git exit={proc.returncode}"
    except Exception as exc:  # noqa: BLE001 - 缺 git / 超时都按不可用上报
        result["ms"] = round((time.perf_counter() - start) * 1000.0, 3)
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def run_bench(
    root: str,
    *,
    files: int = 50,
    size_kb: int = 64,
    rounds: int = 10,
    skip_git: bool = False,
    git_timeout: float = 30.0,
) -> dict:
    """在 root 下跑一轮 I/O 基准，返回报告（含 verdict）。不打印、不抛异常。"""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return {
            "root": root,
            "error": f"挂载点不存在或不是目录: {root}",
            "errors": [f"挂载点不存在或不是目录: {root}"],
            "read": _metric([]),
            "write": _metric([]),
            "list": _metric([]),
            "rename": _metric([]),
            "delete": _metric([]),
            "git": {"available": None, "ms": None, "error": "", "skipped": skip_git},
        }

    payload = (b"chiron-bench\n" * max(1, size_kb * 1024 // 13))[: size_kb * 1024]
    bench_dir = os.path.join(root, f".chiron-bench-{uuid.uuid4().hex[:8]}")
    read_ms: list[float] = []
    write_ms: list[float] = []
    rename_ms: list[float] = []
    delete_ms: list[float] = []
    list_ms: list[float] = []
    errors: list[str] = []
    files_paths: list[str] = []

    try:
        os.makedirs(bench_dir, exist_ok=False)
        for i in range(max(1, files)):
            path = os.path.join(bench_dir, f"f{i}.bin")
            elapsed, _ = timed(lambda p=path: _write(p, payload))
            write_ms.append(elapsed)
            files_paths.append(path)

        # 列表/重复读：模拟一次回合前 walk/read 的热路径。
        for _ in range(max(1, rounds)):
            elapsed, _ = timed(lambda: len(os.listdir(bench_dir)))
            list_ms.append(elapsed)
            for path in files_paths[: min(20, len(files_paths))]:
                elapsed, _ = timed(lambda p=path: _read(p))
                read_ms.append(elapsed)

        # rename/delete 必须真的生效，否则"写成功"只是缓存假象。
        for i, path in enumerate(files_paths[: min(20, len(files_paths))]):
            new_path = path + ".renamed"
            elapsed, _ = timed(lambda a=path, b=new_path: os.replace(a, b))
            rename_ms.append(elapsed)
            files_paths[i] = new_path

        for path in files_paths:
            elapsed, _ = timed(lambda p=path: os.remove(p))
            delete_ms.append(elapsed)
    except Exception as exc:  # noqa: BLE001 - 基准失败也要输出已采集部分
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(bench_dir, ignore_errors=True)

    if skip_git:
        git = {"available": None, "ms": None, "error": "", "skipped": True}
    else:
        git = {"skipped": False, **_git_probe(root, timeout=git_timeout)}

    return {
        "root": root,
        "files": files,
        "size_kb": size_kb,
        "rounds": rounds,
        "read": _metric(read_ms),
        "write": _metric(write_ms),
        "list": _metric(list_ms),
        "rename": _metric(rename_ms),
        "delete": _metric(delete_ms),
        "git": git,
        "errors": errors,
    }


def evaluate(
    report: dict,
    *,
    read_p95_ms: float = DEFAULT_READ_P95_MS,
    write_p95_ms: float = DEFAULT_WRITE_P95_MS,
) -> dict:
    """把报告折成门禁结论。每条理由都能直接贴进验收记录。"""
    reasons: list[str] = []
    read = report.get("read") or {}
    write = report.get("write") or {}
    git = report.get("git") or {}

    if report.get("error"):
        reasons.append(str(report["error"]))
    if report.get("errors"):
        reasons.extend(f"I/O 错误: {e}" for e in report["errors"])
    if not read.get("samples"):
        reasons.append("没有读到任何样本（挂载点不可读？）")
    elif float(read.get("p95_ms") or 0) > read_p95_ms:
        reasons.append(f"读 P95 {read['p95_ms']}ms > 门限 {read_p95_ms}ms")
    if not write.get("samples"):
        reasons.append("没有写入任何样本（挂载点不可写？）")
    elif float(write.get("p95_ms") or 0) > write_p95_ms:
        reasons.append(f"写 P95 {write['p95_ms']}ms > 门限 {write_p95_ms}ms")
    if not git.get("skipped") and git.get("available") is not True:
        reasons.append("git 不可用（挂载目录不是可识别的工作树）" + (f"：{git['error']}" if git.get("error") else ""))

    return {
        "passed": not reasons,
        "reasons": reasons,
        "thresholds": {"read_p95_ms": read_p95_ms, "write_p95_ms": write_p95_ms},
    }


def format_summary(report: dict, verdict: dict) -> str:
    lines = [
        f"R2 基准目录：{report.get('root', '')}",
        f"  读   P50 {report['read']['p50_ms']}ms  P95 {report['read']['p95_ms']}ms "
        f"（门限 {verdict['thresholds']['read_p95_ms']}ms，{report['read']['samples']} 样本）",
        f"  写   P50 {report['write']['p50_ms']}ms  P95 {report['write']['p95_ms']}ms "
        f"（门限 {verdict['thresholds']['write_p95_ms']}ms，{report['write']['samples']} 样本）",
        f"  rename P95 {report['rename']['p95_ms']}ms  delete P95 {report['delete']['p95_ms']}ms",
    ]
    git = report.get("git") or {}
    if git.get("skipped"):
        lines.append("  git  已跳过（--skip-git，验收门禁不给结论）")
    else:
        lines.append(f"  git  {'可用' if git.get('available') else '不可用'}  {git.get('ms') or '-'}ms")
    if verdict["passed"]:
        lines.append("R2 I/O + git 决策门：通过")
    else:
        lines.append("R2 I/O + git 决策门：未通过")
        lines.extend(f"  - {r}" for r in verdict["reasons"])
    lines.append("仍待真实主机确认：fs/git/run_code 全链路联调、rename/delete 业务侧生效、10 分钟稳定性。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="rclone 挂载点（client 项目根）")
    parser.add_argument("--files", type=int, default=50)
    parser.add_argument("--size-kb", type=int, default=64)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--read-p95-ms", type=float, default=DEFAULT_READ_P95_MS)
    parser.add_argument("--write-p95-ms", type=float, default=DEFAULT_WRITE_P95_MS)
    parser.add_argument("--skip-git", action="store_true")
    parser.add_argument("--json-out", default="", help="把报告写到文件（仍会打印到 stdout）")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"挂载点不存在或不是目录: {root}", file=sys.stderr)
        return 2

    report = run_bench(
        root,
        files=args.files,
        size_kb=args.size_kb,
        rounds=args.rounds,
        skip_git=args.skip_git,
    )
    verdict = evaluate(report, read_p95_ms=args.read_p95_ms, write_p95_ms=args.write_p95_ms)
    report["thresholds"] = verdict["thresholds"]
    report["verdict"] = verdict
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(format_summary(report, verdict))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
