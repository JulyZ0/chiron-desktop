# 团队服务模式：域名直连部署（Windows 主机 + 反代）

目标：成员**不安装客户端**，浏览器打开 `https://<域名>`，用邮箱/用户名 + 密码登录。
主机上运行 Chiron 桌面程序（owner 即本机首个 Windows 用户），由本机反向代理终止 TLS
并把域名转发给网关。

> 服务模式是后台的一个高级开关；打开后关闭公开注册，成员由「后台 → 用户管理」创建。

---

## 1. 前置条件

1. 在服务器主机上安装并运行 Chiron（owner = 首个 Windows 用户）。
2. 为 owner 设置密码：**个人资料 → 修改密码**（`password_set=false` 时首次设置不要求旧密码）。
3. 一个域名（如 `chiron.example.com`）的 DNS A/AAAA 记录指向这台主机。
4. 主机允许 80/443 入站；网关端口不对外暴露。

---

## 2. 固定网关端口

桌面壳默认使用随机空闲端口，反代无法稳定指向。设置系统环境变量后重启 Chiron：

```bat
setx CHIRON_GATEWAY_PORT 8088
```

（设置后需要重新启动 Chiron 进程。可用 `netstat -ano | findstr :8088` 确认监听。）

---

## 3. 打开服务模式

后台「服务」页：

1. **团队服务模式**：打开。
2. **访问范围 / 绑定地址**：填 `127.0.0.1`（只允许本机反代访问，最安全）。
3. **对外地址**：`https://chiron.example.com`（决定 Cookie 的 Secure 推导与分享链接）。
4. 保存。监听地址与对外地址需要**重启网关**生效（页面会提示）；服务模式开关保存即生效。

> 为什么不选「局域网」：域名访问由本机反代完成，网关只需对回环开放。把 8088 暴露到
> 局域网/公网等于绕过 TLS 和反代的安全头。

---

## 4. 反向代理（Caddy 推荐）

Caddy 自动申请/续期证书，且默认支持 SSE（关闭缓冲）。

`Caddyfile`：

```caddyfile
chiron.example.com {
    encode zstd gzip

    # SSE / 长连接：不要缓冲，超时放宽
    reverse_proxy 127.0.0.1:8088 {
        flush_interval -1
        transport http {
            read_timeout 10m
            write_timeout 10m
        }
    }

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options nosniff
        Referrer-Policy strict-origin-when-cross-origin
        -Server
    }
}
```

启动：

```bat
caddy run --config Caddyfile
```

生产建议用 `nssm`/计划任务把 Caddy 注册为服务。

### Nginx 替代示例

```nginx
server {
    listen 443 ssl http2;
    server_name chiron.example.com;

    ssl_certificate     C:/certs/fullchain.pem;
    ssl_certificate_key C:/certs/privkey.pem;

    client_max_body_size 70m;          # 知识库单文件上限 64MB

    location /v1/ {
        proxy_pass http://127.0.0.1:8088;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE：对话流式输出
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }

    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

> 前端产物由网关同源托管（`resources/web`）。如果页面空白，先确认安装包的
> `resources/web/index.html` 存在，并在启动日志里看到 `SPA hosting enabled`。

---

## 5. 防火墙（建议）

```powershell
New-NetFirewallRule -DisplayName "Chiron HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
New-NetFirewallRule -DisplayName "Chiron HTTP"  -Direction Inbound -Protocol TCP -LocalPort 80  -Action Allow
# 8088 不开放
```

---

## 6. 首次验收

1. 手机/另一台电脑打开 `https://chiron.example.com` → 出现登录页（无注册入口）。
2. owner 登录 → 后台「用户管理」新建成员，复制一次性初始密码。
3. 成员用邮箱或用户名 + 初始密码登录 → 强制设置新密码 → 进入工作台。
4. 刷新页面保持登录；点「退出登录」回登录页。
5. 分享链接 `https://chiron.example.com/s/<token>` 匿名可打开。
6. 禁用该成员后，其已登录页面下一次请求被退回登录页。
7. 对话流式输出正常（不是最后一次性刷出）。

---

## 7. 运行与排障

- **主机不能睡眠/休眠**；建议电源计划设为「从不睡眠」，并让 Chiron 随开机启动。
- 日志：Electron 主日志在 `%APPDATA%\gh_Athenavi_chiron\logs\main.log`（含网关与引擎 stderr）。
- 端口：`%APPDATA%\gh_Athenavi_chiron\local\runtime-state.json` 记录本次实际端口。
- 证书：Caddy 数据在 `%APPDATA%\Caddy`；续期失败先查 80 端口是否可达。
- 常见现象：
  - 「登录后立刻又变未登录」：`PUBLIC_BASE_URL` 是 https 但访问的是 http，Cookie 带 Secure 不会被回传。
  - 「对话卡住才一次性输出」：反代未关闭 SSE 缓冲（Caddy `flush_interval -1` / Nginx `proxy_buffering off`）。
  - 「页面 404」：`CHIRON_WEB_ROOT` 未解析到 `resources/web`，看启动日志的 `resource layout`。
  - 「改了端口重启后打不开」：`CHIRON_GATEWAY_PORT` 被占用；释放端口或换一个。

---

## 8. 与桌面单机模式的关系

- 同一套前端产物：桌面窗口用 `file://`，浏览器用域名；桌面专属页（UAC 提权助手、
  本机隔离档位、运行模式、引擎环境）在 Web 端自动隐藏，后端也会拒绝相应端点。
- owner 仍可在本机回环免口令进入（`LOCAL_BOOTSTRAP` 保留），用于首次设置密码与运维。
- 关闭服务模式后，公开注册重新可用（除非 `DISABLE_REGISTRATION` 或 `CHIRON_DEPLOY_MODE=team` 阻止）。

### 8.1 部署后验收（可重复）

```bat
set CHIRON_OWNER_PASSWORD=<owner 密码>
python scripts/team_acceptance.py --base-url https://<域名> --email owner@<公司> ^
    --r2-root R:\project --sse --strict-web ^
    --json-out .artifacts\team-acceptance.json
```

- 阶段：`env`（TLS/`/health`/`/ready`/凭据）→ `r2`（读 P95≤50ms、写 P95≤150ms、git 可用）
  → `web`（登录、首登改密、邀请、强制下线、匿名分享页、可选 SSE）→ `r6`（摄入压测结果）。
- 退出码：`0` 无 FAIL 且有 PASS；`1` 有 FAIL；`77` 全部 SKIP（缺主机/挂载点/凭据，
  是"环境不具备"而不是通过）。CI 可直接用 `--json` 消费。
- 没有团队主机时先跑 `python scripts/team_acceptance.py`：四项都应为 SKIP 且退出 77；
  任何 SKIP 都会列在 `pending_real_host`，不会被当成已验证。
- 只验 Web 账号闭环可用 `python scripts/team_smoke.py --base-url ... --email ... --sse`；
  只验挂载点可用 `python scripts/r2_mount_bench.py --root R:\project`。
- 这些脚本只读配置/跑临时文件，不修改服务端代码；测试账号在结束时软删除，失败也会清理。

---

## 9. 运行隔离与容量规划（WSL2 + 容器）

> 现状先说清楚：当前 **shell/终端命令** 可走独立 `sandbox-service`；但模型生成的
> **Python 代码（`run_code`）仍由引擎进程拉起子进程执行**，文件工具也在引擎侧做
> 路径钳制。下面的容器方案是目标形态，落地前需要先定"引擎跑在哪里"（见 §9.3）。

### 9.1 目标架构

| 层面 | 决策 |
| :--- | :--- |
| 宿主 | Windows + **Docker Engine**（非 Desktop；`--register-service`，`docker-users` 组授权） |
| Linux 子系统 | WSL2，`.wslconfig` 硬限制 `memory` / `processors` |
| 容器运行时 | `sysbox-runc`（用户命名空间 + `/proc`/`/sys` 虚拟化），作为按需运行时 |
| Agent 镜像 | Alpine + Python 3.14 + Git，非 root（UID 1000），`--cap-drop=ALL` |
| 数据 | 每用户目录在 WSL2 内 `/var/agent-data/{user_id}`，bind mount 进容器（避开 9P） |
| 资源 | 单容器 `--memory 512m`（重型 1g）、`--cpus 1`、`pids-limit`；WSL2 层总上限兜底 |
| 日志 | Docker `max-size=10m` / `max-file=3`，按 `tenant/user/session` 打标签 |
| 命名 | 容器名从会话 UUID 派生；标签 `managed-by=agent-platform`、`tenant`、`user`、`session` |

### 9.2 容量估算（并发活跃比例：15 人约 50%，100 人约 20–30%）

| | 15 人 | 100 人 |
| :--- | :--- | :--- |
| 内存 | **32 GB**（下限 24 GB） | **128 GB**（下限 96 GB，需强并发控制） |
| CPU | **8 vCPU**（下限 6） | **32 vCPU**（下限 16，需任务队列） |
| 磁盘 | **500 GB SSD**（VHDX 上限 300 GB） | **2 TB SSD**（VHDX 上限 1–1.5 TB） |
| 并发假设 | ~7–8 活跃 | ~20–30 活跃 |
| 单容器内存 | 256–512 MB | 256–512 MB，重型单独放宽 |

内存不是"容器工作集之和"：WSL2 动态膨胀且不主动归还，32/128 GB 主要是为峰值与
页缓存留余量。`.wslconfig` 模板见 [`deploy/wsl/.wslconfig.example`](../deploy/wsl/.wslconfig.example)。

两个比总内存更关键的杠杆：

1. `.wslconfig` 的 `memory` 是硬闸，必须设置（建议物理内存减去 Windows 侧 8–16 GB）。
2. `ext4.vhdx` 只增不缩：监控文件大小，低峰期 `wsl --shutdown` 后压缩；容器删除不会
   自动释放 Windows 磁盘。

### 9.3 必须先定的拓扑

sysbox 容器运行在 WSL2 内，数据也必须在 WSL2 内；而当前打包版 **Python 引擎跑在
Windows 上**，文件工具写的是 Windows 路径。两者不能直接共用目录（9P 性能与路径映射），
所以需要二选一：

- **A. 全栈进 WSL2/Linux（推荐）**：网关 + 引擎 + pageindex + 沙箱用 Docker Compose
  跑在 WSL2；Windows 只跑 Docker Engine 与反代。数据天然在 `/var/agent-data`。
  代价：服务器形态不再用 Electron 桌面壳，需要独立的 owner 引导/部署入口。
- **B. 引擎留在 Windows + Docker Desktop**：容器 bind mount Windows 路径（9P 慢，
  且与"数据放 WSL2"的决策冲突）。不推荐。
- **C. 网关在 Windows、引擎在 WSL2**：需要跨边界共享项目目录，文件工具与执行必须
  走同一层（即文件操作也要远程化），改造量最大。

**确认拓扑后再落容器执行**；在确认前，已落地的隔离是：
数据侧（项目目录按用户根隔离）、执行侧（shell 走独立 sandbox-service + 资源限制，
`run_code` 仍走引擎进程的 Windows 隔离档位）。

---

## 10. Windows 桌面托管（当前唯一支持形态）

2026-11-27 决定：**本版本只支持 Windows 桌面端部署**（Electron 壳 + 本机反代）。
§9 的 WSL2/容器拓扑保留为未来选项，未纳入本版本承诺。

### 10.1 进程守护

Electron 需要交互式会话，不能用 Windows 服务（Session 0 无 GUI）。用任务计划程序：

```powershell
# 管理员 PowerShell
.\deploy\windows\install-autostart.ps1 `
  -AppPath "$env:LOCALAPPDATA\Programs\Chiron\Chiron.exe"
```

- 登录时启动、最高权限、失败每分钟重启（不限次数）；
- 服务器需为该账户配置**自动登录**（否则重启后没人登录，任务不会启动）；
- 卸载：`.\deploy\windows\uninstall-autostart.ps1`。

### 10.2 日志

- 应用/网关/引擎：`logs/*.stdout.log` / `*.stderr.log`，**追加 + 10MB × 5 轮转**
  （`run.py` 已改为不截断历史）；
- 嵌入式 PG/Redis：数据目录 `local/logs/`；
- 审计事实在数据库 `audit_logs`，不要依赖日志文件做合规检索。

### 10.3 备份

```powershell
.\deploy\windows\backup.ps1 -BackupDir D:\ChironBackups
```

必须先退出 Chiron（运行中复制 PGDATA 会损坏）。备份包含数据目录但不含
`APP_SECRET` / `KMS_KEK_KEYS` —— 它们单独托管，丢失后密文配置不可解。
恢复：停应用 → 解压覆盖 → 启动 → `python scripts/team_smoke.py --sse` 验收。

### 10.4 升级与回滚

1. 备份；
2. 退出 Chiron；
3. 覆盖安装新版本（数据目录不变）；
4. 启动 → 登录 → 发一条消息确认模型可用；
5. 失败：卸载新版本 → 安装旧版本 → 必要时用备份恢复数据目录。

### 10.5 单机边界

- 无副本冗余：主机宕机 = 服务中断；备份与**定期恢复演练**是唯一保障。
- 自动更新只校验同一发布仓库的 SHA-512，未接入发布签名（H51）。
- 更多运维动作（健康检查、告警、故障速查、KMS 轮换）见
  [ops-runbook.md](ops-runbook.md)。
