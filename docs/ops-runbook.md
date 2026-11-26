# 运维手册（上线最小集）

> 目标是**能重复执行**：备份→恢复、升级→回滚、故障→定位，每条都有命令或明确判据。
> 未覆盖项列在末尾，不要当成"已经做完"。

## 1. 先定部署形态

| 形态 | 适用 | 需要补的 |
|---|---|---|
| **A. 单机桌面托管** | 小团队、Windows 主机 + Caddy | 进程守护（NSSM/计划任务）、日志轮转、数据目录备份、单点故障说明 |
| **B. 容器化** | 多实例/云主机 | 生产 compose（gateway/engine/pageindex/pg/redis + healthcheck + 资源上限 + 卷）、迁移 job、反代 |

当前仓库只有集成用 compose；生产编排需要按上表补齐。`make dev-up`/`docker-build` 引用的文件不在仓库里，不要照抄。

## 2. 生产环境基线（必须显式设置）

| 变量 | 要求 |
|---|---|
| `APP_SECRET` | ≥32 字符随机值；**离机托管**。丢失 = 密文配置/provider key 不可解 |
| `JWT_SECRET` / `INTERNAL_TOKEN` | 不设则由 `APP_SECRET` 派生；多实例必须一致 |
| `POSTGRES_DSN` | 生产库；只读副本另配 `POSTGRES_READ_DSNS` |
| `REDIS_ADDR` / `REDIS_PASSWORD` | Redis 是硬依赖：不可用时限流/撤销校验会拒绝请求（503），不是"功能变慢" |
| `BIND_HOST` / `PUBLIC_BASE_URL` / `COOKIE_SECURE` | 团队模式：`0.0.0.0` + 域名 + `1`；网关应只对反代开放 |
| `TRUSTED_PROXY_CIDRS` | 只有来自这些网段的 `X-Forwarded-For` 才被采信；不配 = 不信任任何转发头 |
| `KMS_KEK_KEYS` / `KMS_ACTIVE_KEY_ID` | 可选；显式 KEK 时**网关与引擎都要下发**（Redis keyset 是密文） |
| `STORAGE_BACKEND` / `MEDIA_STORE_BACKEND` | 生产用 `s3`；两者必须同值 |
| 配额 | 后台「配额」默认 0=不限且读库失败 fail-open；团队模式必须设租户/用户日费用上限 |

反向代理的 SSE 配置见 `team-domain-deployment.md`（关闭缓冲、放宽读写超时）。

## 3. 备份与恢复（上线前必须演练一次）

```bash
# 备份（建议维护窗口内停写；脚本不替你做停机决策）
POSTGRES_DSN='postgresql://…' CHIRON_DATA_DIR=/data \
  ./scripts/backup.sh /backups/chiron-$(date +%F)

# 恢复到空环境（会覆盖目标库/卷；必须显式 --yes）
POSTGRES_DSN='postgresql://…' CHIRON_DATA_DIR=/data \
  ./scripts/restore.sh /backups/chiron-2026-11-27 --yes
```

- 备份包含：`db.dump`、`volumes.tgz`（项目文件/检查点/知识库原文）、`manifest.txt`、`SHA256SUMS`。
- **不包含**：`APP_SECRET`、`KMS_KEK_KEYS`。它们单独托管（Vault/KMS/密封信封），恢复时先对齐。
- 卷里排除 `local/pgdata`、`local/redis`：嵌入式 PG 用 `db.dump` 恢复，不直接拷数据目录。
- 演练判据：空环境恢复 → 启动（迁移门禁通过）→ `team_smoke --sse` 登录/会话/文件/分享逐项 PASS。
- 备份包含用户内容与密文，权限 0600 并加密离机保存；保留期与 `usage/shares/checkpoints/turns` 的 retention 分开管理。

## 4. 升级与回滚

固定顺序，**不要跳步**：

1. 记录当前版本（`git rev-parse --short HEAD` / 镜像 tag）与 `alembic_version`；
2. 备份（上一节）；
3. 停新写入：反代摘流量或停网关；等 `DrainInFlightRuns` 日志结束（最长 10s 宽限）；
4. 跑迁移：`alembic upgrade head`（应用**不会**自动迁移；版本不匹配会拒绝启动并只开诊断端口）；
5. 启动新版本 → `GET /ready` 200 → 跑 `team_smoke --sse`；
6. 失败回滚：停新版本 → 恢复上一版镜像/安装包 → 用备份恢复 DB 与卷（迁移不可逆时这是唯一路径）。

注意：`APP_SECRET` 不变时，密钥/配置无需重做；KMS 轮换见第 7 节。

## 5. 健康与告警

| 端点 | 语义 |
|---|---|
| 网关 `/health` | 进程存活，不查依赖（liveness） |
| 网关 `/ready` | PG / Redis 就绪（readiness） |
| 引擎 `/healthz` | 进程存活 |
| 引擎 `/readyz` | Redis + （配置了 DSN 时）PostgreSQL |
| 网关 `/metrics` | Prometheus 文本；引擎 `/metrics` 同名端点 |

最小告警集见 `deploy/prometheus-alerts.yml`（网关/引擎不可达、SSE 积压与同步降级、队列积压、
DLQ 增长、限流降级、内存异常）。先接这组，再按事故复盘补规则。

## 6. 故障处置速查

| 现象 | 先看 | 处置 |
|---|---|---|
| 全员 401/被登出 | Redis 是否抖动 | 已改为"存储不可用 → 503"；若仍是 401，查 `session_revocation` 日志与前端版本 |
| 接口大量 503 | Redis 可用性 | Redis 是硬依赖；恢复 Redis 后自动恢复，不要重启网关来"修复" |
| 模型全部失败 | provider 熔断 / key | 后台看熔断与 key 状态；`llm_circuit_state`、`no_usable_key` 日志 |
| 长任务"卡住" | SSE 连接与 `running` | 刷新后会自动重挂；若仍无增量，看 `chiron_sse_publish_*` 与引擎队列 |
| 任务消失/重复 | DLQ、`claim_count`、幂等键 | `queue_dlq_total`、worker 日志；重投走 DLQ 接口，不要手工 XADD |
| 上传失败 | 反代 `client_max_body_size`、读超时 | 反代与网关 `READ_TIMEOUT` 都要放宽；分片上传受每片限制 |
| 磁盘涨 | 检查点/媒体/知识库/日志 | 后台 retention 只覆盖 usage/shares/checkpoints/turns；其余按第 8 节手工治理 |

## 7. 密钥轮换

1. 先加新 KEK：`KMS_KEK_KEYS` 同时保留旧+新，`KMS_ACTIVE_KEY_ID` 指向新；
2. `GET /v1/admin/kms/status` 看规模 → `POST /v1/admin/kms/rotate {"dry_run":true}`；
3. 正式 rotate → 确认 `failed=0`；
4. 网关与引擎都更新后，再删旧 KEK；
5. `APP_SECRET` 轮换会让所有 JWT 失效（全员重新登录），且旧 v1 密文需要先完成迁移，按维护窗口执行。

## 8. 已知未覆盖

- 多实例演练（C2）、Kiri 容器挡位真机 E2E（C1b）、团队主机压测（C1）仍需环境；
- 发布签名（H51）未落地：自动更新只校验同一仓库清单的 SHA-512；
- 媒体/知识库 blob/日志/审计的自动清理未纳入 retention；
- 容器化生产编排尚未入库（第 1 节 B 形态）。
