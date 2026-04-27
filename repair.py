"""
repair.py — re-run metadata heuristics over already-converted books.

Walks each row in the DB, opens the corresponding .md file, re-derives the
metadata from the source file (still on disk under ~/6_reading) plus the
converted markdown body, and rewrites the YAML frontmatter. Then re-indexes.

This does NOT re-convert PDFs/eBooks. It's the cheap repair pass for the
sizable majority of the library that's already ingested. Re-conversion
would take days; this completes in seconds-to-minutes.

Usage:
    python3 repair.py                # dry-run preview of changes
    python3 repair.py --apply        # actually rewrite files
    python3 repair.py --apply --enrich   # also call Open Library / Crossref
    python3 repair.py --apply --only-author    # only fix author fields
    python3 repair.py --apply --only-title     # only fix title fields
    python3 repair.py --apply --only-pages     # only backfill pages
    python3 repair.py --apply --only-hash      # only backfill sha256
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from pathlib import Path

import metadata as md_meta
from index import DB_PATH, TARGET_DIR, setup_db, run_index, start_run, finish_run

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")


def parse_frontmatter(content: str) -> tuple[dict, str, int]:
    """Return (meta, body, frontmatter_end_index). End index is 0 if no FM."""
    meta: dict = {}
    m = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    if not m:
        return meta, content, 0
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip().strip('"')
    return meta, content[m.end():], m.end()


def render_frontmatter(meta: dict) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if value == "" or value is None:
            continue
        s = str(value)
        if any(c in s for c in ':#{}[]|>&*"\''):
            s = f'"{s}"'
        lines.append(f"{key}: {s}")
    lines.append("---\n")
    return "\n".join(lines) + "\n"


def repair_one(md_path: Path, source_path: Path | None, opts) -> tuple[dict, dict] | None:
    """Return (old_meta, new_meta) if any change is needed, else None."""
    try:
        with open(md_path, "r", errors="replace") as f:
            content = f.read()
    except OSError as e:
        logging.warning(f"Cannot read {md_path}: {e}")
        return None

    old_meta, body, fm_end = parse_frontmatter(content)
    new_meta = dict(old_meta)

    # If we don't have a source file we can still clean what's in frontmatter
    # and pull a title from the body.
    if opts.fix_author or opts.fix_all:
        new_meta["author"] = md_meta.clean_author(new_meta.get("author"))

    if opts.fix_title or opts.fix_all:
        raw_title = (new_meta.get("title") or md_path.stem).strip()
        new_meta["title"] = md_meta.clean_title_from_filename(raw_title)
        if md_meta.title_looks_garbage(new_meta["title"]):
            h1 = md_meta.title_from_markdown(md_path)
            if h1 and not md_meta.title_looks_garbage(h1):
                new_meta["title"] = h1

    if (opts.fix_pages or opts.fix_all) and source_path and source_path.exists():
        try:
            cur_pages = int(new_meta.get("pages", 0) or 0)
        except (TypeError, ValueError):
            cur_pages = 0
        if cur_pages <= 0:
            suffix = source_path.suffix.lower()
            if suffix == ".epub":
                new_meta["pages"] = md_meta.epub_page_count(source_path)
            else:
                # For PDF/MOBI fall back to estimating from the markdown body.
                new_meta["pages"] = md_meta.estimate_pages_from_text(body)

    if (opts.fix_hash or opts.fix_all) and source_path and source_path.exists():
        if not new_meta.get("sha256"):
            try:
                new_meta["sha256"] = md_meta.sha256_file(source_path)
            except OSError as e:
                logging.warning(f"Hash failed for {source_path}: {e}")

    if (opts.fix_enrich or (opts.enrich and opts.fix_all)) and source_path and source_path.exists():
        md_meta.enrich(new_meta, source_path, body_path=md_path, enrich_external=True)

    # Treat None and "" as equivalent — both are skipped by the YAML renderer.
    def _norm(d):
        return {k: v for k, v in d.items() if v not in (None, "")}

    if _norm(new_meta) == _norm(old_meta):
        return None
    return old_meta, new_meta


def main():
    parser = argparse.ArgumentParser(description="Re-run metadata heuristics over already-ingested books.")
    parser.add_argument("--apply", action="store_true", help="Actually rewrite files (default: dry-run).")
    parser.add_argument("--enrich", action="store_true", help="Also call Open Library / Crossref.")
    parser.add_argument("--limit", type=int, default=0, help="Stop after N rows (0 = all).")
    parser.add_argument("--only-author", action="store_true")
    parser.add_argument("--only-title",  action="store_true")
    parser.add_argument("--only-pages",  action="store_true")
    parser.add_argument("--only-hash",   action="store_true")
    parser.add_argument("--only-enrich", action="store_true")
    args = parser.parse_args()

    only_flags = [args.only_author, args.only_title, args.only_pages, args.only_hash, args.only_enrich]
    args.fix_all     = not any(only_flags)
    args.fix_author  = args.only_author
    args.fix_title   = args.only_title
    args.fix_pages   = args.only_pages
    args.fix_hash    = args.only_hash
    args.fix_enrich  = args.only_enrich

    conn = sqlite3.connect(DB_PATH)
    setup_db(conn)
    rows = conn.execute("SELECT md_path, source FROM books").fetchall()
    conn.close()

    if args.limit:
        rows = rows[: args.limit]

    logging.info(f"Repair pass over {len(rows)} rows. Apply={args.apply}. Enrich={args.enrich}.")

    changed = 0
    skipped_no_md = 0
    for md_path_str, source_str in rows:
        md_path = Path(md_path_str)
        if not md_path.exists():
            skipped_no_md += 1
            continue

        source_path = Path(source_str) if source_str else None
        result = repair_one(md_path, source_path, args)
        if not result:
            continue
        old_meta, new_meta = result
        changed += 1

        diffs = []
        for k in sorted(set(old_meta) | set(new_meta)):
            ov, nv = old_meta.get(k), new_meta.get(k)
            if ov in (None, "") and nv in (None, ""):
                continue
            if ov != nv:
                diffs.append(f"{k}: {ov!r} -> {nv!r}")
        if not diffs:
            continue
        logging.info(f"{md_path.name}\n  " + "\n  ".join(diffs))

        if args.apply:
            with open(md_path, "r", errors="replace") as f:
                content = f.read()
            _, body, fm_end = parse_frontmatter(content)
            new_content = render_frontmatter(new_meta) + (body if fm_end else content)
            with open(md_path, "w") as f:
                f.write(new_content)

    logging.info(f"Done. {changed} files needed updates, {skipped_no_md} markdown files missing on disk.")

    if args.apply and changed:
        # Re-index so DB reflects the new frontmatter.
        conn = sqlite3.connect(DB_PATH)
        setup_db(conn)
        rid = start_run(conn, notes="repair.py post-apply reindex")
        conn.close()
        n = run_index(run_id=rid)
        conn = sqlite3.connect(DB_PATH)
        finish_run(conn, rid, n, 0)
        conn.close()
        logging.info(f"Reindex complete: {n} rows updated under run {rid}.")


if __name__ == "__main__":
    sys.exit(main())
