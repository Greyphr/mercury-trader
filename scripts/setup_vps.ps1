# ──────────────────────────────────────────────────────────────
# Mercury Trader — One-time VPS setup (Windows Server)
# Run as Administrator. Installs Python, PostgreSQL, and project deps.
# ──────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"
Write-Host "== Mercury Trader VPS Setup ==" -ForegroundColor Cyan

# 1. Python (winget) --------------------------------------------------
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Python 3.12..." -ForegroundColor Yellow
    winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    # Refresh PATH
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    Write-Host "Python installed. Re-run this script after a shell restart if python is not found." -ForegroundColor Yellow
}

# 2. PostgreSQL (winget) -------------------------------------------------
if (-not (Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue)) {
    Write-Host "Installing PostgreSQL..." -ForegroundColor Yellow
    winget install --id PostgreSQL.PostgreSQL.16 --silent --accept-package-agreements --accept-source-agreements
}

# 3. Create the Python virtual environment + install deps --------------
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $RepoRoot ".venv"
if (-not (Test-Path $Venv)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    python -m venv $Venv
}
$Pip = Join-Path $Venv "Scripts\pip.exe"
& $Pip install --upgrade pip
Get-ChildItem (Join-Path $RepoRoot "requirements\*.txt") | ForEach-Object {
    & $Pip install -r $_.FullName
}

# 4. Create environment file from example if missing --------------------
$EnvFile = Join-Path $RepoRoot ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $RepoRoot ".env.example") $EnvFile
    Write-Host "Created .env from example — EDIT IT with your Exness + Telegram credentials." -ForegroundColor Yellow
}

Write-Host "Setup complete. Next: configure .env, run scripts/init_db.ps1, then scripts/setup_mt5.ps1." -ForegroundColor Green
