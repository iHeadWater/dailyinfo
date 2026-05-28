# CLI Reference

DailyInfo command-line interface built on `click` + `uv`.

## Installation

### Using uv (recommended)

```bash
git clone <repo-url>
cd dailyinfo

cp .env.example .env
# edit .env with your API keys

uv sync --python python3
uv pip install -e .
```

NotebookLM automation is optional. Install it only on machines where you want
`dailyinfo zotero-brief` to talk to NotebookLM directly:

```bash
uv pip install -e ".[notebooklm]"
```

### Using pip (fallback)

```bash
pip install -e .
```

## Environment Setup

```bash
dailyinfo install
```

This command:

1. Validates `.env` — `OPENROUTER_API_KEY` and `DISCORD_BOT_TOKEN` must be non-empty and not a placeholder.
2. Creates the workspace under `~/.myagentdata/dailyinfo/` (`freshrss/data`, `briefings/*`, `pushed/*`).
3. Installs Python dependencies via `uv sync` (falls back to `pip install -e .`).

**It does NOT write to the host crontab.** Scheduling is expected to be driven by an external cron, e.g. myopenclaw's hermes cron.

## Commands

### Service Management

```bash
dailyinfo start      # Start FreshRSS (Docker)
dailyinfo stop       # Stop services
dailyinfo restart    # Restart FreshRSS
```

### Pipeline Execution

```bash
dailyinfo run                      # Run all pipelines
dailyinfo run -p 1                 # Pipeline 1 (RSS papers/news)
dailyinfo run -p 2                 # Pipeline 2 (code trending)
dailyinfo run -p 3                 # Pipeline 3 (university news)
dailyinfo run -f all               # Force regenerate every source today
dailyinfo run -p 1 -f arxiv_cs_ai  # Force regenerate one source only
```

`dailyinfo run` is **idempotent**: if a non-placeholder briefing already exists
for today (either in `briefings/` waiting to be pushed, or already archived in
`pushed/`), the source is skipped and no AI call is made. Use `-f / --force`
to override — pass `all` to refresh everything, or repeat the flag with
specific source names (matches `config/sources.json`).

If the primary model (`moonshotai/kimi-k2.5`) returns empty responses after 3
retries with exponential backoff (2s / 5s / 10s), `run` automatically falls
back to the model in `DAILYINFO_FALLBACK_MODEL` (default
`deepseek/deepseek-chat-v3.1`) for 2 more attempts before giving up.

### Push to Discord

```bash
dailyinfo push                    # Push today's briefings
dailyinfo push -d 2026-04-22      # Backfill a specific day (YYYY-MM-DD)
```

Scans files under `~/.myagentdata/dailyinfo/briefings/{category}/` whose name
contains the target date, posts to the mapped Discord channel, and moves
successfully pushed files to `pushed/{category}/`. `push` is idempotent: a day
with no pending files just emits a "暂无新简报" notice and exits cleanly.

### Zotero -> NotebookLM Briefing

This CLI is the capability layer for Codex/openclaw orchestration. For daily
interactive use, load `skills/zotero-notebooklm` and let Codex run these checks,
handle NotebookLM auth handoff, trigger Zotero PDF hydration, inspect
`notebooklm.json`, and continue through manual fallback steps when needed.
For new-machine setup and agent handoff details, see
[`docs/zotero-notebooklm.md`](zotero-notebooklm.md) or
[`docs/zotero-notebooklm.zh.md`](zotero-notebooklm.zh.md).

```bash
dailyinfo zotero-brief                         # Process today's Zotero additions
dailyinfo zotero-brief --date 2026-05-27       # Process one Zotero dateAdded day
dailyinfo zotero-brief --collection water      # Restrict to a Zotero collection
dailyinfo zotero-brief --force                 # Overwrite existing local output
dailyinfo zotero-brief --artifact audio        # Also request Audio Overview
dailyinfo zotero-brief --artifact video        # Also request Video Overview
dailyinfo zotero-brief --artifact both         # Request both artifacts
dailyinfo zotero-brief --open-missing-pdfs     # Open cloud-only Zotero attachments, wait, retry copy
dailyinfo zotero-brief --manual-only           # Prepare local materials only
```

This command is separate from `dailyinfo run`: it does not call OpenRouter or
the DailyInfo `call_ai` helper. It reads the local Zotero API at
`http://127.0.0.1:23119`, filters top-level papers by `dateAdded`, copies local
PDF attachments when available, writes `source_index.md`, and lets NotebookLM
generate the Chinese markdown briefing from the uploaded sources. For cloud-only
PDF paths such as Google Drive placeholders, `--open-missing-pdfs` opens the
Zotero attachment URI first, then falls back to the local file path if needed,
and waits before retrying the copy. This is intended to trigger Zotero and the
user's sync client to hydrate the file.

NotebookLM auth is intentionally allowed to be manual. First run:

```bash
uv run --extra notebooklm notebooklm login
```

Then run:

```bash
uv run --extra notebooklm dailyinfo zotero-brief --collection water --artifact audio --open-missing-pdfs
```

If the default NotebookLM profile directory is not writable, set
`NOTEBOOKLM_HOME` or pass `--notebooklm-home <dir>` and use the same directory
for both `notebooklm login` and `dailyinfo zotero-brief`.

Output is written to `~/.myagentdata/dailyinfo/zotero/YYYY-MM-DD/`:

| File | Purpose |
|------|---------|
| `source_index.md` | Lightweight paper metadata and Chinese reading instructions uploaded to NotebookLM |
| `briefing_prompt.md` | Prompt to paste into NotebookLM chat when completing the run manually |
| `pdfs/` | Copied Zotero PDF attachments |
| `briefing.md` | NotebookLM-generated Chinese briefing, or a placeholder when manual action is required |
| `notebooklm.json` | Notebook/source/artifact ids, copied PDF status, warnings, and failures |
| `audio_overview.mp3` | Present only when Audio Overview downloads successfully |
| `video_overview.mp4` | Present only when Video Overview downloads successfully |
| `MANUAL_NOTEBOOKLM_STEPS.md` | Browser fallback steps for auth, upload, generation, and download |

`notebooklm-py` is a non-official NotebookLM interface intended for personal
automation. If Google changes the NotebookLM UI/API or auth is not ready, the
command degrades to the local material package so the run can be completed
manually in the NotebookLM web UI.

### Status & Logs

```bash
dailyinfo status    # Counts of today's briefings / pushed files
dailyinfo logs      # Tail logs/dailyinfo.log (if enabled)
```

## Environment Variables

Create `.env` in the project root:

```env
OPENROUTER_API_KEY=sk-or-v1-xxxxx
DISCORD_BOT_TOKEN=your_discord_token
FRESHRSS_USER=owen
FRESHRSS_PASSWORD=freshrss123
# DAILYINFO_DATA_ROOT=   # optional override, defaults to ~/.myagentdata/dailyinfo
```

| Key | Purpose |
|-----|---------|
| `OPENROUTER_API_KEY` | LLM API for generating summaries |
| `DISCORD_BOT_TOKEN` | Discord bot token used by `dailyinfo push` |
| `DISCORD_CHANNEL_PAPERS` / `_AI_NEWS` / `_CODE` / `_RESOURCE` | Per-category channel IDs (missing ones are skipped, not fatal) |
| `FRESHRSS_USER` | FreshRSS username (default: `$USER`) |
| `FRESHRSS_PASSWORD` | FreshRSS password |
| `DAILYINFO_DATA_ROOT` | Override data root (default `~/.myagentdata/dailyinfo`) |
| `DAILYINFO_FALLBACK_MODEL` | Fallback LLM when the primary model returns empty (default `deepseek/deepseek-chat-v3.1`) |
| `ZOTERO_LOCAL_BASE_URL` | Zotero local API base URL for `zotero-brief` (default `http://127.0.0.1:23119`) |
| `NOTEBOOKLM_HOME` | NotebookLM profile/auth directory used by `notebooklm-py`; must match the login run |

## Scheduling via myopenclaw hermes cron

dailyinfo 提供两个幂等命令供调度器调用：

| Command | Purpose |
|---------|---------|
| `dailyinfo run -p 1` | 06:00 — RSS papers + AI news |
| `dailyinfo run -p 2` | 06:15 — code trending |
| `dailyinfo run -p 3` | 06:30 — university news |
| `dailyinfo push` | 07:00 — push to Discord |

在 myopenclaw 的 hermes cron 或其他外部调度器中注册这些命令即可。

如果暂时没用 hermes，可手动配置 crontab：

```cron
0 6 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 1 >> logs/pipeline1.log 2>&1
15 6 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 2 >> logs/pipeline2.log 2>&1
30 6 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 3 >> logs/pipeline3.log 2>&1
0 7 * * * cd /path/to/dailyinfo && python3 scripts/push_to_discord.py >> logs/discord_push.log 2>&1
```

## Docker Services

Only FreshRSS runs in Docker:

```bash
docker compose up -d freshrss          # Start
docker compose down                    # Stop
docker compose logs -f freshrss        # Logs
# Web UI: http://localhost:8081
```

FreshRSS data is persisted at `~/.myagentdata/dailyinfo/freshrss/data/`.
