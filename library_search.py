"""
Semantic search wrapper over the `book_chunks` collection.

Prepends `search_query:` (required by nomic-embed-text-v1.5) and shells out
to `llm similar`. Joins back to book_chunks for title + snippet context.
"""
import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

DEFAULT_DB = "/home/kevadk/Library/Markdown/library.db"
DEFAULT_COLLECTION = "book_chunks"
LLM_BIN = Path.home() / ".local/bin/llm"


def search(query: str, db: str, collection: str, n: int):
    prefixed = f"search_query: {query}"
    result = subprocess.run(
        [str(LLM_BIN), "similar", collection, "-d", db, "-c", prefixed, "-n", str(n)],
        capture_output=True, text=True, check=True,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def fetch_context(db: str, ids):
    conn = sqlite3.connect(db)
    rows = conn.execute(
        f"SELECT id, title, text FROM book_chunks WHERE id IN ({','.join('?' * len(ids))})",
        ids,
    ).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("-n", type=int, default=10)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument("--full", action="store_true", help="Print full chunk text, not just snippet")
    parser.add_argument("--book", default=None, help="Constrain results to chunks whose title contains this substring (case-insensitive)")
    args = parser.parse_args()

    # When filtering by --book we oversample (10x) and post-filter, since
    # `llm similar` has no native title-side filter.
    fetch_n = args.n * 10 if args.book else args.n
    hits = search(args.query, args.db, args.collection, fetch_n)
    if not hits:
        print("No results.", file=sys.stderr)
        return

    ids = [int(h["id"]) for h in hits]
    ctx = fetch_context(args.db, ids)

    if args.book:
        needle = args.book.lower()
        hits = [h for h in hits if needle in ctx.get(int(h["id"]), ("", ""))[0].lower()]
        hits = hits[: args.n]
        if not hits:
            print(f"No results in books matching '{args.book}'. Try a broader --book or drop the filter.", file=sys.stderr)
            return

    for hit in hits:
        cid = int(hit["id"])
        score = hit.get("score", 0.0)
        title, text = ctx.get(cid, ("(unknown)", ""))
        snippet = text if args.full else text[:300].replace("\n", " ") + ("…" if len(text) > 300 else "")
        print(f"[{score:.3f}] {title}")
        print(f"        {snippet}\n")


if __name__ == "__main__":
    main()
