import re
import sqlite3
import logging
from pathlib import Path
from datetime import datetime

# --- 1. CONFIGURATION ---
TARGET_DIR = Path("~/Library/Markdown").expanduser()
DB_PATH    = TARGET_DIR / "library.db"


# --- 2. DATABASE SETUP ---

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


def index_file(conn, md_path):
    with open(md_path, "r", errors="replace") as f:
        content = f.read()

    meta, body = parse_frontmatter(content)

    title     = meta.get("title", md_path.stem)
    author    = meta.get("author", "")
    source    = meta.get("source", "")
    converted = meta.get("converted", "")
    pages_raw = meta.get("pages", "")
    pages     = int(pages_raw) if pages_raw.isdigit() else 0
    now       = datetime.now().isoformat()

    conn.execute("""
        INSERT INTO books (md_path, title, author, source, converted, pages, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(md_path) DO UPDATE SET
            title=excluded.title,
            author=excluded.author,
            source=excluded.source,
            converted=excluded.converted,
            pages=excluded.pages,
            indexed_at=excluded.indexed_at
    """, (str(md_path), title, author, source, converted, pages, now))

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


# --- 5. MAIN ---

def run_index(target_dir=None):
    td = target_dir or TARGET_DIR

    conn = sqlite3.connect(DB_PATH)
    setup_db(conn)
    indexed = get_indexed(conn)

    md_files  = list(td.rglob("*.md"))
    new_count = 0

    for md_path in md_files:
        path_str = str(md_path)
        mtime    = datetime.fromtimestamp(md_path.stat().st_mtime).isoformat()

        # Skip if indexed and index is current
        if path_str in indexed and indexed[path_str] >= mtime:
            continue

        try:
            index_file(conn, md_path)
            new_count += 1
        except Exception as e:
            logging.error(f"Failed to index {md_path.name}: {e}")

    conn.close()
    logging.info(f"Index complete. {new_count} file(s) updated.")
    return new_count


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    run_index()
