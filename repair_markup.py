#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["sentence-transformers>=3", "einops", "numpy", "torch"]
# [[tool.uv.index]]
# name = "pytorch-cpu"
# url = "https://download.pytorch.org/whl/cpu"
# explicit = true
# [tool.uv.sources]
# torch = { index = "pytorch-cpu" }
# ///
"""Re-embed chunks whose stored text carries converter markup.

`chunk_and_embed.py` now strips markup at ingest, so every NEW book is clean.
This repairs the ones already in the store.

WHY IT IS THRESHOLDED, AND NOT A FULL RE-EMBED
----------------------------------------------
55,546 of 98,170 chunks (56.6%) contain markup, and re-embedding all of them
costs 27.1 hours at this machine's measured 0.57 chunks/sec. Almost all of that
would buy nothing. Measured cos(dirty, clean) on a 120-chunk sample stratified
across the density range -- both tails and the middle, because one draw reports
a mode rather than a range:

    markup density 0.002  ->  cos 0.9997     (indistinguishable)
    markup density 0.048  ->  cos 0.9908     (median chunk; negligible)
    markup density 0.900  ->  cos 0.6855     (a different vector entirely)

The corpus median density is 0.048. So the typical affected chunk barely moves,
and the whole value sits in the tail: p90 = 0.178, p99 = 0.556. For scale, the
median nearest-BOOK cosine in this anisotropic store is 0.941 -- a pair below
that is further apart than two different books.

Default cut is 0.10, just above where the vector stops moving meaningfully:
13,132 chunks, ~6.4 hours, 13.4% of the corpus. Raise it to go faster.

RESUMABLE BY CONSTRUCTION. A repaired chunk's text no longer matches the markup
pattern, so it drops out of the work set on the next run. Kill it any time; it
commits per batch and picks up where it stopped. No separate progress file to
go stale.

    repair_markup.py --dry-run          # what would change, and the cost
    repair_markup.py --threshold 0.15   # only the worse tail (~3.6h)
    repair_markup.py --apply            # do it
"""
import argparse, datetime, hashlib, json, sqlite3, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import chunk_and_embed as ce

DB = Path.home() / "Library/Markdown/library.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.10,
                    help="minimum markup density to bother re-embedding (default 0.10)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--db", type=Path, default=DB)
    a = ap.parse_args()
    if not (a.apply or a.dry_run):
        sys.exit("refusing to guess: pass --dry-run or --apply")

    conn = sqlite3.connect(a.db)
    conn.execute("PRAGMA journal_mode=WAL")
    row = conn.execute("SELECT id FROM collections WHERE name=?", (ce.COLLECTION,)).fetchone()
    if row is None:
        sys.exit(f"no '{ce.COLLECTION}' collection in {a.db} — refusing to invent one")
    coll = row[0]

    work, skipped_empty, skipped_nonprose = [], [], []
    for cid, text in conn.execute("SELECT id, text FROM book_chunks"):
        clean = ce.strip_markup(text)
        if clean == text:
            continue
        d = (len(text) - len(clean)) / max(len(text), 1)
        if d <= a.threshold:
            continue
        # A chunk stripped down to nothing is not a repair, it is a deletion, and
        # deletion is not this tool's job. Leave it and report it.
        if len(clean.strip()) < ce.MIN_CHARS:
            skipped_empty.append(cid); continue
        # Some high-density chunks are not damaged PROSE, they are wreckage --
        # Word style boilerplate (`<w:LsdException Name="Medium Shading 1"/>`),
        # vertical table cells, colophons. Stripping gives them a cleaner vector
        # for something that should not be in the store at all, so re-embedding
        # them buys nothing and costs the same as a real chunk. The share rises
        # with density: 9% of the 0.10-0.30 band, 45% above 0.70.
        if not ce.is_prose(clean):
            skipped_nonprose.append((cid, d)); continue
        work.append((cid, clean, d))
    work.sort(key=lambda r: -r[2])
    if a.limit:
        work = work[:a.limit]

    total = conn.execute("SELECT COUNT(*) FROM book_chunks").fetchone()[0]
    print(f"db            {a.db}")
    print(f"corpus        {total:,} chunks")
    print(f"threshold     markup density > {a.threshold}")
    print(f"to repair     {len(work):,} chunks ({100*len(work)/total:.1f}% of corpus)")
    print(f"skipped       {len(skipped_nonprose):,} not prose after repair (wreckage, not damage)"
          f" + {len(skipped_empty):,} that strip to nothing")
    if work:
        print(f"density       max {work[0][2]:.3f}  min {work[-1][2]:.3f}")
        print(f"estimate      {len(work)/0.57/3600:.1f} h at the measured 0.57 chunks/sec")
    def record(done):
        state = Path.home() / ".local/state/library-tools"
        state.mkdir(parents=True, exist_ok=True)
        (state / "markup-repair.json").write_text(json.dumps({
            "completed": datetime.datetime.now().replace(microsecond=0).isoformat(),
            "threshold": a.threshold, "repaired": done,
            "skipped_nonprose": len(skipped_nonprose),
            "skipped_empty": len(skipped_empty), "corpus": total,
        }, indent=2) + "\n")
        print(f"recorded  {state/'markup-repair.json'}")

    if a.dry_run:
        print("\ndry run — nothing written"); return
    if not work:
        print("\nnothing to do at this threshold — recording completion")
        record(0); return

    print(ce.prefer_cached_hub())
    t0 = time.time()
    model = ce.SentenceTransformer(ce.MODEL, trust_remote_code=True) \
        if hasattr(ce, "SentenceTransformer") else __import__(
            "sentence_transformers").SentenceTransformer(ce.MODEL, trust_remote_code=True)
    print(f"model load    {time.time()-t0:.1f}s\n")

    done = 0
    t0 = time.time()
    for s in range(0, len(work), ce.BATCH):
        batch = work[s:s + ce.BATCH]
        contents = [ce.PREFIX + c for _, c, _ in batch]
        vecs = model.encode(contents, batch_size=ce.BATCH, show_progress_bar=False)
        now = int(time.time())
        for (cid, clean, _), v, content in zip(batch, vecs, contents):
            conn.execute("UPDATE book_chunks SET text=? WHERE id=?", (clean, cid))
            conn.execute(
                "UPDATE embeddings SET embedding=?, content=?, content_hash=?, updated=? "
                "WHERE collection_id=? AND id=?",
                (v.astype("float32").tobytes(), content,
                 hashlib.md5(content.encode("utf8")).digest(), now, coll, str(cid)))
        conn.commit()            # per batch — an interrupt costs <= BATCH chunks
        done += len(batch)
        if done % (ce.BATCH * 10) == 0 or done == len(work):
            el = time.time() - t0
            print(f"  {done:,}/{len(work):,}  {done/el:.2f} chunks/sec  "
                  f"{(len(work)-done)/max(done/el,1e-9)/3600:.1f} h left", flush=True)

    print(f"\nrepaired {done:,} chunks in {(time.time()-t0)/3600:.2f} h")
    print("re-run to confirm: the work set should now be empty at this threshold.")

    # Record completion so a backlog check can ask "did this finish?" in
    # milliseconds. Recomputing means scanning all 98k chunks and stripping each,
    # far beyond triage-verify's 20 s budget -- and a check that times out reports
    # the item LIVE forever, the exact failure `check backlog` gates against.
    record(done)


if __name__ == "__main__":
    main()
