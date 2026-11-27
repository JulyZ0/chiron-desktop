<#
.SYNOPSIS
  Remove the Chiron autostart scheduled task (does not uninstall or delete data).
#>
[CmdletBinding()]
param(
  [string]$TaskName = "Chiron"
)

$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
  Write-Host "Scheduled task $TaskName does not exist."
  exit 0
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

Write-Host "Removed scheduled task: $TaskName"
Write-Host "Chiron keeps running; exit it from the tray or Stop-Process -Name Chiron."
