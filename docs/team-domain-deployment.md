# 团队服务模式：Windows 主机 + 反向代理

目标：成员**不安装客户端**，浏览器打开 `https://<域名>`，用邮箱/用户名 + 密码登录。
主机上运行 Chiron 桌面程序（owner = 本机首个 Windows 用户），由本机反向代理终止 TLS
并把域名转发给网关。

> 服务模式是后台的高级开关；打开后关闭公开注册，成员由「后台 → 用户管理」创建。
> 本版本**只支持 Windows 桌面端部署**；容器/WSL 编排不在承诺范围内（见 §9）。

## 1. 前置条件

1. 在服务器主机安装并运行 Chiron（owner = 首个 Windows 用户）；
2. 为 owner 设置密码：**个人资料 → 修改密码**（首次设置不要求旧密码）；
3. 域名（如 `chiron.example.com`）的 DNS A/AAAA 记录指向该主机；
4. 主机允许 80/443 入站，**网关端口不对外暴露**。

## 2. 固定网关端口

桌面壳默认使用随机空闲端口，反代无法稳定指向。设置系统环境变量后重启 Chiron：

```bat
setx CHIRON_GATEWAY_PORT 8088
```

确认监听：`netstat -ano | findstr :8088`。

## 3. 打开服务模式

后台「服务」页：

1. **团队服务模式**：打开；
2. **访问范围 / 绑定地址**：`127.0.0.1`（只允许本机反代访问）；
3. **对外地址**：`https://chiron.example.com`（决定 Cookie Secure 推导与分享链接）；
4. 保存。监听地址与对外地址需**重启网关**生效；服务模式开关保存即生效。

> 不要把 8088 暴露到局域网/公网：那等于绕过 TLS 与反代的安全头。域名访问由本机反代完成。

## 4. 反向代理

### Caddy（推荐）

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

生产建议用 `nssm`/计划任务把 Caddy 注册为服务。

### Nginx 替代

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

        proxy_buffering off;            # SSE
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

前端产物由网关同源托管（`resources/web`）。页面空白时先确认
`resources/web/index.html` 存在，并在启动日志里看到 `SPA hosting enabled`。

## 5. 防火墙

```powershell
New-NetFirewallRule -DisplayName "Chiron HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
New-NetFirewallRule -DisplayName "Chiron HTTP"  -Direction Inbound -Protocol TCP -LocalPort 80  -Action Allow
# 8088 不开放
```

## 6. 首次验收

1. 另一台电脑打开 `https://chiron.example.com` → 登录页（无注册入口）；
2. owner 登录 → 后台「用户管理」新建成员，复制一次性初始密码；
3. 成员用邮箱/用户名 + 初始密码登录 → 强制改密 → 进入工作台；
4. 刷新保持登录；「退出登录」回登录页；
5. 分享链接 `https://chiron.example.com/s/<token>` 匿名可打开；
6. 禁用成员后，其已登录页面下一次请求被退回登录页；
7. 对话流式输出正常（不是最后一次性刷出）。

## 7. 运行与排障

- **主机不能睡眠/休眠**；电源计划设为「从不睡眠」，并让 Chiron 登录自启（§8）。
- 日志：`%APPDATA%\gh_Athenavi_chiron\logs\`（应用/网关/引擎），
  网关与引擎日志由 `run.py` 追加写入并 10MB×5 轮转。
- 端口：`%APPDATA%\gh_Athenavi_chiron\local\runtime-state.json` 记录本次实际端口。
- 证书：Caddy 数据在 `%APPDATA%\Caddy`；续期失败先查 80 端口是否可达。
- 常见现象：
  - 登录后立刻变未登录：`PUBLIC_BASE_URL` 是 https 但用 http 访问，Secure Cookie 不回传；
  - 对话卡住才一次性输出：反代未关闭 SSE 缓冲；
  - 页面 404：`CHIRON_WEB_ROOT` 未解析到 `resources/web`；
  - 改端口后打不开：端口被占用，换一个或释放。

## 8. 进程守护、备份与升级

```powershell
# 登录自启 + 崩溃每分钟重启（管理员 PowerShell）
.\deploy\windows\install-autostart.ps1 -AppPath "$env:LOCALAPPDATA\Programs\Chiron\Chiron.exe"

# 停机一致性备份（运行中默认拒绝；维护窗口 -Force 风险自负）
.\deploy\windows\backup.ps1 -BackupDir D:\ChironBackups
```

- 卸载自启：`.\deploy\windows\uninstall-autostart.ps1`；
- 服务器需为该账户配置**自动登录**，否则重启后任务不会启动；
- 升级：备份 → 退出 Chiron → 覆盖安装新版本 → 启动/登录/发一条消息确认；
  失败则卸新装旧，必要时用备份恢复数据目录；
- 备份**不包含** `APP_SECRET` / `KMS_KEK_KEYS`，需单独托管；丢失后密文配置不可解；
- 更多运维动作见 [ops-runbook.md](ops-runbook.md)。

### 8.1 部署后验收（可重复）

```bat
set CHIRON_OWNER_PASSWORD=<owner 密码>
python scripts/team_acceptance.py --base-url https://<域名> --email owner@<公司> ^
    --r2-root R:\project --sse --strict-web ^
    --json-out .artifacts\team-acceptance.json
```

- 阶段：`env`（TLS/`/health`/`/ready`/凭据）→ `r2`（读 P95≤50ms、写 P95≤150ms、git 可用）
  → `web`（登录、首登改密、邀请、强制下线、匿名分享页、可选 SSE）→ `r6`（压测结果）；
- 退出码：`0` 无 FAIL 且有 PASS；`1` 有 FAIL；`77` 全部 SKIP（环境不具备，**不是通过**）；
- 只验 Web 闭环：`scripts/team_smoke.py --base-url ... --email ... --sse`；
  只验挂载：`scripts/r2_mount_bench.py --root R:\project`。

## 9. 与桌面单机模式、容器拓扑的关系

- 同一套前端产物：桌面窗口用 `file://`，浏览器用域名；桌面专属页（UAC、本机隔离档、
  运行模式、引擎环境）在 Web 端自动隐藏，后端也拒绝相应端点；
- owner 仍可在本机回环免口令进入（`LOCAL_BOOTSTRAP`），用于首次设置密码与运维；
- 关闭服务模式后公开注册恢复（除非 `DISABLE_REGISTRATION` 或 `CHIRON_DEPLOY_MODE=team`）；
- **容器/WSL 编排（含 Kiri/sysbox）不在本版本承诺**：当前部署形态是 Windows 桌面 +
  本机反代。若未来改为容器化，需要先定"网关/引擎/沙箱运行在哪、数据落在哪"，
  并补齐生产 compose、迁移 job 与容量规划。
