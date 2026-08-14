#!/usr/bin/env bash
# rebuild_embeddings_if_complete.sh — idempotent guard for the chunk+embed rebuild.
#
# Run periodically by library-embed.timer. Does the (expensive) semantic-search
# rebuild ONLY when:
#   - no ingest is currently running (ingest.py / run.sh / marker_single), and
#   - there are zero unconverted source books, and
#   - the converted-book count has changed since the last successful rebuild.
# Otherwise it exits 0 immediately (cheap no-op every tick).
#
# Reboot-safe: invoked by a user-systemd timer with Linger=yes, so it resumes
# polling after a reboot with no login. Replaces the old PID-keyed detached watcher.
set -uo pipefail

TOOLS="$HOME/Library/tools"
BOOTSTRAP="$HOME/2_projects/sessions/2026-05-03_semantic-search/bootstrap.sh"
SENTINEL="$TOOLS/.embed_rebuild_state"      # stores converted-count last embedded
LOG="$TOOLS/auto_rebuild.log"
LOCK="$TOOLS/.embed_rebuild.lock"

log() { echo "[$(date -Iseconds)] $*" >>"$LOG"; }

# Single-instance: if a rebuild (or a prior tick) is mid-flight, bail.
exec 9>"$LOCK"
flock -n 9 || { log "another instance holds the lock; skipping tick."; exit 0; }

# 1. Don't act while ingestion is live (would rebuild against a partial corpus).
if pgrep -f "ingest.py|run.sh|marker_single" >/dev/null 2>&1; then
    exit 0
fi

# 2. Count converted vs unconverted source books.
read -r CONVERTED UNCONVERTED < <(python3 - <<'PY'
from pathlib import Path
SRC=Path("~/6_reading").expanduser(); MD=Path("~/Library/Markdown").expanduser()
CONV={".pdf",".epub",".mobi"}
def done(stem):
    md=MD/stem/f"{stem}.md"
    if not md.exists(): return False
    try:
        with open(md) as fh:
            for _ in range(60):
                l=fh.readline()
                if not l: break
                if l.strip()=="status: done": return True
    except OSError: pass
    return False
files=[f for f in SRC.iterdir() if f.is_file() and f.suffix.lower() in CONV]
c=sum(1 for f in files if done(f.stem)); u=len(files)-c
print(c, u)
PY
)

# 3. Only rebuild when everything is converted.
if [[ "$UNCONVERTED" -ne 0 ]]; then
    exit 0
fi

# 4. Skip if we've already embedded this exact converted-count.
PREV=""; [[ -f "$SENTINEL" ]] && PREV="$(cat "$SENTINEL" 2>/dev/null || true)"
if [[ "$PREV" == "$CONVERTED" ]]; then
    exit 0
fi

log "All $CONVERTED books converted, 0 unconverted (was embedded at: '${PREV:-never}'). Starting rebuild."
if bash "$BOOTSTRAP" >>"$LOG" 2>&1; then
    echo "$CONVERTED" >"$SENTINEL"
    log "Rebuild COMPLETE for $CONVERTED books. Semantic search up to date."
else
    rc=$?
    log "Rebuild FAILED (exit $rc). Will retry next tick. Inspect $LOG."
fi
