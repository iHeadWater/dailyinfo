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

1. Validates `.env` — `DEEPSEEK_API_KEY` and `DISCORD_BOT_TOKEN` must be non-empty and not a placeholder.
2. Creates the workspace under `~/.myagentdata/dailyinfo/` (`freshrss/data`, `briefings/*`, `pushed/*`).
3. Installs Python dependencies via `uv sync` (falls back to `pip install -e .`).

**It does NOT write to the host crontab.** Scheduling is expected to be driven by any external cron-like trigger (system crontab, systemd timer, container scheduler, or an agent runtime such as myopenclaw's hermes cron — see [Agent Config](agent-config.md)).

## Commands

### Service Management

```bash
dailyinfo start      # Start FreshRSS (Docker)
dailyinfo stop       # Stop services
dailyinfo restart    # Restart FreshRSS
```

### Pipeline Execution

```bash
dailyinfo run                      # Run all 5 pipelines
dailyinfo run -p 1                 # Pipeline 1 (papers)
dailyinfo run -p 2                 # Pipeline 2 (AI news)
dailyinfo run -p 3                 # Pipeline 3 (arXiv CS.AI)
dailyinfo run -p 4                 # Pipeline 4 (code trending)
dailyinfo run -p 5                 # Pipeline 5 (university news)
dailyinfo run -f all               # Force regenerate every source today
dailyinfo run -p 1 -f arxiv_cs_ai  # Force regenerate one source only
```

`dailyinfo run` is **idempotent**: if a non-placeholder briefing already exists
for today (either in `briefings/` waiting to be pushed, or already archived in
`pushed/`), the source is skipped and no AI call is made. Use `-f / --force`
to override — pass `all` to refresh everything, or repeat the flag with
specific source names (matches `config/sources.json`).

If the primary model (`deepseek-v4-pro` via DeepSeek API) returns empty responses after 3
retries with exponential backoff (2s / 5s / 10s), `run` automatically falls
back to the model in `DAILYINFO_FALLBACK_MODEL` (default
`moonshotai/kimi-k2.5` via OpenRouter) for 2 more attempts before giving up.

### Push to Discord

```bash
dailyinfo push                    # Push today's briefings
dailyinfo push -d 2026-04-22      # Backfill a specific day (YYYY-MM-DD)
```

Scans files under `~/.myagentdata/dailyinfo/briefings/{category}/` whose name
contains the target date, posts to the mapped Discord channel, and moves
successfully pushed files to `pushed/{category}/`. `push` is idempotent: a day
with no pending files just emits a "暂无新简报" notice and exits cleanly.

### Weekly Recap

```bash
dailyinfo weekly                # 汇总过去 7 天的 AI 新闻
dailyinfo weekly --days 14      # 自定义回溯窗口
dailyinfo weekly --force        # 覆盖今天已生成的 recap
```

### Status & Logs

```bash
dailyinfo status    # Counts of today's briefings / pushed files
dailyinfo logs      # Tail logs/dailyinfo.log (if enabled)
```

## Environment Variables

Create `.env` in the project root:

```env
DEEPSEEK_API_KEY=sk-your_deepseek_key_here
DISCORD_BOT_TOKEN=your_discord_token
FRESHRSS_USER=owen
FRESHRSS_PASSWORD=freshrss123
# DAILYINFO_DATA_ROOT=   # optional override, defaults to ~/.myagentdata/dailyinfo
```

| Key | Purpose |
|-----|---------|
| `DEEPSEEK_API_KEY` | DeepSeek API for generating summaries (primary) |
| `OPENROUTER_API_KEY` | OpenRouter API key (optional, for fallback model) |
| `DISCORD_BOT_TOKEN` | Discord bot token used by `dailyinfo push` |
| `DISCORD_CHANNEL_PAPERS` / `_AI_NEWS` / `_CODE` / `_RESOURCE` / `_ARXIV` | Per-category channel IDs (missing ones are skipped, not fatal) |
| `FRESHRSS_USER` | FreshRSS username (default: `$USER`) |
| `FRESHRSS_PASSWORD` | FreshRSS password |
| `DAILYINFO_DATA_ROOT` | Override data root (default `~/.myagentdata/dailyinfo`) |
| `DAILYINFO_ENV` | Environment: `prod` / `dev` / `staging` (default `prod`) |
| `DAILYINFO_FALLBACK_MODEL` | Fallback LLM when the primary model returns empty (default `moonshotai/kimi-k2.5`) |

## Scheduling

dailyinfo 提供幂等的 CLI 命令，由任意外部 cron 触发即可。推荐时刻表：

| Command | Scheduled time | Purpose |
|---------|----------------|---------|
| `dailyinfo run -p 3` | 03:00 | arXiv CS.AI |
| `dailyinfo run -p 5` | 03:30 | university news |
| `dailyinfo run -p 4` | 03:45 | code trending |
| `dailyinfo run -p 1` | 04:00 | papers |
| `dailyinfo run -p 2` | 04:30 | AI news |
| `dailyinfo push` | 05:30-07:00 | push to Discord |

系统 crontab 示例：

```cron
0 3 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 3 >> logs/pipeline3.log 2>&1
30 3 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 5 >> logs/pipeline5.log 2>&1
45 3 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 4 >> logs/pipeline4.log 2>&1
0 4 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 1 >> logs/pipeline1.log 2>&1
30 4 * * * cd /path/to/dailyinfo && python3 scripts/run_pipelines.py --pipeline 2 >> logs/pipeline2.log 2>&1
30 5 * * * cd /path/to/dailyinfo && python3 scripts/push_to_discord.py --categories ai_news,code,resource >> logs/discord_push.log 2>&1
0 6 * * * cd /path/to/dailyinfo && python3 scripts/push_to_discord.py --categories papers >> logs/discord_push.log 2>&1
0 7 * * * cd /path/to/dailyinfo && python3 scripts/push_to_discord.py --categories arxiv >> logs/discord_push.log 2>&1
```

如果你也在用 myopenclaw 等 agent 生态来统一管理这些 cron，可以参考 [Agent Config](agent-config.md)。

## Docker Services

Only FreshRSS runs in Docker:

```bash
docker compose up -d freshrss          # Start
docker compose down                    # Stop
docker compose logs -f freshrss        # Logs
# Web UI: http://localhost:8081
```

FreshRSS data is persisted at `~/.myagentdata/dailyinfo/freshrss/data/`.
