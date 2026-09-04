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
NO_INGEST=0
for a in "$@"; do
  case "$a" in
    --verify-only) VERIFY_ONLY=1 ;;
    --skip-embed)  SKIP_EMBED=1 ;;
    --no-ingest)   NO_INGEST=1 ;;
    *) echo "unknown flag: $a" >&2
       echo "usage: run.sh [--verify-only] [--skip-embed] [--no-ingest]" >&2; exit 64 ;;
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
    # CHUNK/EMBEDDING PARITY. Chunking and embedding are two steps in one
    # script, and only the first is pure python -- the second needs
    # sentence-transformers, which only exists under `uv run --script`. Run the
    # file with a bare python3 and it chunks happily, then dies on the import.
    # That happened on 2026-09-03 and left 1,130 chunks with no vectors: every
    # book had chunks, so every "is anything missing" query said yes, and the
    # book was silently invisible to /librarysearch. Same shape as the 95-books-
    # with-zero-chunks incident, one layer down and harder to see, because the
    # obvious health question was already answering "fine".
    #
    # Counting rows is enough and is instant. A mismatch is never benign here:
    # chunk_and_embed.py commits per batch, so an interrupt leaves a gap that
    # only a re-run closes.
    ce=$("$PYTHON" - "$SCRIPT_DIR" <<'PYEOF'
import sqlite3, sys
from pathlib import Path
db = Path("~/Library/Markdown/library.db").expanduser()
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
ch = c.execute("SELECT count(*) FROM book_chunks").fetchone()[0]
em = c.execute("SELECT count(*) FROM embeddings").fetchone()[0]
print(f"{ch} {em}")
PYEOF
)
    read -r n_chunks n_embeds <<<"$ce"
    if [[ "$n_chunks" == "$n_embeds" ]]; then
        printf '  chunks == embeddings (%s)\n' "$n_chunks"
    else
        printf '  CHUNK/EMBEDDING MISMATCH: %s chunks, %s embeddings (%s unembedded)\n' \
               "$n_chunks" "$n_embeds" "$(( n_chunks - n_embeds ))"
        echo   "    fix: cd $SCRIPT_DIR && uv run --script chunk_and_embed.py"
        echo   "    (NOT \`python3 chunk_and_embed.py\` — that chunks, then fails to import)"
        rc=1
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

# --no-ingest: index + chunk/embed + report, WITHOUT the conversion scan.
#
# WHY THIS MODE EXISTS. ingest.py scans SOURCE_DIR (~/6_reading) with a
# top-level iterdir(). That directory holds five entries and no books; the
# 61-title EPUB shelf is one level down in books/epub/, which the scan has never
# looked at. So the weekly timer spent ~17 min CPU and 2.9 GB finding nothing,
# every week, and reported success for doing it.
#
# The obvious fix -- repoint SOURCE_DIR at books/epub/ -- was MEASURED before
# being applied (2026-08-31) and turns out to add nothing: all 61 EPUBs on that
# shelf are already in the library, every one matching an existing row on an
# exact normalised-token comparison, at every threshold from 0.3 to 1.0. There
# is nothing there to ingest.
#
# Nothing arrives in ~/6_reading on its own any more either: it is a symlink
# into Nextcloud2, and the Nextcloud leg died with kevadk in 2026-06. Files can
# still be dropped there by hand -- which is exactly when you want a FULL run,
# by hand, rather than a weekly scan of a directory nobody writes to.
#
# So the timer keeps the half that does real work (index + embed + report,
# which is what makes /librarysearch current) and drops the half that cannot.
if (( NO_INGEST )); then
    echo "[$(date -Iseconds)] --no-ingest: conversion scan skipped (nothing arrives in SOURCE_DIR unattended)"
    echo "                    add a book by hand? run without this flag."
else
    echo "[$(date -Iseconds)] Starting ingest..."
    "$PYTHON" "$SCRIPT_DIR/ingest.py" --smallest-first
fi

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
