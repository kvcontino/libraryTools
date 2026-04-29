#!/usr/bin/env python3
"""cleanup_orphans.py — find & remove debris from interrupted ingestions.

An "orphan dir" is a subdirectory of ~/Library/Markdown/ that lacks the
expected <dir>/<dirname>.md file. It typically means marker started a
conversion but ingest.py was killed before writing the final .md.

Two populations:
  - HEALABLE: source file still exists in ~/6_reading/. The next ingest
    run will re-attempt and overwrite. Leave alone.
  - ZOMBIE: source file is gone. Nothing will retry this. Safe to delete.

Default: dry-run (report only). Use --apply to delete zombies.

Safety: only deletes dirs that contain nothing, or contain only an
empty/non-empty `images/` subdir. Anything else gets flagged for manual
review — never blindly removed.
"""
import argparse
import shutil
from pathlib import Path

LIBRARY = Path("~/Library/Markdown").expanduser()
SOURCES = Path("~/6_reading").expanduser()
SKIP_DIRS = {"reports"}
SOURCE_EXTS = (".pdf", ".epub", ".mobi")


def find_source(stem):
    """Return a matching source file in ~/6_reading/, or None.

    First tries exact stem match. Falls back to prefix match in case
    marker truncated the original filename when creating the dir."""
    for ext in SOURCE_EXTS:
        candidate = SOURCES / f"{stem}{ext}"
        if candidate.exists():
            return candidate

    for src in SOURCES.iterdir():
        if not src.is_file() or src.suffix.lower() not in SOURCE_EXTS:
            continue
        if src.stem.startswith(stem) or stem.startswith(src.stem):
            return src
    return None


def is_safe_to_delete(orphan):
    """Only delete dirs whose contents are debris.

    Allowed: empty, or only an `images/` subdir. Anything else (a stray
    .md, .json, .pdf, etc.) means manual review."""
    contents = list(orphan.iterdir())
    if not contents:
        return True
    if len(contents) == 1 and contents[0].name == "images" and contents[0].is_dir():
        return True
    return False


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--apply", action="store_true",
                   help="Actually delete zombies (default: dry-run)")
    args = p.parse_args()

    healable = []
    zombies = []
    skipped_unsafe = []
    weird = []

    for entry in LIBRARY.iterdir():
        try:
            if not entry.is_dir():
                continue
        except OSError as e:
            weird.append((entry, str(e)))
            continue

        if entry.name in SKIP_DIRS or entry.name.startswith("."):
            continue

        expected_md = entry / f"{entry.name}.md"
        try:
            if expected_md.exists():
                continue
        except OSError as e:
            weird.append((entry, str(e)))
            continue

        try:
            source = find_source(entry.name)
        except OSError as e:
            weird.append((entry, str(e)))
            continue

        if source is not None:
            healable.append((entry, source))
        elif is_safe_to_delete(entry):
            zombies.append(entry)
        else:
            skipped_unsafe.append(entry)

    print(f"Healable (source still in ~/6_reading/, will heal on next ingest): {len(healable)}")
    print(f"Zombies  (source gone, safe to delete):                            {len(zombies)}")
    print(f"Skipped  (unexpected contents, manual review):                     {len(skipped_unsafe)}")
    print(f"Weird    (path access errors):                                     {len(weird)}")
    print()

    if zombies:
        print("=== Zombies ===")
        for z in zombies:
            print(f"  {z}")
        print()

    if skipped_unsafe:
        print("=== Skipped (manual review) ===")
        for s in skipped_unsafe:
            try:
                contents = ", ".join(c.name for c in s.iterdir())
            except OSError:
                contents = "<unreadable>"
            print(f"  {s}  contents={contents}")
        print()

    if weird:
        print("=== Weird path errors ===")
        for path, err in weird:
            print(f"  {path}: {err}")
        print()

    if args.apply and zombies:
        print(f"Deleting {len(zombies)} zombie dirs...")
        for z in zombies:
            shutil.rmtree(z)
            print(f"  removed {z}")
    elif zombies:
        print(f"(dry-run) {len(zombies)} zombies would be removed. Re-run with --apply.")


if __name__ == "__main__":
    main()
