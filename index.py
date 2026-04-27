import os
import re
import sqlite3
import logging
import socket
import uuid
from pathlib import Path
from datetime import datetime

import metadata as md_meta

# --- 1. CONFIGURATION ---
TARGET_DIR = Path("~/Library/Markdown").expanduser()
DB_PATH    = TARGET_DIR / "library.db"


# --- 2. DATABASE SETUP ---

# Columns added by phase 2: SHA-256, run_id, telemetry, normalized_title.
# We use ALTER TABLE … ADD COLUMN guarded by PRAGMA table_info so existing
# DBs migrate in place without a schema rebuild.
PHASE2_COLUMNS = [
    ("sha256",             "TEXT"),
    ("run_id",             "TEXT"),
    ("conversion_seconds", "REAL"),
    ("converter",          "TEXT"),
    ("converter_version",  "TEXT"),
    ("extracted_chars",    "INTEGER"),
    ("ocr_used",           "INTEGER"),
    ("status",             "TEXT"),
    ("error",              "TEXT"),
    ("normalized_title",   "TEXT"),
    ("isbn",               "TEXT"),
    ("doi",                "TEXT"),
]


def setup_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS books (
            id          INTEGER PRIMARY KEY,
            md_path     TEXT UNIQUE,
            title       TEXT,
            author      TEXT,
            source      TEXT,
            converted   TEXT,
            pages       INTEGER,
            indexed_at  TEXT
        );

        -- FTS5 with Porter stemming: 'search' matches 'searched', 'searching', etc.
        CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
            title,
            author,
            body,
            tokenize='porter unicode61'
        );

        CREATE TABLE IF NOT EXISTS runs (
            id                  TEXT PRIMARY KEY,
            started_at          TEXT,
            ended_at            TEXT,
            host                TEXT,
            files_total         INTEGER,
            files_done          INTEGER,
            files_failed        INTEGER,
            converter_versions  TEXT,
            notes               TEXT
        );
    """)

    existing = {row[1] for row in conn.execute("PRAGMA table_info(books)")}
    for col, ctype in PHASE2_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE books ADD COLUMN {col} {ctype}")

    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_books_sha256           ON books(sha256);
        CREATE INDEX IF NOT EXISTS idx_books_normalized_title ON books(normalized_title);
        CREATE INDEX IF NOT EXISTS idx_books_run_id           ON books(run_id);
    """)

    conn.commit()


# --- 3. FRONTMATTER PARSING ---

def parse_frontmatter(content):
    """Return (meta dict, body string). Body has frontmatter stripped."""
    meta = {}
    body = content

    m = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip().strip('"')
        body = content[m.end():]

    return meta, body


# --- 4. INDEXING ---

def get_indexed(conn):
    """Return {md_path: indexed_at} for all currently indexed files."""
    rows = conn.execute("SELECT md_path, indexed_at FROM books").fetchall()
    return {r[0]: r[1] for r in rows}


def _coerce_int(raw):
    if raw in (None, ""):
        return None
    try:
        return int(float(str(raw)))
    except (TypeError, ValueError):
        return None


def _coerce_float(raw):
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def warn_near_duplicates(conn, md_path, normalized_title, sha256):
    """(9) Warn when this file looks like a near-duplicate of one already in the DB."""
    if sha256:
        rows = conn.execute(
            "SELECT md_path FROM books WHERE sha256=? AND md_path<>?",
            (sha256, str(md_path)),
        ).fetchall()
        if rows:
            others = ", ".join(r[0] for r in rows)
            logging.warning(f"Duplicate (sha256) of {md_path.name}: {others}")
            return

    if normalized_title and len(normalized_title) >= 8:
        rows = conn.execute(
            "SELECT md_path FROM books WHERE normalized_title=? AND md_path<>?",
            (normalized_title, str(md_path)),
        ).fetchall()
        if rows:
            others = ", ".join(r[0] for r in rows)
            logging.warning(f"Near-duplicate title for {md_path.name} matches: {others}")


def index_file(conn, md_path, run_id=None):
    with open(md_path, "r", errors="replace") as f:
        content = f.read()

    meta, body = parse_frontmatter(content)

    title     = meta.get("title", md_path.stem)
    author    = meta.get("author", "")
    source    = meta.get("source", "")
    converted = meta.get("converted", "")
    pages     = _coerce_int(meta.get("pages")) or 0
    now       = datetime.now().isoformat()

    sha256          = meta.get("sha256") or None
    conversion_secs = _coerce_float(meta.get("conversion_seconds"))
    converter       = meta.get("converter") or None
    conv_version    = meta.get("converter_version") or None
    extracted_chars = _coerce_int(meta.get("extracted_chars"))
    ocr_used_raw    = meta.get("ocr_used")
    ocr_used        = (
        1 if str(ocr_used_raw).lower() in {"1", "true", "yes"}
        else 0 if str(ocr_used_raw).lower() in {"0", "false", "no"}
        else None
    )
    status          = meta.get("status") or "done"
    error           = meta.get("error") or None
    isbn            = meta.get("isbn") or None
    doi             = meta.get("doi") or None
    normalized      = md_meta.normalize_title(title)

    warn_near_duplicates(conn, md_path, normalized, sha256)

    conn.execute("""
        INSERT INTO books (
            md_path, title, author, source, converted, pages, indexed_at,
            sha256, run_id, conversion_seconds, converter, converter_version,
            extracted_chars, ocr_used, status, error, normalized_title, isbn, doi
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(md_path) DO UPDATE SET
            title=excluded.title,
            author=excluded.author,
            source=excluded.source,
            converted=excluded.converted,
            pages=excluded.pages,
            indexed_at=excluded.indexed_at,
            sha256=COALESCE(excluded.sha256, books.sha256),
            run_id=COALESCE(excluded.run_id, books.run_id),
            conversion_seconds=COALESCE(excluded.conversion_seconds, books.conversion_seconds),
            converter=COALESCE(excluded.converter, books.converter),
            converter_version=COALESCE(excluded.converter_version, books.converter_version),
            extracted_chars=COALESCE(excluded.extracted_chars, books.extracted_chars),
            ocr_used=COALESCE(excluded.ocr_used, books.ocr_used),
            status=excluded.status,
            error=excluded.error,
            normalized_title=excluded.normalized_title,
            isbn=COALESCE(excluded.isbn, books.isbn),
            doi=COALESCE(excluded.doi, books.doi)
    """, (
        str(md_path), title, author, source, converted, pages, now,
        sha256, run_id, conversion_secs, converter, conv_version,
        extracted_chars, ocr_used, status, error, normalized, isbn, doi,
    ))

    rowid = conn.execute(
        "SELECT id FROM books WHERE md_path=?", (str(md_path),)
    ).fetchone()[0]

    # Replace FTS entry
    conn.execute("DELETE FROM books_fts WHERE rowid=?", (rowid,))
    conn.execute(
        "INSERT INTO books_fts (rowid, title, author, body) VALUES (?, ?, ?, ?)",
        (rowid, title, author, body),
    )

    conn.commit()
    logging.info(f"Indexed: {md_path.name}")


# --- 5. RUN TRACKING ---

def start_run(conn, notes=""):
    """Create a row in runs and return the run_id."""
    rid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO runs (id, started_at, host, notes) VALUES (?, ?, ?, ?)",
        (rid, datetime.now().isoformat(), socket.gethostname(), notes),
    )
    conn.commit()
    return rid


def finish_run(conn, run_id, files_done, files_failed):
    conn.execute(
        """UPDATE runs SET ended_at=?, files_done=?, files_failed=?, files_total=?
           WHERE id=?""",
        (datetime.now().isoformat(), files_done, files_failed, files_done + files_failed, run_id),
    )
    conn.commit()


# --- 6. MAIN ---

def run_index(target_dir=None, run_id=None):
    td = target_dir or TARGET_DIR

    conn = sqlite3.connect(DB_PATH)
    setup_db(conn)
    indexed = get_indexed(conn)

    md_files  = list(td.rglob("*.md"))
    new_count = 0
    failed    = 0

    own_run = False
    if run_id is None:
        run_id = start_run(conn, notes="index.py standalone")
        own_run = True

    for md_path in md_files:
        path_str = str(md_path)
        mtime    = datetime.fromtimestamp(md_path.stat().st_mtime).isoformat()

        # Skip if indexed and index is current
        if path_str in indexed and indexed[path_str] >= mtime:
            continue

        try:
            index_file(conn, md_path, run_id=run_id)
            new_count += 1
        except Exception as e:
            failed += 1
            logging.error(f"Failed to index {md_path.name}: {e}")

    if own_run:
        finish_run(conn, run_id, new_count, failed)

    conn.close()
    logging.info(f"Index complete. {new_count} file(s) updated, {failed} failed.")
    return new_count


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    run_index()
