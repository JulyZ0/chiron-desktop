# Chiron 桌面端「开机自启 + 崩溃重启」（Windows）

> 适用：把 Chiron 桌面程序常驻在一台 Windows 主机上，作为团队服务端。
> 用**任务计划程序**而不是 Windows 服务：Chiron 是 Electron 应用，需要交互式用户会话
> （服务运行在 Session 0，GUI/托盘不可用）。因此主机需要保持该用户登录（可配置自动登录）。

## 安装

管理员 PowerShell：

```powershell
.\deploy\windows\install-autostart.ps1 `
  -AppPath "$env:LOCALAPPDATA\Programs\Chiron\Chiron.exe"
```

不传 `-AppPath` 时会依次尝试：

- `%LOCALAPPDATA%\Programs\Chiron\Chiron.exe`
- `%PROGRAMFILES%\Chiron\Chiron.exe`
- `%PROGRAMFILES(X86)%\Chiron\Chiron.exe`

脚本会：

1. 创建 `Chiron` 计划任务：**当前用户登录时启动**、最高权限、失败每分钟重启（不限次数）；
2. 立即启动一次（若已在运行则跳过）；
3. 打印任务状态与数据目录。

> 为什么用「登录时」而不是「开机时」：`-AtStartup` 的任务在无人登录时无法显示
> Electron 窗口；若主机配置了自动登录，二者效果等价。

## 卸载

```powershell
.\deploy\windows\uninstall-autostart.ps1
```

## 检查

```powershell
Get-ScheduledTask -TaskName Chiron | Get-ScheduledTaskInfo
Get-Process Chiron -ErrorAction SilentlyContinue
```

## 备份

停机一致性备份（会检查进程是否在跑）：

```powershell
.\deploy\windows\backup.ps1 -BackupDir D:\ChironBackups
```

- 默认打包 `%APPDATA%\gh_Athenavi_chiron`（含嵌入式 PG/Redis 数据、日志、配置）。
- **必须先退出 Chiron**：运行中复制 PGDATA 会得到损坏快照。脚本检测到进程默认拒绝，
  维护窗口内可加 `-Force`（风险自负）。
- 备份包含用户内容与密文配置；`APP_SECRET`/`KMS_KEK_KEYS` **不在其中**，需单独托管。
- 恢复：停应用 → 解压覆盖数据目录 → 启动 → `python scripts/team_smoke.py --sse` 验收。

## 日志

- 应用/网关/引擎日志由 `run.py` 写入 `logs/*.stdout.log`、`*.stderr.log`，
  **追加 + 10MB × 5 份轮转**（不再每次启动截断）。
- 嵌入式 PG/Redis 日志在数据目录 `local/logs/`。
- 不要用 `logs` 目录做长期审计：审计事实在数据库 `audit_logs`。

## 升级

1. 备份（上一节）；
2. 退出 Chiron（托盘 → 退出，或 `Stop-Process -Name Chiron`）；
3. 运行新安装包覆盖安装（数据目录不变）；
4. 启动 → 登录 → 发一条消息确认模型可用；
5. 失败回滚：卸载新版本 → 安装旧版本 → 必要时用备份恢复数据目录。

## 需要确认的边界

- 单机部署没有副本冗余：主机宕机 = 服务中断。备份与定期恢复演练是唯一保障。
- 反向代理/TLS/域名按 `docs/team-domain-deployment.md` 配置（Caddy 推荐）。
- 自动更新只校验同一发布仓库的 SHA-512，未接入发布签名（H51）。
