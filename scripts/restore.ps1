# PlumoAI Self-Hosted — restore MySQL, MongoDB, and Traefik TLS certs from a
# backup directory created by scripts/backup.ps1.
# Usage: .\scripts\restore.ps1 -InDir <path>

param([Parameter(Mandatory=$true)][string]$InDir)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

if (!(Test-Path $InDir)) {
    Write-Host "Error: backup dir not found: $InDir" -ForegroundColor Red
    exit 1
}
$InDirFull = (Resolve-Path $InDir).Path

$envFileArg = ""
if (Test-Path ".env") { $envFileArg = "--env-file .env" }

Write-Host "This will overwrite the current MySQL and MongoDB data with the contents of: $InDir" -ForegroundColor Yellow
$confirm = Read-Host "Type 'yes' to continue"
if ($confirm -ne "yes") {
    Write-Host "Aborted."
    exit 1
}

if (Test-Path "$InDirFull/mysql.sql") {
    Write-Host "  Restoring MySQL..."
    $mysqlCmd = "docker compose $envFileArg exec -T mysql sh -c `"mysql -uroot -p\`"`$(cat /run/secrets/mysql_root_password)\`"`" < `"$InDirFull\mysql.sql`""
    cmd /c $mysqlCmd
} else {
    Write-Host "  No mysql.sql in backup — skipping MySQL restore."
}

if (Test-Path "$InDirFull/mongo.archive") {
    Write-Host "  Restoring MongoDB..."
    $mongoCmd = "docker compose $envFileArg exec -T mongodb sh -c `"mongorestore --username \`"`$(cat /run/secrets/mongo_user)\`" --password \`"`$(cat /run/secrets/mongo_password)\`" --authenticationDatabase admin --archive --drop`" < `"$InDirFull\mongo.archive`""
    cmd /c $mongoCmd
} else {
    Write-Host "  No mongo.archive in backup — skipping MongoDB restore."
}

if (Test-Path "$InDirFull/acme.json") {
    Write-Host "  Restoring Traefik TLS store..."
    $copyArgs = @("compose")
    if ($envFileArg) { $copyArgs += "--env-file", ".env" }
    $copyArgs += "cp", "$InDirFull/acme.json", "traefik:/letsencrypt/acme.json"
    & docker $copyArgs
} else {
    Write-Host "  No acme.json in backup — skipping TLS store restore."
}

Write-Host "Restore complete. Restart services to pick up the restored data:" -ForegroundColor Green
Write-Host "  docker compose $envFileArg restart"
