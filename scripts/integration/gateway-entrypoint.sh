#!/bin/sh
# 集成测试网关的启动脚本：确保**共享项目卷**里有一个可回滚的 git 仓库，然后启动网关。
#
# 为什么要在这里做：
#   * 项目根路径必须同时在网关容器与引擎容器里存在（引擎按它建检查点、网关按它跑 git）；
#   * named volume 只在**首次**创建时从镜像内容初始化。如果卷是先被某个没有该目录的
#     镜像创建的，demo 目录就不存在，`POST /v1/session-map/projects` 会以
#     "root_path does not exist" 400 —— 脚本第 2 步就红，而原因在卷的初始化历史里。
#
# 幂等：目录/文件/仓库都在就什么都不做。仓库要有一次初始提交（HEAD），
# 否则检查点的 `read-tree HEAD` 只能走空仓库分支，回滚语义也更接近"什么都没保护"。
set -eu

PROJECT_ROOT="/srv/chiron/projects/demo"

mkdir -p "$PROJECT_ROOT" /srv/chiron/checkpoints

if [ ! -f "$PROJECT_ROOT/README.md" ]; then
  printf 'integration fixture: the codeword is ORANGE-COMPASS\n' >"$PROJECT_ROOT/README.md"
fi

if [ ! -d "$PROJECT_ROOT/.git" ]; then
  git init -q "$PROJECT_ROOT"
  git -C "$PROJECT_ROOT" -c user.email=integration@chiron.local -c user.name=integration add -A
  git -C "$PROJECT_ROOT" -c user.email=integration@chiron.local -c user.name=integration \
    commit -qm "integration fixture: initial commit"
fi

exec /app/chiron
