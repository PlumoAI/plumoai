#!/usr/bin/env bash
# PlumoAI Self-Hosted — restore MySQL, MongoDB, and Traefik TLS certs from a
# backup directory created by scripts/backup.sh.
# Usage: ./scripts/restore.sh <backup-dir>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

IN="${1:-}"
if [ -z "$IN" ] || [ ! -d "$IN" ]; then
  echo "Usage: scripts/restore.sh <backup-dir>" >&2
  echo "  e.g. scripts/restore.sh backups/20260101-120000" >&2
  exit 1
fi

COMPOSE_BIN="docker compose"
if ! docker compose version >/dev/null 2>&1; then
  COMPOSE_BIN="docker-compose"
fi

ENV_ARGS=""
[ -f .env ] && ENV_ARGS="--env-file .env"

echo "This will overwrite the current MySQL and MongoDB data with the contents of: $IN"
read -r -p "Type 'yes' to continue: " confirm
if [ "$confirm" != "yes" ]; then
  echo "Aborted."
  exit 1
fi

if [ -f "$IN/mysql.sql" ]; then
  echo "  Restoring MySQL..."
  $COMPOSE_BIN $ENV_ARGS exec -T mysql sh -c 'mysql -uroot -p"$(cat /run/secrets/mysql_root_password)"' < "$IN/mysql.sql"
else
  echo "  No mysql.sql in backup — skipping MySQL restore."
fi

if [ -f "$IN/mongo.archive" ]; then
  echo "  Restoring MongoDB..."
  $COMPOSE_BIN $ENV_ARGS exec -T mongodb sh -c 'mongorestore --username "$(cat /run/secrets/mongo_user)" --password "$(cat /run/secrets/mongo_password)" --authenticationDatabase admin --archive --drop' < "$IN/mongo.archive"
else
  echo "  No mongo.archive in backup — skipping MongoDB restore."
fi

if [ -f "$IN/acme.json" ]; then
  echo "  Restoring Traefik TLS store..."
  $COMPOSE_BIN $ENV_ARGS cp "$IN/acme.json" traefik:/letsencrypt/acme.json
else
  echo "  No acme.json in backup — skipping TLS store restore."
fi

echo "Restore complete. Restart services to pick up the restored data:"
echo "  $COMPOSE_BIN $ENV_ARGS restart"
