#!/bin/bash
# PlumoAI Self-Hosted — install (Linux / macOS / WSL / Git Bash)
# Keep behavior aligned with install.ps1 (Windows).
# Usage: ./install.sh
#        ./install.sh --fresh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

plumo_install_banner() {
  # Wordmark: magenta "Plumo", bright cyan "Ai" — brand PNG: assets/plumoai-logo.png
  echo ""
  if [ -t 1 ]; then
    printf '  \033[0;35mPlumo\033[0;96mAi\033[0m\n'
    printf '  \033[0;90mSelf-Hosted · installer\033[0m\n'
  else
    echo "  PlumoAi"
    echo "  Self-Hosted · installer"
  fi
  echo ""
}
plumo_install_banner

FRESH=false
for arg in "$@"; do
  case "$arg" in
    --fresh|-Fresh) FRESH=true ;;
  esac
done

# --- compose shim (docker compose vs docker-compose) ---
# Some hosts ship Docker Engine without the Compose v2 plugin; others only have the legacy docker-compose binary.
# We prefer `docker compose` when available, then fall back to `docker-compose`.
COMPOSE_BIN=""
if docker compose version >/dev/null 2>&1; then
  COMPOSE_BIN="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_BIN="docker-compose"
else
  echo "Error: Docker Compose not found." >&2
  echo "Install the Compose plugin (recommended) or docker-compose binary, then retry." >&2
  exit 1
fi

# --- .env (current dir or parent, same as install.ps1) ---
ENV_FILE=""
[ -f "../.env" ] && ENV_FILE="../.env"
[ -f ".env" ] && ENV_FILE=".env"
if [ -z "$ENV_FILE" ]; then
  cp .env.example .env
  ENV_FILE=".env"
fi

get_env_value() {
  local key="$1"
  [ -f "$ENV_FILE" ] || { echo ""; return; }
  local line
  line=$(grep -E "^${key}=" "$ENV_FILE" | head -1) || true
  [ -n "$line" ] || { echo ""; return; }
  echo "${line#*=}" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

set_env_key() {
  local key="$1" val="$2"
  [ -f "$ENV_FILE" ] || touch "$ENV_FILE"
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/plumo-env.XXXXXX")"
  grep -v "^${key}=" "$ENV_FILE" 2>/dev/null > "$tmp" || true
  echo "${key}=${val}" >> "$tmp"
  mv "$tmp" "$ENV_FILE"
}

# Placeholders for domain-related prompts (matches install.ps1 Test-Placeholder)
is_placeholder() {
  [[ -z "$1" || "$1" == *"<"* || "$1" == "your-domain.com" || "$1" == "admin@your-domain.com" ]]
}

plumoai_version_needs_default() {
  [[ -z "$1" || "$1" == *"<"* || "$1" == "@VERSION@" ]]
}

RUN_MODE=$(get_env_value RUN_MODE)
DOMAIN_NAME=$(get_env_value DOMAIN_NAME)
SSL_EMAIL=$(get_env_value SSL_EMAIL)
LOCALHOST_PORT=$(get_env_value LOCALHOST_PORT)
PLUMOAI_VERSION=$(get_env_value PLUMOAI_VERSION)

# Infer PLUMOAI_VERSION from quickstart (same idea as install.ps1)
if plumoai_version_needs_default "$PLUMOAI_VERSION"; then
  PLUMOAI_VERSION=""
  if [ -f "$SCRIPT_DIR/quickstart.sh" ]; then
    line=$(grep -E '^VERSION=' "$SCRIPT_DIR/quickstart.sh" | head -1 || true)
    if [[ "$line" =~ VERSION=\"([^\"]+)\" ]]; then
      PLUMOAI_VERSION="${BASH_REMATCH[1]}"
    fi
  fi
  if plumoai_version_needs_default "$PLUMOAI_VERSION" && [ -f "$SCRIPT_DIR/quickstart.ps1" ]; then
    line=$(grep -E '^\$VERSION\s*=' "$SCRIPT_DIR/quickstart.ps1" | head -1 || true)
    if [[ "$line" =~ \"([^\"]+)\" ]]; then
      PLUMOAI_VERSION="${BASH_REMATCH[1]}"
    fi
  fi
  if plumoai_version_needs_default "$PLUMOAI_VERSION"; then
    PLUMOAI_VERSION="v1.0.1"
  fi
  set_env_key PLUMOAI_VERSION "$PLUMOAI_VERSION"
  PLUMOAI_VERSION=$(get_env_value PLUMOAI_VERSION)
fi

needs_prompt=false
is_placeholder "$RUN_MODE" && needs_prompt=true
is_placeholder "$DOMAIN_NAME" && needs_prompt=true
is_placeholder "$SSL_EMAIL" && needs_prompt=true

if [ "$needs_prompt" = true ] && [ -t 0 ]; then
  echo ""
  echo "How do you want to run PlumoAI?"
  echo "  1) Domain (HTTPS with Let's Encrypt) - for production"
  echo "  2) Localhost (HTTP) - for local development"
  read -r -p "Choose [1/2]: " choice
  case "${choice:-}" in
    2)
      RUN_MODE=localhost
      DOMAIN_NAME=localhost
      SSL_EMAIL=not-used@localhost
      ;;
    1)
      RUN_MODE=domain
      ;;
    *)
      if is_placeholder "$RUN_MODE"; then
        RUN_MODE=domain
      fi
      ;;
  esac
  if [ "$RUN_MODE" = "localhost" ]; then
    DOMAIN_NAME=localhost
    SSL_EMAIL=not-used@localhost
  fi
  if [ "$RUN_MODE" != "localhost" ]; then
    is_placeholder "$DOMAIN_NAME" && read -r -p "Enter your domain (e.g. self.plumoai.com): " DOMAIN_NAME
    is_placeholder "$SSL_EMAIL" && read -r -p "Enter SSL email for Let's Encrypt: " SSL_EMAIL
  fi
  for var in RUN_MODE DOMAIN_NAME SSL_EMAIL LOCALHOST_PORT PLUMOAI_VERSION; do
    val="${!var}"
    if [[ -n "$val" ]]; then
      set_env_key "$var" "$val"
    fi
  done
  RUN_MODE=$(get_env_value RUN_MODE)
  DOMAIN_NAME=$(get_env_value DOMAIN_NAME)
  SSL_EMAIL=$(get_env_value SSL_EMAIL)
  LOCALHOST_PORT=$(get_env_value LOCALHOST_PORT)
  PLUMOAI_VERSION=$(get_env_value PLUMOAI_VERSION)
fi

RUN_MODE="${RUN_MODE:-domain}"
LOCALHOST_PORT="${LOCALHOST_PORT:-80}"

# Localhost: always prompt for port when interactive (Enter keeps default)
if [ "$RUN_MODE" = "localhost" ] && [ -t 0 ]; then
  default_port="$LOCALHOST_PORT"
  [[ "$default_port" =~ ^[0-9]+$ ]] || default_port=80
  read -r -p "Enter port for localhost [$default_port]: " entered_port
  LOCALHOST_PORT="${entered_port:-$default_port}"
  set_env_key LOCALHOST_PORT "$LOCALHOST_PORT"
fi

if [ "$RUN_MODE" = "localhost" ]; then
  if is_placeholder "$DOMAIN_NAME"; then
    DOMAIN_NAME=localhost
    set_env_key DOMAIN_NAME "$DOMAIN_NAME"
  fi
  if is_placeholder "$SSL_EMAIL"; then
    SSL_EMAIL=not-used@localhost
    set_env_key SSL_EMAIL "$SSL_EMAIL"
  fi
fi

if [ "$RUN_MODE" != "localhost" ]; then
  if is_placeholder "$DOMAIN_NAME" || is_placeholder "$SSL_EMAIL"; then
    echo "Error: For domain mode, DOMAIN_NAME and SSL_EMAIL must be set in .env (or run interactively)"
    exit 1
  fi
fi

email_provider_needs_prompt() {
  local p="$1"
  [[ -z "$p" || "$p" == *"<"* ]]
}

EMAIL_PROVIDER=$(get_env_value EMAIL_PROVIDER)
if email_provider_needs_prompt "$EMAIL_PROVIDER" && [ -t 0 ]; then
  echo ""
  echo "Optional: auth service email only (password reset, verification, etc.)"
  echo "  The rest of the install (secrets, databases, Docker) runs no matter what you pick."
  echo "  1) Configure email now (SES or SMTP)"
  echo "  2) Turn off outbound email (EMAIL_PROVIDER=disabled)"
  echo "  3) Skip email setup for now (EMAIL_PROVIDER=none; edit .env later — still installs everything else)"
  read -r -p "Email option [1/2/3]: " em_choice
  case "${em_choice:-}" in
    2)
      set_env_key EMAIL_PROVIDER "disabled"
      ;;
    3)
      set_env_key EMAIL_PROVIDER "none"
      ;;
    1)
      echo "Transport:"
      echo "  1) Amazon SES (default)"
      echo "  2) SMTP"
      read -r -p "Choose [1/2]: " tx_choice
      read -r -p "MAIL_FROM address [Enter for app default PlumoAi<noreply@plumoai.com>]: " mf_val
      case "${tx_choice:-1}" in
        2)
          set_env_key EMAIL_PROVIDER "smtp"
          read -r -p "SMTP_HOST (required): " sh_val
          if [[ -z "$sh_val" ]]; then
            echo "Error: SMTP_HOST is required for SMTP." >&2
            exit 1
          fi
          set_env_key SMTP_HOST "$sh_val"
          read -r -p "SMTP_PORT [587]: " sp_val
          sp_val="${sp_val:-587}"
          set_env_key SMTP_PORT "$sp_val"
          read -r -p "SMTP_USER [optional]: " su_val
          set_env_key SMTP_USER "${su_val:-}"
          read -r -p "SMTP_PASS [optional]: " spw_val
          set_env_key SMTP_PASS "${spw_val:-}"
          read -r -p "SMTP_SECURE (TLS for port 465) [y/N]: " ss_val
          if [[ "$ss_val" =~ ^[yY] ]]; then
            set_env_key SMTP_SECURE "true"
          else
            set_env_key SMTP_SECURE "false"
          fi
          if [[ -n "$mf_val" ]]; then
            set_env_key MAIL_FROM "$mf_val"
          fi
          ;;
        *)
          set_env_key EMAIL_PROVIDER "ses"
          read -r -p "ACCESSKEYID (AWS for SES): " ak_val
          read -r -p "SECRETACCESSKEY (AWS for SES): " sk_val
          read -r -p "REGION (e.g. us-east-1): " rg_val
          if [[ -z "$ak_val" || -z "$sk_val" || -z "$rg_val" ]]; then
            echo "Error: ACCESSKEYID, SECRETACCESSKEY, and REGION are required for SES." >&2
            exit 1
          fi
          set_env_key ACCESSKEYID "$ak_val"
          set_env_key SECRETACCESSKEY "$sk_val"
          set_env_key REGION "$rg_val"
          if [[ -n "$mf_val" ]]; then
            set_env_key MAIL_FROM "$mf_val"
          fi
          ;;
      esac
      ;;
  esac
fi

storage_backend_needs_prompt() {
  local v="$1"
  [[ -z "$v" || "$v" == *"<"* ]]
}

normalize_storage_backend() {
  local v
  v="$(echo "${1:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  case "$v" in
    s3|local) echo "$v" ;;
    *) echo "" ;;
  esac
}

STORAGE_BACKEND=$(get_env_value STORAGE_BACKEND)
LOCAL_STORAGE_PATH=$(get_env_value LOCAL_STORAGE_PATH)

if [ -t 0 ] && storage_backend_needs_prompt "$STORAGE_BACKEND"; then
  echo ""
  echo "File storage (company service uploads):"
  echo "  1) Local disk (default)"
  echo "  2) S3"
  read -r -p "Choose [1/2] (Enter = 1): " st_choice
  case "${st_choice:-1}" in
    2) STORAGE_BACKEND="s3" ;;
    *) STORAGE_BACKEND="local" ;;
  esac
  set_env_key STORAGE_BACKEND "$STORAGE_BACKEND"
fi

# Default to local if unset/invalid (non-interactive installs)
STORAGE_BACKEND="$(normalize_storage_backend "$STORAGE_BACKEND")"
STORAGE_BACKEND="${STORAGE_BACKEND:-local}"
set_env_key STORAGE_BACKEND "$STORAGE_BACKEND"

if [ "$STORAGE_BACKEND" = "s3" ]; then
  if [ -t 0 ]; then
    echo ""
    echo "S3 configuration (required for STORAGE_BACKEND=s3):"
    read -r -p "AWS_S3_BUCKET: " AWS_S3_BUCKET
    read -r -p "AWS_REGION: " AWS_REGION
    read -r -p "AWS_ACCESS_KEY_ID: " AWS_ACCESS_KEY_ID
    read -r -p "AWS_SECRET_ACCESS_KEY: " AWS_SECRET_ACCESS_KEY
    set_env_key AWS_S3_BUCKET "$AWS_S3_BUCKET"
    set_env_key AWS_REGION "$AWS_REGION"
    set_env_key AWS_ACCESS_KEY_ID "$AWS_ACCESS_KEY_ID"
    set_env_key AWS_SECRET_ACCESS_KEY "$AWS_SECRET_ACCESS_KEY"
  fi
else
  # Local storage uses a folder next to docker-compose.yml
  LOCAL_STORAGE_PATH="${LOCAL_STORAGE_PATH:-$SCRIPT_DIR/storage}"
  mkdir -p "$LOCAL_STORAGE_PATH"
  set_env_key LOCAL_STORAGE_PATH "$LOCAL_STORAGE_PATH"
fi

echo "Setting up secrets..."

mkdir -p secrets

echo -n "authdb_prod" > secrets/mysql_db.txt
echo -n "plumoai_user" > secrets/mysql_user.txt
echo -n "plumoai_mongo" > secrets/mongo_db.txt
echo -n "plumoai_mongo_user" > secrets/mongo_user.txt

if [ ! -f secrets/mysql_password.txt ]; then
  openssl rand -base64 32 | tr -d '\n' > secrets/mysql_password.txt
  echo "  Created new mysql_password"
else
  echo "  Keeping existing mysql_password"
fi
if [ ! -f secrets/mysql_root_password.txt ]; then
  openssl rand -base64 32 | tr -d '\n' > secrets/mysql_root_password.txt
  echo "  Created new mysql_root_password"
else
  echo "  Keeping existing mysql_root_password"
fi
if [ ! -f secrets/mongo_password.txt ]; then
  openssl rand -base64 32 | tr -d '\n' > secrets/mongo_password.txt
  echo "  Created new mongo_password"
else
  echo "  Keeping existing mongo_password"
fi

chmod 600 secrets/* 2>/dev/null || true
[ -f scripts/mongo-secrets-entrypoint.sh ] && chmod +x scripts/mongo-secrets-entrypoint.sh
[ -f scripts/init-mongo-user.sh ] && chmod +x scripts/init-mongo-user.sh
[ -f scripts/mysql-root-secrets-entrypoint.sh ] && chmod +x scripts/mysql-root-secrets-entrypoint.sh

# Strip CR from compose-mounted scripts (Windows CRLF breaks dash: "set: Illegal option -")
repair_docker_mount_lf() {
  for f in "$SCRIPT_DIR/scripts/"*.sh "$SCRIPT_DIR/init-privileges.sql"; do
    [ -f "$f" ] || continue
    if grep -q $'\r' "$f" 2>/dev/null; then
      tr -d '\r' < "$f" > "${f}.lf.$$" && mv "${f}.lf.$$" "$f"
      echo "  Fixed Windows line endings: $(basename "$f")"
    fi
  done
}
repair_docker_mount_lf

COMPOSE_FILES="-f docker-compose.yml"
[ "$RUN_MODE" = "localhost" ] && COMPOSE_FILES="-f docker-compose.yml -f docker-compose.local.yml"

ENV_ARGS=""
[ -f "$ENV_FILE" ] && ENV_ARGS="--env-file $ENV_FILE"

PS_HINT="$COMPOSE_BIN $ENV_ARGS -f docker-compose.yml"
[ "$RUN_MODE" = "localhost" ] && PS_HINT="$PS_HINT -f docker-compose.local.yml ps"

echo "Starting services..."
if [ "$FRESH" = true ]; then
  echo "  Fresh install: stopping existing stack..."
  if ! $COMPOSE_BIN $ENV_ARGS $COMPOSE_FILES down --remove-orphans --timeout 20 2>/dev/null; then
    echo "Error: failed to stop existing services (fresh mode)." >&2
    exit 1
  fi
  docker volume rm plumoai-self-hosted_mysql_data 2>/dev/null || true
  echo "  Fresh install: MySQL data volume removed"
else
  echo "  Existing stack detected: applying changes without full restart..."
fi
echo "  (First run: pulling images and starting DBs may take 5-10 min)"
if ! $COMPOSE_BIN $ENV_ARGS $COMPOSE_FILES up -d --remove-orphans; then
  echo "Error: failed to start services. Run '$PS_HINT' for details." >&2
  exit 1
fi

echo ""
if [ "$RUN_MODE" = "localhost" ]; then
  echo "PlumoAI is running at http://localhost:${LOCALHOST_PORT}"
else
  echo "PlumoAI is running at https://${DOMAIN_NAME}"
fi
