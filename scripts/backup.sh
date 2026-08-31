#!/usr/bin/env bash
# PlumoAI Self-Hosted — back up MySQL, MongoDB, Traefik TLS certs, secrets, and .env.
# Usage: ./scripts/backup.sh [output-dir]
# Default output-dir: backups/<timestamp>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

COMPOSE_BIN="docker compose"
if ! docker compose version >/dev/null 2>&1; then
  COMPOSE_BIN="docker-compose"
fi

ENV_ARGS=""
[ -f .env ] && ENV_ARGS="--env-file .env"

if [ ! -f secrets/mysql_root_password.txt ] || [ ! -f secrets/mongo_user.txt ] || [ ! -f secrets/mongo_password.txt ]; then
  echo "Error: secrets/ is missing expected files. Run install.sh first." >&2
  exit 1
fi

OUT="${1:-backups/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$OUT"

echo "Backing up to $OUT ..."

echo "  Dumping MySQL..."
if ! $COMPOSE_BIN $ENV_ARGS exec -T mysql sh -c 'mysqldump -uroot -p"$(cat /run/secrets/mysql_root_password)" --all-databases --routines --triggers' > "$OUT/mysql.sql"; then
  echo "Error: MySQL dump failed (is the mysql service running?)." >&2
  exit 1
fi

echo "  Dumping MongoDB..."
if ! $COMPOSE_BIN $ENV_ARGS exec -T mongodb sh -c 'mongodump --username "$(cat /run/secrets/mongo_user)" --password "$(cat /run/secrets/mongo_password)" --authenticationDatabase admin --archive' > "$OUT/mongo.archive"; then
  echo "Error: MongoDB dump failed (is the mongodb service running?)." >&2
  exit 1
fi

echo "  Copying Traefik TLS store (acme.json)..."
if ! $COMPOSE_BIN $ENV_ARGS cp traefik:/letsencrypt/acme.json "$OUT/acme.json" 2>/dev/null; then
  echo "    (skipped — traefik not running yet, or no certificate issued)"
fi

echo "  Copying secrets/ and .env..."
cp -r secrets "$OUT/secrets"
[ -f .env ] && cp .env "$OUT/.env"

chmod 600 "$OUT"/*.txt 2>/dev/null || true
chmod -R 600 "$OUT/secrets" 2>/dev/null || true

echo "Backup complete: $OUT"
echo "Restore with: ./scripts/restore.sh $OUT"
