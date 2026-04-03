#!/bin/sh
# Create the same user in the application database so apps can connect with
# mongodb://user:pass@mongodb:27017/plumoai_mongo (auth against plumoai_mongo).
# Root user exists only in admin; without this, "Authentication failed" occurs.
set -e
[ -f /run/secrets/mongo_user ] || exit 0
[ -f /run/secrets/mongo_password ] || exit 0
[ -f /run/secrets/mongo_db ] || exit 0

USER="$(cat /run/secrets/mongo_user)"
DB="$(cat /run/secrets/mongo_db)"
PASS="$(cat /run/secrets/mongo_password)"
# Escape for use inside double-quoted JS string: \ and "
PASS_ESC="$(printf '%s' "$PASS" | sed 's/\\/\\\\/g; s/"/\\"/g')"

mongosh --quiet --username "$USER" --password "$PASS" --authenticationDatabase admin "$DB" --eval "
  if (db.getUser('$USER')) { quit(0); }
  db.createUser({
    user: '$USER',
    pwd: \"$PASS_ESC\",
    roles: [
      { role: 'readWrite', db: '$DB' },
      { role: 'dbAdmin', db: '$DB' }
    ]
  });
"
