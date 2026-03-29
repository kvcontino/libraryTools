# libraryTools
toward a personal library that's efficient, searchable, and readable

## Description
A specialized ETL pipeline designed to transform a stagnant collection of PDFs and eBooks into a high-utility, searchable, and reflowable Markdown library.

## The Strategy
This project addresses the "middle-layer inefficiency" of document management by bypassing standard PDF viewers in favor of a web-state library. It uses:

* **Marker (AI-OCR):** For complex PDF layout reconstruction.
* **Pandoc:** For high-fidelity eBook-to-Markdown conversion.
* **Chunked Ingestion:** A memory-safe approach for processing large volumes of data on limited hardware (like my Lenovo X1 Carbon) without triggering OOM errors.

## Components

| File | Purpose |
|------|---------|
| `ingest.py` | Converts PDFs and eBooks to Markdown with YAML frontmatter and extracted images |
| `index.py` | Builds a SQLite3 full-text search index from all converted Markdown files |
| `watch.py` | Daemon that monitors the source directory and auto-triggers ingest + index on new files |
| `run.sh` | Runs `ingest.py` then `index.py` in sequence; suitable for ad-hoc use or cron |
| `library.service` | systemd user service that runs `watch.py` as a persistent background daemon |

## Dependencies

**Python packages:**
```bash
pip install marker-pdf watchdog tqdm
```

**System tools:**
```bash
sudo apt install pandoc poppler-utils   # poppler-utils provides pdfinfo
```

## Configuration

`ingest.py` and `index.py` share two path constants at the top of each file. Edit them to match your setup:

```python
SOURCE_DIR = "~/6_reading"        # where you drop source documents (.pdf, .epub, .mobi)
TARGET_DIR = "~/Library/Markdown" # where converted Markdown and the search index are written
```

## Usage

### One-off run
Process all pending files and update the index:
```bash
chmod +x run.sh   # first time only
./run.sh
```

### Scheduled (cron)
Edit your crontab (`crontab -e`) to run nightly at 2am:
```
0 2 * * * /path/to/run.sh >> ~/Library/Markdown/cron.log 2>&1
```

### Daemon (recommended)
Install the systemd service to watch for new files and process them automatically:
```bash
cp library.service ~/.config/systemd/user/
systemctl --user enable --now library.service

# Check status / logs
systemctl --user status library.service
journalctl --user -u library.service -f
```

Drop any `.pdf`, `.epub`, or `.mobi` file into `~/6_reading` and it will be converted and indexed automatically within ~10 seconds.

## Searching the Library

Open the database:
```bash
sqlite3 ~/Library/Markdown/library.db
```

**Full-text search** (Porter stemming — `work` matches `working`, `worked`, etc.):
```sql
SELECT b.title, b.author, snippet(books_fts, 2, '[', ']', '...', 20)
FROM books_fts
JOIN books b ON books_fts.rowid = b.id
WHERE books_fts MATCH 'your search terms'
ORDER BY rank;
```

**Filter by author:**
```sql
SELECT title, author, pages FROM books WHERE author LIKE '%Saval%';
```

**Most recently converted:**
```sql
SELECT title, author, converted FROM books ORDER BY converted DESC LIMIT 10;
```

**Books missing author metadata:**
```sql
SELECT title, source FROM books WHERE author = '' OR author IS NULL;
```
