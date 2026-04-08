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
$LOCALHOST_PORT = if ([string]::IsNullOrWhiteSpace($LOCALHOST_PORT)) { "7861" } else { $LOCALHOST_PORT }

# In localhost mode, always prompt for port when interactive (Enter keeps current/default).
if ($RUN_MODE -eq "localhost" -and [Environment]::UserInteractive) {
    $defaultPort = if ([string]::IsNullOrWhiteSpace($LOCALHOST_PORT) -or $LOCALHOST_PORT -notmatch '^\d+$') { "7861" } else { $LOCALHOST_PORT }
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

function Test-EmailProviderNeedsPrompt {
    param([string]$Val)
    return [string]::IsNullOrWhiteSpace($Val) -or ($Val -like "*<*" )
}

$EMAIL_PROVIDER = Get-EnvValue $ENV_FILE "EMAIL_PROVIDER"
if ([Environment]::UserInteractive -and (Test-EmailProviderNeedsPrompt $EMAIL_PROVIDER)) {
    Write-Host ""
    Write-Host "Optional: auth service email only (password reset, verification, etc.)"
    Write-Host "  The rest of the install (secrets, databases, Docker) runs no matter what you pick."
    Write-Host "  1) Configure email now (SES or SMTP)"
    Write-Host "  2) Turn off outbound email (EMAIL_PROVIDER=disabled)"
    Write-Host "  3) Skip email setup for now (EMAIL_PROVIDER=none; edit .env later - still installs everything else)"
    $emChoice = Read-Host "Email option [1/2/3]"
    if ($emChoice -eq "2") {
        Set-EnvValue $ENV_FILE "EMAIL_PROVIDER" "disabled"
    } elseif ($emChoice -eq "3") {
        Set-EnvValue $ENV_FILE "EMAIL_PROVIDER" "none"
    } elseif ($emChoice -eq "1") {
        Write-Host "Transport:"
        Write-Host "  1) Amazon SES (default)"
        Write-Host "  2) SMTP"
        $tx = Read-Host "Choose [1/2]"
        $mf = Read-Host "MAIL_FROM address [Enter for app default PlumoAi<noreply@plumoai.com>]"
        if ($tx -eq "2") {
            Set-EnvValue $ENV_FILE "EMAIL_PROVIDER" "smtp"
            $sh = Read-Host "SMTP_HOST (required)"
            if ([string]::IsNullOrWhiteSpace($sh)) {
                Write-Host "Error: SMTP_HOST is required for SMTP." -ForegroundColor Red
                exit 1
            }
            Set-EnvValue $ENV_FILE "SMTP_HOST" $sh
            $sp = Read-Host "SMTP_PORT [587]"
            if ([string]::IsNullOrWhiteSpace($sp)) { $sp = "587" }
            Set-EnvValue $ENV_FILE "SMTP_PORT" $sp
            $su = Read-Host "SMTP_USER [optional]"
            Set-EnvValue $ENV_FILE "SMTP_USER" $su
            $spw = Read-Host "SMTP_PASS [optional]"
            Set-EnvValue $ENV_FILE "SMTP_PASS" $spw
            $ss = Read-Host "SMTP_SECURE (TLS for port 465) [y/N]"
            $ssVal = if ($ss -match '^[yY]') { "true" } else { "false" }
            Set-EnvValue $ENV_FILE "SMTP_SECURE" $ssVal
            if (![string]::IsNullOrWhiteSpace($mf)) { Set-EnvValue $ENV_FILE "MAIL_FROM" $mf }
        } else {
            Set-EnvValue $ENV_FILE "EMAIL_PROVIDER" "ses"
            $ak = Read-Host "ACCESSKEYID (AWS for SES)"
            $sk = Read-Host "SECRETACCESSKEY (AWS for SES)"
            $rg = Read-Host "REGION (e.g. us-east-1)"
            if ([string]::IsNullOrWhiteSpace($ak) -or [string]::IsNullOrWhiteSpace($sk) -or [string]::IsNullOrWhiteSpace($rg)) {
                Write-Host "Error: ACCESSKEYID, SECRETACCESSKEY, and REGION are required for SES." -ForegroundColor Red
                exit 1
            }
            Set-EnvValue $ENV_FILE "ACCESSKEYID" $ak
            Set-EnvValue $ENV_FILE "SECRETACCESSKEY" $sk
            Set-EnvValue $ENV_FILE "REGION" $rg
            if (![string]::IsNullOrWhiteSpace($mf)) { Set-EnvValue $ENV_FILE "MAIL_FROM" $mf }
        }
    }
}

function Normalize-StorageBackend {
    param([string]$Val)
    if ([string]::IsNullOrWhiteSpace($Val)) { return "" }
    $v = $Val.Trim().ToLowerInvariant()
    if ($v -eq "s3" -or $v -eq "local") { return $v }
    return ""
}

function Test-StorageBackendNeedsPrompt {
    param([string]$Val)
    return [string]::IsNullOrWhiteSpace($Val) -or ($Val -like "*<*")
}

$OPENAI_API_KEY_KNOWLEDGEBASE = Get-EnvValue $ENV_FILE "OPENAI_API_KEY_KNOWLEDGEBASE"
if ([Environment]::UserInteractive -and (Test-Placeholder $OPENAI_API_KEY_KNOWLEDGEBASE)) {
    Write-Host ""
    Write-Host "Optional: Knowledgebase embeddings (company service)"
    Write-Host "  Provide an OpenAI API key to generate embeddings for knowledgebase content."
    Write-Host "  Press Enter to skip."
    $kb = Read-Host "OPENAI_API_KEY_KNOWLEDGEBASE"
    if (![string]::IsNullOrWhiteSpace($kb)) {
        Set-EnvValue $ENV_FILE "OPENAI_API_KEY_KNOWLEDGEBASE" $kb
    }
}

$STORAGE_BACKEND = Get-EnvValue $ENV_FILE "STORAGE_BACKEND"
$LOCAL_STORAGE_HOST_PATH = Get-EnvValue $ENV_FILE "LOCAL_STORAGE_HOST_PATH"
$LOCAL_STORAGE_CONTAINER_PATH = Get-EnvValue $ENV_FILE "LOCAL_STORAGE_CONTAINER_PATH"

if ([Environment]::UserInteractive -and (Test-StorageBackendNeedsPrompt $STORAGE_BACKEND)) {
    Write-Host ""
    Write-Host "File storage (company service uploads):"
    Write-Host "  1) Local disk (default)"
    Write-Host "  2) S3"
    $stChoice = Read-Host "Choose [1/2] (Enter = 1)"
    if ($stChoice -eq "2") { $STORAGE_BACKEND = "s3" } else { $STORAGE_BACKEND = "local" }
    Set-EnvValue $ENV_FILE "STORAGE_BACKEND" $STORAGE_BACKEND
}

# Default to local if unset/invalid (non-interactive installs)
$STORAGE_BACKEND = Normalize-StorageBackend $STORAGE_BACKEND
if ([string]::IsNullOrWhiteSpace($STORAGE_BACKEND)) { $STORAGE_BACKEND = "local" }
Set-EnvValue $ENV_FILE "STORAGE_BACKEND" $STORAGE_BACKEND

if ($STORAGE_BACKEND -eq "s3") {
    if ([Environment]::UserInteractive) {
        Write-Host ""
        Write-Host "S3 configuration (required for STORAGE_BACKEND=s3):"
        $AWS_S3_BUCKET = Read-Host "AWS_S3_BUCKET"
        $AWS_REGION = Read-Host "AWS_REGION"
        $AWS_ACCESS_KEY_ID = Read-Host "AWS_ACCESS_KEY_ID"
        $AWS_SECRET_ACCESS_KEY = Read-Host "AWS_SECRET_ACCESS_KEY"
        Set-EnvValue $ENV_FILE "AWS_S3_BUCKET" $AWS_S3_BUCKET
        Set-EnvValue $ENV_FILE "AWS_REGION" $AWS_REGION
        Set-EnvValue $ENV_FILE "AWS_ACCESS_KEY_ID" $AWS_ACCESS_KEY_ID
        Set-EnvValue $ENV_FILE "AWS_SECRET_ACCESS_KEY" $AWS_SECRET_ACCESS_KEY
    }
} else {
    # Local storage uses a folder next to docker-compose.yml
    $defaultLocal = (Join-Path $PSScriptRoot "storage")
    $localPath = if ([string]::IsNullOrWhiteSpace($LOCAL_STORAGE_HOST_PATH) -or ($LOCAL_STORAGE_HOST_PATH -like "*<*")) { $defaultLocal } else { $LOCAL_STORAGE_HOST_PATH }
    New-Item -ItemType Directory -Force -Path $localPath | Out-Null
    $resolved = (Resolve-Path $localPath).Path
    Set-EnvValue $ENV_FILE "LOCAL_STORAGE_HOST_PATH" $resolved

    # Container path must be POSIX (do not use drive-letter paths inside containers)
    $containerPath = if ([string]::IsNullOrWhiteSpace($LOCAL_STORAGE_CONTAINER_PATH) -or ($LOCAL_STORAGE_CONTAINER_PATH -like "*<*")) { "/data/plumoai/storage" } else { $LOCAL_STORAGE_CONTAINER_PATH }
    Set-EnvValue $ENV_FILE "LOCAL_STORAGE_CONTAINER_PATH" $containerPath
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

# Docker bind-mounts .sh from Windows with CRLF: dash sees "set -e^M" -> "set: Illegal option -". Force LF.
function Repair-DockerMountLineEndings {
    param([string]$Root)
    $toFix = @()
    $scriptsDir = Join-Path $Root "scripts"
    if (Test-Path $scriptsDir) {
        $toFix += Get-ChildItem -Path $scriptsDir -Filter "*.sh" -File -ErrorAction SilentlyContinue
    }
    $sql = Join-Path $Root "init-privileges.sql"
    if (Test-Path $sql) { $toFix += Get-Item $sql }
    foreach ($item in $toFix) {
        if (-not $item) { continue }
        $text = [IO.File]::ReadAllText($item.FullName)
        if ($text.IndexOf("`r") -lt 0) { continue }
        $fixed = $text -replace "`r`n", "`n" -replace "`r", "`n"
        [IO.File]::WriteAllText($item.FullName, $fixed, [Text.UTF8Encoding]::new($false))
        Write-Host "  Fixed Windows line endings: $($item.Name)" -ForegroundColor DarkGray
    }
}
Repair-DockerMountLineEndings -Root $PSScriptRoot

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
