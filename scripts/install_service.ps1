# ──────────────────────────────────────────────────────────────
# Mercury Trader — Install as a Windows service (NSSM)
# Keeps the bot running 24/7 and restarts it on failure.
# Requires nssm (https://nssm.cc). Run as Administrator.
# ──────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$Nssm = "C:\Program Files\nssm\nssm.exe"
$ServiceName = "MercuryTrader"

if (-not (Test-Path $VenvPython)) {
    Write-Host "Virtual env python not found. Run scripts/setup_vps.ps1 first." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $Nssm)) {
    Write-Host @"
nssm not found. Download from https://nssm.cc/download and place nssm.exe in
'C:\Program Files\nssm\nssm.exe', then re-run this script.
"@ -ForegroundColor Yellow
    exit 1
}

& $Nssm install $ServiceName $VenvPython "-m mercury.main run"
& $Nssm set $ServiceName AppDirectory $RepoRoot
& $Nssm set $ServiceName DisplayName "Mercury Trader"
& $Nssm set $ServiceName Description "Adaptive intelligent trading bot/agent (24/7)"
& $Nssm set $ServiceName AppStdout (Join-Path $RepoRoot "logs\service_stdout.log")
& $Nssm set $ServiceName AppStderr (Join-Path $RepoRoot "logs\service_stderr.log")
& $Nssm set $ServiceName AppExit Default Restart
& $Nssm set $ServiceName AppRestartDelay 10000
& $Nssm set $ServiceName Start SERVICE_AUTO_START

& $Nssm start $ServiceName
Write-Host "Service '$ServiceName' installed and started." -ForegroundColor Green
