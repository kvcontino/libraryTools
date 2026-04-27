"""
report.py — write a weekly health report for the markdown library.

Designed to run after ingest.py + index.py as the third stage in run.sh.
Outputs a dated markdown file to ~/Library/Markdown/reports/ and prints
the same summary to stdout (which systemd captures into the journal).

Usage:
    python3 report.py            # write today's report
    python3 report.py --stdout   # only print, don't write a file
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

TARGET_DIR  = Path("~/Library/Markdown").expanduser()
SOURCE_DIR  = Path("~/6_reading").expanduser()
DB_PATH     = TARGET_DIR / "library.db"
REPORTS_DIR = TARGET_DIR / "reports"


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def build_report() -> str:
    conn = sqlite3.connect(DB_PATH)
    now  = datetime.now()

    total_rows = q(conn, "SELECT COUNT(*) FROM books")[0][0]

    # Source vs DB gap
    source_files = [
        f for f in SOURCE_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in {".pdf", ".epub", ".mobi", ".djvu"}
    ] if SOURCE_DIR.exists() else []
    source_count = len(source_files)
    backlog      = max(0, source_count - total_rows)

    # Most recent run
    last_run_rows = q(conn, """
        SELECT id, started_at, ended_at, files_done, files_failed, notes
        FROM runs
        ORDER BY started_at DESC
        LIMIT 1
    """)
    last_run = last_run_rows[0] if last_run_rows else None

    # Runs in the last 7 days
    week_ago = (now - timedelta(days=7)).isoformat()
    recent_runs = q(conn, """
        SELECT id, started_at, ended_at, files_done, files_failed, notes
        FROM runs WHERE started_at >= ? ORDER BY started_at
    """, (week_ago,))

    # Quality flags
    no_author      = q(conn, "SELECT COUNT(*) FROM books WHERE author IS NULL OR author = ''")[0][0]
    zero_pages     = q(conn, "SELECT COUNT(*) FROM books WHERE pages IS NULL OR pages = 0")[0][0]
    no_sha256      = q(conn, "SELECT COUNT(*) FROM books WHERE sha256 IS NULL OR sha256 = ''")[0][0]
    failed_status  = q(conn, "SELECT COUNT(*) FROM books WHERE status = 'failed'")[0][0]

    # Stale junk authors that should have been cleaned by repair.py
    junk_authors = q(conn, """
        SELECT title, author, source FROM books
        WHERE LOWER(author) IN ('bobo','eurotrash','sabarba','sadegh','test','human','florian','contino')
        ORDER BY author, title
    """)

    # Duplicates by content hash
    dup_hashes = q(conn, """
        SELECT sha256, COUNT(*) AS n, GROUP_CONCAT(title, ' | ')
        FROM books WHERE sha256 IS NOT NULL AND sha256 <> ''
        GROUP BY sha256 HAVING n > 1
        ORDER BY n DESC LIMIT 10
    """)

    # Slowest conversions in last run (if telemetry present)
    slow = []
    if last_run and last_run[0]:
        slow = q(conn, """
            SELECT title, pages, conversion_seconds
            FROM books
            WHERE run_id = ? AND conversion_seconds IS NOT NULL
            ORDER BY conversion_seconds DESC LIMIT 5
        """, (last_run[0],))

    # ETA for backlog
    avg_secs = q(conn, """
        SELECT AVG(conversion_seconds) FROM books
        WHERE conversion_seconds IS NOT NULL AND conversion_seconds > 0
    """)[0][0]
    eta_str = "(no telemetry yet)"
    if avg_secs and backlog:
        eta_minutes = (backlog * avg_secs) / 60
        if eta_minutes < 60:
            eta_str = f"~{eta_minutes:.0f} min at {avg_secs:.0f}s/book"
        elif eta_minutes < 60 * 24:
            eta_str = f"~{eta_minutes/60:.1f} h at {avg_secs:.0f}s/book"
        else:
            eta_str = f"~{eta_minutes/60/24:.1f} days at {avg_secs:.0f}s/book"

    conn.close()

    # --- Render -----------------------------------------------------------

    L = []
    L.append(f"# Library report — {now.strftime('%Y-%m-%d %H:%M')}")
    L.append("")
    L.append(f"- Total books in DB: **{total_rows}**")
    L.append(f"- Source files in `~/6_reading`: **{source_count}**")
    L.append(f"- Backlog (source – DB): **{backlog}**" + ("" if backlog else " — caught up"))
    L.append(f"- Backlog ETA: {eta_str}")
    L.append("")

    L.append("## Quality flags")
    L.append("")
    L.append(f"- Rows with empty author: {no_author}")
    L.append(f"- Rows with 0 pages: {zero_pages}")
    L.append(f"- Rows missing sha256: {no_sha256}")
    L.append(f"- Rows with status='failed': {failed_status}")
    if junk_authors:
        L.append(f"- **Junk-author rows still present (run `repair.py --apply --only-author`):** {len(junk_authors)}")
        for title, author, source in junk_authors[:10]:
            L.append(f"    - `{author}` — {title}")
        if len(junk_authors) > 10:
            L.append(f"    - …and {len(junk_authors) - 10} more")
    L.append("")

    L.append("## Last run")
    L.append("")
    if last_run:
        rid, started, ended, done, failed, notes = last_run
        L.append(f"- ID: `{rid[:8]}`")
        L.append(f"- Started: {started}")
        L.append(f"- Ended:   {ended or '(still running or crashed)'}")
        L.append(f"- Files done: {done if done is not None else '?'}")
        L.append(f"- Files failed: {failed if failed is not None else '?'}")
        L.append(f"- Notes: {notes or ''}")
    else:
        L.append("- No runs recorded.")
    L.append("")

    L.append("## Last 7 days")
    L.append("")
    if recent_runs:
        L.append("| Started | Done | Failed | Notes |")
        L.append("|---|---|---|---|")
        for rid, started, ended, done, failed, notes in recent_runs:
            L.append(f"| {started[:16]} | {done or 0} | {failed or 0} | {notes or ''} |")
    else:
        L.append("- No runs in the last 7 days.")
    L.append("")

    if slow:
        L.append("## Slowest conversions in last run")
        L.append("")
        L.append("| Title | Pages | Seconds |")
        L.append("|---|---|---|")
        for title, pages, secs in slow:
            L.append(f"| {title[:60]} | {pages or '?'} | {secs:.1f} |")
        L.append("")

    if dup_hashes:
        L.append("## Possible duplicates (same SHA-256)")
        L.append("")
        for sha, n, titles in dup_hashes:
            L.append(f"- `{sha[:12]}` × {n}: {titles}")
        L.append("")

    return "\n".join(L) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdout", action="store_true", help="Print only; don't write a file.")
    args = parser.parse_args()

    text = build_report()
    print(text)

    if not args.stdout:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        out = REPORTS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.md"
        out.write_text(text)
        print(f"\nReport written to {out}")


if __name__ == "__main__":
    main()
