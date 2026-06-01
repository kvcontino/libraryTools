#!/usr/bin/env bash
# auto_rebuild_after_ingest.sh — wait for the running ingest.py to finish,
# then rebuild chunks + embeddings so newly converted books enter semantic search.
#
# Launched detached (setsid) at end of the 2026-05-31 library-prune session so the
# rebuild runs even if the Claude session is closed. One-shot: exits after rebuild.
set -uo pipefail

INGEST_PID="${1:?usage: auto_rebuild_after_ingest.sh <ingest_pid>}"
BOOTSTRAP="$HOME/2_projects/sessions/2026-05-03_semantic-search/bootstrap.sh"
LOG="$HOME/Library/tools/auto_rebuild.log"

log() { echo "[$(date -Iseconds)] $*" | tee -a "$LOG"; }

log "Watcher started; waiting for ingest PID $INGEST_PID to exit."

# Wait for the specific ingest process to finish. Poll every 60s.
while kill -0 "$INGEST_PID" 2>/dev/null; do
    sleep 60
done
log "Ingest PID $INGEST_PID has exited."

# Safety: also wait out any lingering marker_single child still flushing a book.
while pgrep -f "marker_single" >/dev/null 2>&1; do
    log "marker_single still active; waiting 60s."
    sleep 60
done

log "Starting chunk + embed rebuild via bootstrap.sh."
if bash "$BOOTSTRAP" >>"$LOG" 2>&1; then
    log "Rebuild COMPLETE. Semantic search is up to date."
else
    rc=$?
    log "Rebuild FAILED (exit $rc). Inspect $LOG and re-run bootstrap.sh manually."
fi
log "Watcher exiting."
