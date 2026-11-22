"""把已确认的 GitHub Release 草稿公开。

发布流程是「tag 出包 → 草稿 Release → 人工确认签名 → 公开」：desktop.yml 刻意
保持 `draft: true`，因为未签名安装包不应自动对公众可见。本脚本只做最后一步，
并且**默认 dry-run** —— 不加 `--yes` 不会改任何远端状态。

用法：
    python scripts/publish_release.py V0.1.261122.02
    python scripts/publish_release.py V0.1.261122.02 --yes

前置：本机已 `gh auth login`，且对仓库有 write 权限。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


def run_gh(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="公开已确认的 GitHub Release 草稿")
    parser.add_argument("tag", help="发布 tag（如 V0.1.261122.02）")
    parser.add_argument(
        "--yes", action="store_true", help="真正执行公开；缺省只做检查并打印命令"
    )
    args = parser.parse_args()

    view = run_gh("release", "view", args.tag, "--json", "isDraft,assets,url")
    if view.returncode != 0:
        sys.stderr.write(view.stderr or view.stdout)
        return 1

    data = json.loads(view.stdout or "{}")
    assets = [str(item.get("name") or "") for item in data.get("assets") or []]
    if "latest.yml" not in assets:
        # 客户端在线升级读的是 releases/latest/download/latest.yml；缺了它，
        # 即使 Release 公开，升级检查仍会 404。
        print("警告：该 Release 没有 latest.yml —— 客户端在线检查仍会 404。")

    if not data.get("isDraft"):
        print(f"{args.tag} 已是公开状态：{data.get('url') or ''}")
        return 0

    if not args.yes:
        print(f"资产：{', '.join(assets) or '（空）'}")
        print("确认安装包签名状况后执行：")
        print(f"  python scripts/publish_release.py {args.tag} --yes")
        return 0

    edit = run_gh("release", "edit", args.tag, "--draft=false")
    if edit.returncode != 0:
        sys.stderr.write(edit.stderr or edit.stdout)
        return 1
    print(f"已公开：https://github.com/{_repo()}/releases/tag/{args.tag}")
    return 0


def _repo() -> str:
    view = run_gh("repo", "view", "--json", "nameWithOwner")
    if view.returncode != 0:
        return "<owner>/<repo>"
    try:
        return str(json.loads(view.stdout or "{}").get("nameWithOwner") or "<owner>/<repo>")
    except json.JSONDecodeError:
        return "<owner>/<repo>"


if __name__ == "__main__":
    raise SystemExit(main())
