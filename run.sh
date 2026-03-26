#!/usr/bin/env bash
# run.sh — run ingest + index in sequence.
#
# AD HOC:
#   chmod +x run.sh
#   ./run.sh
#
# CRON (nightly at 2am — edit with `crontab -e`):
#   0 2 * * * /path/to/run.sh >> /home/$USER/Library/Markdown/cron.log 2>&1
#
# QUERY REFERENCE (sqlite3):
#   sqlite3 ~/Library/Markdown/library.db
#
#   -- Full-text search (Porter stemming, so 'work' matches 'working', 'worked')
#   SELECT b.title, b.author, snippet(books_fts, 2, '[', ']', '...', 20)
#   FROM books_fts
#   JOIN books b ON books_fts.rowid = b.id
#   WHERE books_fts MATCH 'your search terms'
#   ORDER BY rank;
#
#   -- Filter by author
#   SELECT title, author, pages FROM books WHERE author LIKE '%Saval%';
#
#   -- Most recently converted
#   SELECT title, author, converted FROM books ORDER BY converted DESC LIMIT 10;
#
#   -- Books with missing author metadata (candidates for manual cleanup)
#   SELECT title, source FROM books WHERE author = '' OR author IS NULL;

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[$(date -Iseconds)] Starting ingest..."
python3 "$SCRIPT_DIR/ingest.py"

echo "[$(date -Iseconds)] Starting index..."
python3 "$SCRIPT_DIR/index.py"

echo "[$(date -Iseconds)] Done."
