#!/usr/bin/env bash
# sync_6reading_from_nc.sh — pull new books from Nextcloud into ~/6_reading/.
#
# Runs hourly via cron. Finds files in the Nextcloud 6_reading folder that
# don't yet exist locally and copies them to ~/6_reading/ so the ingest
# pipeline can process them.
#
# Requires: docker (kevadk user is in the docker group).

set -euo pipefail

NC_CONTAINER="nextcloud"
NC_SOURCE="/var/www/html/data/kevadk/files/6_reading"
LOCAL_TARGET="$HOME/6_reading"
LOG="$HOME/Library/tools/sync_6reading.log"

mkdir -p "$LOCAL_TARGET"

# List files present in Nextcloud's 6_reading folder
mapfile -t nc_files < <(docker exec "$NC_CONTAINER" \
    find "$NC_SOURCE" -maxdepth 1 -type f 2>/dev/null || true)

copied=0
for nc_path in "${nc_files[@]}"; do
    [[ -z "$nc_path" ]] && continue
    filename="$(basename "$nc_path")"
    local_path="$LOCAL_TARGET/$filename"

    if [[ ! -f "$local_path" ]]; then
        echo "[$(date -Iseconds)] Copying: $filename" | tee -a "$LOG"
        docker cp "$NC_CONTAINER:$nc_path" "$local_path"
        ((copied++)) || true
    fi
done

if [[ $copied -gt 0 ]]; then
    echo "[$(date -Iseconds)] Synced $copied new file(s) from Nextcloud to ~/6_reading/" | tee -a "$LOG"
fi
