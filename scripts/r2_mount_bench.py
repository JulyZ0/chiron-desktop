#!/usr/bin/env python
"""R2 决策门：对 rclone/WinFsp 挂载目录跑文件基准（团队主机上执行）。

前置：团队主机已安装 rclone + WinFsp，并已把 client 项目挂到某个盘符/目录
（rclone mount 的挂载点）。脚本只在该目录下创建临时子目录做写测试，结束时删除。

    python scripts/r2_mount_bench.py --root R:\\project

判定阈值（PRD §9.2）：单文件读 P95 <= 50ms、写 P95 <= 150ms。低于阈值退出码 0，
否则 1，并输出 JSON 供记录。`fs/git/run_code 可用` 与 10 分钟稳定性仍需人工/上层
联调确认，本脚本只覆盖 I/O 数字与 git 基本可用性。
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="rclone 挂载点（client 项目根）")
    parser.add_argument("--files", type=int, default=50)
    parser.add_argument("--size-kb", type=int, default=64)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--read-p95-ms", type=float, default=50.0)
    parser.add_argument("--write-p95-ms", type=float, default=150.0)
    parser.add_argument("--skip-git", action="store_true")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"挂载点不存在或不是目录: {root}", file=sys.stderr)
        return 2

    payload = (b"chiron-bench\n" * max(1, args.size_kb * 1024 // 13))[: args.size_kb * 1024]
    bench_dir = os.path.join(root, f".chiron-bench-{uuid.uuid4().hex[:8]}")
    os.makedirs(bench_dir, exist_ok=False)

    read_ms: list[float] = []
    write_ms: list[float] = []
    rename_ms: list[float] = []
    delete_ms: list[float] = []
    list_ms: list[float] = []
    errors: list[str] = []
    files: list[str] = []

    try:
        for i in range(args.files):
            path = os.path.join(bench_dir, f"f{i}.bin")
            elapsed, _ = timed(lambda p=path: _write(p, payload))
            write_ms.append(elapsed)
            files.append(path)

        for _ in range(args.rounds):
            elapsed, _ = timed(lambda: len(os.listdir(bench_dir)))
            list_ms.append(elapsed)
            for path in files[: min(20, len(files))]:
                elapsed, _ = timed(lambda p=path: _read(p))
                read_ms.append(elapsed)

        for path in files[: min(20, len(files))]:
            new_path = path + ".renamed"
            elapsed, _ = timed(lambda a=path, b=new_path: os.replace(a, b))
            rename_ms.append(elapsed)
            files[files.index(path)] = new_path

        for path in files:
            elapsed, _ = timed(lambda p=path: os.remove(p))
            delete_ms.append(elapsed)
    except Exception as exc:  # noqa: BLE001 - 基准失败也要输出已采集部分
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(bench_dir, ignore_errors=True)

    git_ok = None
    git_ms = None
    if not args.skip_git:
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                ["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                timeout=30,
                text=True,
            )
            git_ms = (time.perf_counter() - start) * 1000.0
            git_ok = proc.returncode == 0 and proc.stdout.strip() == "true"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"git: {type(exc).__name__}: {exc}")

    report = {
        "root": root,
        "files": args.files,
        "size_kb": args.size_kb,
        "rounds": args.rounds,
        "read": {"p50_ms": percentile(read_ms, 50), "p95_ms": percentile(read_ms, 95), "samples": len(read_ms)},
        "write": {"p50_ms": percentile(write_ms, 50), "p95_ms": percentile(write_ms, 95), "samples": len(write_ms)},
        "list": {"p50_ms": percentile(list_ms, 50), "p95_ms": percentile(list_ms, 95), "samples": len(list_ms)},
        "rename": {"p50_ms": percentile(rename_ms, 50), "p95_ms": percentile(rename_ms, 95), "samples": len(rename_ms)},
        "delete": {"p50_ms": percentile(delete_ms, 50), "p95_ms": percentile(delete_ms, 95), "samples": len(delete_ms)},
        "git": {"available": git_ok, "ms": git_ms},
        "errors": errors,
        "thresholds": {"read_p95_ms": args.read_p95_ms, "write_p95_ms": args.write_p95_ms},
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))

    passed = (
        not errors
        and report["read"]["samples"] > 0
        and report["write"]["samples"] > 0
        and report["read"]["p95_ms"] <= args.read_p95_ms
        and report["write"]["p95_ms"] <= args.write_p95_ms
    )
    print("R2 I/O 决策门：" + ("通过" if passed else "未通过（见 errors/阈值）"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
