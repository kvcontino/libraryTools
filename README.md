# libraryTools

A small ETL pipeline that turns a folder of PDFs/EPUBs/MOBIs into a searchable Markdown library backed by SQLite + FTS5.

```
~/6_reading/        →   ingest.py   →   ~/Library/Markdown/<book>/<book>.md
                        index.py    →   ~/Library/Markdown/library.db
```

---

## Components

| File | Purpose |
|---|---|
| `ingest.py`   | Convert source files to Markdown via Marker (PDF) or Pandoc (EPUB/MOBI). Writes YAML frontmatter. |
| `index.py`    | Read frontmatter from `*.md`, upsert rows into `books`, rebuild the FTS index. |
| `metadata.py` | Pure utility module — author sanity filter, title cleanup, ISBN/DOI extraction, Open Library/Crossref lookup, page counting, hashing. Stdlib only. |
| `repair.py`   | Re-run metadata heuristics over already-ingested books *without* re-converting. |
| `report.py`   | Write a dated health report (backlog, quality flags, last run, slowest conversions). |
| `cleanup_orphans.py` | Detect and remove debris from interrupted conversions. Dry-run by default. |
| `run.sh`      | Sequential `ingest → index → report`. Always uses the venv Python. |
| `launch.sh`   | Wraps `run.sh` in a fresh `systemd-run` scope with the standard memory cap. Use this for daily manual runs — it sidesteps the multi-line copy-paste hazard. |
| `library-ingest.service`, `library-ingest.timer` | systemd user units; the timer fires weekly and runs the full pipeline. |
| `watch.py`, `library.service` | (legacy) optional filesystem watcher. Superseded by the timer for most uses. |

---

## Daily use

```bash
# One-shot
./run.sh

# Or, for a fresh ingest with options:
.venv/bin/python ingest.py [--dry-run] [--enrich] [--smallest-first]
.venv/bin/python index.py
.venv/bin/python report.py
```

Always invoke the venv Python explicitly (`.venv/bin/python ...`) rather than
the system `python3`. The venv is where `marker_pdf`, `mobi`, and the rest of
the dependencies actually live. See [docs/python-environments.md](docs/python-environments.md)
for the mental model.

## Automation status

**Currently: manual runs only.** The systemd timer below is disabled while
this lives on the laptop. Marker can spike to 9–10 GB on OCR-heavy PDFs,
which is too close to the 16 GB ceiling — `systemd-oomd` kills the run
under PSI pressure even when the cgroup is under its absolute cap, and
gnome-shell crashes in the same window. The plan is to enable it on the
32 GB server, where uptime is reliable and headroom is comfortable.

To run the pipeline manually:

```bash
~/Library/tools/run.sh
# or:
systemctl --user start library-ingest.service     # service still installed; just not timed
```

### Re-enabling the timer (do this on the server, not the laptop)

The unit files at `library-ingest.service` and `library-ingest.timer` are
ready to use. Before re-enabling on any host, verify three things:

1. **Memory headroom** — leave at least 8 GB above whatever ceiling you set.
   Marker's working set is 3–10 GB depending on the PDF.
2. **Persistent=true behavior** — a missed scheduled run fires immediately
   when the user-systemd starts (i.e. on login). Acceptable on a server
   that's always up; *not* acceptable on a laptop that may be carried home
   and reopened during work hours.
3. **Lid / suspend behavior** — `OnCalendar=Mon 03:00` only fires if the
   machine is awake at 03:00. On a laptop, default `HandleLidSwitch=suspend`
   defeats this. On a server, irrelevant.

```bash
# Install once (idempotent)
mkdir -p ~/.config/systemd/user/
cp library-ingest.service library-ingest.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now library-ingest.timer

# Inspect
systemctl --user list-timers library-ingest
systemctl --user status library-ingest.service
journalctl --user -u library-ingest.service -n 200

# Disable and clear state
systemctl --user disable --now library-ingest.timer
systemctl --user reset-failed library-ingest.service
```

Each run writes a dated report to `~/Library/Markdown/reports/YYYY-MM-DD.md`
covering backlog, quality flags (junk authors, zero-page rows, missing
hashes), the last run summary, and the slowest conversions.

### Flags

- `--dry-run` — list files that would be processed; touch nothing.
- `--smallest-first` — process by ascending file size. Recommended for the first big run on this laptop: you get fast wins, surface failures early, and the giant scans run last when you can leave the machine alone.
- `--enrich` — call Open Library (by ISBN) and Crossref (by DOI) to fix titles and authors. Off by default (network calls). Cached in `~/Library/Markdown/.metadata-cache.json` so re-runs are free. Equivalent: `LIBRARY_ENRICH=1`.

---

## Repairing existing rows

The improvements added in 2026-04 (author sanity filter, title cleanup, page backfill, content hashing, optional external enrichment) all run inside `metadata.py`. `repair.py` walks every row already in the DB, applies those heuristics over the existing `.md` files, and rewrites the frontmatter. It does **not** re-convert source files.

```bash
# Always preview first — dry-run is the default
python3 repair.py

# Apply changes (rewrites frontmatter, then re-runs index.py)
python3 repair.py --apply

# Apply + reach out to Open Library / Crossref for the messy rows
python3 repair.py --apply --enrich

# Scope to one concern at a time
python3 repair.py --apply --only-author
python3 repair.py --apply --only-title
python3 repair.py --apply --only-pages
python3 repair.py --apply --only-hash
```

For the current state of the library: a dry run reports ~360 rows would change, of which ~180 are user-visible cleanups (titles, authors, page backfills) and the rest are silent SHA-256 backfills used for duplicate detection.

---

## Cleaning up partial-conversion debris

When ingest is interrupted (Ctrl+C, OOM, reboot), Marker's per-book working directory and `images/` subdir often survive without a final `.md`. These accumulate over weeks of interrupted runs.

`cleanup_orphans.py` walks `~/Library/Markdown/` and classifies each subdirectory missing its expected `.md`:

- **Healable** — source file still exists in `~/6_reading/`. The next ingest run will reattempt and overwrite. Leave alone.
- **Zombie** — source is gone. Nothing will retry. Safe to delete.
- **Manual review** — directory contains unexpected contents (stray .md, .json, etc.). Never auto-deleted.

```fish
# Always preview first — dry-run is the default
python3 ~/Library/tools/cleanup_orphans.py

# Apply — removes zombies only
python3 ~/Library/tools/cleanup_orphans.py --apply
```

Worth running periodically (every few weeks of active ingestion) and after any major run. **Don't run with `--apply` while ingest is active** — the active book's directory could be classified as an orphan mid-conversion.

**Recommended order on first repair:**

```bash
python3 repair.py --apply --only-hash       # cheap, no judgment calls
python3 repair.py --apply --only-author     # strips PDF-junk authors
python3 repair.py --apply --only-pages      # backfills 0-page rows
python3 repair.py --apply --only-title      # uses H1 fallback for junk filenames
python3 repair.py --apply --enrich          # last — slowest, hits the network
```

Run them in that order so you can stop and inspect the DB between passes.

---

## Ingestion: getting through the rest of the library

The library currently has ~365 books indexed against ~791 source files in `~/6_reading`. The remaining ~425 (mostly PDFs, some EPUBs/MOBIs) will take 1–3 days of wall-clock to process serially on this laptop. Here's how to do it without the machine falling over.

### Memory discipline

Marker loads OCR + layout models that can spike past 6–10 GB per worker. With 16 GB total and swap already in heavy use during normal work, you cannot run two converters at once.

**Cap each conversion in a memory cgroup** so one ugly file can't take the laptop down:

```fish
systemd-run --user --scope --unit=library-(date +%H%M) \
    -p MemoryMax=8G -p MemorySwapMax=2G \
    ~/Library/tools/run.sh
```

**Critical: never reuse a `--unit=` name.** If a previous scope is still loaded under the same name (even after `reset-failed`), the new invocation *silently falls through* and your processes inherit the parent terminal's cgroup with **no cap**. Confirmed bitten on 2026-04-28 — Marker hit 9.5 GB RSS unbounded; Firefox got OOM-killed by systemd-oomd as collateral. Use `(date +%H%M)` or omit `--unit=` entirely.

**Verify the cap is real** before walking away. ~30–60 s after launch, in another terminal:

```fish
set PID (pgrep -f marker_single | head -1)
cat /sys/fs/cgroup(cat /proc/$PID/cgroup | cut -d: -f3)/memory.max
```

Should print a number like `8589934592` (= 8 GiB), **not** the literal string `max`. If `max`, the cap didn't apply — kill (`kill -INT (pgrep -f ingest.py)`) and relaunch with a different unit name.

A single OOM-killed file is recoverable (the script logs the failure and moves on). An OOM-killed session that takes the desktop with it isn't.

### When systemd-oomd kills you anyway

Linux has two independent OOM defenses, and `MemoryMax` only controls one:

- **Kernel OOM killer** — fires when a process exceeds its cgroup's `memory.max`. Your cap protects against this.
- **systemd-oomd** (userspace) — watches *system-wide* PSI (`/proc/pressure/memory`). When sustained pressure trips the threshold, it picks the user-slice cgroup with highest memory+swap and SIGKILLs it, **regardless of per-cgroup caps**. This is the source of the GNOME notification "Application Stopped — Device memory is nearly full."

Even with a perfect cap, the *whole system* being in pressure can trigger oomd. Marker swapping its model weights raises PSI without exceeding the cap. Mitigations:

1. **Reduce baseline before launch.** Close Firefox, IDE, Claude desktop. `free -h` should show ≥10 GB available.
2. **Lower swappiness for the run.** `sudo sysctl -w vm.swappiness=10` (default 60). Resets at reboot.
3. **Watch pressure live.** `cat /proc/pressure/memory` — `avg10` >30 means danger zone.
4. **Optionally exempt your scope from oomd.** Add `-p ManagedOOMMemoryPressureLimit=80%` — raises *this scope's* tolerance so oomd kills something else first. Reasonable on a dedicated ingest run; not while you're working.

### Reduce baseline pressure before running

```bash
free -h                                # check before starting
sudo sysctl -w vm.swappiness=10        # discourage swap thrashing during the run
```

Close Kitty tabs, browsers, and IDEs. Ingestion needs RAM more than your editor does.

### Resume-friendly behavior

`ingest.py` already skips files whose `.md` already exists in `~/Library/Markdown/`. So if the run dies — Ctrl-C, OOM, reboot — just rerun. It picks up where it left off.

If you suspect a particular file is the OOM culprit, move it out of `~/6_reading/` and process it later on the 32 GB server.

### Don't ingest over the network mount

Wait until ingestion is fully done before backing up to the server. Running ingest with sources on a remote mount multiplies I/O cost and risks corrupted partial reads on disconnect.

### A reasonable invocation

```fish
# Pre-flight: confirm clean state and free RAM
systemctl --user list-units --all "library-*"   # should show 0 loaded units
free -h                                          # want ≥10 GB available

# Launch
~/Library/tools/launch.sh
```

`launch.sh` is a thin wrapper around `run.sh` that handles the `systemd-run` scope creation, picks a fresh unit name from the current time, and applies the standard cap (`MemoryMax=8G`, `MemorySwapMax=2G`, `CPUWeight=50`, `IOWeight=50`, plus `nice`/`ionice`). Read the script — it's short.

**Why a wrapper script and not the systemd-run command directly?** Multi-line `systemd-run` invocations are fragile when copy-pasted into a terminal. Trailing whitespace after a backslash silently breaks the line continuation, and fish/bash then run each fragment as a separate command — the systemd-run portion errors out with no executable, and the final `~/Library/tools/run.sh` runs *outside* the scope, in your terminal's cgroup, **with no cap**. This has bitten us twice. A script file isn't subject to copy-paste corruption.

**If you want to launch by hand** (one-off testing, custom flags), keep it on a single line:

```fish
systemd-run --user --scope --unit=library-(date +%H%M) -p MemoryMax=8G -p MemorySwapMax=2G -p CPUWeight=50 -p IOWeight=50 nice -n 19 ionice -c2 -n7 ~/Library/tools/run.sh
```

Run it overnight; check the log in the morning.

### Always use run.sh (not bare ingest.py)

`ingest.py` only converts. `index.py` registers conversions in the DB and runs near-duplicate detection. `report.py` writes the dated health report. Running `ingest.py` standalone leaves the .md files on disk but **unindexed**, and skips dedup warnings.

`run.sh` runs all three stages in order. Use it. The only reason to run `ingest.py` alone is debugging a single file with `--dry-run`.

### Stopping a run early

```fish
# Graceful — current Marker subprocess dies, ingest.py exits cleanly
kill -INT (pgrep -f ingest.py)

# Forceful if SIGINT hangs (rare):
kill -TERM (pgrep -f ingest.py)
```

Either way, the in-flight book becomes a healable orphan that the next run reattempts. The pipeline is fully idempotent.

### Estimating progress

Once the schema migration runs, every new conversion logs `conversion_seconds` into the `books` table. After ~20 conversions you can compute a real ETA:

```sql
SELECT AVG(conversion_seconds) AS avg_s,
       COUNT(*)                AS done,
       SUM(conversion_seconds) AS total_s
FROM books WHERE conversion_seconds IS NOT NULL;
```

Multiply `avg_s` by remaining file count for an estimate that beats anything you can derive from file size alone.

---

## Schema reference

### `books`
Original columns plus phase-2 additions:

| Column | Notes |
|---|---|
| `id`, `md_path`, `title`, `author`, `source`, `converted`, `pages`, `indexed_at` | Original. |
| `sha256`               | SHA-256 of the source file. Used for duplicate detection. |
| `run_id`               | Foreign key to `runs.id`. |
| `conversion_seconds`   | Wall-clock for this book's conversion. |
| `converter`            | `marker`, `pandoc`, `mobi+pandoc`. |
| `converter_version`    | Whatever `--version` returns. |
| `extracted_chars`      | Size in bytes of the resulting `.md` file. Proxy for "did this work". |
| `ocr_used`             | Reserved; not yet populated. |
| `status`               | `done` / future: `failed`, `partial`. |
| `error`                | Future: error string for failed conversions. |
| `normalized_title`     | Lowercased + punctuation-stripped title for dedup keying. |
| `isbn`, `doi`          | Populated by `--enrich`. |

### `runs`
Per-invocation metadata: `id`, `started_at`, `ended_at`, `host`, `files_total`, `files_done`, `files_failed`, `notes`.

### `books_fts`
Original FTS5 virtual table over `(title, author, body)` with Porter stemming.

---

## Common queries

```sql
-- Full-text search
SELECT b.title, b.author, snippet(books_fts, 2, '[', ']', '...', 20)
FROM books_fts
JOIN books b ON books_fts.rowid = b.id
WHERE books_fts MATCH 'your search terms'
ORDER BY rank;

-- Books with no author after the cleanup pass — candidates for --enrich
SELECT title, source FROM books WHERE author IS NULL OR author = '';

-- Books with zero pages — counter is broken or file is empty
SELECT title, source FROM books WHERE pages = 0 OR pages IS NULL;

-- Duplicates by content hash
SELECT sha256, COUNT(*) AS n, GROUP_CONCAT(md_path, ' | ')
FROM books WHERE sha256 IS NOT NULL
GROUP BY sha256 HAVING n > 1;

-- Slowest conversions (where to optimize)
SELECT title, pages, conversion_seconds
FROM books
WHERE conversion_seconds IS NOT NULL
ORDER BY conversion_seconds DESC LIMIT 20;

-- Run history
SELECT id, started_at, ended_at, files_done, files_failed, notes FROM runs ORDER BY started_at;
```

---

## Open follow-ups

These aren't implemented; they're the next obvious wins.

- **Failure rows.** When a conversion fails, write a stub row (`status='failed'`) so the DB knows something was tried. Right now failures only appear in the log.
- **Format-specific routing.** Test PDFs for an extractable text layer (`pdftotext -f 1 -l 1`) and route text-PDFs through a cheap converter like `pymupdf4llm` instead of always paying Marker's load cost.
- **Quality score.** Combine `extracted_chars / pages` into a sanity ratio; flag rows below threshold for re-conversion.
