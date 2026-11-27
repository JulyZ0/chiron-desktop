<#
.SYNOPSIS
  Consistent offline backup of Chiron desktop data.

.DESCRIPTION
  The data directory (%APPDATA%\gh_Athenavi_chiron) contains embedded
  PostgreSQL/Redis data, config, logs and components. Copying PGDATA while the
  app is running yields a corrupt snapshot, so this script refuses by default
  when Chiron is running. Use -Force only inside a maintenance window (the DB
  will need crash recovery after restore).

  APP_SECRET / KMS_KEK_KEYS are not included (or must not be relied on from the
  data dir). Escrow them separately, otherwise encrypted settings and provider
  keys cannot be decrypted after restore.
#>
[CmdletBinding()]
param(
  [string]$BackupDir = (Join-Path $env:USERPROFILE "ChironBackups"),
  [string]$DataDir = (Join-Path $env:APPDATA "gh_Athenavi_chiron"),
  [switch]$Force
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $DataDir)) {
  throw "Data directory not found: $DataDir (start Chiron once, or pass -DataDir)"
}

$running = Get-Process -Name "Chiron", "chiron" -ErrorAction SilentlyContinue
if ($running -and -not $Force) {
  throw "Chiron is running (PID: $($running.Id -join ', ')). Exit it first, or use -Force in a maintenance window."
}
if ($running -and $Force) {
  Write-Warning "Chiron is running. -Force gives a file-level copy, not a DB-consistent one."
}

New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$zip = Join-Path $BackupDir "chiron-data-$stamp.zip"

Write-Host "==> Archiving $DataDir"
Compress-Archive -Path (Join-Path $DataDir "*") -DestinationPath $zip -CompressionLevel Optimal

$hash = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash
$manifest = Join-Path $BackupDir "chiron-data-$stamp.manifest.txt"
@(
  "created_at=$((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))"
  "host=$env:COMPUTERNAME"
  "data_dir=$DataDir"
  "archive=$(Split-Path $zip -Leaf)"
  "sha256=$hash"
  "forced=$($Force.IsPresent)"
  "note=APP_SECRET/KMS_KEK_KEYS are NOT included; escrow them separately"
) | Set-Content -Encoding UTF8 $manifest

Write-Host "==> Done"
Write-Host "  Archive:  $zip"
Write-Host "  Manifest: $manifest"
Write-Host ""
Write-Host "Restore: exit Chiron -> extract over $DataDir -> start -> run team_smoke."
