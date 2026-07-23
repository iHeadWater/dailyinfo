# ADR-001: Use linked_file attachments for Zotero PDF sync

## Status
Accepted

## Date
2026-06-24

## Context

The `download-pdf` skill downloads academic papers (5–90 MB each) through institutional
access (Dalian University of Technology SSO) using Playwright browser automation. After
download, users want the PDF synced to their Zotero library. The user uses ZotMoov to
manage linked PDFs in a Google Drive folder, bypassing Zotero's cloud storage entirely.

Options considered for the sync path:

- **zotero-mcp `zotero_add_from_file`**: one MCP call, zero new code. But creates
  `imported_file` attachments stored in Zotero cloud — the 300 MB quota fills up after
  3–6 papers.
- **zotero-mcp `zotero_add_by_doi` + manual link**: metadata creation via MCP, then
  manual drag-and-drop in Zotero desktop for the attachment. Partially automated but
  not fully agentic.
- **pyzotero + `linked_file`**: custom Python script that copies PDF to GDrive, fetches
  CrossRef metadata, creates Zotero parent item, and attaches a linked_file pointer
  using Zotero's portable `attachments:` scheme. Requires Zotero Web API credentials.

## Decision

Use **pyzotero with `linked_file` attachments** (`scripts/zotero_sync.py`).

The script:
1. Copies PDF to `$GDRIVE_PAPERS_PATH/{doi-slug}.pdf`
2. Fetches rich metadata from Crossref API (title, authors, abstract, journal, volume, pages, ISSN, publisher)
3. Creates a Zotero parent item via pyzotero Web API
4. Creates a linked_file child attachment with path `attachments:{filename}`

Zotero resolves the `attachments:` prefix against the user's "Linked Attachment Base
Directory" preference (set in Zotero Preferences → Advanced → Files and Folders).
The PDF lives in Google Drive, managed by ZotMoov.

## Alternatives Considered

### zotero_add_from_file (MCP)

- Pros: One MCP call, no new Python code, no API key config
- Cons: Creates `imported_file` attachment stored in Zotero cloud (300 MB quota). A single 47 MB paper fills 15% of the quota. No `linked_file` support.
- Rejected: Quota fills too fast for institutional PDF sizes

### GDrive + rclone upload + pyzotero linked_file (myopenclaw pattern)

- Pros: Proven in production in sibling project; rclone handles GDrive upload robustly
- Cons: Requires rclone OAuth config; relies on Docker container for execution; overkill for a local PC workflow
- Rejected: The user's PC already has Google Drive mounted locally; rclone adds unnecessary complexity

### zotero-mcp metadata + Zotero desktop manual link

- Pros: Minimal code; uses existing MCP tools
- Cons: Not fully automated; requires human to drag PDF into Zotero desktop
- Rejected: The point of agent-operated download-pdf is to eliminate manual steps

## Consequences

- zotero-mcp is still used for **collection discovery** (`zotero_search_collections`) and **collection filing** (`zotero_manage_collections`) — the Python script only handles what MCP cannot (linked_file attachment creation)
- Three new env vars required: `ZOTERO_API_KEY`, `ZOTERO_LIBRARY_ID`, `GDRIVE_PAPERS_PATH`
- `pyzotero>=1.5` added to the `zotero` optional dependency group
- All journal metadata fields (`publicationTitle`, `volume`, `issue`, `pages`, `ISSN`, `publisher`) are placed in dedicated Zotero item fields, not the `extra` text blob
- The `attachments:` scheme is portable across OS and user accounts, as long as each machine's Linked Attachment Base Directory is configured correctly
