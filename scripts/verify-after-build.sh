#!/usr/bin/env bash
# verify-after-build.sh —— 重建后自动验收（P0-1）的 git-bash 外壳。
#
# 真正的断言实现只有一份：scripts/verify_after_build_lib.py。这个壳只做三件事 ——
# 找 Python、把 UTF-8 钉死、把参数与退出码原样透传。**不在 shell 里复刻断言**：
# 两份实现必然漂移，而验收脚本漂移的后果是"看起来全绿"。
#
# 为什么要 PYTHONUTF8/PYTHONIOENCODING：Windows 上 Python 默认按 cp936 编码 stdout，
# 服务端返回的中文 detail 会乱码甚至抛 UnicodeEncodeError —— 排查信息本身变成噪音。
#
# 用法：
#   bash scripts/verify-after-build.sh                 # 人读输出
#   bash scripts/verify-after-build.sh --json          # 机器可读
#   bash scripts/verify-after-build.sh --ignore-restricted
#
# 退出码：0=无 FAIL，1=有 FAIL，2=环境不具备（状态文件/就绪/会话）。
# 其余参数见 `--help`。

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HERE/verify_after_build_lib.py"

if [ ! -f "$LIB" ]; then
  echo "verify-after-build: 缺少 $LIB（脚本被搬运时只拷了壳？）" >&2
  exit 2
fi

# 解释器探测顺序：显式覆盖 > python3 > python > Windows 的 py 启动器。
# 逐个试 **能否真的运行**（`-c ""`），而不是只看 `command -v`：Windows 上
# `python` 常常是应用商店的占位 exe，存在但跑不起来。
PY_BIN=""
PY_PREFIX=()
if [ -n "${CHIRON_VERIFY_PYTHON:-}" ] && "$CHIRON_VERIFY_PYTHON" -c "" >/dev/null 2>&1; then
  PY_BIN="$CHIRON_VERIFY_PYTHON"
else
  for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "" >/dev/null 2>&1; then
      PY_BIN="$cand"
      break
    fi
  done
fi
if [ -z "$PY_BIN" ] && command -v py >/dev/null 2>&1 && py -3 -c "" >/dev/null 2>&1; then
  PY_BIN="py"
  PY_PREFIX=(-3)
fi

if [ -z "$PY_BIN" ]; then
  echo "verify-after-build: 找不到可用的 Python 3（可用 CHIRON_VERIFY_PYTHON 指定解释器路径）" >&2
  exit 2
fi

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

exec "$PY_BIN" "${PY_PREFIX[@]}" "$LIB" "$@"
