# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DailyInfo is an automated research intelligence aggregation and push system for AI for Science researchers. It collects RSS feeds, scrapes websites, and queries APIs, then uses DeepSeek V4 Pro (OpenRouter Kimi K2.5 as fallback) to generate Chinese-language summaries pushed to Discord channels.

**Core pipeline**: FreshRSS collection -> AI summary generation (markdown to disk) -> Discord push + archive

**Design principles**: Configuration-driven (`config/sources.json`) + idempotent CLI + external scheduling (any cron / agent runtime). Optional integration with myopenclaw is documented in `docs/agent-config.md`; dailyinfo itself has no runtime dependency on it.

## Tech Stack

- Python 3.10+, package manager: uv (primary) / pip (fallback)
- CLI: Click 8+
- RSS: FreshRSS (Docker/SQLite, `restart: always`, auto-start via myopenclaw launchd)
- AI: DeepSeek V4 Pro official API (fallback: OpenRouter `moonshotai/kimi-k2.5`)
- Push: Discord Bot API via `requests`
- Docs: MkDocs Material (GitHub Pages)
- Lint: Ruff, Format: Black, Test: pytest 8+

## Common Commands

```bash
# Install
uv sync --python python3 && uv pip install -e .
dailyinfo install                # Validate .env + create workspace dirs + install deps

# Run pipelines (idempotent - skips sources with today's briefing)
dailyinfo run                    # All 5 pipelines
dailyinfo run -p 1               # Pipeline 1: papers
dailyinfo run -p 2               # Pipeline 2: AI news
dailyinfo run -p 3               # Pipeline 3: arXiv
dailyinfo run -p 4               # Pipeline 4: code trending
dailyinfo run -p 5               # Pipeline 5: university news
dailyinfo run -f all             # Force regenerate all sources
dailyinfo run -f arxiv_cs_ai    # Force regenerate one source

# Push to Discord
dailyinfo push                   # Today's briefings
dailyinfo push -d 2026-04-22    # Specific date

# Other
dailyinfo status                 # Show today's briefing/pushed counts
dailyinfo start/stop/restart     # FreshRSS Docker container
dailyinfo logs                   # Tail execution log
dailyinfo clean-cache            # Delete FreshRSS cache files older than 24h
dailyinfo clean-cache --dry-run  # Preview what would be deleted

# Download PDFs (agent-operated, requires Playwright MCP `mcp__plugin_playwright_playwright__*`)
# Deterministic patterns per publisher — see skills/download-pdf/SKILL.md for full flow:
#   Nature (OA):       navigate → click "Download PDF" → Chrome native download
#   Nature (inst):      navigate → WAYF login (user does SSO) → click "Download PDF"
#   Wiley/AGU (all):    navigate to pdfdirect?download=true → Chrome native download
#   Cloudflare blocks:  pause, tell user to pass challenge, wait for "done"
dailyinfo download-pdf 10.1016/j.jhydrol.2024.132471           # Print download instructions for the skill
python scripts/download_pdf.py verify <pdf>                    # Verify PDF and extract metadata
python scripts/download_pdf.py detect <url>                    # Detect publisher from URL

# Sync downloaded PDF to Zotero (linked_file, zero cloud quota)
# ⚠️ MUST use `uv run python` — conda Python lacks pyzotero
uv run python scripts/zotero_sync.py <pdf> <doi> --json        # Copy to GDrive + create Zotero item
uv run python scripts/zotero_sync.py <pdf> <doi> --dry-run     # Preview without creating

# Zotero -> NotebookLM (agent-operated)
# Prefer the Claude Code slash command:
# /zotero-notebooklm water 2026-05-28 audio
# New-machine setup: docs/zotero-notebooklm.md
uv run --extra notebooklm dailyinfo zotero-brief --collection water --artifact audio --open-missing-pdfs

# Direct script execution (no install needed)
python3 scripts/run_pipelines.py [--pipeline N] [--force SOURCE|all]
python3 scripts/push_to_discord.py [--date YYYY-MM-DD]

# Testing
uv run pytest                    # All tests
uv run pytest tests/test_paths.py  # Single file

# Lint & format
ruff check .
black .

# Docs
python3 scripts/build_docs.py    # Generate pages from sources.json + README
uv run mkdocs serve              # Local preview
```

## Architecture

### Five Pipelines

| Pipeline | Sources | Output |
|----------|---------|--------|
| 1 | Papers (30+ journals, Chinese water journals via RSS + scrape/API) | `papers/` |
| 2 | AI News (smolai via RSS with deep-content) | `ai_news/` |
| 3 | arXiv CS.AI (RSS, up to 500 articles) | `arxiv/` |
| 4 | GitHub Trending (scrape), HuggingFace (API) | `code/` |
| 5 | DLUT university sites (scrape + API) | `resource/` |

Each pipeline is independent — a failure in one does not affect the others. Common processing logic (fetch → batch → AI → merge → save) is shared via `_process_regular_source()`.

### Data Flow

1. **Collection**: FreshRSS for RSS; `datasource.py` handles scraping/API
2. **Processing** (`run_pipelines.py`): Fetch -> format -> call DeepSeek AI with prompt templates -> save markdown to `~/.myagentdata/dailyinfo/briefings/{category}/`
3. **Push** (`push_to_discord.py`): Scan briefings -> POST to Discord channels -> move to `pushed/{category}/`

### DataSource Class Hierarchy

- `DataSource` (ABC) with factory `DataSource.create(config, defaults, **ctx)`
  - `RSSDataSource` - FreshRSS SQLite DB
  - `ScrapeDataSource` - HTML scraping (GitHub Trending, DLUT sites, Chinese water journals)
  - `APIDataSource` - REST API calls (HuggingFace, DLUT recruitment, Crossref)

### Key Design Patterns

- **Idempotent**: `run` skips sources with today's briefing; `push` won't re-send archived files
- **Configuration-driven**: All sources in `config/sources.json`; adding RSS/scrape/API sources requires no code changes (custom parsers need code)
- **Flat module imports**: Scripts use `from paths import ...`; `sys.path` manipulated at import time (see `cli.py` and `conftest.py`)
- **AI fallback**: 3 retries with exponential backoff (2s/5s/10s), then switches to fallback model for 2 more attempts
- **Batch splitting**: `max_articles_per_batch=10` (default); incomplete AI responses trigger recursive halving
- **Tolerant feed matching**: `resolve_feed_id` tries exact URL -> strip query params -> strip scheme+trailing slash

## Source Configuration

Sources in `config/sources.json` have types: `rss`, `api`, `scrape`. Categories: `papers`, `ai_news`, `code`, `resource`.

Defaults (all overridable per-source): `lookback_hours: 24`, `max_articles_per_batch: 10`, `model: deepseek-v4-pro`.

Prompt templates under `prompt_templates` key use placeholders: `{count}`, `{display_name}`, `{article_list}`, `{items}`, `{date}`, `{content}`.

API sources can specify a `parser` key (e.g. `"parser": "crossref"`) to select a custom response parser.

Scrape sources with custom parsing need matching `if self.name == "..."` dispatch in `ScrapeDataSource.fetch()`.

## Environment Variables

Required: `DEEPSEEK_API_KEY`, `DISCORD_BOT_TOKEN`
Optional: `OPENROUTER_API_KEY` (fallback model), `DISCORD_CHANNEL_PAPERS/AI_NEWS/CODE/RESOURCE`, `FRESHRSS_USER/PASSWORD`, `DAILYINFO_DATA_ROOT` (default: `~/.myagentdata/dailyinfo`), `DAILYINFO_FALLBACK_MODEL`

Zotero sync optional: `ZOTERO_API_KEY`, `ZOTERO_LIBRARY_ID`, `GDRIVE_PAPERS_PATH` (for `zotero_sync.py` linked_file attachment)

## Testing Conventions

- **Autouse `tmp_data_root`** in `conftest.py` redirects all filesystem writes to `tmp_path` and sets `DISCORD_BOT_TOKEN`
- Modules caching paths at import time (`paths`, `datasource`, `run_pipelines`, `push_to_discord`, `cli`) must be reloaded when `DAILYINFO_DATA_ROOT` changes
- `fake_requests` fixture replaces `requests.get`/`requests.post` with a URL-prefix router
- `fake_call_ai` fixture stubs `run_pipelines.call_ai` with deterministic response, disables `time.sleep`
- `rss_db` fixture provides in-memory SQLite with fresh/stale entry fixtures
- Test files mirror source: `test_{module}.py` for `scripts/{module}.py`

## Agent skills

### Issue tracker

Issues are tracked as GitHub issues on `iHeadWater/dailyinfo`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default label vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Download PDF (download-pdf)

Download academic PDFs through institutional access (DUT SSO) using Playwright browser automation. Zotero sync via linked_file attachment. See `skills/download-pdf/SKILL.md`.

**New-machine Playwright setup** (one-time, ~5 min):

```bash
# 1. Enable the official Playwright plugin (provides mcp__plugin_playwright_playwright__* tools)
#    In ~/.claude/settings.json → enabledPlugins → "playwright@claude-plugins-official": true

# 2. Install Chromium (if plugin auto-download fails)
npx playwright install chromium

# 3. Install @playwright/mcp globally (provides the CLI that the plugin wraps)
npm install -g @playwright/mcp@latest
```

The plugin provides `mcp__plugin_playwright_playwright__*` tools (standalone Chromium, NOT the user's personal Chrome).
Browser profile (cookies, WAYF/SSO sessions) persists in `.playwright-mcp/` under the project directory.
Nature login survives across Claude Code restarts; Wiley/AGU Cloudflare challenge must be passed once per session.

**MCP tools to use:**
- `mcp__plugin_playwright_playwright__browser_navigate` — navigate to URL
- `mcp__plugin_playwright_playwright__browser_click` — click element (use ref from snapshot)
- `mcp__plugin_playwright_playwright__browser_snapshot` — get page accessibility tree
- `mcp__plugin_playwright_playwright__browser_type` — type text into field
- `mcp__plugin_playwright_playwright__browser_press_key` — press keyboard key
- `mcp__plugin_playwright_playwright__browser_wait_for` — wait for text or time
- `mcp__plugin_playwright_playwright__browser_run_code_unsafe` — run arbitrary Playwright code
- `mcp__plugin_playwright_playwright__browser_tabs` — manage browser tabs

**DO NOT use:**
- `mcp__plugin_ecc_playwright__*` — requires Chrome extension bridge, needs separate setup
- `browser_evaluate` + `readAsDataURL()` — crashes MCP on PDFs >1MB
- `browser_run_code` + `require('fs')` — `require` is not defined in the MCP runtime

### Bilibili Upload (bilibili-upload)

Upload podcast audio to Bilibili as video (audio + auto-generated cover → MP4 via ffmpeg → biliup upload). See `skills/bilibili-upload/SKILL.md`.

**One-time setup:**

```bash
winget install --id=ForgQi.biliup-rs -e
biliup -u ~/.bilibili/cookies.json login   # scan QR code, valid ~2 years
```

**Usage:**

```bash
# Upload audio (cover auto-generated)
dailyinfo bilibili-upload "output/weekly-review/2026-06-28/podcast/audio_hydrology.mp3" \
  --title "水文AI周报 2026-W26" \
  --tags "AI,水文,科研"

# Preview only (no upload)
dailyinfo bilibili-upload audio.mp3 --title "Test" --dry-run

# In Claude Code, just say: "上传这周的水文周报音频到B站"
```

biliup cookie at `~/.bilibili/cookies.json` persists for ~2 years.
If upload fails with code 601, wait a few minutes and retry (rate limit).

### Domain docs

Single-context — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Language

- UI, documentation, and AI prompts are primarily in Chinese
- Code comments and variable names are in English
- AI-generated briefing content is in Chinese
