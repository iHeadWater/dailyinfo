# DailyInfo

[中文](https://github.com/iHeadWater/dailyinfo/blob/main/README.zh-CN.md) | English

DailyInfo is an automated research intelligence collector for AI for Science researchers. Every day it gathers journal papers, AI news, arXiv preprints, code trends, and university announcements from RSS feeds, scraped pages, and APIs — then turns them into concise Chinese-language briefings delivered straight to your Discord.

## How It Works

One pipeline, five independent sources of information:

```text
FreshRSS / scrape / API sources
  -> dailyinfo run
  -> structured summaries + canonical PublicationStore
  -> dailyinfo publish --sink web / discord
  -> independent delivery sinks + local state
```

1. **Collect** — five independent pipelines pull from 40+ feeds, pages, and APIs. A failure in one never blocks the others.
2. **Summarize and finalize** — DeepSeek produces structured summaries; DailyInfo validates and persists one canonical PublicationBundle per category/date while retaining the legacy Markdown output.
3. **Deliver** — `dailyinfo publish` sends the canonical bundle to selected sinks. Discord and Web delivery state are independent and retryable.

You wake up to a curated digest of everything relevant to your field — no feed readers to check, no email, no noise.

## Features

| | |
|---|---|
| **Five pipelines** | Papers (30+ journals, including Chinese water-resources journals) · AI news · arXiv CS.AI (up to 500 preprints/day) · GitHub trending + HuggingFace models · University updates |
| **Chinese-first briefings** | AI summaries in Chinese with automatic fallback to an OpenRouter model when the primary API fails |
| **Configuration-driven** | Add RSS, scrape, or API sources in `config/sources.json` — no code changes required |
| **Idempotent & safe to rerun** | Sources with today's briefing are skipped; pushed files are never re-sent |
| **Resilient** | Retries with exponential backoff, batch splitting on partial AI responses, per-source isolation |
| **Scheduler-agnostic** | Bring your own cron, systemd timer, or agent runtime — DailyInfo owns the pipeline, not the clock |

## Screenshots

Put screenshots in `pictures/` with these names and they will render here.

### Discord Briefings

#### University Updates

![Discord university updates briefing](pictures/discord-university-updates.png)

#### Journal Papers

![Discord journal papers briefing](pictures/discord-journal-papers.png)

#### arXiv Papers

![Discord arXiv papers briefing](pictures/discord-arxiv-papers.png)

#### AI News

![Discord AI news briefing](pictures/discord-ai-news.png)

#### Code Trending

![Discord code trending briefing](pictures/discord-code-trending.png)

## Data Layout

Default data root: `~/.myagentdata/dailyinfo/`. Override it with `DAILYINFO_DATA_ROOT`.

```text
~/.myagentdata/dailyinfo/
├── freshrss/data/       # FreshRSS SQLite + config
├── briefings/           # Markdown files waiting to be pushed
│   ├── papers/
│   ├── ai_news/
│   ├── arxiv/
│   ├── code/
│   └── resource/
├── pushed/              # Successfully pushed archive
│   ├── papers/
│   ├── ai_news/
│   ├── arxiv/
│   ├── code/
│   └── resource/
├── publications/         # Canonical Publication v1 store
└── deliveries/           # Independent Discord/Web delivery state
```

## Quick Start

```bash
git clone <repo-url>
cd dailyinfo

cp .env.example .env
# Fill DEEPSEEK_API_KEY and DISCORD_BOT_TOKEN.

uv sync --python python3
uv pip install -e .
dailyinfo install

dailyinfo start
dailyinfo run
dailyinfo push
```

`dailyinfo install` validates `.env`, creates local data directories, and installs dependencies. It does not write crontab entries; scheduling belongs to your cron or agent runtime.

## Main Commands

| Command | Purpose |
|---------|---------|
| `dailyinfo install` | Validate environment and create data directories |
| `dailyinfo start` / `stop` / `restart` | Manage the FreshRSS container |
| `dailyinfo run` | Run all briefing pipelines |
| `dailyinfo run -p 1` | Pipeline 1: journal papers |
| `dailyinfo run -p 2` | Pipeline 2: AI news |
| `dailyinfo run -p 3` | Pipeline 3: arXiv CS.AI |
| `dailyinfo run -p 4` | Pipeline 4: code trending |
| `dailyinfo run -p 5` | Pipeline 5: university/resource |
| `dailyinfo run -f all` | Force regeneration for all sources |
| `dailyinfo push` | Push pending briefings to Discord and archive them |
| `dailyinfo push -d 2026-04-22` | Push briefings for a specific date |
| `dailyinfo publish --sink web` | Publish canonical briefings to the configured `dailyinfo-web` checkout |
| `dailyinfo publish --sink discord` | Deliver canonical briefings to Discord |
| `dailyinfo publish --sink all` | Attempt Discord and Web independently |
| `dailyinfo publish --sink web --force` | Reconcile Web even when delivery state is already successful |
| `dailyinfo push -c weekly` | Push the weekly recap only |
| `dailyinfo weekly` | Generate a weekly AI news recap from recent briefings |
| `dailyinfo status` | Show today's briefing/archive counts |
| `dailyinfo logs` | Tail the execution log |
| `dailyinfo cache-clear` | Clear a source's FreshRSS cache (default `arxiv_cs_ai`) |
| `dailyinfo clean-cache` | Delete FreshRSS cache files older than N hours (default 24h) |

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `DEEPSEEK_API_KEY` | DeepSeek API key (primary model for `dailyinfo run`) |
| `DISCORD_BOT_TOKEN` | Discord bot token for `dailyinfo push` |
| `DISCORD_CHANNEL_PAPERS` / `_AI_NEWS` / `_ARXIV` / `_CODE` / `_RESOURCE` | Optional category channel IDs |
| `FRESHRSS_USER` | FreshRSS username |
| `FRESHRSS_PASSWORD` | FreshRSS initial password |
| `DAILYINFO_DATA_ROOT` | Override default data root |
| `DAILYINFO_WEB_REPO` | Required local `dailyinfo-web` checkout for Web publishing |
| `DAILYINFO_WEB_REMOTE` | Expected Web `origin` URL (default official repository) |
| `DAILYINFO_WEB_BRANCH` | Expected Web branch (default `main`) |
| `OPENROUTER_API_KEY` | OpenRouter API key (optional, used for fallback model) |
| `DAILYINFO_ENV` | Environment: `prod` / `dev` / `staging` (default `prod`) |
| `DAILYINFO_FALLBACK_MODEL` | Fallback model when DeepSeek returns empty (default `moonshotai/kimi-k2.5`) |

## Scheduling and Agents

DailyInfo intentionally avoids owning the scheduler. Recommended ownership:

| Responsibility | Owner |
|----------------|-------|
| FreshRSS container and local data layout | DailyInfo |
| Markdown generation via `dailyinfo run` | DailyInfo |
| Discord push/archive via `dailyinfo push` | DailyInfo |
| Timed execution | cron, myopenclaw, openclaw, or agent runtime |

## Documentation

- [Architecture](architecture.md)
- [CLI Reference](cli.md)
- [Agent Config](agent-config.md)
- [Information Sources](sources.md)

## License

BSD 3-Clause License. See [LICENSE](LICENSE) for details.
