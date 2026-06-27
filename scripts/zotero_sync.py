#!/usr/bin/env python3
"""Sync a downloaded PDF to Zotero as a linked_file attachment.

Copies the PDF to the Google Drive papers folder (ZotMoov-managed), fetches
rich metadata from Crossref, and creates a Zotero item with a linked_file
attachment via pyzotero.  Zotero cloud storage is **not** used — the PDF lives
in the GDrive folder and Zotero stores only a pointer.

Usage::

    python scripts/zotero_sync.py <pdf_path> <doi> [--dry-run] [--json]

Environment variables::

    ZOTERO_API_KEY      Zotero Web API key (https://www.zotero.org/settings/keys)
    ZOTERO_LIBRARY_ID   Numeric Zotero user library ID
    GDRIVE_PAPERS_PATH  Local path to GDrive papers folder (ZotMoov target)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

# Make scripts/ importable so we can import from download_pdf.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from download_pdf import normalize_doi, slug_from_doi, verify_pdf  # noqa: E402

CROSSREF_UA = "dailyinfo-zotero-sync/1.0 (mailto:wenyuouyang@outlook.com)"

# Load .env so users don't need to export vars manually.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
if _ENV_FILE.exists():
    from dotenv import load_dotenv
    load_dotenv(_ENV_FILE)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def get_zotero_client():
    """Create a pyzotero client from environment variables.

    Returns:
        :class:`pyzotero.zotero.Zotero`

    Raises:
        ValueError: if required env vars are missing.
    """
    api_key = os.environ.get("ZOTERO_API_KEY", "")
    library_id = os.environ.get("ZOTERO_LIBRARY_ID", "")

    if not api_key or not library_id:
        raise ValueError(
            "ZOTERO_API_KEY and ZOTERO_LIBRARY_ID must be set in environment.\n"
            "Get an API key at https://www.zotero.org/settings/keys"
        )

    from pyzotero import zotero

    return zotero.Zotero(library_id, "user", api_key)


def get_gdrive_papers_path() -> Path:
    """Get the Google Drive papers folder path from env.

    Raises:
        ValueError: if ``GDRIVE_PAPERS_PATH`` is not set.
    """
    path = os.environ.get("GDRIVE_PAPERS_PATH", "")
    if not path:
        raise ValueError(
            "GDRIVE_PAPERS_PATH must be set in environment.\n"
            "This should point to the Google Drive folder where ZotMoov "
            "stores linked PDFs."
        )
    return Path(path)


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------


def _copy_to_gdrive(pdf_path: str | Path, gdrive_dir: str | Path, dest_filename: str) -> Path:
    """Copy PDF to the Google Drive papers folder.

    Args:
        pdf_path: Path to the source PDF.
        gdrive_dir: Google Drive papers directory.
        dest_filename: Destination filename (e.g. ``j.jhydrol.2024.132471.pdf``).

    Returns:
        Destination path.
    """
    src = Path(pdf_path)
    dst = Path(gdrive_dir) / dest_filename
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


# ---------------------------------------------------------------------------
# Metadata enrichment
# ---------------------------------------------------------------------------


def _fetch_crossref_metadata(doi: str) -> dict | None:
    """Fetch rich metadata from Crossref for a DOI.

    Returns:
        The ``message`` dict from Crossref API response, or ``None`` on failure.
    """
    url = f"https://api.crossref.org/works/{doi}"
    req = urllib.request.Request(url, headers={"User-Agent": CROSSREF_UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()).get("message")
    except Exception:
        return None


def _build_zotero_item(
    doi: str,
    crossref_data: dict | None,
    pdf_metadata,
) -> tuple[dict, dict]:
    """Build a Zotero item dict from Crossref metadata, falling back to PDF metadata.

    Args:
        doi: Normalized DOI.
        crossref_data: Crossref ``message`` dict, or ``None``.
        pdf_metadata: :class:`PdfMetadata` from :func:`verify_pdf`.

    Returns:
        ``(item_data, extra_fields)`` tuple ready for pyzotero.
    """
    item: dict = {}
    extra: dict = {}

    if crossref_data:
        # -- Title
        titles = crossref_data.get("title", [])
        item["title"] = titles[0] if titles else (pdf_metadata.title or "")

        # -- Creators
        creators = []
        for a in crossref_data.get("author", []):
            creators.append(
                {
                    "creatorType": "author",
                    "firstName": a.get("given", ""),
                    "lastName": a.get("family", ""),
                }
            )
        if creators:
            item["creators"] = creators

        # -- Abstract
        abstract = crossref_data.get("abstract")
        if abstract:
            item["abstractNote"] = abstract

        # -- Date (prefer published-print, then issued/created/deposited)
        date_parts = (
            crossref_data.get("published-print")
            or crossref_data.get("created")
            or crossref_data.get("issued")
            or crossref_data.get("deposited")
        )
        if date_parts and "date-parts" in date_parts:
            dp = date_parts["date-parts"][0]
            if len(dp) >= 3:
                item["date"] = f"{dp[0]:04d}-{dp[1]:02d}-{dp[2]:02d}"
            elif len(dp) >= 1:
                item["date"] = str(dp[0])

        # -- Item type
        cr_type = crossref_data.get("type", "")
        item["itemType"] = "journalArticle" if cr_type == "journal-article" else "journalArticle"

        # -- Journal / container info → dedicated Zotero fields
        container = crossref_data.get("container-title", [])
        if container:
            item["publicationTitle"] = container[0]
        vol = crossref_data.get("volume")
        if vol:
            item["volume"] = str(vol)
        issue = crossref_data.get("issue")
        if issue:
            item["issue"] = str(issue)
        page = crossref_data.get("page")
        if page:
            item["pages"] = str(page)
        issn = crossref_data.get("ISSN", [])
        if issn:
            item["ISSN"] = issn[0] if isinstance(issn, list) else str(issn)
        publisher = crossref_data.get("publisher")
        if publisher:
            item["publisher"] = publisher

    else:
        # Crossref unavailable — fall back to PDF-embedded metadata
        item["title"] = pdf_metadata.title or ""
        if pdf_metadata.authors:
            creators = []
            for author in pdf_metadata.authors:
                parts = author.rsplit(" ", 1) if " " in author else [author, ""]
                creators.append(
                    {
                        "creatorType": "author",
                        "firstName": parts[0] if len(parts) > 1 else "",
                        "lastName": parts[-1] if len(parts) > 1 else author,
                    }
                )
            item["creators"] = creators
        item["itemType"] = "journalArticle"

    # -- Common fields
    item["DOI"] = doi
    item["url"] = f"https://doi.org/{doi}"
    item["language"] = "en"

    return item, extra


# ---------------------------------------------------------------------------
# Zotero item creation
# ---------------------------------------------------------------------------


def _create_zotero_item(
    zot,
    item_data: dict,
    extra_fields: dict,
    attachment_filename: str,
) -> tuple[str, str]:
    """Create a Zotero parent item with a linked_file attachment.

    Args:
        zot: :class:`pyzotero.zotero.Zotero` client.
        item_data: Item dict from :func:`_build_zotero_item`.
        extra_fields: Extra fields to write into the ``extra`` field.
        attachment_filename: Filename for the linked_file (e.g. ``paper.pdf``).

    Returns:
        ``(parent_key, attachment_key)`` — 8-character Zotero item keys.

    Raises:
        RuntimeError: if the Zotero API returns a failure.
    """
    # Pop itemType before template to avoid overwriting
    item_type = item_data.pop("itemType", "journalArticle")
    tmpl = zot.item_template(item_type)
    tmpl.update(item_data)

    # Format extra fields as key: value lines
    if extra_fields:
        extra_parts = [f"{k}: {v}" for k, v in extra_fields.items()]
        tmpl["extra"] = "\n".join(extra_parts)

    # Create parent item
    resp = zot.create_items([tmpl])
    created = resp.get("success", {})
    if not created:
        failed = resp.get("failed", {})
        err_msg = failed.get("0", {}).get("message", str(resp))
        raise RuntimeError(f"Failed to create Zotero item: {err_msg}")

    parent_key = list(created.values())[0]

    # Create linked_file attachment using Zotero's portable attachments: scheme.
    # Zotero resolves this against the user's "Linked Attachment Base Directory"
    # preference (set in Zotero → Preferences → Advanced → Files and Folders).
    attach_tmpl = zot.item_template("attachment", "linked_file")
    attach_tmpl["title"] = attachment_filename
    attach_tmpl["parentItem"] = parent_key
    attach_tmpl["path"] = f"attachments:{attachment_filename}"

    attach_resp = zot.create_items([attach_tmpl])
    attach_created = attach_resp.get("success", {})
    attach_key = list(attach_created.values())[0] if attach_created else ""

    return parent_key, attach_key


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    """Console script entry point — argparse CLI matching ``download_pdf.py`` style."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Sync a downloaded PDF to Zotero as a linked_file attachment.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python scripts/zotero_sync.py paper.pdf 10.1016/j.jhydrol.2024.132471
  python scripts/zotero_sync.py paper.pdf 10.1016/j.jhydrol.2024.132471 --dry-run
  python scripts/zotero_sync.py paper.pdf 10.1016/j.jhydrol.2024.132471 --json""",
    )
    parser.add_argument("pdf_path", help="Path to the downloaded PDF file")
    parser.add_argument(
        "doi", help="DOI of the paper (e.g. 10.1016/j.jhydrol.2024.132471)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without creating Zotero item"
    )
    parser.add_argument(
        "--json", action="store_true", help="Output result as JSON"
    )

    args = parser.parse_args()

    # -- Validate inputs --------------------------------------------------
    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(f"Error: PDF not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    doi = normalize_doi(args.doi)
    slug = slug_from_doi(doi)
    dest_filename = f"{slug}.pdf"

    try:
        gdrive_dir = get_gdrive_papers_path()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)

    # -- Verify PDF (extracts embedded metadata for fallback) --------------
    try:
        pdf_meta = verify_pdf(pdf_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: Invalid PDF: {e}", file=sys.stderr)
        sys.exit(3)

    # -- Enrich metadata via Crossref -------------------------------------
    crossref_data = _fetch_crossref_metadata(doi)
    item_data, extra_fields = _build_zotero_item(doi, crossref_data, pdf_meta)

    # -- Dry-run: preview only --------------------------------------------
    if args.dry_run:
        print("[dry-run] Would:")
        print(f"  Copy PDF to: {gdrive_dir / dest_filename}")
        print(f"  DOI:         {doi}")
        print(f"  Type:        {item_data.get('itemType', '?')}")
        print(f"  Title:       {item_data.get('title', '')[:80]}")
        print(f"  Authors:     {len(item_data.get('creators', []))}")
        print(f"  Date:        {item_data.get('date', '')}")
        print(f"  Abstract:    {'yes' if item_data.get('abstractNote') else 'no'}")
        print(f"  Extra:       {list(extra_fields.keys())}")
        print(f"  Attachment:  linked_file → attachments:{dest_filename}")
        return

    # -- Copy PDF to Google Drive folder ----------------------------------
    try:
        gdrive_path = _copy_to_gdrive(pdf_path, gdrive_dir, dest_filename)
    except OSError as e:
        print(f"Error: Failed to copy PDF to GDrive: {e}", file=sys.stderr)
        sys.exit(4)

    # -- Create Zotero item + linked_file attachment ----------------------
    try:
        zot = get_zotero_client()
        parent_key, attach_key = _create_zotero_item(
            zot, item_data, extra_fields, dest_filename
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(5)

    # -- Report -----------------------------------------------------------
    result = {
        "ok": True,
        "zotero_key": parent_key,
        "attachment_key": attach_key,
        "doi": doi,
        "title": item_data.get("title", ""),
        "gdrive_path": str(gdrive_path),
        "attachment_path": f"attachments:{dest_filename}",
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=True))
    else:
        print("OK — Zotero item created")
        print(f"  Key:        {parent_key}")
        print(f"  Title:      {item_data.get('title', '')}")
        print(f"  DOI:        {doi}")
        print(f"  Attachment: linked_file → attachments:{dest_filename}")
        print(f"  PDF copied: {gdrive_path}")


if __name__ == "__main__":
    main()
