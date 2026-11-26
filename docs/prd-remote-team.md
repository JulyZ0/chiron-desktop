# PRD：Chiron 团队服务模式 —— 上线阻塞与后续工作

> 只保留**未完成**项与已定决策。已完成项的结论在 `git log`（提交信息带 ID 与"修前会怎样"），
> 证据在对应测试文件。契约版本见 `contracts/versioned.json`，外部验收入口见
> `scripts/team_acceptance.py`。

## 1. 当前状态

S0–S4、R1/R3/R4/R5、H1–H86 可本机验证部分、G1–G30、A 波（KB 链路/状态归一/租户归属/
Provider 亲和）、H20 信封加密均已合入。2026-11-26 完成一轮**生产就绪审计**（Go 网关 /
Python 引擎 / 前端三面），发现的问题集中在"长连接、停机、合规、运行面"，不是功能缺口。
当前没有在飞的 worktree。

## 2. 上线阻塞（P0）

| ID | 事项 | 为什么阻塞 | 完成判据 | 量级 |
|---|---|---|---|---|

## 3. 上线后首月加固（P1）

### 3.1 网关

| ID | 事项 | 证据/后果 | 量级 |
|---|---|---|---|
| L07 | 分享密码可爆破（匿名 per-IP 限流已补） | 密码最短 4 位，无 per-share/IP 失败计数与短锁 | S |
| L08 | 停机宽限 10s < 在途预算 5min；后台协程不 join | 滚动发布切断长请求；审计/画布最终 flush 可能丢 | M |
| L11 | DB 无 acquire/查询超时；无请求超时中间件 | 池耗尽后整实例雪崩 | M |
| L13 | `REDIS_KEY_PREFIX` 在包级 var 里丢前缀 | 仅 `.env` 配置的多环境共用 Redis 会串键 | S |

### 3.2 引擎 / 队列

| ID | 事项 | 证据/后果 | 量级 |
|---|---|---|---|
| L19 | 无实例级并发上限；沙箱配额默认关闭 | 绕过网关直连/自动续跑可打满进程 | M |

### 3.3 前端

| ID | 事项 | 证据/后果 | 量级 |
|---|---|---|---|
| L23 | 首屏预加载仍含 charts/diagram ~0.8MB br（fileviewer 1.8MB 已移除；二者仍随入口预载，成因待查） | 弱网首屏被图表/图形库抢带宽 | S |

### 3.4 运维 / 供应链

| ID | 事项 | 完成判据 | 量级 |
|---|---|---|---|
| L26 | 备份/恢复演练 | 脚本与 runbook 已备（`scripts/backup.sh`/`restore.sh`、`docs/ops-runbook.md`）；**空环境恢复并跑 team_smoke 一次**待做 | M |
| L27 | 生产部署形态与入口 | 二选一：桌面托管（NSSM/日志轮转/重启）或容器化（prod compose + 迁移 job + 资源上限）；修掉失效的 `make docker-build/dev-up` | L |
| L28 | 升级/回滚演练 | 规程已写入 `docs/ops-runbook.md`；按规程完整演练一次（含迁移失败回滚） | M |
| L29 | 依赖与供应链 | 脚本已备（`scripts/dep_audit.sh`、`make dep-audit[-sbom]`）；CI 接入/镜像扫描/SBOM 归档待做；H51 签名清单待决策 | M |
| L31 | 长会话内存/渲染 | `runtimes`/`seenIds` 淘汰；流式不重投影；`read_file` 折叠 | M |

## 4. 需环境 / 需决策

| 项 | 内容 | 卡点 |
|---|---|---|
| C1 | A1/A3 真实集成 + R2 挂载 + R6 压测 | docker 主机 |
| C1b | Kiri `container` 挡位端到端 | Linux + Kiri |
| C2/C3 | 租户删除、多实例演练、观测看板 | 团队主机 |
| H2/G25 | Linux 内核级隔离；`bwrap` 不可用即拒跑 + 默认关开关 | Linux 真机 + 产品决策 |
| H51 | 发布签名根归属 | 组织决策 |
| H19/H22 | ACP 完整协议；构造性只读子 Agent | 产品决策 |
| E4 | a11y 运行时检查是否引入 axe | 依赖决策 |

## 5. 已定决策（不再讨论）

- **H20**：进程内信封加密 + 可插拔 KMS。`v2:<keyID>:<DEK>:<data>`；KEK 来自 `KMS_KEK_KEYS`
  或 `APP_SECRET` 派生；`/v1/admin/kms/{status,rotate}`；Redis keyset 也只存密文。
- **Kiri 挡位**：`container` 用 Kiri（Docker Engine API），**不内置** Kiri/Docker，按需下载
  + 内嵌 SHA-256 校验；不可用即拒跑；规格不可放宽；Linux 宿主内核，不是 VM；Windows
  命名管道本版不支持。
- **G22** MCP 不并入统一注册表；**G24** 不做定时触发；**G25** 并入 H2。
- **插件化**：不做 Cordis 式"一切皆可替换"；只做能力 seam + 声明式 bundle（候选 E6）。
- **P1-3 steer**：顶层会话维持现状（run 级入口见 E1）。

## 6. 落地路线（由易到难）

**第 1 批 · 已完成（2026-11-26）**
L01 SSE Unwrap、L02 503、L03 上传期限、L04 日志桥、L09/L10 启动自检、L21 全局错误兜底、
L24 CSP、L23 fileviewer 预加载（1.8MB br 已移除；charts/diagram 余量留 P2）。

**第 2 批 · 已完成（2026-11-26）**
L06 前端运行态（running 重挂/切回不清 loading/按会话收尾）、L20 cancelAll 与 outbox 幂等、
L16 会话互斥与审批路由围栏、L18 PG 就绪/队列深度/健康检查、L12 中继 panic 隔离、
L07 匿名 per-IP 限流（分享密码锁定仍待做）。

**第 2 批补充 · 已完成**
L05：no_retention 下**禁用**后台任务（结果写 Redis 24h + followup 回注都会留存；
明确拒绝比"写一半再声称没留存"合规）。其余队列任务的隐私位待评估。

**第 3 批 · 已完成（2026-11-27）**
L15 停机排空/拒绝新 spawn、DLQ 走 `append_task`、reclaim 与业务失败分开计数、flush 不误 ACK
他实例任务；L14 空闲自停加"非 HTTP 活跃工作"探针；L17 后台命令输出边读边限、附件流式
截断（4MB）；L22 SSE 指数退避 + 抖動（8 次）、子 Agent 心跳看门狗、上线后重挂**所有**
运行中的会话。

**第 4 批 · 进行中**
已完成：L25 `gzip_static`、L30 最小告警集（`deploy/prometheus-alerts.yml`）、
L26/L28 的脚本与 runbook（`docs/ops-runbook.md`）。
待做：L27 部署形态与生产编排（需先定 A/B）、L26/L28 的空环境演练、L29 的 CI/镜像扫描接入。

**第 5 批 · 按环境/决策**
C1/C1b/C2/C3 → H2/H51/H19/H22 → L31 长会话性能 → E4 a11y → E1 父→子 steer → E2 字号第二波 → E5 Git 面板增强。

合并纪律：每项自己跑窄回归；合入后在 main 跑跨包回归；每项合入后从本文件删除对应行。

## 7. 外部验收入口

`python scripts/team_acceptance.py --base-url https://<域名> --email owner@<公司> --r2-root R:\project --sse --strict-web`
（退出码 0=无 FAIL 且至少一项 PASS，1=有 FAIL，77=全部 SKIP；**SKIP 绝不当 PASS**）。
R2 挂载、团队 Web E2E、R6 压测的判据见 `scripts/` 下对应脚本输出。

## 8. 参考

- ZCode 对照分析：[gap-analysis-chiron-vs-zcode.md](gap-analysis-chiron-vs-zcode.md)
- 桌面本地模式契约：[desktop-local/contracts.md](desktop-local/contracts.md)
- 团队域名部署：[team-domain-deployment.md](team-domain-deployment.md)
