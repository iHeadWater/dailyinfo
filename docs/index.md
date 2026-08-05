# DailyInfo

[中文](https://github.com/iHeadWater/dailyinfo/blob/main/README.zh-CN.md) | English

DailyInfo is an automated research intelligence **collector** for AI for Science researchers. It collects papers, AI news, code trends, and institutional updates from FreshRSS feeds, scraped pages, and APIs, writes local Markdown briefings, and pushes them to Discord.

> **Scope**: DailyInfo is a pure intelligence collector — collection, summarization, and push. **It does not handle Zotero.** All Zotero-related work (PDF download, ingestion, deep literature analysis, analysis cards, themed reviews, WeChat articles, podcasts, Bilibili publishing) lives in the sibling repo [`OuyangWenyu/mylibrary`](https://github.com/OuyangWenyu/mylibrary).

## Overview

Core flow:

```text
FreshRSS / scrape / API sources
  -> dailyinfo run
  -> Markdown briefings
  -> dailyinfo push
  -> Discord channels + local archive
```

Design principles:

- Configuration-driven sources in `config/sources.json`.
- Idempotent CLI commands that can be safely rerun.
- External scheduling through cron, myopenclaw, openclaw, or other agent runtimes.

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
│   ├── code/
│   └── resource/
└── pushed/              # Successfully pushed archive
    ├── papers/
    ├── ai_news/
    ├── code/
    └── resource/
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
| `dailyinfo weekly` | Generate a weekly AI news recap from recent briefings |
| `dailyinfo status` | Show today's briefing/archive counts |
| `dailyinfo logs` | Tail the execution log |
| `dailyinfo cache-clear` | Clear a source's FreshRSS cache (default `arxiv_cs_ai`) |
| `dailyinfo clean-cache` | Delete FreshRSS cache files older than N hours (default 24h) |

## Zotero / Deep Literature — moved to mylibrary

DailyInfo is now a pure collector. **All Zotero-related work lives in the sibling repository:**

- **Repository**: [`OuyangWenyu/mylibrary`](https://github.com/OuyangWenyu/mylibrary) (local: `D:\code\mylibrary`)
- **Handles**: PDF download & Zotero ingestion (`download-pdf`, `zotero_sync`), weekly reviews, analysis cards, themed clustering, WeChat articles, NotebookLM podcasts, Bilibili publishing.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `DEEPSEEK_API_KEY` | DeepSeek API key (primary model for `dailyinfo run`) |
| `DISCORD_BOT_TOKEN` | Discord bot token for `dailyinfo push` |
| `DISCORD_CHANNEL_PAPERS` / `_AI_NEWS` / `_CODE` / `_RESOURCE` | Optional category channel IDs |
| `FRESHRSS_USER` | FreshRSS username |
| `FRESHRSS_PASSWORD` | FreshRSS initial password |
| `DAILYINFO_DATA_ROOT` | Override default data root |
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
| Zotero / deep literature | mylibrary |

## Documentation

- [Architecture](architecture.md)
- [CLI Reference](cli.md)
- [Agent Config](agent-config.md)
- [Information Sources](sources.md)

## License

BSD 3-Clause License. See [LICENSE](LICENSE) for details.
