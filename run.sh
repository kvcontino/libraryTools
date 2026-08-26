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

echo "[$(date -Iseconds)] Chunking and embedding new books..."
# 2026-08-14: THE MISSING STAGE. For months this pipeline was ingest -> index ->
# report: it converted new books and made them full-text searchable, then
# stopped. The chunk+embed half lived in a session directory
# (2026-05-03_semantic-search/). kevadk graduated it on 2026-06-01 as
# rebuild_embeddings_if_complete.sh + library-embed.timer, but THIS clone sat on
# an older commit until 2026-08-14 and never had it. By then, 95 of 281
# books had ZERO chunks -- a third of the library invisible to /librarysearch
# while looking perfectly healthy in every other view.
#
# chunk_and_embed.py is incremental and additive: it never drops the table and
# never touches a book that already has chunks, so it is safe to run every
# week and safe to interrupt. It is a `uv run --script` with its own inline
# deps (sentence-transformers + CPU torch), NOT $PYTHON -- the ingest venv has
# no sentence-transformers and the two dependency sets should stay separate.
#
# It can be slow on a large backlog (~1.5 chunks/sec measured on the laptop).
# A normal week is a handful of books; the first catch-up run is the long one.
"$SCRIPT_DIR/chunk_and_embed.py" || echo "  !! chunk/embed failed — books are indexed but NOT semantically searchable"

echo "[$(date -Iseconds)] Writing report..."
"$PYTHON" "$SCRIPT_DIR/report.py"

echo "[$(date -Iseconds)] Done."
