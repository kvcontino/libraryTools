"""
watch.py — monitors SOURCE_DIR for new PDFs/eBooks and triggers the pipeline.

Run directly:     python3 watch.py
As a service:     see library.service (systemd --user)
"""

import sys
import time
import logging
import subprocess
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- 1. CONFIGURATION ---
SOURCE_DIR = Path("~/6_reading").expanduser()
SCRIPT_DIR = Path(__file__).parent
EXTENSIONS = {".pdf", ".epub", ".mobi"}
DEBOUNCE   = 10  # seconds after last event before triggering pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)


# --- 2. EVENT HANDLER ---

class LibraryHandler(FileSystemEventHandler):
    def __init__(self):
        # pending: {path_str -> last_seen timestamp}
        self.pending = {}

    def on_created(self, event):
        if event.is_directory:
            return
        self._enqueue(event.src_path)

    def on_moved(self, event):
        # Covers atomic saves: editor writes temp file then renames
        self._enqueue(event.dest_path)

    def _enqueue(self, path_str):
        path = Path(path_str)
        if path.suffix.lower() in EXTENSIONS:
            self.pending[path_str] = time.time()
            logging.info(f"Detected: {path.name} — queued (debounce {DEBOUNCE}s).")

    def flush_pending(self):
        """Call periodically. Triggers pipeline for files stable for DEBOUNCE seconds."""
        now   = time.time()
        ready = [p for p, t in self.pending.items() if now - t >= DEBOUNCE]

        if not ready:
            return

        for p in ready:
            del self.pending[p]

        logging.info(f"{len(ready)} file(s) ready — running pipeline.")
        _run_pipeline()


# --- 3. PIPELINE TRIGGER ---

def _run_pipeline():
    for script in ("ingest.py", "index.py"):
        path = SCRIPT_DIR / script
        try:
            subprocess.run(
                [sys.executable, str(path)],
                check=True,
            )
        except subprocess.CalledProcessError as e:
            logging.error(f"{script} failed: {e}")
            return  # don't run index if ingest failed


# --- 4. MAIN ---

def main():
    handler  = LibraryHandler()
    observer = Observer()
    observer.schedule(handler, str(SOURCE_DIR), recursive=False)
    observer.start()
    logging.info(f"Watching {SOURCE_DIR}")

    try:
        while True:
            time.sleep(2)
            handler.flush_pending()
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        logging.info("Watcher stopped.")


if __name__ == "__main__":
    main()
