#!/usr/bin/env python3
"""PDF download helper for academic papers via institutional access.

Provides publisher detection, DOI resolution, base64 decoding, and PDF
verification. Used by the ``download-pdf`` Claude Code skill and the
``dailyinfo download-pdf`` CLI command.

Usage (standalone)::

    python scripts/download_pdf.py detect <url>
    python scripts/download_pdf.py decode <input_file> <output_path>
    python scripts/download_pdf.py verify <pdf_path>
    python scripts/download_pdf.py doi <doi>
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class Publisher(Enum):
    """Academic publisher identifiers."""

    ELSEVIER = auto()  # ScienceDirect
    SPRINGER = auto()  # Springer Link
    WILEY = auto()  # Wiley Online Library
    TAYLOR_FRANCIS = auto()  # Taylor & Francis Online
    AGU = auto()  # AGU/Wiley
    UNKNOWN = auto()


@dataclass
class PdfMetadata:
    """Metadata extracted from a downloaded PDF."""

    title: str = ""
    authors: list[str] = field(default_factory=list)
    doi: str = ""
    pages: int = 0
    version: str = ""
    file_size_bytes: int = 0

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "authors": self.authors,
            "doi": self.doi,
            "pages": self.pages,
            "version": self.version,
            "file_size_bytes": self.file_size_bytes,
        }


# ---------------------------------------------------------------------------
# Publisher detection
# ---------------------------------------------------------------------------

# Order matters: more specific patterns must come before general ones.
# e.g. "agupubs.onlinelibrary.wiley.com" before "wiley.com"
PUBLISHER_PATTERNS: dict[str, Publisher] = {
    "sciencedirect.com": Publisher.ELSEVIER,
    "elsevier.com": Publisher.ELSEVIER,
    "nature.com": Publisher.SPRINGER,  # Nature is part of Springer Nature
    "link.springer.com": Publisher.SPRINGER,
    "springer.com": Publisher.SPRINGER,
    "agupubs.onlinelibrary.wiley.com": Publisher.AGU,
    "onlinelibrary.wiley.com": Publisher.WILEY,
    "wiley.com": Publisher.WILEY,
    "tandfonline.com": Publisher.TAYLOR_FRANCIS,
}


def detect_publisher(url: str) -> Publisher:
    """Detect the academic publisher from a URL.

    Returns:
        The matching Publisher enum value, or Publisher.UNKNOWN.

    Example:
        >>> detect_publisher("https://www.sciencedirect.com/science/article/pii/S0022169424018675")
        <Publisher.ELSEVIER: 1>
    """
    for pattern, publisher in PUBLISHER_PATTERNS.items():
        if pattern in url:
            return publisher
    return Publisher.UNKNOWN


# ---------------------------------------------------------------------------
# DOI resolution
# ---------------------------------------------------------------------------

DOI_PREFIX = "https://doi.org/"


def normalize_doi(doi: str) -> str:
    """Strip common DOI prefixes and whitespace.

    Accepts::

        "10.1016/j.jhydrol.2024.132471"
        "https://doi.org/10.1016/j.jhydrol.2024.132471"
        "doi:10.1016/j.jhydrol.2024.132471"

    Returns bare DOI (e.g. "10.1016/j.jhydrol.2024.132471").
    """
    doi = doi.strip()
    doi = re.sub(r"^https?://doi\.org/", "", doi)
    doi = re.sub(r"^doi:", "", doi)
    return doi.strip()


def doi_to_url(doi: str) -> str:
    """Convert a DOI to its resolver URL."""
    return f"{DOI_PREFIX}{normalize_doi(doi)}"


def pii_to_sciencedirect_url(pii: str) -> str:
    """Convert a PII to a ScienceDirect article URL.

    PII format: ``S0022169424018675`` (starts with 'S', ~17 chars).
    """
    pii = pii.strip().upper()
    return f"https://www.sciencedirect.com/science/article/pii/{pii}"


def classify_input(user_input: str) -> dict:
    """Classify user input as DOI, PII, URL, or unknown.

    Returns:
        dict with keys:
        - ``type``: "doi", "pii", "url", "unknown"
        - ``normalized``: cleaned value
        - ``url``: article page URL (if resolvable without HTTP)
    """
    user_input = user_input.strip()

    # DOI with full URL prefix — check before generic URL
    doi_match = re.match(r"^https?://doi\.org/(10\.\d{4,}/.+)", user_input)
    if doi_match:
        return {
            "type": "doi",
            "normalized": doi_match.group(1),
            "url": user_input,
        }

    # Direct URL
    if user_input.startswith(("http://", "https://")):
        return {"type": "url", "normalized": user_input, "url": user_input}

    # DOI (starts with 10.)
    if re.match(r"^10\.\d{4,}/", user_input):
        return {
            "type": "doi",
            "normalized": user_input,
            "url": doi_to_url(user_input),
        }

    # DOI with doi: prefix
    doi_prefix_match = re.match(r"^doi:(10\.\d{4,}/.+)", user_input)
    if doi_prefix_match:
        return {
            "type": "doi",
            "normalized": doi_prefix_match.group(1),
            "url": doi_to_url(doi_prefix_match.group(1)),
        }

    # PII (starts with S followed by digits)
    if re.match(r"^S\d{10,}$", user_input, re.IGNORECASE):
        return {
            "type": "pii",
            "normalized": user_input.upper(),
            "url": pii_to_sciencedirect_url(user_input),
        }

    return {"type": "unknown", "normalized": user_input, "url": ""}


# ---------------------------------------------------------------------------
# Output path resolution
# ---------------------------------------------------------------------------

DEFAULT_PDF_ROOT = Path.home() / ".myagentdata" / "dailyinfo" / "papers"


def default_pdf_dir() -> Path:
    """Default directory for downloaded PDFs.

    Respects ``DAILYINFO_DATA_ROOT`` if set, otherwise uses
    ``~/.myagentdata/dailyinfo/papers/``.
    """
    data_root = os.environ.get("DAILYINFO_DATA_ROOT", "")
    if data_root:
        return Path(data_root) / "papers"
    return DEFAULT_PDF_ROOT


def slug_from_doi(doi: str) -> str:
    """Generate a filename-safe slug from a DOI.

    Example: ``10.1016/j.jhydrol.2024.132471`` → ``j.jhydrol.2024.132471``
    """
    doi = normalize_doi(doi)
    # Drop the publisher prefix (everything before the first '/')
    if "/" in doi:
        doi = doi.split("/", 1)[1]
    # Replace remaining non-alphanumeric chars with underscores
    slug = re.sub(r"[^a-zA-Z0-9._-]", "_", doi)
    return slug


def output_path_for(input_ref: str, output_dir: str | Path | None = None) -> Path:
    """Compute the default output path for a given input reference.

    If the input contains a DOI, the filename is derived from it.
    Otherwise, falls back to a timestamped name.

    Args:
        input_ref: DOI, PII, or URL.
        output_dir: Override output directory. Defaults to ``default_pdf_dir()``.

    Returns:
        Full output path ending in ``.pdf``.
    """
    result = classify_input(input_ref)
    if result["type"] in ("doi", "pii"):
        slug = slug_from_doi(result["normalized"])
    else:
        # URL or unknown — use a time-based fallback
        from datetime import datetime

        slug = f"paper_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    out_dir = Path(output_dir) if output_dir else default_pdf_dir()
    return out_dir / f"{slug}.pdf"


# ---------------------------------------------------------------------------
# Base64 decoding
# ---------------------------------------------------------------------------


def _clean_base64(raw_text: str) -> str:
    """Extract and clean a base64 string from raw tool output.

    Handles:
    - ``### Result\\n`` header prefix from MCP tool output
    - JSON-wrapped base64 (``{"base64": "data:application/pdf;base64,..."}``)
    - Data URL prefix (``data:application/pdf;base64,...``)
    - Line-wrapped base64 (newlines inside the base64 string)
    - Non-ASCII artifacts in the base64 stream
    """
    text = raw_text

    # Strip MCP tool output wrapper
    if "### Result" in text:
        _, _, text = text.partition("\n")

    # Try JSON extraction first
    text_stripped = text.strip()
    if text_stripped.startswith("{"):
        try:
            data = json.loads(text_stripped)
            if isinstance(data, dict) and "base64" in data:
                text = data["base64"]
        except json.JSONDecodeError:
            pass

    # Strip data URL prefix
    if "," in text[:200]:
        # Only strip data URI prefix (data:...;base64,)
        prefix_end = text.index(",") + 1
        text = text[prefix_end:]

    # Remove whitespace and non-base64 chars
    text = re.sub(r"[^A-Za-z0-9+/=]", "", text)

    return text


def decode_base64_file(input_path: str | Path, output_path: str | Path) -> int:
    """Decode a base64-encoded tool output file to a PDF.

    The input file can be:
    1. A JSON file with ``{"base64": "data:application/pdf;base64,..."}``
    2. A raw text file with base64-encoded PDF data (possibly with
       ``### Result`` header wrapper).

    Returns:
        Number of bytes written.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    raw_bytes = input_path.read_bytes()
    text = raw_bytes.decode("utf-8", errors="ignore")

    b64_clean = _clean_base64(text)

    pdf_bytes = __import__("base64").b64decode(b64_clean)
    output_path.write_bytes(pdf_bytes)
    return len(pdf_bytes)


# ---------------------------------------------------------------------------
# PDF verification and metadata extraction
# ---------------------------------------------------------------------------


def verify_pdf(pdf_path: str | Path) -> PdfMetadata:
    """Verify a PDF file and extract its metadata.

    Returns:
        PdfMetadata with extracted fields. Raises ValueError if the file
        is not a valid PDF.
    """
    pdf_path = Path(pdf_path)

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    file_size = pdf_path.stat().st_size
    if file_size == 0:
        raise ValueError("PDF file is empty")

    content = pdf_path.read_bytes()

    # Check PDF header
    if not content.startswith(b"%PDF-"):
        raise ValueError(f"Not a valid PDF (missing %PDF- header): {pdf_path}")

    metadata = PdfMetadata(file_size_bytes=file_size)

    # PDF version
    ver_match = re.search(rb"%PDF-(\d+\.\d+)", content[:100])
    if ver_match:
        metadata.version = ver_match.group(1).decode("ascii")

    # Page count (approximate from /Type /Page entries, not /Pages for kids)
    metadata.pages = len(re.findall(rb"/Type\s*/Page[^s]", content))

    # Title extraction
    # Try PDF metadata first: /Title (...)
    title_match = re.search(rb"/Title\s*<([0-9A-Fa-f]+)>", content)
    if title_match:
        try:
            metadata.title = bytes.fromhex(title_match.group(1).decode("ascii")).decode(
                "utf-16-be", errors="replace"
            )
        except (ValueError, UnicodeDecodeError):
            pass

    if not metadata.title:
        title_match = re.search(rb"/Title\s*\(([^)]*)\)", content)
        if title_match:
            metadata.title = title_match.group(1).decode("latin-1", errors="replace")

    # Author extraction
    author_match = re.search(rb"/Author\s*<([0-9A-Fa-f]+)>", content)
    if author_match:
        try:
            author_str = bytes.fromhex(author_match.group(1).decode("ascii")).decode(
                "utf-16-be", errors="replace"
            )
            metadata.authors = [a.strip() for a in author_str.split(";")]
        except (ValueError, UnicodeDecodeError):
            pass

    if not metadata.authors:
        author_match = re.search(rb"/Author\s*\(([^)]*)\)", content)
        if author_match:
            author_str = author_match.group(1).decode("latin-1", errors="replace")
            metadata.authors = [a.strip() for a in author_str.split(";")]

    # DOI extraction (from dc:identifier or prism:doi)
    doi_match = re.search(rb"/doi\s*\(([^)]*)\)", content)
    if doi_match:
        metadata.doi = doi_match.group(1).decode("latin-1", errors="replace")

    return metadata


def format_metadata(metadata: PdfMetadata) -> str:
    """Format PdfMetadata as a human-readable table."""
    lines = [
        f"  Title:   {metadata.title}",
        f"  Author:  {', '.join(metadata.authors) if metadata.authors else 'N/A'}",
        f"  DOI:     {metadata.doi or 'N/A'}",
        f"  Pages:   {metadata.pages}",
        f"  Version: PDF {metadata.version}" if metadata.version else "  Version: N/A",
        f"  Size:    {metadata.file_size_bytes:,} bytes ({metadata.file_size_bytes / 1024:.0f} KB)",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Console script (Entry point)
# ---------------------------------------------------------------------------


def main():
    """Console script entry point for download_pdf.py.

    Same signature as Click but uses plain argparse to avoid dependency
    issues when running standalone.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="PDF download helper for academic papers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Commands:
  detect <url>            Detect publisher from URL
  doi <doi>               Resolve DOI to article URL and publisher
  decode <input> <output> Decode base64 tool output to PDF
  verify <pdf>            Verify PDF and extract metadata""",
    )
    parser.add_argument(
        "command", choices=["detect", "doi", "decode", "verify", "classify", "output-path"]
    )
    parser.add_argument("args", nargs="*", help="Command arguments")

    args = parser.parse_args()

    if args.command == "detect" and len(args.args) >= 1:
        publisher = detect_publisher(args.args[0])
        print(f"Publisher: {publisher.name}")

    elif args.command == "classify" and len(args.args) >= 1:
        result = classify_input(args.args[0])
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "doi" and len(args.args) >= 1:
        result = classify_input(args.args[0])
        print(f"Type:  {result['type']}")
        print(f"DOI:   {result['normalized']}")
        print(f"URL:   {result['url']}")
        if result["url"]:
            pub = detect_publisher(result["url"])
            print(f"Publisher: {pub.name}")

    elif args.command == "decode" and len(args.args) >= 2:
        input_path = args.args[0]
        output_path = args.args[1]
        size = decode_base64_file(input_path, output_path)
        print(f"Decoded {size:,} bytes → {output_path}")

    elif args.command == "output-path" and len(args.args) >= 1:
        path = output_path_for(args.args[0])
        print(str(path))

    elif args.command == "verify" and len(args.args) >= 1:
        try:
            metadata = verify_pdf(args.args[0])
            print("OK Valid PDF")
            print(format_metadata(metadata))
        except (FileNotFoundError, ValueError) as e:
            print(f"FAIL {e}", file=sys.stderr)
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
