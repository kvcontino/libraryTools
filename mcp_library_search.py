#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp",
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
"""MCP server exposing semantic search over the markdown library.

Wraps library_search.py (same directory) as a tool so Claude Code and
its subagents can query the library mid-task. Runs over stdio; the
embedding model and chunk matrix stay loaded between calls.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import library_search as ls

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("librarysearch")


@mcp.tool()
def search_library(query: str, n: int = 10, book: str | None = None) -> str:
    """Semantic (conceptual, not keyword) search over the user's personal
    markdown library of ~250 books. Returns the top chunks by cosine
    similarity as `[score] Title (chunk N)` plus a snippet. Scores of
    0.55-0.80 are usable matches; below 0.50 is noise. Optional `book`
    filters results to titles containing that substring."""
    results = ls.search(query, n=n, book=book)
    if not results:
        return "No results" + (f" in books matching {book!r}." if book else ".")
    return "\n".join(
        f"[{score:.3f}] {title} (chunk {idx})\n    {' '.join(text.split())[:300]}"
        for score, title, idx, text in results
    )


if __name__ == "__main__":
    mcp.run()
