#!/usr/bin/env python3
"""Split a large Zotero fulltext JSON file into manageable chunks.

Used by the weekly-review skill when a paper or report exceeds the sub-agent's
context window.  Supports two modes:

  **Marker mode** — split by chapter/section markers (preferred for reports
  with known structure like AI Index Report).

  **Fixed-size mode** — split into equal-sized character chunks (fallback for
  papers without clear section markers).

Usage::

    # Marker mode (requires a markers.json file)
    python scripts/chunk_fulltext.py fulltext.json --markers markers.json \\
        --output-dir output/weekly-review/2026-07-05/chunks/

    # Fixed-size mode
    python scripts/chunk_fulltext.py fulltext.json --chunk-size 50000 \\
        --output-dir output/weekly-review/2026-07-05/chunks/

Markers JSON format::

    {
      "section_name": {"start": "## Introduction", "end": "## Methods"},
      ...
    }

Each marker pair defines an extraction range: ``text[start:end]``.
Sections are saved as ``{section_name}.txt`` under ``--output-dir``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def split_by_markers(text: str, markers: dict[str, dict[str, str]]) -> dict[str, str]:
    """Split text by section boundary markers.

    Args:
        text: Full document text.
        markers: ``{section_name: {"start": "...", "end": "..."}}`` dict.

    Returns:
        ``{section_name: extracted_text}`` dict.  Sections whose start marker
        is not found are silently skipped.
    """
    result: dict[str, str] = {}
    for name, bounds in markers.items():
        start_str = bounds.get("start", "")
        end_str = bounds.get("end", "")

        start_idx = text.find(start_str)
        if start_idx < 0:
            print(
                f"WARNING: start marker not found for '{name}' — skipping",
                file=sys.stderr,
            )
            continue

        end_idx = text.find(end_str, start_idx + 1) if end_str else len(text)
        if end_idx < 0:
            end_idx = len(text)

        result[name] = text[start_idx:end_idx]
    return result


def split_by_size(text: str, chunk_size: int) -> list[str]:
    """Split text into fixed-size (character count) chunks."""
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Split a large Zotero fulltext JSON file into manageable chunks.",
    )
    parser.add_argument("input", help="Path to the fulltext JSON file from Zotero MCP")
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory to write chunk files into",
    )
    parser.add_argument(
        "--markers",
        help="Path to a markers JSON file for marker-mode splitting",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=50000,
        help="Characters per chunk in fixed-size mode (default: 50000)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(input_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"ERROR: cannot parse JSON from {input_path}: {exc}", file=sys.stderr)
        sys.exit(2)

    text = data.get("result", "")
    if not text:
        print("ERROR: JSON has no 'result' field or text is empty", file=sys.stderr)
        sys.exit(3)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.markers:
        markers_path = Path(args.markers)
        if not markers_path.exists():
            print(f"ERROR: markers file not found: {markers_path}", file=sys.stderr)
            sys.exit(4)
        with open(markers_path, "r", encoding="utf-8") as fh:
            markers = json.load(fh)

        chunks = split_by_markers(text, markers)
        if not chunks:
            print("ERROR: no markers matched — check marker strings", file=sys.stderr)
            sys.exit(5)

        for name, content in chunks.items():
            out_path = out_dir / f"{name}.txt"
            out_path.write_text(content, encoding="utf-8")
            print(f"  {name}: {len(content):,} chars → {out_path}")

    else:
        chunks = split_by_size(text, args.chunk_size)
        for i, content in enumerate(chunks, start=1):
            out_path = out_dir / f"chunk_{i:03d}.txt"
            out_path.write_text(content, encoding="utf-8")
            print(f"  chunk_{i:03d}: {len(content):,} chars → {out_path}")

    print(f"\nDone — {len(chunks)} chunk(s) written to {out_dir}")


if __name__ == "__main__":
    main()
