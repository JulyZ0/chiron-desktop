<#
.SYNOPSIS
    Post-rebuild acceptance check for the desktop local stack (P0-1).

.DESCRIPTION
    Thin Windows shell around scripts/verify_after_build_lib.py. The assertions
    live in exactly one place (Python) so the PowerShell and git-bash entry
    points cannot drift; a verifier that disagrees with itself reads as "all
    green" while nothing is checked.

    Preconditions: the desktop app is already running in local mode, so that
    %APPDATA%\gh_Athenavi_chiron\local\runtime-state.json exists. Ports are read
    from that file (they are random per launch); never type them by hand.

    The script prints PASS/FAIL/SKIP per assertion plus the next place to look,
    and never prints the owner token.

.PARAMETER VerifyArgs
    Arguments passed straight through to the Python implementation, e.g.
    --json, --runtime-state <path>, --base-url <url>, --ignore-restricted.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-after-build.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify-after-build.ps1 --json

.NOTES
    Exit codes: 0 = no FAIL, 1 = at least one FAIL, 2 = environment unusable
    (state file / readiness / session).
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$VerifyArgs
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$lib = Join-Path $here 'verify_after_build_lib.py'

if (-not (Test-Path -LiteralPath $lib -PathType Leaf)) {
    Write-Error "verify-after-build: missing $lib (was only the wrapper copied?)"
    exit 2
}

# Decode child-process output as UTF-8. Windows PowerShell 5.1 defaults the
# console to the OEM code page (936 here), which turns the server's Chinese
# detail strings into mojibake and hides the evidence we are collecting.
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
} catch {
    # Redirected output or a host without a console: the env vars below still
    # make Python emit UTF-8, so this is best-effort only.
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# Interpreter order: explicit override, then the Windows `py` launcher, then
# `python`. Each candidate is probed by actually running it: on Windows
# `python` is often the Store alias, which exists but cannot execute.
#
# The probe uses `-c "import sys"` (a non-empty argument on purpose): Windows
# PowerShell drops empty-string arguments when invoking a native exe, so
# `-c ""` reaches py.exe as a bare `-c` and fails with "Argument expected".
$pyExe = $null
$pyPrefix = @()
if ($env:CHIRON_VERIFY_PYTHON) {
    $candidate = $env:CHIRON_VERIFY_PYTHON
    & $candidate -c "import sys" *> $null
    if ($LASTEXITCODE -eq 0) { $pyExe = $candidate }
}
if (-not $pyExe) {
    foreach ($name in @('py', 'python', 'python3')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        if ($name -eq 'py') {
            & $cmd.Source -3 -c "import sys" *> $null
            if ($LASTEXITCODE -eq 0) {
                $pyExe = $cmd.Source
                $pyPrefix = @('-3')
                break
            }
        } else {
            & $cmd.Source -c "import sys" *> $null
            if ($LASTEXITCODE -eq 0) {
                $pyExe = $cmd.Source
                break
            }
        }
    }
}

if (-not $pyExe) {
    Write-Error "verify-after-build: no usable Python 3 found (set CHIRON_VERIFY_PYTHON to an interpreter path)"
    exit 2
}

$forwarded = @()
if ($VerifyArgs) { $forwarded = @($VerifyArgs) }

& $pyExe @pyPrefix $lib @forwarded
exit $LASTEXITCODE
