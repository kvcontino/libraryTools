#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "sentence-transformers>=3",
#     "einops",
#     "numpy",
#     "torch",
# ]
# [[tool.uv.index]]
# name = "pytorch-cpu"
# url = "https://download.pytorch.org/whl/cpu"
# explicit = true
# [tool.uv.sources]
# torch = { index = "pytorch-cpu" }
# ///
"""Semantic search over the markdown library, fully laptop-local.

REWRITTEN 2026-09-03 BECAUSE IT WAS DEAD.
-----------------------------------------
Both entry points raised on every call, and nothing noticed for months:

  * `DEFAULT_DB` still pointed at `/home/kevadk/Library/Markdown/library.db`.
    That machine was retired 2026-08-21 and its disks wiped 2026-08-26.
  * `search()` shelled out to `~/.local/bin/llm`, which is not installed on
    this laptop and never has been.
  * `mcp_library_search.py` calls `ls.search(query, n=n, book=book)`, but this
    module defined `search(query, db, collection, n)` -- no `book` parameter and
    a different return shape. The MCP tool therefore failed with
    `TypeError: search() got an unexpected keyword argument 'book'` on EVERY
    invocation, while still registering and advertising itself normally.

A registered MCP tool that raises looks exactly like a working one until you
call it. That is most of why the library "went from 2026-05 to 2026-08-30
without being read from once": the read path did not execute.

This module is now the single implementation. The MCP server imports it and its
existing call signature is the contract, so the signature here is written to
match rather than the other way round.

TWO THINGS THE VECTORS REQUIRE, both easy to get silently wrong:

  1. nomic-embed-text-v1.5 is an ASYMMETRIC model. Documents were embedded with
     `search_document: ` and a query MUST carry `search_query: `. Omit it and
     results are quietly worse rather than obviously broken.
  2. The stored vectors are UNNORMALIZED (norms ~18.9-20.7). Cosine similarity
     needs unit vectors, so both sides are normalized here. Skipping this does
     not error -- it returns a ranking dominated by vector magnitude.

    library_search.py "query"                 # top 10
    library_search.py "query" -n 20 --full
    library_search.py "query" --book "Seeing Like a State"
    library_search.py --near 41822            # passages near a given chunk
    library_search.py --near 41822 --per-book 1
"""
import argparse
import sqlite3
import struct
import sys
from pathlib import Path

import numpy as np

DEFAULT_DB = str(Path.home() / "Library/Markdown/library.db")
COLLECTION = "book_chunks"
MODEL = "nomic-ai/nomic-embed-text-v1.5"
QUERY_PREFIX = "search_query: "

_model = None
_cache = {}


def _load_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL, trust_remote_code=True, device="cpu")
    return _model


def _load_matrix(db: str):
    """(ids, unit-norm matrix, {id: (title, chunk_index, text)}) for the collection.

    Cached per-process: the MCP server stays resident, so this is paid once and
    every later call is a matmul.
    """
    if db in _cache:
        return _cache[db]
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    coll = conn.execute("SELECT id FROM collections WHERE name=?", (COLLECTION,)).fetchone()
    if not coll:
        sys.exit(f"no '{COLLECTION}' collection in {db}")
    rows = conn.execute(
        "SELECT e.id, e.embedding FROM embeddings e WHERE e.collection_id=?", (coll[0],)
    ).fetchall()
    if not rows:
        sys.exit(f"no embeddings in {db}")
    ids = [int(r[0]) for r in rows]
    dim = len(rows[0][1]) // 4
    V = np.empty((len(rows), dim), dtype=np.float32)
    for i, (_, blob) in enumerate(rows):
        V[i] = struct.unpack(f"<{dim}f", blob)
    # Normalize ONCE, here. See the note above -- the store is unnormalized.
    V /= np.linalg.norm(V, axis=1, keepdims=True).clip(min=1e-12)
    meta = {
        cid: (title, idx, text)
        for cid, title, idx, text in conn.execute(
            "SELECT id, title, chunk_index, text FROM book_chunks"
        )
    }
    _cache[db] = (np.array(ids), V, meta)
    return _cache[db]


def _rank(sims, ids, meta, n, book=None, exclude=None, per_book=None):
    """Top-n by score, with optional title filter and per-book cap.

    per_book exists because of memory feedback-nuisance-grouping-dominates: this
    space clusters by BOOK before topic (median nearest-book cosine 0.941), so an
    uncapped neighbour list for a passage is mostly the same book's next pages --
    true, and useless as an answer.
    """
    order = np.argsort(-sims)
    out, seen = [], {}
    for j in order:
        cid = int(ids[j])
        if exclude is not None and cid == exclude:
            continue
        m = meta.get(cid)
        if not m:
            continue
        title, idx, text = m
        if book and book.lower() not in (title or "").lower():
            continue
        if per_book is not None:
            if seen.get(title, 0) >= per_book:
                continue
            seen[title] = seen.get(title, 0) + 1
        out.append((float(sims[j]), title, idx, text, cid))
        if len(out) >= n:
            break
    return out


def _clean(text: str) -> str:
    """Strip converter markup for DISPLAY. Never touches the stored text.

    The vectors were repaired on 2026-09-06, but only above 0.10 markup density
    -- below that the embedding does not measurably move (cos 0.9908 at the
    corpus median) and re-embedding 43k more chunks would have cost 20+ hours for
    nothing. The *snippet* is a different matter: `{.calibre17}` and
    `::: calibre38` are equally unreadable at density 0.04, and they are what a
    person actually reads. So results are cleaned on the way out, which costs a
    regex per hit and needs no re-embedding at all.

    Uses chunk_and_embed's pattern so display and ingest cannot drift apart.
    """
    try:
        import chunk_and_embed as _ce
        import re as _re
        t = _ce.strip_markup(text)
        # Image references are legitimate markdown, not converter cruft, so they
        # must NOT be stripped at ingest -- that would change the embeddings.
        # In a snippet they are pure noise, and a hit that opens with
        # "![Image](images/images/00008.jpg)" wastes the line you actually read.
        # Display-only, deliberately not in strip_markup().
        t = _re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
        return " ".join(t.split())
    except Exception:
        return " ".join(text.split())


def search(query: str, n: int = 10, book: str | None = None, db: str = DEFAULT_DB):
    """[(score, title, chunk_index, text)] — the signature mcp_library_search expects."""
    ids, V, meta = _load_matrix(db)
    q = _load_model().encode([QUERY_PREFIX + query], show_progress_bar=False)[0]
    q = np.asarray(q, dtype=np.float32)
    q /= max(float(np.linalg.norm(q)), 1e-12)
    # 4-tuples: mcp_library_search.py unpacks (score, title, idx, text) and that
    # signature is the contract. The id rides along internally for the CLI.
    return [(sc, ti, ix, _clean(tx)) for sc, ti, ix, tx in
            (h[:4] for h in _rank(V @ q, ids, meta, n, book=book))]


def near(chunk_id: int, n: int = 10, per_book: int | None = 2, db: str = DEFAULT_DB):
    """Passages nearest an EXISTING chunk — 'more like this', not a text query.

    A different question from search(): no model call and no query prefix, since
    the anchor is already a document vector in the same space. Defaults to at
    most 2 hits per book so the answer is not just the anchor's own neighbours.
    """
    ids, V, meta = _load_matrix(db)
    where = np.nonzero(ids == chunk_id)[0]
    if not len(where):
        sys.exit(f"chunk {chunk_id} has no embedding in {db}")
    return _rank(V @ V[where[0]], ids, meta, n, exclude=chunk_id, per_book=per_book)  # 5-tuples


def main():
    p = argparse.ArgumentParser(description="Semantic search over the markdown library")
    p.add_argument("query", nargs="?", help="natural-language query")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--full", action="store_true", help="print the whole chunk, not a snippet")
    p.add_argument("--book", default=None, help="restrict to titles containing this substring")
    p.add_argument("--near", type=int, default=None, metavar="CHUNK_ID",
                   help="passages near this chunk id instead of a text query")
    p.add_argument("--per-book", type=int, default=None,
                   help="cap hits per book (default 2 for --near, uncapped otherwise)")
    a = p.parse_args()

    if a.near is not None:
        cap = a.per_book if a.per_book is not None else 2
        hits = near(a.near, n=a.n, per_book=cap, db=a.db)
        anchor = _load_matrix(a.db)[2].get(a.near)
        if anchor:
            print(f"near: {anchor[0]} (chunk {anchor[1]})")
            print(f"      {_clean(anchor[2])[:200]}\n")
    elif a.query:
        ids_, V_, meta_ = _load_matrix(a.db)
        q = _load_model().encode([QUERY_PREFIX + a.query], show_progress_bar=False)[0]
        q = np.asarray(q, dtype=np.float32); q /= max(float(np.linalg.norm(q)), 1e-12)
        hits = _rank(V_ @ q, ids_, meta_, a.n, book=a.book, per_book=a.per_book)
    else:
        p.error("give a query, or --near CHUNK_ID")

    if not hits:
        print("No results.")
        return
    for h in hits:
        score, title, idx, text = h[:4]
        cid = h[4] if len(h) > 4 else None
        # The id is what --near takes, so it has to be visible. Printing only
        # chunk_index made the round-trip impossible: the first --near attempt
        # here pasted a displayed "chunk 512" and got "no embedding".
        tag = f"id {cid}, chunk {idx}" if cid is not None else f"chunk {idx}"
        body = _clean(text) if a.full else _clean(text)[:300]
        print(f"[{score:.3f}] {title} ({tag})\n    {body}\n")


if __name__ == "__main__":
    main()
