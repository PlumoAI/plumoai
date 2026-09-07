# PlumoAI Self-Hosted — back up MySQL, MongoDB, Traefik TLS certs, secrets, and .env.
# Usage: .\scripts\backup.ps1 [-OutDir <path>]

param([string]$OutDir)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

$envFileArg = ""
if (Test-Path ".env") { $envFileArg = "--env-file .env" }

if (!(Test-Path "secrets/mysql_root_password.txt") -or !(Test-Path "secrets/mongo_user.txt") -or !(Test-Path "secrets/mongo_password.txt")) {
    Write-Host "Error: secrets/ is missing expected files. Run install.ps1 first." -ForegroundColor Red
    exit 1
}

if ([string]::IsNullOrWhiteSpace($OutDir)) {
    $OutDir = "backups/$(Get-Date -Format 'yyyyMMdd-HHmmss')"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDirFull = (Resolve-Path $OutDir).Path

Write-Host "Backing up to $OutDir ..." -ForegroundColor Cyan

# Redirected through cmd /c: PowerShell's own redirection re-encodes native stdout as
# text, which corrupts a binary mongodump archive and can mangle SQL dump line endings.
# cmd's ">" passes the byte stream straight through untouched.

Write-Host "  Dumping MySQL..."
$mysqlCmd = "docker compose $envFileArg exec -T mysql sh -c `"mysqldump -uroot -p\`"`$(cat /run/secrets/mysql_root_password)\`" --all-databases --routines --triggers`" > `"$OutDirFull\mysql.sql`""
cmd /c $mysqlCmd
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: MySQL dump failed (is the mysql service running?)." -ForegroundColor Red
    exit 1
}

Write-Host "  Dumping MongoDB..."
$mongoCmd = "docker compose $envFileArg exec -T mongodb sh -c `"mongodump --username \`"`$(cat /run/secrets/mongo_user)\`" --password \`"`$(cat /run/secrets/mongo_password)\`" --authenticationDatabase admin --archive`" > `"$OutDirFull\mongo.archive`""
cmd /c $mongoCmd
if ($LASTEXITCODE -ne 0) {
    Write-Host "Error: MongoDB dump failed (is the mongodb service running?)." -ForegroundColor Red
    exit 1
}

Write-Host "  Copying Traefik TLS store (acme.json)..."
$copyArgs = @("compose")
if ($envFileArg) { $copyArgs += "--env-file", ".env" }
$copyArgs += "cp", "traefik:/letsencrypt/acme.json", "$OutDirFull/acme.json"
& docker $copyArgs 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "    (skipped — traefik not running yet, or no certificate issued)" -ForegroundColor DarkGray
}

Write-Host "  Copying secrets/ and .env..."
Copy-Item -Recurse -Force "secrets" "$OutDirFull/secrets"
if (Test-Path ".env") { Copy-Item -Force ".env" "$OutDirFull/.env" }

Write-Host "Backup complete: $OutDir" -ForegroundColor Green
Write-Host "Restore with: .\scripts\restore.ps1 -InDir `"$OutDir`""
