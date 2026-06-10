# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

DailyInfo is an automated research intelligence aggregation and push system for AI for Science researchers. It collects RSS feeds, scrapes websites, and queries APIs, then uses DeepSeek V4 Pro (OpenRouter Kimi K2.5 as fallback) to generate Chinese-language summaries pushed to Discord channels.

**Core pipeline**: FreshRSS collection -> AI summary generation (markdown to disk) -> Discord push + archive

**Design principles**: Configuration-driven (`config/sources.json`) + idempotent CLI + external scheduling (myopenclaw hermes cron)

## Tech Stack

- Python 3.10+, package manager: uv (primary) / pip (fallback)
- CLI: Click 8+
- RSS: FreshRSS (Docker/SQLite, `restart: always`, auto-start via myopenclaw launchd)
- AI: DeepSeek V4 Pro official API (fallback: OpenRouter `moonshotai/kimi-k2.5`)
- Push: Discord Bot API via `requests`
- Paper download: `dailyinfo_fetcher/` (async httpx + browser agents; `uv sync --extra paper`)
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

### Paper Download (optional)

`dailyinfo_fetcher/` provides Discord Bot-based paper download (`!paper <title/DOI>`). Requires `--extra paper` dependencies. Download chain: arXiv -> Unpaywall -> Semantic Scholar -> Crossref -> PMC -> campus IP -> library (playwright/browser-use).

## Source Configuration

Sources in `config/sources.json` have types: `rss`, `api`, `scrape`. Categories: `papers`, `ai_news`, `code`, `resource`.

Defaults (all overridable per-source): `lookback_hours: 24`, `max_articles_per_batch: 10`, `model: deepseek-v4-pro`.

Prompt templates under `prompt_templates` key use placeholders: `{count}`, `{display_name}`, `{article_list}`, `{items}`, `{date}`, `{content}`.

API sources can specify a `parser` key (e.g. `"parser": "crossref"`) to select a custom response parser.

Scrape sources with custom parsing need matching `if self.name == "..."` dispatch in `ScrapeDataSource.fetch()`.

## Environment Variables

Required: `DEEPSEEK_API_KEY`, `DISCORD_BOT_TOKEN`
Optional: `OPENROUTER_API_KEY` (fallback model), `DISCORD_CHANNEL_PAPERS/AI_NEWS/CODE/RESOURCE`, `FRESHRSS_USER/PASSWORD`, `DAILYINFO_DATA_ROOT` (default: `~/.myagentdata/dailyinfo`), `DAILYINFO_FALLBACK_MODEL`

## Testing Conventions

- **Autouse `tmp_data_root`** in `conftest.py` redirects all filesystem writes to `tmp_path` and sets `DISCORD_BOT_TOKEN`
- Modules caching paths at import time (`paths`, `datasource`, `run_pipelines`, `push_to_discord`, `cli`) must be reloaded when `DAILYINFO_DATA_ROOT` changes
- `fake_requests` fixture replaces `requests.get`/`requests.post` with a URL-prefix router
- `fake_call_ai` fixture stubs `run_pipelines.call_ai` with deterministic response, disables `time.sleep`
- `rss_db` fixture provides in-memory SQLite with fresh/stale entry fixtures
- Test files mirror source: `test_{module}.py` for `scripts/{module}.py`; `test_fetcher_*.py` for `dailyinfo_fetcher/`

## Agent skills

### Issue tracker

Issues are tracked as GitHub issues on `iHeadWater/dailyinfo`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default label vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Language

- UI, documentation, and AI prompts are primarily in Chinese
- Code comments and variable names are in English
- AI-generated briefing content is in Chinese
