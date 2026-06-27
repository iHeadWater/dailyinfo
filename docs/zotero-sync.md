# Zotero Sync (linked_file)

Sync downloaded PDFs to Zotero as linked_file attachments — zero Zotero cloud storage used.

## Overview

```text
PDF downloaded and verified
  → zotero_sync.py <pdf> <doi>
  → Copy PDF to GDRIVE_PAPERS_PATH
  → Fetch metadata from Crossref
  → Create Zotero parent item via pyzotero Web API
  → Create linked_file attachment (attachments:<filename> scheme)
```

## Prerequisites

### Environment Variables

```env
ZOTERO_API_KEY=<key>           # Create at https://www.zotero.org/settings/keys
ZOTERO_LIBRARY_ID=6716904      # Numeric user ID
GDRIVE_PAPERS_PATH=G:\...\Zotero_Papers  # Local GDrive path
```

### Zotero Desktop Configuration

Zotero → Preferences → Advanced → Files and Folders:
- **Linked Attachment Base Directory**: same path as `GDRIVE_PAPERS_PATH`

### Dependencies

```bash
uv pip install pyzotero
```

## Usage

```bash
# Sync with JSON output
uv run python scripts/zotero_sync.py <pdf_path> <doi> --json

# Dry run (preview only, no API writes)
uv run python scripts/zotero_sync.py <pdf_path> <doi> --dry-run
```

> ⚠️ Use `uv run python`, not bare `python`. Conda Python doesn't have pyzotero.

## What It Does

1. **Copy PDF** to `$GDRIVE_PAPERS_PATH/{doi-slug}.pdf`
2. **Fetch metadata** from Crossref API (title, authors, journal, volume, issue, pages, ISSN, publisher, abstract)
3. **Create parent item** in Zotero via pyzotero Web API
4. **Create linked_file attachment** with portable `attachments:<filename>` path

## Why linked_file

| Attachment | Storage | Quota |
|-----------|---------|-------|
| `imported_file` | Zotero cloud | 300 MB |
| `linked_file` | Google Drive | Unlimited |

Institutional PDFs are 10-90 MB each — Zotero's 300 MB fills in ~5 papers.
See [ADR-001](decisions/adr-001-zotero-linked-file-sync.md).

## Attachment Scheme

```
path: "attachments:s41586-026-10704-3.pdf"
```

Zotero resolves `attachments:` against "Linked Attachment Base Directory". Cross-machine portable.

## Failure Handling

| Scenario | Behavior |
|----------|----------|
| `ZOTERO_API_KEY` / `ZOTERO_LIBRARY_ID` not set | Error — PDF already saved locally |
| `GDRIVE_PAPERS_PATH` not set | Error — PDF already saved locally |
| Crossref metadata fails | Falls back to PDF-embedded metadata |
| Zotero API write fails | PDF in GDrive — user can manually link |
| `UnicodeEncodeError: 'gbk'` | Sync succeeds; print-only error on Windows |

## Related

- [PDF Download](download-pdf.md)
- [ADR-001](decisions/adr-001-zotero-linked-file-sync.md)
- [Agent Skill](../skills/download-pdf/SKILL.md)
