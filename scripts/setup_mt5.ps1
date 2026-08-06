# ──────────────────────────────────────────────────────────────
# Mercury Trader — Exness MetaTrader 5 terminal setup
# Downloads the Exness MT5 installer and runs the official install.
# After install, log in to your Exness account inside the terminal.
# ──────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"
Write-Host "== Exness MT5 Setup ==" -ForegroundColor Cyan

$ExnessUrl = "https://www.exness.com/static/download/trading-platform/exnessmt5setup.exe"
$Installer = Join-Path $env:TEMP "exnessmt5setup.exe"
$DefaultInstall = "C:\Program Files\Exness MetaTrader 5"

Write-Host "Downloading Exness MT5 installer..." -ForegroundColor Yellow
Invoke-WebRequest -Uri $ExnessUrl -OutFile $Installer

Write-Host "Running installer (follow the wizard)..." -ForegroundColor Yellow
Start-Process -FilePath $Installer -Wait

if (Test-Path $DefaultInstall) {
    Write-Host "MT5 installed at: $DefaultInstall" -ForegroundColor Green
    Write-Host @"

Next steps:
  1. Open the terminal and log in with your Exness LIVE account.
  2. Add the XAUUSD symbol to Market Watch (Symbols > Metals > XAUUSD).
  3. In the terminal settings enable: Tools > Options > Server >
     'Enable automated trading' (Algo Trading checkbox).
  4. Confirm the terminal stays open 24/7 on this VPS.
"@
} else {
    Write-Host "MT5 not found at default path. Set MT5_TERMINAL_PATH in .env to the actual install path." -ForegroundColor Yellow
}
