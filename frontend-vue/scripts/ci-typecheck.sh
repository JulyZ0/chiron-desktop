#!/usr/bin/env bash
# PR、桌面发布与 ci-smoke 共用的类型检查硬门禁。
# 不解析、不过滤诊断：vue-tsc 的任何非零退出（含启动失败/崩溃）原样向上传播。
set -euo pipefail

FRONTEND_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$FRONTEND_ROOT"

# -b 覆盖 app/node 两个项目；--force 避免 buildinfo 把旧错误缓存成绿色，--noEmit 不写产物。
exec pnpm exec vue-tsc -b --force --noEmit
