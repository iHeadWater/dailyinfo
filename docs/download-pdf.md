# PDF Download (Institutional Access)

This workflow downloads academic papers through institutional access (Dalian University of Technology SSO) using Claude Code + Playwright browser automation.

> **Dependency**: The download step requires Claude Code. It cannot run as a standalone CLI command — the browser orchestration happens through Claude Code's Playwright MCP plugin.

## Overview

```text
User types: /download-pdf <DOI>
  → Claude Code opens standalone Chromium
  → Navigates publisher site
  → Handles institutional login (human completes DUT SSO when needed)
  → Triggers PDF download via Chrome native download manager
  → Verifies PDF with download_pdf.py verify
  → [Optional] Syncs to Zotero via zotero_sync.py
```

## One-Time Setup

### 1. Enable the Playwright plugin

In `~/.claude/settings.json`:

```json
"enabledPlugins": {
  "playwright@claude-plugins-official": true
}
```

This provides `mcp__plugin_playwright_playwright__*` MCP tools (standalone Chromium).

### 2. Install Chromium

```bash
npx playwright install chromium
```

### 3. Install project dependencies

```bash
uv sync --python python3
uv pip install -e .
uv pip install pyzotero   # for Zotero sync
```

### 4. Configure environment

In `.env`:

```env
# Required for Zotero sync
ZOTERO_API_KEY=<key>          # from https://www.zotero.org/settings/keys
ZOTERO_LIBRARY_ID=<id>        # numeric user ID
GDRIVE_PAPERS_PATH=<path>     # local GDrive path (ZotMoov target)
```

## Usage

Open the project in Claude Code, then:

```
/download-pdf 10.1038/s41586-026-10704-3
```

The agent opens a Chromium window. You only interact when:
- **Cloudflare challenge** appears (Wiley/AGU) — click the checkbox in the browser
- **Institutional login** is needed — complete DUT SSO in the browser

## Supported Publishers

| Publisher | Access Method | Download Pattern |
|-----------|--------------|------------------|
| **Nature** | OA: direct | Click "Download PDF" → Chrome native download |
| **Nature** | Institutional | WAYF → DUT SSO → click "Download PDF" |
| **Wiley** | Any | `pdfdirect?download=true` URL triggers native download |
| **AGU** | Any | Same as Wiley (`agupubs.onlinelibrary.wiley.com`) |
| **Elsevier (ScienceDirect)** | SSO auto-detect | Navigate → "View PDF" → new tab → download |

## How It Works

### Nature / Springer

Nature articles use a simple pattern:

```
Article: https://www.nature.com/articles/s41586-026-10704-3
PDF:     https://www.nature.com/articles/s41586-026-10704-3.pdf
```

- **OA papers**: "Download PDF" link on article page triggers Chrome native download.
- **Institutional papers**: The browser is redirected to `wayf.springernature.com` (WAYF). The agent selects "Dalian University of Technology", and the user completes DUT SSO login. WAYF/SSO cookies persist in the browser profile across Claude Code restarts.

### Wiley / AGU

Wiley uses Cloudflare Turnstile anti-bot protection. Once the user passes the challenge manually, the agent uses the `pdfdirect` endpoint:

```
https://onlinelibrary.wiley.com/doi/pdfdirect/<DOI>?download=true
https://agupubs.onlinelibrary.wiley.com/doi/pdfdirect/<DOI>?download=true
```

This triggers Chrome's native download directly — no PDF viewer, no base64 encoding.
Cloudflare cookies persist within the session, so subsequent Wiley/AGU papers skip the challenge.

### Anti-patterns (DO NOT USE)

| Pattern | Why it breaks |
|---------|---------------|
| `browser_evaluate` + `fetch()` + `readAsDataURL()` | Base64 > MCP transport limit → crash |
| `browser_run_code` + `require('fs')` | `require` not defined in MCP runtime |
| `mcp__plugin_ecc_playwright__*` | Needs Chrome extension bridge, not standalone |
| `python scripts/zotero_sync.py` | Use `uv run python` — conda Python lacks dependencies |

## Commands

```bash
# Resolve and classify input
python scripts/download_pdf.py doi <doi>
python scripts/download_pdf.py classify <input>
python scripts/download_pdf.py detect <url>

# Compute output path
python scripts/download_pdf.py output-path <doi>

# Decode base64 (legacy, for small PDFs only)
python scripts/download_pdf.py decode <input> <output>

# Verify PDF
python scripts/download_pdf.py verify <pdf>
```

## Failure Modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| Page title "请稍候…" / 403 errors | Cloudflare Turnstile blocking | User passes challenge in browser |
| "Access through your institution" | Not logged in / session expired | Follow WAYF → DUT SSO flow |
| "Buy or subscribe" / no PDF link | Journal not in DUT subscription | Skip — download from other source |
| MCP crash on large PDF | base64 encoding overflow | Use Chrome native download, not evaluate |
| `ModuleNotFoundError: pyzotero` | Wrong Python (`python` vs `uv run python`) | Always use `uv run python` |

## Related

- [Zotero Sync (linked_file)](zotero-sync.md)
- [Agent Skill Definition](../skills/download-pdf/SKILL.md)
- [ADR-001: linked_file Design](decisions/adr-001-zotero-linked-file-sync.md)
- [CLI Reference](cli.md)
