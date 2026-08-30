#!/usr/bin/env bash
# run.sh — run ingest + index + report in sequence.
#
# Used both by the user (manual `./run.sh`) and by the systemd
# library-ingest.service. Always uses the venv Python so all dependencies
# (marker_pdf, mobi, etc.) resolve correctly.

set -euo pipefail

# --verify-only: prove the pipeline is WIRED without paying for a full pass.
# Written 2026-08-30. The 2026-08-26 Nextcloud-push fix could not be confirmed
# without a ~17 min / 2.9G run, so it shipped "verified structurally" and sat
# unproven until a boot catch-up happened to exercise it three days later.
# This runs every stage's --help / import path and the report, which is the
# part that reads the database and would notice a schema drift, and skips the
# two expensive stages (conversion and embedding).
VERIFY_ONLY=0
SKIP_EMBED=0
for a in "$@"; do
  case "$a" in
    --verify-only) VERIFY_ONLY=1 ;;
    --skip-embed)  SKIP_EMBED=1 ;;
    *) echo "unknown flag: $a" >&2
       echo "usage: run.sh [--verify-only] [--skip-embed]" >&2; exit 64 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "FATAL: venv Python not found at $PYTHON" >&2
    echo "       Bootstrap with: cd $SCRIPT_DIR && uv sync   (or: uv venv && uv pip install -r requirements.txt)" >&2
    exit 1
fi

if [[ "$VERIFY_ONLY" == 1 ]]; then
    echo "[$(date -Iseconds)] --verify-only: checking the pipeline is wired"
    rc=0
    for stage in ingest.py index.py report.py; do
        if "$PYTHON" -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$SCRIPT_DIR/$stage"; then
            echo "  $stage parses"
        else
            echo "  $stage DOES NOT PARSE"; rc=1
        fi
    done
    # chunk_and_embed.py is a `uv run --script` with its own inline deps, so it
    # is parsed with the system python rather than the ingest venv's.
    if python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$SCRIPT_DIR/chunk_and_embed.py"; then
        echo "  chunk_and_embed.py parses"
    else
        echo "  chunk_and_embed.py DOES NOT PARSE"; rc=1
    fi
    # The report is the cheap end-to-end proof: it opens the database, runs
    # every query, and would fail loudly on a schema drift.
    echo "[$(date -Iseconds)] Running report (reads the DB, writes nothing else)..."
    if "$PYTHON" "$SCRIPT_DIR/report.py" >/dev/null; then
        echo "  report.py ran against the live database"
    else
        echo "  report.py FAILED"; rc=1
    fi
    if [[ "$rc" == 0 ]]; then
        echo "[$(date -Iseconds)] verify-only: OK"
    else
        echo "[$(date -Iseconds)] verify-only: FINDINGS above"
    fi
    exit "$rc"
fi

echo "[$(date -Iseconds)] Starting ingest..."
"$PYTHON" "$SCRIPT_DIR/ingest.py" --smallest-first

echo "[$(date -Iseconds)] Starting index..."
"$PYTHON" "$SCRIPT_DIR/index.py"

if [[ "$SKIP_EMBED" == 1 ]]; then
    echo "[$(date -Iseconds)] --skip-embed: chunk/embed stage skipped by request"
else
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
fi

echo "[$(date -Iseconds)] Writing report..."
"$PYTHON" "$SCRIPT_DIR/report.py"

echo "[$(date -Iseconds)] Done."
