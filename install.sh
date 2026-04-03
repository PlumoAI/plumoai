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

COMPOSE_FILES="-f docker-compose.yml"
[ "$RUN_MODE" = "localhost" ] && COMPOSE_FILES="-f docker-compose.yml -f docker-compose.local.yml"

ENV_ARGS=""
[ -f "$ENV_FILE" ] && ENV_ARGS="--env-file $ENV_FILE"

PS_HINT="docker compose $ENV_ARGS -f docker-compose.yml"
[ "$RUN_MODE" = "localhost" ] && PS_HINT="$PS_HINT -f docker-compose.local.yml ps"

echo "Starting services..."
if [ "$FRESH" = true ]; then
  echo "  Fresh install: stopping existing stack..."
  if ! docker compose $ENV_ARGS $COMPOSE_FILES down --remove-orphans --timeout 20 2>/dev/null; then
    echo "Error: failed to stop existing services (fresh mode)." >&2
    exit 1
  fi
  docker volume rm plumoai-self-hosted_mysql_data 2>/dev/null || true
  echo "  Fresh install: MySQL data volume removed"
else
  echo "  Existing stack detected: applying changes without full restart..."
fi
echo "  (First run: pulling images and starting DBs may take 5-10 min)"
if ! docker compose $ENV_ARGS $COMPOSE_FILES up -d --remove-orphans; then
  echo "Error: failed to start services. Run '$PS_HINT' for details." >&2
  exit 1
fi

echo ""
if [ "$RUN_MODE" = "localhost" ]; then
  echo "PlumoAI is running at http://localhost:${LOCALHOST_PORT}"
else
  echo "PlumoAI is running at https://${DOMAIN_NAME}"
fi
