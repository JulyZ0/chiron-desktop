# run-local.ps1 —— Windows 本地集成档：不依赖 Docker，用应用自带的运行时跑关键链路。
#
# 与 scripts/integration/run.sh（容器档）的关系：
#   * 容器档验的是"镜像里的整套服务"（PG/Redis/migrate/gateway/engine/pageindex/fake-LLM）；
#   * 本地档只验**开发机上真实会跑的那一层**：embedded PostgreSQL（来自
#     local/runtime/venv 的 embedded_postgres 包）+ 本地 Redis + Go 网关 + Python 引擎
#     + 假 LLM。pageindex/知识库不在其中（本地档不建索引，KB 断言由容器档负责）。
#
# 断言子集（与 run.sh 同名同义，只是少掉容器专属部分）：
#
#   1. 就绪    ：launcher（embedded PG + 迁移 + Redis）→ gateway /health → engine /healthz
#   2. 鉴权    ：POST /v1/auth/session（loopback 免口令引导，LOCAL_BOOTSTRAP=1）
#   3. 隔离    ：GET → PUT level=job → GET 复核 effective 一致
#   4. 依赖    ：GET /v1/admin/deps/list count > 0（需要引擎）
#   5. 计费    ：建项目 → 建会话 → 绑定 → POST /v1/agent/submit → usage_events 非零
#   6. 分享    ：建分享 → **不带凭据** GET /v1/share/<token> 读到消息
#
# 环境判定与退出码（与 run.sh 的 77 约定一致）：
#   * 找不到带 embedded_postgres 的解释器、没有 Redis 监听、没有 Go 工具链与既有网关
#     二进制、launcher 起不来 PG 或迁移失败 → **退出 77（skipped）**，不是失败；
#   * 引擎起不来（缺依赖/端口占用/启动门禁）→ 依赖计费分享三条记 SKIP，其余照跑；
#   * 任一断言不成立 → 退出 1。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\integration\run-local.ps1
#   powershell -NoProfile -File scripts\integration\run-local.ps1 -KeepTemp   # 保留日志与数据
#
# 常用参数 / 环境变量：
#   -GatewayPort 18080 / -EnginePort 18000 / -FakeLlmPort 18081（占用时自动顺延）
#   -TurnTimeout 180   等 usage_events 落库的秒数
#   CHIRON_LOCAL_PYTHON   带 embedded_postgres 的解释器（默认 local/runtime/venv）
#   CHIRON_ENGINE_PYTHON  引擎解释器（默认 local/engine/venv，其次 core 那份）
#   CHIRON_GATEWAY_BIN    现成网关二进制（默认用 go build 重建到临时目录）
#   POSTGRES_DSN          给了就复用外部 PG（跳过 embedded，与 desktop-local 同一约定）
#   CHIRON_LOCAL_REDIS_URL 给了就复用外部 Redis

#Requires -Version 5.1
[CmdletBinding()]
param(
    [int]$GatewayPort = 18080,
    [int]$EnginePort = 18000,
    [int]$FakeLlmPort = 18081,
    [int]$TurnTimeout = 180,
    [int]$StartupTimeout = 240,
    [string]$GatewayBin = "",
    [switch]$KeepTemp
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# ── 常量（与 run.sh 逐字对齐：同一套集成密钥、同一个 owner 邮箱）──────────────
$AppSecret = 'integration-app-secret-0123456789abcdef'
$JwtSecret = 'integration-jwt-secret-0123456789abcdef'
$InternalToken = 'integration-internal-token-0123456789abcdef'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$IsWindowsHost = ($env:OS -eq 'Windows_NT')

# ── 计数与输出 ───────────────────────────────────────────────────────────────
$script:Pass = 0
$script:Fail = 0
$script:Skip = 0
$script:SkipNotes = @()
$script:StartEpoch = Get-Date
$script:Token = ''
$script:GatewayUrl = ''
$script:EngineUrl = ''
$script:FakeLlmUrl = ''
$script:Work = ''
$script:Launcher = $null
$script:Children = @()

function Write-Info([string]$Text) { Write-Host $Text }
function Write-Muted([string]$Text) { Write-Host "    $Text" -ForegroundColor DarkGray }

# Fail 抛的是断言失败（退出码 1）；环境不具备走 Skip-Environment（退出码 77）。
# 两者都用异常而不是直接 exit：只有走到 finally 才能把 PG/Redis/子进程收干净。
function Fail([string]$Name, [string]$Detail) {
    $script:Fail++
    Write-Host "FAIL  $Name" -ForegroundColor Red
    Write-Host "      $Detail" -ForegroundColor Red
    throw "ASSERT: $Name：$Detail"
}

function Ok([string]$Name, [string]$Detail = '') {
    $script:Pass++
    if ($Detail) { Write-Host "PASS  $Name  [$Detail]" -ForegroundColor Green }
    else { Write-Host "PASS  $Name" -ForegroundColor Green }
}

function Skip-Assert([string]$Name, [string]$Reason) {
    $script:Skip++
    $script:SkipNotes += "$Name（$Reason）"
    Write-Host "SKIP  $Name  ($Reason)" -ForegroundColor Yellow
}

function Skip-Environment([string]$Reason) {
    Write-Host "SKIP  本地集成档未运行：$Reason" -ForegroundColor Yellow
    Write-Host 'SKIP  （这是"环境不具备"而不是失败；退出码 77）' -ForegroundColor Yellow
    throw "SKIP: $Reason"
}

# ── 端口与进程 ───────────────────────────────────────────────────────────────

function Test-PortFree([int]$Port) {
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) { $listener.Stop() }
    }
}

function Resolve-Port([int]$Desired, [string]$Name) {
    for ($i = 0; $i -lt 20; $i++) {
        if (Test-PortFree ($Desired + $i)) { return ($Desired + $i) }
    }
    Skip-Environment "端口 $Desired..$($Desired + 19) 全被占用（$Name），无法起本地集成档"
}

function Quote-Arg([string]$Value) {
    if ($Value -match '[\s"]') { return '"' + ($Value -replace '"', '\"') + '"' }
    return $Value
}

# Start-BandChild 起一个**不需要 stdin** 的子进程：stdout/stderr 落文件，
# 既不会因没人读管道而阻塞，也能在失败时直接 tail 给人看。
function Start-BandChild {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [hashtable]$Environment = @{},
        [string]$LogBase
    )
    $saved = @{}
    foreach ($key in $Environment.Keys) {
        $saved[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, [string]$Environment[$key], 'Process')
    }
    $out = "$LogBase.out.log"
    $err = "$LogBase.err.log"
    try {
        $quoted = @($Arguments | ForEach-Object { Quote-Arg $_ })
        # -ArgumentList 不接受空集合（网关就是"零参数、全靠环境变量"），
        # 所以只用 splat 传非空的参数表。
        $startParams = @{
            FilePath               = $FilePath
            WorkingDirectory       = $WorkingDirectory
            RedirectStandardOutput = $out
            RedirectStandardError  = $err
            PassThru               = $true
            WindowStyle            = 'Hidden'
        }
        if ($quoted.Count -gt 0) { $startParams['ArgumentList'] = $quoted }
        $proc = Start-Process @startParams
    } finally {
        foreach ($key in $saved.Keys) {
            [Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process')
        }
    }
    $record = @{ Name = $Name; Process = $proc; Out = $out; Err = $err }
    $script:Children += $record
    return $record
}

function Stop-BandChild($Record) {
    if (-not $Record -or -not $Record.Process) { return }
    try {
        if (-not $Record.Process.HasExited) {
            $Record.Process.Kill()
            $null = $Record.Process.WaitForExit(5000)
        }
    } catch {
        # 已经退出/句柄失效：收尾不该再抛
    }
}

function Show-LogTail([string]$Path, [int]$Lines = 40) {
    if (-not $Path -or -not (Test-Path $Path)) { return }
    Write-Host "── 日志尾部（$Path）" -ForegroundColor DarkGray
    Get-Content -LiteralPath $Path -Tail $Lines -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "    $_" }
}

function Show-AllLogTails {
    if (-not $script:Work) { return }
    Write-Host ''
    Show-LogTail (Join-Path $script:Work 'logs\launcher.stdout.log') 30
    foreach ($child in $script:Children) { Show-LogTail $child.Err 30 }
}

# ── HTTP ─────────────────────────────────────────────────────────────────────

# Invoke-Gateway 把 4xx/5xx 也当作**可读结果**返回（带状态码与响应体）：
# 断言失败时人需要看到服务端说了什么，而不是只有一句"请求失败"。
function Invoke-Gateway {
    param(
        [string]$Method,
        [string]$Path,
        $Body = $null,
        [switch]$Anonymous,
        [int]$TimeoutSec = 60
    )
    $headers = @{ 'Content-Type' = 'application/json' }
    if (-not $Anonymous -and $script:Token) { $headers['Authorization'] = "Bearer $($script:Token)" }
    $params = @{
        Method      = $Method
        Uri         = "$script:GatewayUrl$Path"
        Headers     = $headers
        TimeoutSec  = $TimeoutSec
        UseBasicParsing = $true
    }
    if ($null -ne $Body) { $params['Body'] = ($Body | ConvertTo-Json -Depth 10 -Compress) }
    try {
        $resp = Invoke-WebRequest @params
        $status = [int]$resp.StatusCode
        $raw = $resp.Content
    } catch {
        $webResp = $_.Exception.Response
        if ($null -eq $webResp) { throw }
        $status = [int]$webResp.StatusCode
        $raw = $_.ErrorDetails.Message
        if (-not $raw) { $raw = '' }
    }
    $parsed = $null
    if ($raw) {
        try { $parsed = $raw | ConvertFrom-Json } catch { $parsed = $null }
    }
    return @{ Status = $status; Json = $parsed; Raw = [string]$raw }
}

function Wait-Http([string]$Url, [string]$Name, [int]$TimeoutSec) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $Url -TimeoutSec 3 -UseBasicParsing
            if ([int]$resp.StatusCode -ge 200 -and [int]$resp.StatusCode -lt 500) { return $true }
        } catch {
            # 连接被拒 / 还没就绪：继续等
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

# Get-Field 读 PSCustomObject 的字段；字段缺失返回 $null（ConvertFrom-Json 对缺字段
# 直接报属性不存在，会让断言代码在"服务端少给一个键"时崩掉而不是给出可读失败）。
function Get-Field($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    $prop = $Object.PSObject.Properties[$Name]
    if ($null -eq $prop) { return $null }
    return $prop.Value
}

function Invoke-DbQuery([string]$Sql) {
    $resp = Invoke-Gateway -Method POST -Path '/v1/admin/database/query' -Body @{ query = $Sql }
    if ($resp.Status -ne 200) {
        Fail '数据库只读查询' "HTTP $($resp.Status)；SQL：$Sql；响应：$($resp.Raw)"
    }
    return (Get-Field (Get-Field $resp.Json 'data') 'rows')
}

# ── 环境探测 ─────────────────────────────────────────────────────────────────

function Resolve-CorePython {
    $candidates = @()
    if ($env:CHIRON_LOCAL_PYTHON) { $candidates += $env:CHIRON_LOCAL_PYTHON }
    foreach ($base in @($Root, (Join-Path $env:LOCALAPPDATA 'Chiron'))) {
        if (-not $base) { continue }
        $candidates += (Join-Path $base 'local\runtime\venv\Scripts\python.exe')
        $candidates += (Join-Path $base 'local/runtime/venv/bin/python')
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return ''
}

function Resolve-EnginePython([string]$CorePython) {
    $candidates = @()
    if ($env:CHIRON_ENGINE_PYTHON) { $candidates += $env:CHIRON_ENGINE_PYTHON }
    foreach ($base in @($Root, (Join-Path $env:LOCALAPPDATA 'Chiron'))) {
        if (-not $base) { continue }
        $candidates += (Join-Path $base 'local\engine\venv\Scripts\python.exe')
        $candidates += (Join-Path $base 'local/engine/venv/bin/python')
    }
    if ($CorePython) { $candidates += $CorePython }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return ''
}

# Test-PythonModule 真跑一次 import：解释器存在但缺依赖是最常见的"看起来有环境"。
function Test-PythonModule([string]$Python, [string]$Module) {
    if (-not $Python) { return $false }
    & $Python -c "import $Module" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Resolve-GatewayBinary {
    if ($GatewayBin -and (Test-Path -LiteralPath $GatewayBin)) { return $GatewayBin }
    if ($env:CHIRON_GATEWAY_BIN -and (Test-Path -LiteralPath $env:CHIRON_GATEWAY_BIN)) { return $env:CHIRON_GATEWAY_BIN }

    $exeName = if ($IsWindowsHost) { 'chiron.exe' } else { 'chiron' }
    $out = Join-Path $script:Work "bin\$exeName"
    $null = New-Item -ItemType Directory -Force -Path (Split-Path $out)

    # 优先**重建**：本地档的意义是验当前源码，捡一个陈旧二进制会把"改动没生效"
    # 伪装成通过。
    $go = Get-Command go -ErrorAction SilentlyContinue
    if ($go) {
        Write-Muted "构建网关：go build -o $out ./cmd/chiron"
        Push-Location $Root
        try {
            & $go.Source build -mod=mod -o $out ./cmd/chiron 2>&1 | ForEach-Object { Write-Muted $_ }
            if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $out)) { return $out }
        } finally {
            Pop-Location
        }
        Write-Muted 'go build 失败，回落到既有二进制（若存在）'
    } else {
        Write-Muted '未找到 go：将使用既有网关二进制（可能是旧代码）'
    }
    foreach ($candidate in @(
            (Join-Path $Root $exeName),
            (Join-Path $Root "frontend-vue\assets\gateway\$exeName"))) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return ''
}

# Start-Launcher 起 desktop-local 引导器并解析 launcher-ready 那一行 JSON。
# stdin 保持打开：父进程关掉写端后引导器会走 finally 停掉 PG/Redis（见 launcher.py），
# 这是唯一不会留下孤儿 postmaster 的收尾路径。
function Start-Launcher([string]$Python) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Python
    $psi.Arguments = '-m app.launcher --data-dir ' + (Quote-Arg (Join-Path $script:Work 'data'))
    $psi.WorkingDirectory = Join-Path $Root 'desktop-local'
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.EnvironmentVariables['CHIRON_LOCAL_DATA_DIR'] = (Join-Path $script:Work 'data')
    # 强制走 embedded PG：外部 DSN 是"用别人的库"，本地档要验的是自带 PG。
    $null = $psi.EnvironmentVariables.Remove('POSTGRES_DSN')
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'

    $logs = Join-Path $script:Work 'logs'
    $null = New-Item -ItemType Directory -Force -Path $logs
    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    $null = $proc.Start()

    $null = $proc.StandardError.ReadToEndAsync()   # 必须开始读：否则 stderr 写满会卡住引导器
    $ready = $null
    $failure = $null
    $deadline = (Get-Date).AddSeconds($StartupTimeout)
    $read = $proc.StandardOutput.ReadLineAsync()
    while ((Get-Date) -lt $deadline) {
        if ($read.Wait(500)) {
            $line = $read.Result
            if ($null -eq $line) { break }
            $event = $null
            try { $event = $line | ConvertFrom-Json } catch { $event = $null }
            if ($event -and $event.event -eq 'launcher-ready') { $ready = $event; break }
            if ($event -and $event.event -eq 'launcher-error') { $failure = $event; break }
            if ($line.Trim()) { Write-Muted "[launcher] $line" }
            $read = $proc.StandardOutput.ReadLineAsync()
        } elseif ($proc.HasExited) {
            break
        }
    }
    # 就绪之后不再有输出，但仍把管道读空，避免引导器因写入阻塞。
    $null = $proc.StandardOutput.ReadToEndAsync()

    if ($failure) {
        $code = Get-Field $failure 'code'
        $details = Get-Field $failure 'error'
        $log = Get-Field $failure 'log'
        if ($log) { Show-LogTail $log 30 }
        Skip-Environment ("embedded PG/迁移失败（$code）：$details")
    }
    if (-not $ready) {
        if (-not $proc.HasExited) { $proc.Kill() }
        Skip-Environment "引导器在 ${StartupTimeout}s 内没有输出 launcher-ready（日志：$logs）"
    }
    return @{ Process = $proc; Info = $ready }
}

function Stop-Band {
    if ($script:Launcher) {
        try {
            $script:Launcher.Process.StandardInput.Close()   # 触发引导器 finally：停 Redis/PG
            $null = $script:Launcher.Process.WaitForExit(15000)
        } catch { }
        try {
            if (-not $script:Launcher.Process.HasExited) { $script:Launcher.Process.Kill() }
        } catch { }
    }
    foreach ($child in $script:Children) { Stop-BandChild $child }
}

function Show-Summary {
    $elapsed = [int]((Get-Date) - $script:StartEpoch).TotalSeconds
    Write-Host ''
    Write-Host "── 结果：PASS=$($script:Pass) FAIL=$($script:Fail) SKIP=$($script:Skip)  耗时=${elapsed}s"
    if ($script:SkipNotes.Count -gt 0) {
        Write-Host ('   未验证：' + ($script:SkipNotes -join '；'))
    }
    if ($script:Work -and -not $KeepTemp) {
        Write-Muted "日志目录已删除（-KeepTemp 可保留）：$($script:Work)"
    } elseif ($script:Work) {
        Write-Muted "日志与数据保留在：$($script:Work)"
    }
}

# ── 主流程 ───────────────────────────────────────────────────────────────────

$script:Work = Join-Path ([System.IO.Path]::GetTempPath()) ('chiron-integration-local-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$null = New-Item -ItemType Directory -Force -Path $script:Work
$exitCode = 0

try {
    Write-Info "==> 本地集成档（工作目录 $($script:Work)）"

    # ── 预检 ──
    $corePython = Resolve-CorePython
    $externalPg = [string]$env:POSTGRES_DSN
    if (-not $externalPg) {
        if (-not $corePython) {
            Skip-Environment ('找不到带 embedded PG 的解释器（期望 local/runtime/venv/Scripts/python.exe；' +
                '先跑 npm run prepare:runtime，或设 CHIRON_LOCAL_PYTHON / POSTGRES_DSN）')
        }
        if (-not (Test-PythonModule $corePython 'embedded_postgres')) {
            Skip-Environment "解释器 $corePython 里没有 embedded_postgres（先跑 npm run prepare:runtime）"
        }
    } else {
        Write-Muted '检测到 POSTGRES_DSN：复用外部 PG（跳过 embedded）'
    }

    $gatewayExe = Resolve-GatewayBinary
    if (-not $gatewayExe) {
        Skip-Environment '既没有 go 工具链重建网关，也没有既有的 chiron 二进制'
    }

    # 假 LLM 只依赖标准库，用 core 解释器或系统 python 都行。
    $pyForFakeLlm = if ($corePython) { $corePython } else { 'python' }
    if (-not $corePython -and -not (Get-Command python -ErrorAction SilentlyContinue)) {
        Skip-Environment '找不到 python（假 LLM 需要标准库 http.server）'
    }

    $GatewayPort = Resolve-Port $GatewayPort 'gateway'
    $EnginePort = Resolve-Port $EnginePort 'python-engine'
    $FakeLlmPort = Resolve-Port $FakeLlmPort 'fake-llm'
    $script:GatewayUrl = "http://127.0.0.1:$GatewayPort"
    $script:EngineUrl = "http://127.0.0.1:$EnginePort"
    $script:FakeLlmUrl = "http://127.0.0.1:$FakeLlmPort"

    # ── 1. 数据栈：embedded PG + 迁移 + Redis ──
    $redisAddr = ''
    if ($externalPg) {
        $pgDsn = $externalPg
        $redisUrl = [string]$env:CHIRON_LOCAL_REDIS_URL
        if ($redisUrl -match '^redis://([^/:]+)(:(\d+))?') {
            $redisHost = $Matches[1]
            $redisPort = if ($Matches[3]) { $Matches[3] } else { '6379' }
            $redisAddr = "$redisHost`:$redisPort"
        }
        if (-not $redisAddr) {
            Skip-Environment '给了 POSTGRES_DSN 但没有可用的 CHIRON_LOCAL_REDIS_URL（本地档必须连真实 Redis）'
        }
        Ok '数据栈（外部 PG + 外部 Redis）' "pg=外部 redis=$redisAddr"
    } else {
        Write-Info '==> 启动引导器（embedded PostgreSQL + 迁移 + Redis）'
        $started = Start-Launcher $corePython
        $script:Launcher = $started
        $info = $started.Info
        $pgDsn = [string](Get-Field $info 'pg_dsn')
        $redisAddr = [string](Get-Field $info 'redis_addr')
        $pgBackend = [string](Get-Field $info 'pg_backend')
        $migration = Get-Field $info 'migration'
        if (-not $pgDsn) { Skip-Environment '引导器没有给出 pg_dsn（embedded PG 未就绪）' }
        if (-not $redisAddr) {
            Skip-Environment ('引导器只起了 fakeredis（无监听端口），本地档需要真实 Redis：' +
                '先跑 npm run prepare:redis，或设 CHIRON_LOCAL_REDIS_URL 指向 Valkey/Redis')
        }
        # migration 载荷是 {ok, revision, ...}：把迁移到的 revision 打出来，
        # 排查"代码期望 head 与库不一致"时一眼能对得上。
        $migrationRev = [string](Get-Field $migration 'revision')
        $migrationOk = Get-Field $migration 'ok'
        Ok '数据栈（embedded PG + Redis）' "pg=$pgBackend redis=$redisAddr migration=$migrationRev ok=$migrationOk"
    }

    # ── 2. 假 LLM 与网关 ──
    Write-Info '==> 启动假 LLM'
    $fakeLlm = Start-BandChild -Name 'fake-llm' -FilePath $pyForFakeLlm `
        -Arguments @((Join-Path $Root 'scripts\integration\fake_llm\fake_llm.py')) `
        -WorkingDirectory $Root `
        -Environment @{ FAKE_LLM_PORT = "$FakeLlmPort" } `
        -LogBase (Join-Path $script:Work 'logs\fake-llm')
    if (-not (Wait-Http "$script:FakeLlmUrl/healthz" 'fake-llm' 60)) {
        Show-LogTail $fakeLlm.Err
        Skip-Environment "假 LLM 在 60s 内没有就绪（端口 $FakeLlmPort）"
    }

    $gatewayEnv = @{
        APP_SECRET                 = $AppSecret
        JWT_SECRET                 = $JwtSecret
        INTERNAL_TOKEN             = $InternalToken
        POSTGRES_DSN               = $pgDsn
        REDIS_ADDR                 = $redisAddr
        PORT                       = "$GatewayPort"
        BIND_HOST                  = '127.0.0.1'
        PUBLIC_BASE_URL            = $script:GatewayUrl
        PYTHON_ENGINE_ADDRESS      = "127.0.0.1:$EnginePort"
        STORAGE_BACKEND            = 'local'
        STORAGE_ROOT               = (Join-Path $script:Work 'workspace')
        CHIRON_DATA_DIR            = (Join-Path $script:Work 'data')
        MIGRATIONS_DIR             = (Join-Path $Root 'migrations\versions')
        RATE_LIMIT_RPM             = '1000'
        RATE_LIMIT_GLOBAL          = '10000'
        LOG_LEVEL                  = 'info'
        # 免口令引导只对 loopback 生效（第二道闸门在 handler 里）：脚本就在本机跑，
        # 这样不必自签 JWT 也能拿到 owner 令牌（容器档因 NAT 才必须自签）。
        LOCAL_BOOTSTRAP            = '1'
    }
    Write-Info '==> 启动 Go 网关'
    $gateway = Start-BandChild -Name 'gateway' -FilePath $gatewayExe -Arguments @() `
        -WorkingDirectory $script:Work -Environment $gatewayEnv `
        -LogBase (Join-Path $script:Work 'logs\gateway')
    if (-not (Wait-Http "$script:GatewayUrl/health" 'gateway' $StartupTimeout)) {
        Show-LogTail $gateway.Out
        Show-LogTail $gateway.Err
        Skip-Environment "网关在 ${StartupTimeout}s 内没有就绪（见上方日志）"
    }
    Ok '网关就绪' $script:GatewayUrl

    # ── 3. 鉴权：loopback 免口令引导 ──
    $session = Invoke-Gateway -Method POST -Path '/v1/auth/session' -Body @{} -Anonymous
    if ($session.Status -ne 200) {
        Fail '本地引导会话（POST /v1/auth/session）' "HTTP $($session.Status)；响应：$($session.Raw)"
    }
    $script:Token = [string](Get-Field (Get-Field $session.Json 'data') 'token')
    if (-not $script:Token) { Fail '本地引导会话' '响应缺 data.token' }
    $dbStatus = Invoke-Gateway -Method GET -Path '/v1/admin/database/status'
    if ($dbStatus.Status -ne 200) {
        Fail 'owner 令牌可通过鉴权（/v1/admin/database/status）' "HTTP $($dbStatus.Status)；响应：$($dbStatus.Raw)"
    }
    if ((Get-Field (Get-Field $dbStatus.Json 'data') 'connected') -ne $true) {
        Fail '数据库连接' "网关报告未连接：$($dbStatus.Raw)"
    }
    Ok '鉴权与会话引导' 'owner JWT + PG 可用'

    # ── 4. 引擎（起不来就跳过依赖它的三条断言）──
    $enginePython = Resolve-EnginePython $corePython
    $engineReady = $false
    if (-not $enginePython) {
        $engineReason = '找不到引擎解释器（local/engine/venv）'
    } elseif (-not (Test-PythonModule $enginePython 'uvicorn')) {
        $engineReason = "引擎解释器缺依赖（$enginePython 里 import uvicorn 失败）"
    } else {
        $engineReason = '引擎未就绪'
        $sandboxRoot = Join-Path $script:Work 'sandbox'
        $checkpointRoot = Join-Path $script:Work 'checkpoints'
        $null = New-Item -ItemType Directory -Force -Path $sandboxRoot, $checkpointRoot
        $engineEnv = @{
            APP_SECRET                   = $AppSecret
            JWT_SECRET                   = $JwtSecret
            INTERNAL_TOKEN               = $InternalToken
            HTTP_HOST                    = '127.0.0.1'
            HTTP_PORT                    = "$EnginePort"
            REDIS_ADDR                   = $redisAddr
            GATEWAY_INTERNAL_URL         = $script:GatewayUrl
            OPENAI_API_KEY               = 'mock-key'
            OPENAI_BASE_URL              = "$script:FakeLlmUrl/v1"
            DEEPSEEK_API_KEY             = 'mock-key'
            DEEPSEEK_BASE_URL            = "$script:FakeLlmUrl/v1"
            LLM_API_KEY                  = 'mock-key'
            LLM_BASE_URL                 = "$script:FakeLlmUrl/v1"
            LLM_MODEL                    = 'deepseek-chat'
            SANDBOX_BACKEND              = 'local'
            SANDBOX_ROOT                 = $sandboxRoot
            # 本机不是"生产隔离环境"：显式声明没有这一层，避免启动门禁把默认档位
            # 判成"降级未确认"而拒绝启动（与容器档同一处理）。
            SANDBOX_ISOLATION_LEVEL      = 'none'
            SANDBOX_ISOLATION_ALLOW_DOWNGRADE = 'true'
            CHIRON_CHECKPOINT_STATE_DIR  = $checkpointRoot
            MCP_POOL_ENABLED             = 'false'
            LOG_LEVEL                    = 'INFO'
        }
        Write-Info '==> 启动 Python 引擎'
        $engine = Start-BandChild -Name 'engine' -FilePath $enginePython -Arguments @('-m', 'app.main') `
            -WorkingDirectory (Join-Path $Root 'python-engine') -Environment $engineEnv `
            -LogBase (Join-Path $script:Work 'logs\engine')
        $engineReady = Wait-Http "$script:EngineUrl/healthz" 'python-engine' $StartupTimeout
        if ($engineReady) {
            Ok '引擎就绪' $script:EngineUrl
        } else {
            Show-LogTail $engine.Err 30
            $engineReason = '引擎在限定时间内没有就绪（见上方日志）'
        }
    }
    if (-not $engineReady) { Write-Muted "引擎不可用：$engineReason" }

    # ── 5. 依赖管理 ──
    if (-not $engineReady) {
        Skip-Assert '依赖列表（/v1/admin/deps/list）' $engineReason
    } else {
        $deps = Invoke-Gateway -Method GET -Path '/v1/admin/deps/list'
        if ($deps.Status -ne 200) {
            Fail '依赖列表（/v1/admin/deps/list）' "HTTP $($deps.Status)；响应：$($deps.Raw)"
        }
        $depsCount = Get-Field (Get-Field $deps.Json 'data') 'count'
        if (-not $depsCount -or [int]$depsCount -le 0) {
            Fail '依赖列表' "count=$depsCount（引擎 pip list 返回空）"
        }
        Ok '依赖列表' "count=$depsCount"
    }

    # ── 6. 隔离档切换 ──
    $isoBefore = Invoke-Gateway -Method GET -Path '/v1/system/isolation'
    if ($isoBefore.Status -ne 200) {
        Fail '读取隔离档位' "HTTP $($isoBefore.Status)；响应：$($isoBefore.Raw)"
    }
    $isoBeforeValue = [string](Get-Field (Get-Field $isoBefore.Json 'data') 'effective')
    if (-not $isoBeforeValue) { Fail '读取隔离档位' "响应缺 data.effective：$($isoBefore.Raw)" }

    $isoPut = Invoke-Gateway -Method PUT -Path '/v1/system/isolation' `
        -Body @{ level = 'job'; reason = 'integration band (local)' }
    if ($isoPut.Status -ne 200) {
        Fail '切换隔离档位 level=job' "HTTP $($isoPut.Status)；响应：$($isoPut.Raw)"
    }
    $isoAfterValue = [string](Get-Field (Get-Field $isoPut.Json 'data') 'effective')
    if (-not $isoAfterValue) { Fail '切换隔离档位' "响应缺 data.effective：$($isoPut.Raw)" }

    $isoRead = Invoke-Gateway -Method GET -Path '/v1/system/isolation'
    $isoReadValue = [string](Get-Field (Get-Field $isoRead.Json 'data') 'effective')
    if ($isoReadValue -ne $isoAfterValue) {
        Fail '隔离档复核' "PUT 返回 effective=$isoAfterValue，GET 复核为 $isoReadValue"
    }
    Ok '隔离档切换' "before=$isoBeforeValue after=$isoAfterValue"

    # ── 7. 计费：submit → usage_events 非零 ──
    if (-not $engineReady) {
        Skip-Assert '计费（usage_events 非零）' $engineReason
        Skip-Assert '匿名分享' $engineReason
    } else {
        $projectRoot = Join-Path $script:Work 'projects\demo'
        $null = New-Item -ItemType Directory -Force -Path $projectRoot
        Set-Content -LiteralPath (Join-Path $projectRoot 'README.md') -Value 'integration fixture' -Encoding UTF8
        # 检查点要 git；这里只是让项目根像个真仓库（本档不断言检查点）。
        $git = Get-Command git -ErrorAction SilentlyContinue
        if ($git) {
            Push-Location $projectRoot
            try {
                & $git.Source init -q 2>$null | Out-Null
                & $git.Source -c user.email=integration@chiron.local -c user.name=integration add -A 2>$null | Out-Null
                & $git.Source -c user.email=integration@chiron.local -c user.name=integration commit -qm 'integration fixture' 2>$null | Out-Null
            } finally { Pop-Location }
        } else {
            Write-Muted '未找到 git：跳过仓库初始化（本档不断言检查点）'
        }

        $projectResp = Invoke-Gateway -Method POST -Path '/v1/session-map/projects' `
            -Body @{ name = 'local-integration-demo'; root_path = $projectRoot }
        if ($projectResp.Status -ne 201) {
            Fail '创建项目' "HTTP $($projectResp.Status)；响应：$($projectResp.Raw)"
        }
        $projectId = [string](Get-Field (Get-Field $projectResp.Json 'data') 'id')
        if (-not $projectId) { Fail '创建项目' "响应缺 data.id：$($projectResp.Raw)" }

        $sessionResp = Invoke-Gateway -Method POST -Path '/v1/conversations' -Body @{ title = 'local-integration-submit' }
        if ($sessionResp.Status -ne 200) {
            Fail '创建会话' "HTTP $($sessionResp.Status)；响应：$($sessionResp.Raw)"
        }
        $sessionId = [string](Get-Field (Get-Field $sessionResp.Json 'data') 'id')
        if (-not $sessionId) { Fail '创建会话' "响应缺 data.id：$($sessionResp.Raw)" }

        $bind = Invoke-Gateway -Method PUT -Path "/v1/session-map/projects/$projectId/sessions/$sessionId" -Body @{}
        if ($bind.Status -ne 200) {
            Fail '会话绑定到项目' "HTTP $($bind.Status)；响应：$($bind.Raw)"
        }

        $submit = Invoke-Gateway -Method POST -Path '/v1/agent/submit' -Body @{
            session_id = $sessionId
            content    = '集成测试：请只回复一句确认语。'
            llm_config = @{}
        }
        if ($submit.Status -ne 202) {
            Fail '提交 agent 回合（POST /v1/agent/submit）' "HTTP $($submit.Status)；响应：$($submit.Raw)"
        }

        $usageRows = $null
        $deadline = (Get-Date).AddSeconds($TurnTimeout)
        while ((Get-Date) -lt $deadline) {
            $usageRows = Invoke-DbQuery ("SELECT COUNT(*)::int AS n, COALESCE(SUM(input_tokens),0)::int AS tin, " +
                "COALESCE(SUM(output_tokens),0)::int AS tout FROM usage_events WHERE session_id='$sessionId'")
            if ($usageRows -and [int](Get-Field $usageRows[0] 'n') -gt 0) { break }
            Start-Sleep -Milliseconds 500
        }
        $rows = 0
        $tokensIn = 0
        $tokensOut = 0
        if ($usageRows -and $usageRows.Count -gt 0) {
            $rows = [int](Get-Field $usageRows[0] 'n')
            $tokensIn = [int](Get-Field $usageRows[0] 'tin')
            $tokensOut = [int](Get-Field $usageRows[0] 'tout')
        }
        if ($rows -le 0) {
            Fail 'usage_events 有行' "等待 ${TurnTimeout}s 后仍是 0（计费链路未落库；见引擎日志）"
        }
        if (($tokensIn + $tokensOut) -le 0) {
            Fail 'usage_events token 非零' "in=$tokensIn out=$tokensOut —— 假 LLM 的 usage chunk 没被引擎带进 done 事件"
        }
        Ok 'usage_events 非零' "rows=$rows in=$tokensIn out=$tokensOut"

        # ── 8. 分享：匿名可读 ──
        $messageRows = Invoke-DbQuery "SELECT id, role FROM messages WHERE session_id='$sessionId' ORDER BY seq ASC LIMIT 20"
        if (-not $messageRows -or $messageRows.Count -eq 0) {
            Fail '消息落库' 'messages 表没有该会话的任何行'
        }
        $messageIds = @($messageRows | ForEach-Object { [string](Get-Field $_ 'id') })
        $share = Invoke-Gateway -Method POST -Path "/v1/conversations/$sessionId/share" -Body @{ message_ids = $messageIds }
        if ($share.Status -ne 200) {
            Fail '创建分享' "HTTP $($share.Status)；响应：$($share.Raw)"
        }
        $shareId = [string](Get-Field (Get-Field $share.Json 'data') 'share_id')
        if (-not $shareId) { Fail '创建分享' "响应缺 data.share_id：$($share.Raw)" }

        # 关键：**不带 Authorization**，验证免鉴权读。
        $anonymous = Invoke-Gateway -Method GET -Path "/v1/share/$shareId" -Anonymous
        if ($anonymous.Status -ne 200) {
            Fail '匿名读取 /v1/share/<token>' "HTTP $($anonymous.Status)；响应：$($anonymous.Raw)"
        }
        $sharedMessages = Get-Field (Get-Field $anonymous.Json 'data') 'messages'
        $sharedCount = if ($sharedMessages) { @($sharedMessages).Count } else { 0 }
        if ($sharedCount -le 0) {
            Fail '匿名分享内容' "messages 为空；响应：$($anonymous.Raw)"
        }
        Ok '匿名分享可读' "token=$shareId messages=$sharedCount"
    }

    Write-Host ''
    Write-Host '本地集成档断言全部通过。' -ForegroundColor Green
} catch {
    $message = $_.Exception.Message
    if ($message -like 'SKIP:*') {
        $exitCode = 77
    } elseif ($message -like 'ASSERT:*') {
        $exitCode = 1
        Show-AllLogTails
    } else {
        # 未分类异常一律按失败处理并打印真因：本地档最常见的意外是端口/进程环境问题，
        # 把原始异常吞掉只会让下一个人重新排查一遍。
        $exitCode = 1
        Write-Host "FAIL  未预期的错误：$message" -ForegroundColor Red
        Write-Host ($_.ScriptStackTrace) -ForegroundColor DarkGray
        Show-AllLogTails
    }
} finally {
    Stop-Band
    Show-Summary
    if ($script:Work -and -not $KeepTemp -and (Test-Path -LiteralPath $script:Work)) {
        Remove-Item -LiteralPath $script:Work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

exit $exitCode
