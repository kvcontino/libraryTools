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
"""Chunk and embed any book that has no chunks yet. INCREMENTAL and additive.

WHY THIS EXISTS
---------------
`run.sh` was ingest -> index -> report: it converted new books and made them
full-text searchable, then stopped. The chunk+embed half lived in
`~/2_projects/sessions/2026-05-03_semantic-search/` and never graduated, so
nothing had chunked or embedded anything since the original bulk pass. By
2026-08-14, 95 of 281 books had zero chunks and were invisible to
/librarysearch while looking perfectly healthy in every other view.

WHAT MAKES THIS DIFFERENT FROM build_chunks.py
----------------------------------------------
That script opens with `DROP TABLE IF EXISTS book_chunks` and rebuilds from
scratch. Running it now would renumber every chunk id -- and `embeddings.id` is
the chunk id as text, so every one of the 64,515 existing embeddings would be
silently orphaned, pointing at rows that no longer mean what they meant. Six
hours of compute destroyed by a table rebuild that looks idempotent.

This script only ever INSERTs. It never drops, never renumbers, and never
touches a book that already has chunks. Safe to re-run; safe to interrupt.

The chunking algorithm below is COPIED from build_chunks.py deliberately rather
than imported: the existing 64,515 chunks were produced by it, and a chunker
that drifts from the one that built the corpus creates two incompatible
populations in one table. If you change it, you are re-chunking everything.

RELATIONSHIP TO rebuild_embeddings_if_complete.sh (kevadk, 2026-06-01)
----------------------------------------------------------------------
That script is the SERVER's answer to the same problem and the two are not
duplicates -- keep both, and know which machine each belongs to.

  rebuild_...sh   full REBUILD. Polls via library-embed.timer, waits until
                  every source book is converted, then runs bootstrap.sh:
                  build_chunks.py (DROP TABLE) + `llm embed-multi` over every
                  chunk. Correct on a server that can spend a night on it.
  this script     INCREMENTAL. Embeds only what lacks an embedding, resumes
                  exactly, uses sentence-transformers directly.

**DO NOT install library-embed.timer on the laptop.** It would call bootstrap.sh
-> build_chunks.py -> DROP TABLE, and then `llm embed-multi` would fail because
the `llm` CLI is not installed here -- leaving the chunks table rebuilt with new
ids and every existing embedding orphaned, with the timer reporting failure and
retrying the same destruction next tick. Verified 2026-08-14: that timer is NOT
installed here, and this note exists so it stays that way.

The laptop clone sat on a commit BEFORE 2026-06-01 until 2026-08-14, which is
the real reason run.sh here had no chunk stage: the work had graduated on
kevadk two days before the server was boxed, and was never pulled.

USAGE
  chunk_and_embed.py                 # do the work
  chunk_and_embed.py --dry-run       # report what would happen, touch nothing
  chunk_and_embed.py --limit 5       # first N unchunked books (for a smoke test)
"""
import argparse, hashlib, os, re, sqlite3, sys, time
from pathlib import Path

DB_PATH = Path.home() / "Library/Markdown/library.db"
MODEL = "nomic-ai/nomic-embed-text-v1.5"
# `trust_remote_code=True` means the model pulls its MODELING CODE from the Hub
# too, so a load touches the network twice — once for weights, once for
# nomic-bert-2048. Both are cached after the first run and neither changes.
CODE_REPO = "nomic-ai/nomic-bert-2048"
PREFIX = "search_document: "          # nomic requires a task prefix on documents
COLLECTION = "book_chunks"
TARGET_CHARS = 2000
MIN_CHARS = 50
BATCH = 16



# ------------------------------------------------------------ HF offline
def prefer_cached_hub():
    """Load the embedding model from disk, with no Hub call, when we can.

    The 2026-08-29 run took 15m10s to embed 16 chunks. 5m24s of that was the
    sentence-transformers import and model load — not because either is slow
    (16s measured, warm and offline) but because the timer is `Persistent=true`
    and fired during the 07:51 boot catch-up, BEFORE the network was up. Every
    Hub call hung and retried until it was.

    Ordering the unit after the network would be the wrong fix, and the same
    wrong fix `wait-miniflux.sh` exists to avoid: it makes the job wait for a
    dependency it does not actually need. The model is 523M on local disk and
    is not going to change. So the fix is to stop calling out at all.

    `HF_HUB_OFFLINE` is read into a module constant when huggingface_hub is
    imported, so it must be set BEFORE the import — which is why this decides
    from the filesystem rather than by catching a failure and retrying. If
    either repo is missing from the cache (a fresh machine, a cleared cache),
    we stay online and let the download happen normally.
    """
    if os.environ.get("HF_HUB_OFFLINE"):
        return "already set in the environment"
    root = Path(
        os.environ.get("HF_HUB_CACHE")
        or os.environ.get("HF_HOME", Path.home() / ".cache/huggingface") / "hub"
    ).expanduser()
    missing = [
        repo for repo in (MODEL, CODE_REPO)
        # A cached repo is a snapshot dir holding at least one real file; the
        # bare directory can survive an interrupted download.
        if not any((root / ("models--" + repo.replace("/", "--")) / "snapshots").glob("*/*"))
    ]
    if missing:
        return "staying online — not in the cache: " + ", ".join(missing)
    os.environ["HF_HUB_OFFLINE"] = "1"
    return f"offline — both repos cached under {root}"


# ---------------------------------------------------------------- chunking
# Copied verbatim in behaviour from build_chunks.py. See module docstring.
def split_long(text, target=TARGET_CHARS):
    if len(text) <= target:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) <= 1:
        return [text[i:i + target] for i in range(0, len(text), target)]
    expanded = []
    for s in sentences:
        if len(s) > target:
            expanded.extend(s[i:i + target] for i in range(0, len(s), target))
        else:
            expanded.append(s)
    sentences = expanded
    out, buf, buf_len = [], [], 0
    for s in sentences:
        if buf and buf_len + len(s) > target:
            out.append(" ".join(buf))
            buf, buf_len = [], 0
        buf.append(s)
        buf_len += len(s) + 1
    if buf:
        out.append(" ".join(buf))
    return out


def chunk_paragraphs(body, target=TARGET_CHARS):
    paras = [p.strip() for p in re.split(r"\n\s*\n", body or "") if p.strip()]
    out, buf, buf_len = [], [], 0
    for p in paras:
        if len(p) > target:
            if buf:
                out.append("\n\n".join(buf)); buf, buf_len = [], 0
            out.extend(split_long(p, target))
            continue
        if buf and buf_len + len(p) > target:
            out.append("\n\n".join(buf)); buf, buf_len = [], 0
        buf.append(p); buf_len += len(p) + 2
    if buf:
        out.append("\n\n".join(buf))
    return [c for c in out if len(c) >= MIN_CHARS]


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    ap.add_argument("--limit", type=int, help="only the first N unchunked books")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    conn.execute("PRAGMA journal_mode=WAL")

    # The collection id is stored, not assumed -- a second collection would
    # silently split the corpus in half at query time.
    row = conn.execute("SELECT id FROM collections WHERE name=?", (COLLECTION,)).fetchone()
    if row is None:
        sys.exit(f"no '{COLLECTION}' collection in {a.db} — refusing to invent one")
    coll = row[0]

    todo = conn.execute("""
        SELECT b.id, b.title
        FROM books b
        WHERE NOT EXISTS (SELECT 1 FROM book_chunks c WHERE c.book_rowid = b.id)
        ORDER BY b.id
    """).fetchall()
    if a.limit:
        todo = todo[:a.limit]

    have = conn.execute("SELECT COUNT(*) FROM book_chunks").fetchone()[0]
    emb = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    print(f"db          {a.db}")
    print(f"collection  {COLLECTION} (id {coll})")
    print(f"existing    {have:,} chunks / {emb:,} embeddings — NOT touched")
    print(f"unchunked   {len(todo)} books")
    # NOTE (2026-08-22): these two guards used to `return`, which silently
    # defeated phase 2's self-healing promise below. A run killed between
    # phase 1 and phase 2 leaves chunks with no embeddings; on the next run
    # `todo` is empty (those books DO have chunks now), so the old early
    # return skipped straight past the code that would have embedded them.
    # Found live: 10 chunks stranded that way. Fall through instead.
    if not todo:
        print("            no book needs chunking — going straight to phase 2")

    # books_fts is the chunk source; a book with no body yields nothing.
    plan, missing = [], []
    for bid, title in todo:
        r = conn.execute("SELECT body FROM books_fts WHERE rowid=?", (bid,)).fetchone()
        body = r[0] if r else None
        if not body:
            missing.append((bid, title)); continue
        plan.append((bid, title, chunk_paragraphs(body)))

    n_chunks = sum(len(c) for _, _, c in plan)
    print(f"to chunk    {len(plan)} books -> {n_chunks:,} chunks")
    if missing:
        print(f"no body     {len(missing)} books have no books_fts row or an empty one:")
        for bid, t in missing[:10]:
            print(f"              {bid} {t[:66]}")
    if a.dry_run:
        print(f"\nestimated   {n_chunks/1.5/60:.0f} min at the measured 1.5 chunks/sec")
        print("dry run — nothing written"); return
    if not plan:
        print("            nothing chunkable — going straight to phase 2")

    # ---- PHASE 1: chunk everything (fast, no model needed) -----------------
    # Chunking and embedding are separate phases on purpose. The first attempt
    # interleaved them and committed per book; a single large book then ran
    # past a 900s timeout and was killed mid-book, so nothing committed at all
    # and the whole run was wasted. Splitting them means the cheap half always
    # lands, and the expensive half resumes at BATCH granularity.
    for bid, title, chunks in plan:
        conn.executemany(
            "INSERT INTO book_chunks (book_rowid, title, chunk_index, text)"
            " VALUES (?,?,?,?)",
            [(bid, title, i, text) for i, text in enumerate(chunks)])
    conn.commit()
    print(f"phase 1     {n_chunks:,} chunks written")

    # ---- PHASE 2: embed whatever lacks an embedding ------------------------
    # Driven off the DB, not off `plan`. That makes resume exact and makes the
    # script self-healing: any chunk missing an embedding gets one, whether it
    # was written a minute ago or by a run that died last week.
    pending = conn.execute("""
        SELECT c.id, c.text FROM book_chunks c
        WHERE NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.id = CAST(c.id AS TEXT))
        ORDER BY c.id
    """).fetchall()
    print(f"phase 2     {len(pending):,} chunks need an embedding")
    if not pending:
        print("nothing to embed"); return

    print(f"hub         {prefer_cached_hub()}", flush=True)
    t0 = time.time()
    from sentence_transformers import SentenceTransformer
    import numpy as np
    print(f"import      {time.time()-t0:.1f}s", flush=True)

    t0 = time.time()
    model = SentenceTransformer(MODEL, trust_remote_code=True)
    print(f"model       loaded in {time.time()-t0:.1f}s", flush=True)

    done_e = 0
    start = time.time()
    for s in range(0, len(pending), BATCH):
        batch = pending[s:s + BATCH]
        contents = [PREFIX + t for _, t in batch]
        vecs = model.encode(contents, batch_size=BATCH, show_progress_bar=False)
        now = int(time.time())
        conn.executemany(
            "INSERT OR REPLACE INTO embeddings"
            " (collection_id, id, embedding, content, content_hash, updated)"
            " VALUES (?,?,?,?,?,?)",
            [(coll, str(cid),
              # raw little-endian float32, 768 dims — the format
              # library_search.load_matrix() decodes with np.frombuffer('<f4')
              np.asarray(v, dtype="<f4").tobytes(),
              c, hashlib.md5(c.encode("utf8")).digest(), now)
             for (cid, _), v, c in zip(batch, vecs, contents)])
        conn.commit()          # per batch — an interrupt costs <=16 chunks
        done_e += len(batch)

        if (s // BATCH) % 20 == 0 or done_e == len(pending):
            el = time.time() - start
            rate = done_e / el if el else 0
            left = (len(pending) - done_e) / rate / 60 if rate else 0
            print(f"  {done_e:>6,}/{len(pending):,}  {rate:.1f}/s  "
                  f"~{left:.0f} min left", flush=True)

    print(f"\ndone: +{n_chunks:,} chunks, +{done_e:,} embeddings in {(time.time()-start)/60:.1f} min")
    tot_c, tot_e = conn.execute(
        "SELECT (SELECT COUNT(*) FROM book_chunks), (SELECT COUNT(*) FROM embeddings)").fetchone()
    print(f"totals: {tot_c:,} chunks / {tot_e:,} embeddings")
    orphan = conn.execute(
        "SELECT COUNT(*) FROM embeddings e WHERE NOT EXISTS"
        " (SELECT 1 FROM book_chunks c WHERE CAST(c.id AS TEXT)=e.id)").fetchone()[0]
    print(f"orphaned embeddings: {orphan}  (must be 0)")
    conn.close()


if __name__ == "__main__":
    main()
