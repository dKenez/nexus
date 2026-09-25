#!/bin/bash
# Valheim dedicated server entrypoint for nexus.
#
# nexus mounts the game's data directory at /data (restored from the latest backup, or empty
# on first start) and passes settings as environment variables (recipe.toml `env` and
# `secret_env`).
set -euo pipefail

: "${SERVER_NAME:?SERVER_NAME is required}"
: "${WORLD_NAME:?WORLD_NAME is required}"
: "${SERVER_PASSWORD:?SERVER_PASSWORD is required}"
SERVER_PUBLIC="${SERVER_PUBLIC:-1}"
SERVER_PORT="${SERVER_PORT:-2456}"

if [ "${#SERVER_PASSWORD}" -lt 5 ]; then
    echo "SERVER_PASSWORD must be at least 5 characters (Valheim requirement)" >&2
    exit 1
fi

# First start on an empty volume: seed the admin/ban/permit lists.
mkdir -p /data/worlds_local
for file in /opt/nexus/defaults/*; do
    target="/data/$(basename "$file")"
    [ -e "$target" ] || cp "$file" "$target"
done

export LD_LIBRARY_PATH="/app/linux64:${LD_LIBRARY_PATH:-}"
export SteamAppId=892970

cd /app
# -savedir keeps worlds and the lists under /data. exec so docker stop's SIGTERM reaches the
# server, which saves the world on shutdown.
exec ./valheim_server.x86_64 \
    -nographics -batchmode \
    -name "$SERVER_NAME" \
    -port "$SERVER_PORT" \
    -world "$WORLD_NAME" \
    -password "$SERVER_PASSWORD" \
    -public "$SERVER_PUBLIC" \
    -savedir /data
