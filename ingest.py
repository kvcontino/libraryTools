import re
import shutil
import subprocess
import tempfile
import zipfile
import difflib
import logging
from pathlib import Path
from datetime import datetime
from tqdm import tqdm
import mobi

# --- 1. CONFIGURATION ---
SOURCE_DIR    = Path("~/6_reading").expanduser()
TARGET_DIR    = Path("~/Library/Markdown").expanduser()
LOG_PATH      = TARGET_DIR / f"ingest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
MARKER_SINGLE   = Path("~/marker-stable/bin/marker_single").expanduser()

CHUNK_SIZE = 50   # pages per marker_single call
OVERLAP    = 5    # pages shared between consecutive chunks
TAIL_LINES = 100  # lines kept from previous chunk tail for dedup comparison


# --- 2. LOGGING ---

def setup_logging():
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH),
            logging.StreamHandler(),
        ],
    )


# --- 3. METADATA EXTRACTION ---

def extract_pdf_metadata(fpath, pages):
    try:
        info = subprocess.check_output(["pdfinfo", str(fpath)]).decode()
        meta = {}
        for line in info.split("\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()
        return {
            "title":     meta.get("Title") or fpath.stem,
            "author":    meta.get("Author", ""),
            "created":   meta.get("CreationDate", ""),
            "pages":     pages,
            "source":    str(fpath),
            "converted": datetime.now().isoformat(),
        }
    except Exception as e:
        logging.warning(f"Could not extract PDF metadata for {fpath.name}: {e}")
        return {"title": fpath.stem, "source": str(fpath), "converted": datetime.now().isoformat()}


def extract_epub_metadata(fpath):
    try:
        with zipfile.ZipFile(fpath) as z:
            opf_path = next((n for n in z.namelist() if n.endswith(".opf")), None)
            if not opf_path:
                raise ValueError("No OPF file found")
            opf = z.read(opf_path).decode()

        def _tag(tag):
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", opf, re.DOTALL)
            return m.group(1).strip() if m else ""

        return {
            "title":     _tag("dc:title") or fpath.stem,
            "author":    _tag("dc:creator"),
            "created":   _tag("dc:date"),
            "source":    str(fpath),
            "converted": datetime.now().isoformat(),
        }
    except Exception as e:
        logging.warning(f"Could not extract epub metadata for {fpath.name}: {e}")
        return {"title": fpath.stem, "source": str(fpath), "converted": datetime.now().isoformat()}


def write_frontmatter(md_path, meta):
    """Prepend YAML frontmatter to an existing Markdown file."""
    with open(md_path, "r") as f:
        content = f.read()

    lines = ["---"]
    for key, value in meta.items():
        if value == "" or value is None:
            continue
        value_str = str(value)
        if any(c in value_str for c in ':#{}[]|>&*"\''):
            value_str = f'"{value_str}"'
        lines.append(f"{key}: {value_str}")
    lines.append("---\n")

    with open(md_path, "w") as f:
        f.write("\n".join(lines) + "\n" + content)


# --- 4. OVERLAP DEDUPLICATION ---

def find_overlap_cutoff(tail: str, new_chunk: str, min_match: int = 4) -> int:
    """
    Return the line index in new_chunk where non-duplicate content starts.
    Compares the tail of accumulated output against the head of the new chunk
    using SequenceMatcher. Returns 0 if no reliable overlap is found.
    """
    tail_lines  = tail.splitlines()
    chunk_lines = new_chunk.splitlines()
    head        = chunk_lines[: TAIL_LINES * 2]

    matcher = difflib.SequenceMatcher(None, tail_lines, head, autojunk=False)
    blocks  = matcher.get_matching_blocks()

    best = max(
        (b for b in blocks if b.size >= min_match),
        key=lambda b: b.b + b.size,
        default=None,
    )

    if best:
        cutoff = best.b + best.size
        logging.debug(f"  Overlap dedup: skipping {cutoff} lines from chunk head.")
        return cutoff

    return 0


# --- 5. VERIFICATION ---

def verify_image_links(md_path):
    with open(md_path, "r") as f:
        content = f.read()

    raw    = re.findall(r"!\[.*?\]\((.*?)\)", content)
    # Strip optional title attribute: ![alt](path "title") → path
    links  = [r.split()[0] for r in raw]
    # Exclude fragment anchors (#...) — these are footnote refs, not file paths
    links  = [l for l in links if not l.startswith("#")]
    broken = [l for l in links if not (md_path.parent / l).resolve().exists()]

    if broken:
        logging.warning(f"  {len(broken)} missing image(s) in {md_path.name}: {broken}")
    else:
        logging.info(f"  All image links verified in {md_path.name}.")


# --- 6. PROCESSORS ---

def process_pdf(fpath, book_dir):
    final_md = book_dir / f"{fpath.stem}.md"
    if final_md.exists():
        final_md.unlink()

    img_dst = book_dir / "images"
    img_dst.mkdir(exist_ok=True)

    info  = subprocess.check_output(["pdfinfo", str(fpath)]).decode()
    try:
        pages = int(
            next(line for line in info.split("\n") if "Pages:" in line).split()[1]
        )
    except (StopIteration, ValueError, IndexError) as e:
        raise RuntimeError(f"Could not read page count from pdfinfo for {fpath.name}: {e}")

    pbar             = tqdm(total=pages, desc=f"  📄 {fpath.stem[:20]}...", unit="pg", leave=False)
    accumulated_tail = ""

    for start in range(0, pages, CHUNK_SIZE):
        end      = min(start + CHUNK_SIZE + OVERLAP - 1, pages - 1)
        temp_out = book_dir / "temp"

        if temp_out.exists():
            shutil.rmtree(temp_out)

        try:
            subprocess.run(
                [
                    str(MARKER_SINGLE), str(fpath.resolve()),
                    "--output_dir",      str(temp_out.resolve()),
                    "--page_range",      f"{start}-{end}",
                    "--pdftext_workers", "1",
                    "--extract_images",  "True",
                ],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            logging.error(
                f"marker_single failed on {fpath.name} pages {start}-{end}.\n"
                f"  stderr: {e.stderr.decode().strip()}"
            )
            raise

        subdirs = [p for p in temp_out.iterdir() if p.is_dir()]
        if len(subdirs) != 1:
            raise RuntimeError(
                f"Expected 1 output folder from marker_single, got {len(subdirs)} "
                f"(pages {start}-{end} of {fpath.name})"
            )

        gen_folder = subdirs[0]
        src_md     = gen_folder / f"{gen_folder.name}.md"

        with open(src_md, "r") as f:
            chunk_content = f.read()

        # Normalize image links
        chunk_content = re.sub(
            r"!\[(.*?)\]\((?!images/)(.*?)\)",
            r"![\1](images/\2)",
            chunk_content,
        )

        # Deduplicate overlap with previous chunk
        if accumulated_tail:
            cutoff        = find_overlap_cutoff(accumulated_tail, chunk_content)
            chunk_content = "\n".join(chunk_content.splitlines()[cutoff:])

        with open(final_md, "a") as dst:
            dst.write(chunk_content + "\n\n")

        accumulated_tail = "\n".join(chunk_content.splitlines()[-TAIL_LINES:])

        for img_path in gen_folder.rglob("*"):
            if img_path.is_file() and img_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg"}:
                shutil.move(str(img_path), str(img_dst / img_path.name))

        shutil.rmtree(temp_out)
        pbar.update(min(CHUNK_SIZE, pages - start))
    pbar.close()

    meta = extract_pdf_metadata(fpath, pages)
    write_frontmatter(final_md, meta)
    verify_image_links(final_md)


def process_ebook(fpath, book_dir):
    final_md = book_dir / f"{fpath.stem}.md"
    if final_md.exists():
        final_md.unlink()

    img_dst = book_dir / "images"
    img_dst.mkdir(exist_ok=True)

    try:
        subprocess.run(
            [
                "pandoc", str(fpath.resolve()),
                "--to",            "markdown",
                "--extract-media", "images",
                "--output",        str(final_md.resolve()),
            ],
            check=True,
            capture_output=True,
            cwd=book_dir,
        )
    except subprocess.CalledProcessError as e:
        logging.error(
            f"pandoc failed on {fpath.name}.\n"
            f"  stderr: {e.stderr.decode().strip()}"
        )
        raise

    with open(final_md, "r") as f:
        content = f.read()

    content = re.sub(
        r"!\[(.*?)\]\((?!images/).*?/([^/]+\.(png|jpg|jpeg|svg|gif))\)",
        r"![\1](images/\2)",
        content,
        flags=re.IGNORECASE,
    )

    with open(final_md, "w") as f:
        f.write(content)

    meta = extract_epub_metadata(fpath)
    write_frontmatter(final_md, meta)
    verify_image_links(final_md)


def process_mobi(fpath, book_dir):
    final_md = book_dir / f"{fpath.stem}.md"
    if final_md.exists():
        final_md.unlink()

    img_dst = book_dir / "images"
    img_dst.mkdir(exist_ok=True)

    # mobi.extract() unpacks the mobi to a temp dir and returns the main HTML path.
    # Converting HTML→Markdown is far cheaper on memory than mobi→Markdown directly.
    tempdir, html_path = mobi.extract(str(fpath.resolve()))
    html_path = Path(html_path)

    try:
        subprocess.run(
            [
                "pandoc", str(html_path),
                "--to",            "markdown",
                "--extract-media", "images",
                "--output",        str(final_md.resolve()),
            ],
            check=True,
            capture_output=True,
            cwd=book_dir,
        )
    except subprocess.CalledProcessError as e:
        logging.error(
            f"pandoc failed on {fpath.name} (via mobi extraction).\n"
            f"  stderr: {e.stderr.decode().strip()}"
        )
        raise
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)

    with open(final_md, "r") as f:
        content = f.read()

    content = re.sub(
        r"!\[(.*?)\]\((?!images/).*?/([^/]+\.(png|jpg|jpeg|svg|gif))\)",
        r"![\1](images/\2)",
        content,
        flags=re.IGNORECASE,
    )

    with open(final_md, "w") as f:
        f.write(content)

    meta = extract_epub_metadata(fpath)
    write_frontmatter(final_md, meta)
    verify_image_links(final_md)


# --- 7. MAIN ---

def main(dry_run=False):
    setup_logging()

    files_to_process = [
        f for f in SOURCE_DIR.iterdir()
        if f.suffix.lower() in {".pdf", ".epub", ".mobi"}
    ]

    for fpath in tqdm(files_to_process, desc="📚 Total Library Progress", disable=dry_run):
        suffix = fpath.suffix.lower()
        if suffix == ".pdf":
            method = "Marker"
        elif suffix == ".mobi":
            method = "mobi+Pandoc"
        else:
            method = "Pandoc"

        if dry_run:
            print(f"[DRY RUN] {fpath.name}  |  Method: {method}")
            continue

        book_dir = TARGET_DIR / fpath.stem

        if (book_dir / f"{fpath.stem}.md").exists():
            logging.info(f"Skipping {fpath.name} (already converted).")
            continue

        book_dir.mkdir(exist_ok=True)
        logging.info(f"Processing {fpath.name} via {method}.")

        try:
            if suffix == ".pdf":
                process_pdf(fpath, book_dir)
            elif suffix == ".mobi":
                process_mobi(fpath, book_dir)
            else:
                process_ebook(fpath, book_dir)
            logging.info(f"Finished {fpath.name}.")
        except Exception as e:
            logging.error(f"Skipping {fpath.name} after error: {e}")


# --- 8. EXECUTION ---

if __name__ == "__main__":
    main(dry_run=False)
