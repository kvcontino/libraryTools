# libraryTools
toward a personal library that's efficient, searchable, and readable

## Description
A specialized ETL pipeline designed to transform a stagnant collection of PDFs and eBooks into a high-utility, searchable, and reflowable Markdown library.

## The Strategy
This project addresses the "middle-layer inefficiency" of document management by bypassing standard PDF viewers in favor of a **web-state** library. It uses:

* **Marker (AI-OCR):** For complex PDF layout reconstruction.
* **Pandoc:** For high-fidelity eBook-to-Markdown conversion.
* **Chunked Ingestion:** A memory-safe approach for processing large volumes of data on limited hardware (like my Lenovo X1 Carbon) without triggering OOM errors.

## Current Components
- `ingest.py`: automation script that categorizes inbound files, executes chunked processing, and reassembles fragmented outputs into Markdown files with local image assets.

## Workflow
1. Place source files in `~/6_reading` (or rename to your folder of choice)
2. Run `nice -n 19 python ingest.py` to process the library as a background task.
3. Access processed outputs in `~/Library/Markdown` (or rename to your folder of choice)
