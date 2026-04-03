#!/bin/sh
# Set MySQL root password from secret (separate from app user).
set -e
if [ -f /run/secrets/mysql_root_password ]; then
  export MYSQL_ROOT_PASSWORD="$(cat /run/secrets/mysql_root_password)"
fi
exec /usr/local/bin/docker-entrypoint.sh "$@"
