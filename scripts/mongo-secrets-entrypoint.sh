#!/bin/sh
# Use MongoDB-specific secrets (mongo_user, mongo_password, mongo_db).
# Reads from /run/secrets and exports for MongoDB init.
set -e
if [ -f /run/secrets/mongo_user ]; then
  export MONGO_INITDB_ROOT_USERNAME="$(cat /run/secrets/mongo_user)"
fi
if [ -f /run/secrets/mongo_password ]; then
  export MONGO_INITDB_ROOT_PASSWORD="$(cat /run/secrets/mongo_password)"
fi
if [ -f /run/secrets/mongo_db ]; then
  export MONGO_INITDB_DATABASE="$(cat /run/secrets/mongo_db)"
fi
exec /usr/local/bin/docker-entrypoint.sh mongod "$@"
