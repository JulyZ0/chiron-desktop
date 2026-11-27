<#
.SYNOPSIS
  Register Chiron desktop as a logon-autostart task with crash restart.

.DESCRIPTION
  Electron cannot run as a Windows service (Session 0 has no GUI), so this uses
  Task Scheduler bound to the current interactive user. The task runs elevated
  and restarts every minute on failure.

  Run from an elevated PowerShell (Register-ScheduledTask requires admin).

.PARAMETER AppPath
  Full path to Chiron.exe. Detected from common install locations when omitted.

.PARAMETER TaskName
  Scheduled task name. Default: Chiron.
#>
[CmdletBinding()]
param(
  [string]$AppPath = "",
  [string]$TaskName = "Chiron"
)

$ErrorActionPreference = "Stop"

if (-not $AppPath) {
  $candidates = @()
  $local = Join-Path $env:LOCALAPPDATA "Programs\Chiron\Chiron.exe"
  $pf = Join-Path $env:ProgramFiles "Chiron\Chiron.exe"
  $pf86Root = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
  foreach ($p in @($local, $pf)) {
    if ($p -and (Test-Path $p)) { $candidates += $p }
  }
  if ($pf86Root) {
    $pf86 = Join-Path $pf86Root "Chiron\Chiron.exe"
    if (Test-Path $pf86) { $candidates += $pf86 }
  }
  if ($candidates.Count -eq 0) {
    throw "Chiron.exe not found. Pass -AppPath (example: $local)"
  }
  $AppPath = $candidates[0]
}

if (-not (Test-Path $AppPath)) {
  throw "Chiron.exe does not exist: $AppPath"
}
$AppPath = (Resolve-Path $AppPath).Path
$workDir = Split-Path $AppPath -Parent

$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $AppPath -WorkingDirectory $workDir

# Logon trigger: Electron needs an interactive desktop. On servers, enable
# automatic logon for this account.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user

$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable `
  -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $action `
  -Trigger $trigger `
  -Settings $settings `
  -Principal $principal `
  -Description "Chiron team server (logon autostart + crash restart)" `
  -Force | Out-Null

Write-Host "Registered scheduled task: $TaskName"
Write-Host "  App:  $AppPath"
Write-Host "  User: $user"

$running = Get-Process -Name "Chiron" -ErrorAction SilentlyContinue
if (-not $running) {
  Start-ScheduledTask -TaskName $TaskName
  Write-Host "Started once."
} else {
  Write-Host "Chiron is already running; not started again."
}

Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo |
  Format-List TaskName, LastRunTime, LastTaskResult, NextRunTime

Write-Host ""
Write-Host "Notes:"
Write-Host "  - Data dir: $env:APPDATA\gh_Athenavi_chiron"
Write-Host "  - Backup:   .\deploy\windows\backup.ps1 -BackupDir D:\ChironBackups"
Write-Host "  - TLS/proxy: docs\team-domain-deployment.md"
