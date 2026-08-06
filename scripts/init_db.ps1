# ──────────────────────────────────────────────────────────────
# Mercury Trader — PostgreSQL database bootstrap
# Creates the mercury role + database. Run as a postgres-superuser.
# Adjust the password to match your .env DATABASE_URL.
# ──────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"

$DbUser   = "mercury"
$DbPass   = "mercury"
$DbName   = "mercury"
$PgBin    = "C:\Program Files\PostgreSQL\16\bin"

if (-not (Test-Path $PgBin)) {
    $PgBin = Read-Host "PostgreSQL bin directory not found. Enter path (e.g. C:\Program Files\PostgreSQL\16\bin)"
}

$Psql = Join-Path $PgBin "psql.exe"

& $Psql -U postgres -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='$DbUser') THEN CREATE ROLE $DbUser LOGIN PASSWORD '$DbPass'; END IF; END \$\$;"
& $Psql -U postgres -c "SELECT 'CREATE DATABASE $DbName OWNER $DbUser' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='$DbName')\gexec"

Write-Host "Database '$DbName' and role '$DbUser' ensured." -ForegroundColor Green
Write-Host "Update DATABASE_URL in .env if your credentials differ." -ForegroundColor Yellow
