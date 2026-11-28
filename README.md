# Chiron

<p align="center"><img src="assets/chiron-logo-detail.svg" width="180" alt="Chiron"></p>

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Go](https://img.shields.io/badge/Go-1.26-00ADD8?logo=go&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Vue](https://img.shields.io/badge/Vue-3-4FC08D?logo=vue.js&logoColor=white)
![Electron](https://img.shields.io/badge/Electron-desktop-47848F?logo=electron&logoColor=white)

**Chiron 是一个可私有化部署的 AI Agent 工作台。** 桌面端以 Windows 账户为身份，在本机拉起
完整的「网关 + 引擎 + 数据栈」；同一个后端也可以作为团队服务运行，成员用浏览器访问。

## 核心能力

- **深度 Agent 循环**：多轮规划与工具调用、运行中追加指令（steer）、预算/轮次/时长门禁、
  断流恢复、上下文压缩（含压缩后回读最近文件）。
- **子 Agent 协作**：模型自主委派，或用户在面板里直接「＋委派」；支持 nested run、并行、
  独立预算与只读回放，可停止、重试、追加指令。
- **执行隔离**：Windows 走无隔离 / 作业对象 / 低完整性；Linux 可选 Kiri 容器挡位。
  隔离依赖不可用时 **fail-closed**（拒绝执行），不静默回退到进程内。
- **工作台**：文件树与只读预览、改动列表与单文件 diff、提交历史与 diff、项目搜索、
  本回合改动、还原点回滚；diff 意见一键回发 Agent，修复后自动重拉；Git 本地写闭环
  （暂存 / 提交 / 丢弃 / 分支切换，**不含 push**）；多项目标签与现场恢复。
- **知识库**：PageIndex 自托管或 Cloud；批量/文件夹导入、内容去重、引用带页码深链。
- **记忆**：结构化条目（slot/key/value）、冲突待裁决、整理与摘要。
- **扩展**：技能、Agent Profile、MCP 插件、资源市场（安装前展示权限声明）。
- **团队模式**：账号/邀请/角色、会话分享、会话地图、后台运维（用户/配额/审计/模型/服务商）。
- **安全与合规**：Provider 密钥信封加密 + 可插拔 KMS + 轮换；审计日志；`no_retention`
  隐私模式；敏感操作审批。

## 架构

```
Electron（Windows 桌面）
  ├─ Go 网关（唯一业务入口，默认 :8080）
  ├─ Python 引擎（FastAPI，Agent/工具/知识库/记忆，默认 :8000）
  ├─ PageIndex 服务（文档索引与检索，默认 :8100）
  └─ 本地数据栈（embedded PostgreSQL + bundled Redis）

团队模式：浏览器 ──TLS/反代──► Go 网关 ──► Python 引擎 + PostgreSQL + Redis
```

- **Go 网关**：认证、租户隔离、路由、SSE 事件、用量与配额、管理端 API。
- **Python 引擎**：Agent 循环、工具执行、子 Agent、知识库、记忆、队列消费者。
- **Vue 3 + Pinia + Ant Design Vue**：桌面与 Web 共用同一套前端。
- **PostgreSQL** 是权威存储；**Redis** 承担队列、限流、事件扇出与会话运行态。

## 快速开始（源码开发）

前置：Go 1.26、Python 3.11+、Node 22 + pnpm 10、PostgreSQL、Redis。

```bash
cp .env.example .env               # 至少设置 APP_SECRET（≥32 字符）
python scripts/init.py             # 交互式生成密钥、写 .env、自检依赖
python run.py setup                # 安装引擎/前端依赖
python -m alembic upgrade head     # 建库（迁移由发布流程执行，应用不自动迁移）
python run.py start                # 网关 + 引擎 + PageIndex（+ 可选 MCP Broker）

cd frontend-vue && pnpm install && pnpm dev   # http://localhost:5173
```

源码态首次注册的账号自动成为 owner；桌面安装包没有注册入口，身份绑定 Windows 账户
（见[本地模式契约](docs/desktop-local/contracts.md)）。

> 本机没有沙箱服务时需在 `.env` 设 `SANDBOX_BACKEND=local`：默认 `service` 且不可用会
> **拒绝启动**（这是有意的 fail-closed）。

## 部署

当前版本**只支持 Windows 桌面端部署**（Electron + 本机反向代理）：

```powershell
# 管理员 PowerShell：登录自启 + 崩溃重启
.\deploy\windows\install-autostart.ps1 -AppPath "$env:LOCALAPPDATA\Programs\Chiron\Chiron.exe"

# 停机一致性备份
.\deploy\windows\backup.ps1 -BackupDir D:\ChironBackups
```

- 域名 / TLS / Caddy 反代：[团队域名部署](docs/team-domain-deployment.md)
- 备份恢复、升级回滚、告警、故障速查、KMS 轮换：[运维手册](docs/ops-runbook.md)
- 升级前必须先跑 `alembic upgrade head`；schema 不匹配时网关拒绝启动。

## 目录结构

```
cmd/               网关与 CLI
internal/          Go 网关（api/auth/session/engine/storage/monitor…）
python-engine/     引擎（agent/queue/tools/knowledge/memory/subagent…）
frontend-vue/      Vue 3 前端 + Electron 主进程
desktop-local/     本地数据栈引导器（embedded PG + bundled Redis）
migrations/        Alembic 迁移（单一 head）
market/skills/     内置技能资产
pageindex-service/ 文档索引服务
sandbox-service/   独立执行沙箱服务
scripts/           初始化、集成测试、备份/恢复、依赖审计
deploy/            Windows 部署脚本、Prometheus 告警规则
```

## 测试与质量门禁

```bash
make check          # Go build/vet/test + 文档守卫
make check-all      # 再加 Python(pytest/ruff/mypy) 与前端(build/test)
make smoke          # 与 CI 同口径的冒烟（可加 --fast）
make integration    # 需要 docker：submit→usage→checkpoint→分享→知识库→隔离档
make integration-static
make dep-audit      # govulncheck / pip-audit / pnpm audit（缺工具记 SKIP）
```

前端另有 `pnpm run check:ui`：主题 token、z-index、动效、字号棘轮、i18n、a11y 静态契约、
transcript 单写者、RTL 逻辑属性。

## 文档

| 想知道 | 去哪 |
|---|---|
| 产品范围、现状与后续路线 | [PRD](docs/prd-remote-team.md) |
| 本地模式契约（运行模式、preload、数据隔离、组件、推理代理） | [desktop-local/contracts.md](docs/desktop-local/contracts.md) |
| 开发环境、配置、常用命令 | [CONTRIBUTING.md](docs/CONTRIBUTING.md) |
| 团队域名与反代部署 | [team-domain-deployment.md](docs/team-domain-deployment.md) |
| 备份恢复 / 升级回滚 / 告警 / 故障处置 | [ops-runbook.md](docs/ops-runbook.md) |
| 主题与前端设计契约 | [style.md](frontend-vue/src/style.md) |
| i18n 与 RTL 约定 | [i18n/README.md](frontend-vue/src/i18n/README.md) |

## 致谢

设计思想或开源代码参考：

- deepseek-harness:super-agents — <https://www.bilibili.com/opus/1265361021143351318>
- 会话地图 — <https://github.com/liangmianya/dsh-synapse>

## 许可

MIT（见 [LICENSE](LICENSE)）。
