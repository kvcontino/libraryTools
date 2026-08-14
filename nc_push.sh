#!/usr/bin/env bash
# nc_push.sh — sync ~/Library/Markdown/ into the Nextcloud container.
#
# Runs at the end of run.sh (kevadk only). Copies all files from the local
# ~/Library/Markdown/ into the Nextcloud data volume, fixes ownership to
# www-data, then triggers occ files:scan so Nextcloud registers the new files.
#
# Requires: docker (kevadk user is in the docker group).

set -euo pipefail

LIBRARY_DIR="$HOME/Library/Markdown"
NC_CONTAINER="nextcloud"
NC_LIBRARY_PATH="/var/www/html/data/kevadk/files/Library/Markdown"
OCC="php /var/www/html/occ"

echo "[nc_push] Copying ~/Library/Markdown/ into Nextcloud container..."
docker cp "$LIBRARY_DIR/." "$NC_CONTAINER:$NC_LIBRARY_PATH/"

echo "[nc_push] Fixing ownership to www-data..."
docker exec "$NC_CONTAINER" chown -R www-data:www-data "$NC_LIBRARY_PATH"

echo "[nc_push] Running occ files:scan..."
docker exec -u www-data "$NC_CONTAINER" $OCC files:scan \
    --path="/kevadk/files/Library/Markdown" --no-interaction

echo "[nc_push] Done."
