#!/usr/bin/env bash
# run.sh — run ingest + index + report in sequence.
#
# Used both by the user (manual `./run.sh`) and by the systemd
# library-ingest.service. Always uses the venv Python so all dependencies
# (marker_pdf, mobi, etc.) resolve correctly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "FATAL: venv Python not found at $PYTHON" >&2
    echo "       Bootstrap with: cd $SCRIPT_DIR && uv sync   (or: uv venv && uv pip install -r requirements.txt)" >&2
    exit 1
fi

echo "[$(date -Iseconds)] Starting ingest..."
"$PYTHON" "$SCRIPT_DIR/ingest.py" --smallest-first

echo "[$(date -Iseconds)] Starting index..."
"$PYTHON" "$SCRIPT_DIR/index.py"

echo "[$(date -Iseconds)] Writing report..."
"$PYTHON" "$SCRIPT_DIR/report.py"

# Push results to Nextcloud if the sync script is present (kevadk only).
# On the laptop the sync script doesn't exist — this is a no-op there.
if [[ -x "$SCRIPT_DIR/nc_push.sh" ]]; then
    echo "[$(date -Iseconds)] Syncing to Nextcloud..."
    "$SCRIPT_DIR/nc_push.sh"
fi

echo "[$(date -Iseconds)] Done."
