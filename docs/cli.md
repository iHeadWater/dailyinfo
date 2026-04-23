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
