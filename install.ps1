# PlumoAI Self-Hosted - Windows Install
# Usage: .\install.ps1
#        .\install.ps1 -Fresh

param([switch]$Fresh)

$ErrorActionPreference = "Stop"

function Write-PlumoInstallBanner {
    # Terminal wordmark (purple "Plumo", cyan-blue "Ai") - matches brand; PNG at assets/plumoai-logo.png
    Write-Host ""
    Write-Host "  " -NoNewline
    Write-Host "Plumo" -NoNewline -ForegroundColor DarkMagenta
    Write-Host "Ai" -NoNewline -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Self-Hosted · installer" -ForegroundColor DarkGray
    Write-Host ""
}
Write-PlumoInstallBanner

function Get-EnvValue {
    param([string]$FilePath, [string]$Key)
    if (!(Test-Path $FilePath)) { return "" }
    $line = Get-Content $FilePath | Where-Object { $_ -match "^$Key=" } | Select-Object -First 1
    if ($line) { return ($line -replace "^$Key=", "").Trim() }
    return ""
}

function Set-EnvValue {
    param([string]$FilePath, [string]$Key, [string]$Value)
    if (!(Test-Path $FilePath)) { return }
    $content = @(Get-Content $FilePath)
    $found = $false
    for ($i = 0; $i -lt $content.Count; $i++) {
        if ($content[$i] -match "^([^=]+)=" -and $Matches[1] -eq $Key) {
            $content[$i] = "$Key=$Value"
            $found = $true
            break
        }
    }
    if (!$found) { $content += "$Key=$Value" }
    $content | Set-Content $FilePath
}

function Test-Placeholder {
    param([string]$Val)
    return [string]::IsNullOrWhiteSpace($Val) -or 
           $Val -like "*<*" -or 
           $Val -eq "your-domain.com" -or 
           $Val -eq "admin@your-domain.com"
}

function New-RandomBase64 {
    return [Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 }) -as [byte[]])
}

# .env location
$ENV_FILE = $null
if (Test-Path "../.env") { $ENV_FILE = (Resolve-Path "../.env").Path }
if (Test-Path ".env") { $ENV_FILE = (Resolve-Path ".env").Path }
if (!$ENV_FILE) {
    Copy-Item ".env.example" ".env"
    $ENV_FILE = (Resolve-Path ".env").Path
}

# Read values
$RUN_MODE = Get-EnvValue $ENV_FILE "RUN_MODE"
$DOMAIN_NAME = Get-EnvValue $ENV_FILE "DOMAIN_NAME"
$SSL_EMAIL = Get-EnvValue $ENV_FILE "SSL_EMAIL"
$LOCALHOST_PORT = Get-EnvValue $ENV_FILE "LOCALHOST_PORT"
$PLUMOAI_VERSION = Get-EnvValue $ENV_FILE "PLUMOAI_VERSION"

# If PLUMOAI_VERSION isn't set, try to infer from quickstart.ps1 (bundled release)
if (Test-Placeholder $PLUMOAI_VERSION) {
    $qs = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "quickstart.ps1"
    if (Test-Path $qs) {
        $m = Select-String -Path $qs -Pattern '^\s*\$VERSION\s*=\s*"([^"]+)"\s*$' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($m -and $m.Matches.Count -gt 0) {
            $PLUMOAI_VERSION = $m.Matches[0].Groups[1].Value
        }
    }
    if (Test-Placeholder $PLUMOAI_VERSION) { $PLUMOAI_VERSION = "v1.0.1" }
    Set-EnvValue $ENV_FILE "PLUMOAI_VERSION" $PLUMOAI_VERSION
}

$needsPrompt = (Test-Placeholder $RUN_MODE) -or (Test-Placeholder $DOMAIN_NAME) -or (Test-Placeholder $SSL_EMAIL)

if ($needsPrompt -and [Environment]::UserInteractive) {
    Write-Host ""
    Write-Host "How do you want to run PlumoAI?"
    Write-Host "  1) Domain (HTTPS with Let's Encrypt) - for production"
    Write-Host "  2) Localhost (HTTP) - for local development"
    # Always ask for the run mode when interactive so users can override existing .env values.
    $choice = Read-Host "Choose [1/2]"
    if ($choice -eq "2") {
        $RUN_MODE = "localhost"
        $DOMAIN_NAME = "localhost"
        $SSL_EMAIL = "not-used@localhost"
    } elseif ($choice -eq "1") {
        $RUN_MODE = "domain"
    } elseif (Test-Placeholder $RUN_MODE) {
        # Default for invalid input when RUN_MODE isn't already set
        $RUN_MODE = "domain"
    }
    if ($RUN_MODE -eq "localhost") {
        $DOMAIN_NAME = "localhost"
        $SSL_EMAIL = "not-used@localhost"
    }
    if ($RUN_MODE -ne "localhost") {
        if (Test-Placeholder $DOMAIN_NAME) { $DOMAIN_NAME = Read-Host "Enter your domain (e.g. self.plumoai.com)" }
        if (Test-Placeholder $SSL_EMAIL) { $SSL_EMAIL = Read-Host "Enter SSL email for Let's Encrypt" }
    }
    # Save to .env
    if (![string]::IsNullOrWhiteSpace($RUN_MODE)) { Set-EnvValue $ENV_FILE "RUN_MODE" $RUN_MODE }
    if (![string]::IsNullOrWhiteSpace($DOMAIN_NAME)) { Set-EnvValue $ENV_FILE "DOMAIN_NAME" $DOMAIN_NAME }
    if (![string]::IsNullOrWhiteSpace($SSL_EMAIL)) { Set-EnvValue $ENV_FILE "SSL_EMAIL" $SSL_EMAIL }
    if (![string]::IsNullOrWhiteSpace($LOCALHOST_PORT)) { Set-EnvValue $ENV_FILE "LOCALHOST_PORT" $LOCALHOST_PORT }
    if (![string]::IsNullOrWhiteSpace($PLUMOAI_VERSION)) { Set-EnvValue $ENV_FILE "PLUMOAI_VERSION" $PLUMOAI_VERSION }
    # Re-read
    $RUN_MODE = Get-EnvValue $ENV_FILE "RUN_MODE"
    $DOMAIN_NAME = Get-EnvValue $ENV_FILE "DOMAIN_NAME"
    $SSL_EMAIL = Get-EnvValue $ENV_FILE "SSL_EMAIL"
    $LOCALHOST_PORT = Get-EnvValue $ENV_FILE "LOCALHOST_PORT"
    $PLUMOAI_VERSION = Get-EnvValue $ENV_FILE "PLUMOAI_VERSION"
}

$RUN_MODE = if ([string]::IsNullOrWhiteSpace($RUN_MODE)) { "domain" } else { $RUN_MODE }
$LOCALHOST_PORT = if ([string]::IsNullOrWhiteSpace($LOCALHOST_PORT)) { "80" } else { $LOCALHOST_PORT }

# In localhost mode, always prompt for port when interactive (Enter keeps current/default).
if ($RUN_MODE -eq "localhost" -and [Environment]::UserInteractive) {
    $defaultPort = if ([string]::IsNullOrWhiteSpace($LOCALHOST_PORT) -or $LOCALHOST_PORT -notmatch '^\d+$') { "80" } else { $LOCALHOST_PORT }
    $enteredPort = Read-Host "Enter port for localhost [$defaultPort]"
    $LOCALHOST_PORT = if ([string]::IsNullOrWhiteSpace($enteredPort)) { $defaultPort } else { $enteredPort }
    Set-EnvValue $ENV_FILE "LOCALHOST_PORT" $LOCALHOST_PORT
}

# In localhost mode, DOMAIN_NAME and SSL_EMAIL are required by base docker-compose (warns if unset)
if ($RUN_MODE -eq "localhost") {
    if ([string]::IsNullOrWhiteSpace($DOMAIN_NAME) -or (Test-Placeholder $DOMAIN_NAME)) {
        $DOMAIN_NAME = "localhost"
        Set-EnvValue $ENV_FILE "DOMAIN_NAME" $DOMAIN_NAME
    }
    if ([string]::IsNullOrWhiteSpace($SSL_EMAIL) -or (Test-Placeholder $SSL_EMAIL)) {
        $SSL_EMAIL = "not-used@localhost"
        Set-EnvValue $ENV_FILE "SSL_EMAIL" $SSL_EMAIL
    }
}

# Validate
if ($RUN_MODE -ne "localhost") {
    if ((Test-Placeholder $DOMAIN_NAME) -or (Test-Placeholder $SSL_EMAIL)) {
        Write-Host "Error: For domain mode, DOMAIN_NAME and SSL_EMAIL must be set in .env (or run interactively)" -ForegroundColor Red
        exit 1
    }
}

Write-Host "Setting up secrets..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path "secrets" | Out-Null

# Fixed values
"authdb_prod" | Out-File -FilePath "secrets/mysql_db.txt" -Encoding ascii -NoNewline
"plumoai_user" | Out-File -FilePath "secrets/mysql_user.txt" -Encoding ascii -NoNewline
"plumoai_mongo" | Out-File -FilePath "secrets/mongo_db.txt" -Encoding ascii -NoNewline
"plumoai_mongo_user" | Out-File -FilePath "secrets/mongo_user.txt" -Encoding ascii -NoNewline

# Random passwords (only if missing)
$secrets = @(
    @{ File = "mysql_password.txt"; Name = "mysql_password" },
    @{ File = "mysql_root_password.txt"; Name = "mysql_root_password" },
    @{ File = "mongo_password.txt"; Name = "mongo_password" }
)
foreach ($s in $secrets) {
    $path = Join-Path "secrets" $s.File
    if (!(Test-Path $path)) {
        New-RandomBase64 | Out-File -FilePath $path -Encoding ascii -NoNewline
        Write-Host "  Created new $($s.Name)"
    } else {
        Write-Host "  Keeping existing $($s.Name)"
    }
}

# Build docker compose args
$dockerArgs = @("compose", "--env-file", $ENV_FILE, "-f", "docker-compose.yml")
if ($RUN_MODE -eq "localhost") { $dockerArgs += "-f", "docker-compose.local.yml" }

Write-Host "Starting services..." -ForegroundColor Cyan
if ($Fresh) {
    Write-Host "  Fresh install: stopping existing stack..." -ForegroundColor Gray
    & docker ($dockerArgs + @("down", "--remove-orphans", "--timeout", "20")) 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Error: failed to stop existing services (fresh mode)." -ForegroundColor Red
        exit 1
    }
    docker volume rm plumoai-self-hosted_mysql_data 2>$null
    Write-Host "  Fresh install: MySQL data volume removed"
} else {
    Write-Host "  Existing stack detected: applying changes without full restart..." -ForegroundColor Gray
}
Write-Host "  (First run: pulling images and starting DBs may take 5-10 min)" -ForegroundColor Gray
& docker ($dockerArgs + @("up", "-d", "--remove-orphans"))
if ($LASTEXITCODE -ne 0) {
    $composeHint = "docker compose --env-file $ENV_FILE -f docker-compose.yml"
    if ($RUN_MODE -eq "localhost") { $composeHint += " -f docker-compose.local.yml" }
    $composeHint += " ps"
    Write-Host "Error: failed to start services. Run '$composeHint' for details." -ForegroundColor Red
    exit 1
}

Write-Host ""
if ($RUN_MODE -eq "localhost") {
    Write-Host "PlumoAI is running at http://localhost:$LOCALHOST_PORT" -ForegroundColor Green
} else {
    Write-Host "PlumoAI is running at https://$DOMAIN_NAME" -ForegroundColor Green
}
