#!/usr/bin/env python3
"""prune_nonbooks.py — remove documents that were indexed but are not books.

WHY THIS EXISTS
---------------
`~/Library/Markdown` is both the derived-book tree and the archive target for
the RSS briefings. `make_report.py` writes a Markdown copy of each daily
briefing into `Feed Briefings/` with book-shaped YAML frontmatter, deliberately,
"so index.py files it like everything else" -- and it did, one per day since
2026-07-16, plus the Saturday weekly.

`index.py` now skips that directory (see EXCLUDE_DIRS there), so the count stops
growing. This script removes the rows that accumulated BEFORE that fix. It is a
separate tool on purpose: excluding future documents is obviously correct and
was applied immediately; deleting rows destroys embeddings that cost CPU to
build, so it is a decision the human makes once, deliberately, not a side effect
of a bug fix.

WHY THE ROWS ARE WORTH REMOVING, NOT JUST HIDING
------------------------------------------------
Each briefing is the same template filled with different headlines, so they
embed at ~0.995 cosine to one another: a dense synthetic knot in a space whose
real structure is diffuse. That knot won the library map's book-affinity ranking
outright and had to be excluded by hand before the map could say anything true.
`/librarysearch` queries the same vectors and has no such exclusion.

WHAT IT TOUCHES
---------------
For each matching book row: `book_chunks` (by book_rowid), `embeddings` (by the
chunk ids those chunks own, which is how chunk_and_embed.py keys them),
`books_fts` (by rowid), then `books`. Deletion order is child-then-parent so a
crash midway leaves no chunk orphaned from a book that no longer exists.

The Markdown files themselves are NEVER touched. The archive stays on disk and
in Nextcloud; it just stops being a book.

USAGE
    python3 prune_nonbooks.py              # dry run: report only, changes nothing
    python3 prune_nonbooks.py --apply      # do it, after writing a backup
    python3 prune_nonbooks.py --apply --no-backup    # if you already have one

The backup is a full `VACUUM INTO` copy of library.db beside it, timestamped.
Restoring is `mv`. There is no partial undo, which is why --apply is explicit.
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path("~/Library/Markdown/library.db").expanduser()
LIB_ROOT = Path("~/Library/Markdown").expanduser()

# Same rule as index.EXCLUDE_DIRS, applied to md_path rather than to a live
# filesystem walk -- the rows we are removing may name files that still exist.
EXCLUDE_DIRS = {"Feed Briefings"}


def targets(conn):
    """Book rows whose md_path sits under an excluded top-level directory."""
    rows = conn.execute("SELECT id, md_path, title FROM books").fetchall()
    out = []
    for bid, md_path, title in rows:
        if not md_path:
            continue
        try:
            parts = Path(md_path).resolve().relative_to(LIB_ROOT.resolve()).parts
        except ValueError:
            continue
        if parts and parts[0] in EXCLUDE_DIRS:
            out.append((bid, md_path, title))
    return out


def _backup(conn) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = Path("~/backups/library").expanduser()
    bdir.mkdir(parents=True, exist_ok=True)
    backup = bdir / f"library.db.backup-{stamp}"
    conn.execute("VACUUM INTO ?", (str(backup),))
    print(f"backup written: {backup}  ({backup.stat().st_size/1e6:.0f} MB)")
    return backup


def prune_back_matter(conn, args) -> int:
    """Remove CHUNKS that are apparatus rather than prose.

    Uses chunk_and_embed.is_prose() -- the same gate that now runs at ingest --
    so the corpus converges on one definition instead of two. Imported rather
    than copied for exactly that reason.

    This deletes embeddings, which cost CPU to build and are not cheaply
    recreated, so it backs up first and requires --apply like everything else
    here.
    """
    import importlib.util
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("ce", here / "chunk_and_embed.py")
    ce = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ce)

    rows = conn.execute("SELECT id, book_rowid, title, text FROM book_chunks").fetchall()
    bad = [r for r in rows if not ce.is_prose(r[3])]
    if not bad:
        print("no back-matter chunks found")
        return 0

    ids = [str(r[0]) for r in bad]
    q = ",".join("?" * len(ids))
    n_emb = conn.execute(
        f"SELECT COUNT(*) FROM embeddings WHERE id IN ({q})", ids).fetchone()[0]

    from collections import Counter
    per_book = Counter(r[2] for r in bad)
    totals = Counter(r[2] for r in rows)

    print(f"chunks in corpus : {len(rows):,}")
    print(f"fail the prose gate: {len(bad):,} ({100*len(bad)/len(rows):.1f}%), "
          f"{n_emb:,} with embeddings")
    print(f"chunks after     : {len(rows) - len(bad):,}")
    print("\nmost affected books:")
    for t, n in per_book.most_common(10):
        print(f"  {n:>6} of {totals[t]:>6}  ({100*n/totals[t]:>5.1f}%)  {t[:52]}")
    # Concentration is the diagnostic, not the count. When a handful of books own
    # most of the failures they are broken conversions, and re-converting (or
    # removing) them fixes far more than pruning chunks does -- on 2026-08-31
    # deleting two such books took this from 19,678 chunks to 2,295. When the
    # failures are spread thin instead, it is ordinary back-matter and pruning
    # IS the fix. So report the shape and let the reader draw the conclusion.
    concentrated = sum(n for t, n in per_book.most_common(2))
    share = 100 * concentrated / len(bad)
    print(f"\n  note: the top 2 books account for {concentrated:,} of {len(bad):,} ({share:.0f}%).")
    if share >= 50:
        print("  That concentration means these are probably BROKEN CONVERSIONS, not\n"
              "  back-matter. Re-converting or removing those books fixes more than\n"
              "  pruning chunks will.")
    else:
        print("  Spread thin across many books, which is what ordinary back-matter\n"
              "  looks like. Pruning is the right fix here.")

    if not args.apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply --back-matter.")
        return 0

    if not args.no_backup:
        _backup(conn)
    conn.execute(f"DELETE FROM embeddings WHERE id IN ({q})", ids)
    conn.execute(f"DELETE FROM book_chunks WHERE id IN ({q})", ids)
    conn.commit()
    left = conn.execute("SELECT COUNT(*) FROM book_chunks").fetchone()[0]
    print(f"\ndone — {len(bad):,} chunks removed, {left:,} remain")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; without it this only reports")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the VACUUM INTO backup (only if you have one)")
    ap.add_argument("--back-matter", action="store_true",
                    help="instead of whole non-book DOCUMENTS, remove individual "
                         "CHUNKS that fail chunk_and_embed.is_prose() -- indexes, "
                         "bibliographies and pipe-table wreckage")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)

    if args.back_matter:
        return prune_back_matter(conn, args)

    victims = targets(conn)

    if not victims:
        print("nothing to prune — no book rows under", "/".join(sorted(EXCLUDE_DIRS)))
        return 0

    ids = [v[0] for v in victims]
    q = ",".join("?" * len(ids))
    n_chunks = conn.execute(
        f"SELECT COUNT(*) FROM book_chunks WHERE book_rowid IN ({q})", ids).fetchone()[0]
    chunk_ids = [str(r[0]) for r in conn.execute(
        f"SELECT id FROM book_chunks WHERE book_rowid IN ({q})", ids)]
    n_emb = 0
    if chunk_ids:
        qc = ",".join("?" * len(chunk_ids))
        n_emb = conn.execute(
            f"SELECT COUNT(*) FROM embeddings WHERE id IN ({qc})", chunk_ids).fetchone()[0]

    total_books = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    print(f"database      : {DB_PATH}")
    print(f"books total   : {total_books}")
    print(f"to remove     : {len(victims)} documents, {n_chunks} chunks, {n_emb} embeddings")
    print(f"books after   : {total_books - len(victims)}")
    print()
    for _, md_path, title in sorted(victims, key=lambda v: v[2] or ""):
        print(f"  {title}")
    print()

    if not args.apply:
        print("DRY RUN — nothing changed. Re-run with --apply to delete.")
        return 0

    if not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        # NOT beside library.db. That directory is ~/Library/Markdown, which is a
        # symlink into ~/Nextcloud2 -- a 1.2 GB backup written there lands inside
        # a sync tree and inside the Elements backup set, for a file whose whole
        # purpose is to be temporary. ~/backups is where backups live here.
        bdir = Path("~/backups/library").expanduser()
        bdir.mkdir(parents=True, exist_ok=True)
        backup = bdir / f"library.db.backup-{stamp}"
        # VACUUM INTO writes a consistent copy without holding the source open
        # for the duration -- safer than a file copy under a live reader.
        conn.execute("VACUUM INTO ?", (str(backup),))
        print(f"backup written: {backup}  ({backup.stat().st_size/1e6:.0f} MB)")

    # Child rows first: a crash midway must never leave a chunk pointing at a
    # book row that is already gone.
    if chunk_ids:
        qc = ",".join("?" * len(chunk_ids))
        conn.execute(f"DELETE FROM embeddings WHERE id IN ({qc})", chunk_ids)
    conn.execute(f"DELETE FROM book_chunks WHERE book_rowid IN ({q})", ids)
    try:
        conn.execute(f"DELETE FROM books_fts WHERE rowid IN ({q})", ids)
    except sqlite3.OperationalError as e:
        print(f"  note: books_fts not pruned ({e}); rebuild it if search looks stale")
    conn.execute(f"DELETE FROM books WHERE id IN ({q})", ids)
    conn.commit()

    after = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    print(f"done — books now {after}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
