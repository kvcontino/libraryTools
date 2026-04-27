"""
metadata.py — clean and enrich book metadata.

Used by ingest.py (during conversion) and repair.py (over existing rows).
Stdlib only — no external dependencies.

Public surface:
    clean_author(raw)               -> str           sanitised author or ""
    clean_title_from_filename(stem) -> str           cleaned title from a file stem
    title_from_markdown(md_path)    -> str | None    first H1 in body
    extract_isbns(text)             -> list[str]     ISBN-10/13 found in text
    openlibrary_lookup(isbn)        -> dict | None   {"title","author"} or None
    epub_page_count(fpath)          -> int           spine length × pages-per-item
    estimate_pages_from_text(text)  -> int           ~250 words/page
    sha256_file(fpath)              -> str           hex digest of source bytes
    normalize_title(title)          -> str           for near-duplicate detection
    enrich(meta, fpath, body)       -> dict          updated meta with cleanups + optional lookup

The enrich() entry point applies (1) author filter, (3) title cleanup,
(7) source-derived pages, and (8) optional Open Library lookup. It also runs
the (2) title fallback chain when extract_pdf_metadata's title looks like
filename junk.

Set the LIBRARY_ENRICH env var (or pass enrich_external=True) to enable the
Open Library/Crossref network calls. They're cached in
~/Library/Markdown/.metadata-cache.json so re-runs are free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

CACHE_PATH = Path("~/Library/Markdown/.metadata-cache.json").expanduser()


# --- AUTHOR SANITY ----------------------------------------------------------

# PDF /Author values commonly contain the username/handle of whoever produced
# the PDF, the software that generated it, or other junk. These are exact-match
# (case-insensitive) and substring blocklists for the worst offenders we've
# seen plus the obvious generators.
AUTHOR_BLOCKLIST_EXACT = {
    # Handles / usernames seen in this library
    "bobo", "eurotrash", "sabarba", "sadegh", "human", "florian",
    # Generic placeholders
    "test", "admin", "user", "owner", "unknown", "anonymous", "n/a",
    "default", "guest", "root",
    # The user's own handle — would mean they generated the PDF themselves
    "contino", "kvcontino",
}

AUTHOR_BLOCKLIST_SUBSTR = {
    "microsoft office", "microsoft word", "openoffice", "libreoffice",
    "calibre", "acrobat", "preview.app", "ghostscript", "pdftk",
    "scansnap", "abbyy", "iceni", "pdfsam", "tcpdf", "fpdf",
}


def clean_author(raw: str | None) -> str:
    """Filter out garbage PDF /Author values; return a clean author or ''."""
    if not raw:
        return ""
    s = raw.strip().strip('"').strip("'").strip()
    if not s:
        return ""

    low = s.lower()

    if low in AUTHOR_BLOCKLIST_EXACT:
        return ""
    if any(needle in low for needle in AUTHOR_BLOCKLIST_SUBSTR):
        return ""

    # Single all-lowercase token under 8 chars is almost always a handle
    if len(s) < 8 and " " not in s and s == low and s.isalpha():
        return ""

    # All-numeric or mostly punctuation
    if re.fullmatch(r"[\d\W_]+", s):
        return ""

    # Length sanity
    if len(s) < 3 or len(s) > 200:
        return ""

    # Strip trailing junk (e.g. "Joseph Frank;;" or "Frank, Joseph.")
    s = s.rstrip(";.,| ").strip()

    return s


# --- TITLE CLEANUP ----------------------------------------------------------

# Trailing tags from libgen / z-lib / book mirrors. Order matters: longer
# patterns first so we don't half-strip something like "(BookZZ.org)" into
# leftover ".org)".
FILENAME_TAIL_PATTERNS = [
    r"\s*-\s*libgen\.(?:li|lc|rs|me|is)(?:\(\d+\))?\s*$",
    r"\s*\(libgen\.(?:li|lc|rs|me|is)\)\s*$",
    r"\s*\(z-?lib\.org\)\s*$",
    r"\s*\(b-ok\.(?:org|cc)\)\s*$",
    r"\s*\(BookZZ\.org\)\s*$",
    r"\s*\(BookFi\.org\)\s*$",
    r"\s*\[BookOS\.org\]\s*$",
    r"\s*\(\d+\)\s*$",                 # trailing "(1)", "(2)" duplicate marker
    r"\s*-\s*re\.press\s*$",
]

# Leading bracketed publisher/series tags. We only strip when the bracket
# is at the very start, since brackets later in a title may be meaningful.
FILENAME_HEAD_PATTERNS = [
    r"^\s*\[[^\]]{1,80}\]\s*",         # [INSEAD Business Press], [Focus on …]
    r"^\s*\(Anomaly\)\s*",
]

# Garbage stems that indicate the filename itself is not a usable title.
# These trigger the title fallback chain (markdown H1 → external lookup).
JUNK_STEM_PATTERNS = [
    r"^[a-f0-9]{6,}$",                         # all-hex (digest-like)
    r"^-?docs-[A-Z0-9]{2,}$",                  # "-docs-E2F3"
    r"^\d{6,}_?web?$",                         # "9781400833412_Web"
    r".*\.(qxp|doc|docx|indd|pages)$",         # leftover source extensions
    r"^untitled.*$",
    r"^document\d*$",
    r"^scan(\s|_)?\d*$",
    r"^image[_\s]?of[_\s]?the[_\s]?city.*",    # we have one of these
    r"^[a-z]{2,4}\d{3,}.*",                    # short-prefix-then-number IDs (e.g. RAND1458) — must have alpha
    r"^text$",
    r"^hartbookfront\d*",
]


def clean_title_from_filename(stem: str) -> str:
    """Strip libgen-style tails, replace underscores, normalize whitespace."""
    s = stem
    for pat in FILENAME_HEAD_PATTERNS:
        s = re.sub(pat, "", s, flags=re.IGNORECASE)
    for pat in FILENAME_TAIL_PATTERNS:
        s = re.sub(pat, "", s, flags=re.IGNORECASE)
    s = s.replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip(" -–—.")
    return s


def stem_looks_like_junk(stem: str) -> bool:
    """True if the filename stem won't make a usable title on its own."""
    s = stem.strip()
    if not s:
        return True
    for pat in JUNK_STEM_PATTERNS:
        if re.fullmatch(pat, s, flags=re.IGNORECASE):
            return True
    return False


def title_from_markdown(md_path: Path) -> str | None:
    """Return the first H1 heading in the markdown body, or None."""
    try:
        with open(md_path, "r", errors="replace") as f:
            content = f.read()
    except OSError:
        return None

    body = re.sub(r"^---\n.*?\n---\n", "", content, count=1, flags=re.DOTALL)

    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# ") and not line.startswith("##"):
            heading = line.lstrip("#").strip()
            # Strip markdown emphasis (** ** and * *) and inline code ticks
            heading = re.sub(r"^[\*_`]+|[\*_`]+$", "", heading).strip()
            heading = re.sub(r"\*\*(.+?)\*\*", r"\1", heading)
            heading = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", heading)
            heading = re.sub(r"\s+", " ", heading)
            if 4 <= len(heading) <= 200:
                return heading
    return None


def title_looks_garbage(title: str | None) -> bool:
    """Heuristic: title needs a fallback if it's empty or matches junk patterns."""
    if not title:
        return True
    t = title.strip()
    if not t:
        return True
    if len(t) < 4:
        return True
    return stem_looks_like_junk(t)


# --- ISBN / DOI EXTRACTION --------------------------------------------------

ISBN_RE = re.compile(
    r"\b(?:ISBN(?:-1[03])?:?\s*)?(?=[\d\-Xx ]{10,17}\b)"
    r"((?:97[89][- ]?)?(?:\d[- ]?){9}[\dXx])\b"
)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)


def _isbn_checksum_ok(raw: str) -> bool:
    digits = re.sub(r"[^\dXx]", "", raw)
    if len(digits) == 10:
        total = 0
        for i, c in enumerate(digits):
            v = 10 if c in "Xx" else int(c)
            total += v * (10 - i)
        return total % 11 == 0
    if len(digits) == 13:
        total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(digits))
        return total % 10 == 0
    return False


def extract_isbns(text: str) -> list[str]:
    """Return validated unique ISBNs found in `text`."""
    seen: list[str] = []
    for m in ISBN_RE.finditer(text):
        raw = m.group(1)
        if _isbn_checksum_ok(raw):
            normalized = re.sub(r"[^\dXx]", "", raw).upper()
            if normalized not in seen:
                seen.append(normalized)
    return seen


def pdf_first_pages_text(fpath: Path, last_page: int = 5) -> str:
    """Extract text from the first N pages with pdftotext. Returns '' on failure."""
    try:
        out = subprocess.check_output(
            ["pdftotext", "-l", str(last_page), str(fpath), "-"],
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return out.decode("utf-8", errors="replace")
    except (subprocess.SubprocessError, OSError):
        return ""


# --- OPEN LIBRARY / CROSSREF LOOKUP -----------------------------------------

_cache: dict | None = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        if CACHE_PATH.exists():
            try:
                _cache = json.loads(CACHE_PATH.read_text())
            except (OSError, json.JSONDecodeError):
                _cache = {}
        else:
            _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is None:
        return
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(_cache, indent=2, sort_keys=True))
    except OSError as e:
        logging.warning(f"Could not write metadata cache: {e}")


def _http_get_json(url: str, timeout: float = 8.0) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "library-tools/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as e:
        logging.debug(f"HTTP GET failed for {url}: {e}")
        return None


def openlibrary_lookup(isbn: str) -> dict | None:
    """Query Open Library by ISBN. Cached. Returns {'title','author'} or None."""
    cache = _load_cache()
    key = f"isbn:{isbn}"
    if key in cache:
        return cache[key] or None

    url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
    data = _http_get_json(url)
    record = (data or {}).get(f"ISBN:{isbn}")

    result: dict | None = None
    if record:
        title = record.get("title", "")
        subtitle = record.get("subtitle", "")
        if subtitle:
            title = f"{title}: {subtitle}"
        authors = ", ".join(a.get("name", "") for a in record.get("authors", []) if a.get("name"))
        if title or authors:
            result = {"title": title.strip(), "author": authors.strip()}

    cache[key] = result or {}
    _save_cache()
    time.sleep(0.2)  # be polite to Open Library
    return result


def crossref_lookup(doi: str) -> dict | None:
    cache = _load_cache()
    key = f"doi:{doi.lower()}"
    if key in cache:
        return cache[key] or None

    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='/:')}"
    data = _http_get_json(url)
    msg = (data or {}).get("message") or {}

    result: dict | None = None
    titles = msg.get("title") or []
    authors = msg.get("author") or []
    if titles:
        title = titles[0]
        author = ", ".join(
            f"{a.get('given','')} {a.get('family','')}".strip()
            for a in authors if a.get("family")
        )
        result = {"title": title.strip(), "author": author.strip()}

    cache[key] = result or {}
    _save_cache()
    time.sleep(0.2)
    return result


# --- PAGE COUNTING (EPUB / MOBI / fallback) ---------------------------------

def epub_page_count(fpath: Path, words_per_page: int = 300) -> int:
    """
    Estimate pages for an EPUB. Counts spine items × ~3 pages each as a
    cheap heuristic, but if we can read the content cheaply we estimate from
    word count instead, which is much closer to the printed page count.
    """
    try:
        with zipfile.ZipFile(fpath) as z:
            names = z.namelist()
            html_names = [n for n in names if n.lower().endswith((".html", ".xhtml", ".htm"))]

            # Word-count estimate beats spine count when feasible.
            total_words = 0
            for n in html_names[:200]:  # cap for safety
                try:
                    raw = z.read(n).decode("utf-8", errors="replace")
                    # Cheap tag strip
                    text = re.sub(r"<[^>]+>", " ", raw)
                    total_words += len(text.split())
                except (KeyError, UnicodeDecodeError):
                    continue

            if total_words > 0:
                return max(1, total_words // words_per_page)

            # Fall back to spine length × 3 pages/chapter
            opf = next((n for n in names if n.endswith(".opf")), None)
            if opf:
                content = z.read(opf).decode("utf-8", errors="replace")
                spine_items = len(re.findall(r"<itemref\b", content))
                if spine_items:
                    return spine_items * 3
    except (zipfile.BadZipFile, OSError, KeyError):
        pass
    return 0


def estimate_pages_from_text(text: str, words_per_page: int = 300) -> int:
    if not text:
        return 0
    return max(1, len(text.split()) // words_per_page)


# --- HASHING + NORMALIZATION ------------------------------------------------

def sha256_file(fpath: Path, chunk: int = 1 << 20) -> str:
    """Streaming SHA-256 of a file. Used for content-based dedup."""
    h = hashlib.sha256()
    with open(fpath, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and known mirror tags. For dedup keying."""
    if not title:
        return ""
    t = clean_title_from_filename(title).lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# --- ENRICH ENTRY POINT -----------------------------------------------------

def enrich(
    meta: dict,
    fpath: Path,
    body_path: Path | None = None,
    enrich_external: bool | None = None,
) -> dict:
    """
    Apply all available cleanups to `meta` in place and return it.

    - (1) clean_author on meta['author']
    - (3) clean_title_from_filename when title equals the raw stem
    - (7) backfill pages for EPUB/MOBI if missing/zero
    - (2) markdown H1 fallback when title still looks like junk
    - (8) Open Library / Crossref enrichment when enabled

    `body_path` is the .md path (used only for the H1 fallback).
    `enrich_external` overrides the LIBRARY_ENRICH env var when set.
    """
    if enrich_external is None:
        enrich_external = os.environ.get("LIBRARY_ENRICH", "").lower() in {"1", "true", "yes", "on"}

    # --- author ---
    meta["author"] = clean_author(meta.get("author"))

    # --- title cleanup ---
    raw_title = (meta.get("title") or "").strip()
    suffix = fpath.suffix.lower()

    # If the title equals the file stem (the common fallback path in
    # extract_*_metadata), clean it. Same if the title is missing.
    if not raw_title or raw_title == fpath.stem:
        meta["title"] = clean_title_from_filename(fpath.stem)
    else:
        # Even if PDF /Title is set, run cleanup — many PDFs have titles
        # that are just dressed-up filenames.
        meta["title"] = clean_title_from_filename(raw_title)

    # --- pages backfill (7) ---
    pages_val = meta.get("pages")
    pages_int = 0
    if pages_val not in (None, ""):
        try:
            pages_int = int(pages_val)
        except (TypeError, ValueError):
            pages_int = 0

    if pages_int <= 0:
        if suffix == ".epub":
            meta["pages"] = epub_page_count(fpath)
        elif suffix == ".mobi" and body_path and body_path.exists():
            try:
                with open(body_path, "r", errors="replace") as f:
                    meta["pages"] = estimate_pages_from_text(f.read())
            except OSError:
                pass

    # --- title fallback chain (2) ---
    if title_looks_garbage(meta.get("title")) and body_path and body_path.exists():
        h1 = title_from_markdown(body_path)
        if h1 and not title_looks_garbage(h1):
            meta["title"] = h1

    # --- external lookup (8) ---
    if enrich_external:
        isbns: list[str] = []
        if suffix == ".pdf":
            isbns = extract_isbns(pdf_first_pages_text(fpath))
        elif suffix in {".epub", ".mobi"}:
            try:
                with zipfile.ZipFile(fpath) as z:
                    opf_name = next((n for n in z.namelist() if n.endswith(".opf")), None)
                    if opf_name:
                        opf = z.read(opf_name).decode("utf-8", errors="replace")
                        isbns = extract_isbns(opf)
            except (zipfile.BadZipFile, OSError, KeyError, StopIteration):
                pass

        for isbn in isbns:
            hit = openlibrary_lookup(isbn)
            if hit:
                # Only overwrite if we actually got something better
                if hit.get("title") and (title_looks_garbage(meta.get("title")) or len(hit["title"]) > len(meta.get("title", ""))):
                    meta["title"] = hit["title"]
                if hit.get("author") and not meta.get("author"):
                    meta["author"] = hit["author"]
                meta["isbn"] = isbn
                break

        if not meta.get("isbn") and suffix == ".pdf":
            text = pdf_first_pages_text(fpath, last_page=3)
            doi_match = DOI_RE.search(text)
            if doi_match:
                hit = crossref_lookup(doi_match.group(0))
                if hit:
                    if hit.get("title") and title_looks_garbage(meta.get("title")):
                        meta["title"] = hit["title"]
                    if hit.get("author") and not meta.get("author"):
                        meta["author"] = hit["author"]
                    meta["doi"] = doi_match.group(0)

    return meta
