"""`python -m app.isolation` —— 查看/撤销执行隔离。

    python -m app.isolation status     # 当前档位、实际生效档位、各机制可用性
    python -m app.isolation revoke     # 撤销本方案创建的一切（删 AppContainer profile）

刻意做成独立命令而不是塞进 `run.py`：撤销是**安全动作**，应当能在不启动整套服务
的情况下立刻执行。
"""

from __future__ import annotations

import argparse
import json
import sys

from app.isolation import revoke_all, status


def _main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    parser = argparse.ArgumentParser(
        prog="python -m app.isolation",
        description="Windows 原生执行隔离：查看状态 / 撤销",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="显示当前隔离档位与实际能力")
    sub.add_parser("revoke", help="撤销本方案创建的一切（AppContainer profile 与授权）")
    args = parser.parse_args(argv)

    if args.cmd == "status":
        info = status()
        print(json.dumps(info, ensure_ascii=False, indent=2))
        caps = info["capabilities"]
        print(
            f"\n请求档位: {info['requested']}\n"
            f"实际生效: {info['effective']}"
            + (f"  ← 已降级: {'; '.join(info['reasons'])}" if info["degraded"] else ""),
            file=sys.stderr,
        )
        if not caps.get("is_windows"):
            print("非 Windows 平台：使用 POSIX setrlimit 路径。", file=sys.stderr)
        return 0

    result = revoke_all()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("\n已撤销。Job Object 与 Low IL 是进程级的，随进程退出自动消失。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
